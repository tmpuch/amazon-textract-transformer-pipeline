# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Script to prepare raw documents into SMGT/model-ready page images (in batch or real-time)

This script can be used in a SageMaker Processing job to prepare images for SageMaker Ground Truth
labelling in batch (with optional extra thumbnails output by specifying a ProcessingOutput with
source /opt/ml/processing/output/thumbnails).

It can also be deployed as a SageMaker Endpoint (for asynchronous inference, to accommodate large
payloads in request/response) for generating page thumbnail bundles on-the-fly.
"""

# Python Built-Ins:
import argparse
import logging
from multiprocessing import cpu_count, Pool
import os
import shutil
import time
from typing import Iterable, List, Optional, Tuple, Union

logging.basicConfig(level="INFO", format="%(asctime)s %(name)s [%(levelname)s] %(message)s")

# External Dependencies:
import pdf2image
import PIL
from PIL import ExifTags


logger = logging.getLogger("preproc")


def get_exif_tag_id_by_name(name: str) -> Optional[str]:
    """Find a numeric EXIF tag ID by common name

    As per https://pillow.readthedocs.io/en/stable/reference/ExifTags.html
    """
    try:
        return next(k for k in ExifTags.TAGS.keys() if ExifTags.TAGS[k] == name)
    except StopIteration:
        return None


ORIENTATION_EXIF_ID = get_exif_tag_id_by_name("Orientation")


def split_filename(filename):
    basename, _, ext = filename.rpartition(".")
    return basename, ext


class ImageExtractionResult:
    """Result descriptor for extracting a source image/document to image(s)"""

    def __init__(self, rawpath: str, cleanpaths: List[str] = [], cats: List[str] = []):
        self.rawpath = rawpath
        self.cleanpaths = cleanpaths
        self.cats = cats


def resize_image(
    image: PIL.Image.Image,
    size: Union[int, Tuple[int, int]] = (224, 224),
    default_square: bool = True,
    letterbox_color: Optional[Tuple[int, int, int]] = None,
    max_size: Optional[int] = None,
    resample: int = PIL.Image.BICUBIC,
) -> PIL.Image.Image:
    """Resize (stretch or letterbox) a PIL Image

    In the case no resizing was necessary, the original image object may be returned. Otherwise,
    the result will be a copy. This function is similar to the logic in Hugging Face
    image_utils.ImageFeatureExtractionMixin.resize - but defaults to bicubic resampling instead of
    bilinear and also supports letterboxing as well as aspect ratio stretch.

    Arguments
    ---------
    image :
        The (loaded PIL) image to resize
    size :
        The target size to output. May be a sequence of (width, height), or a single number. If
        `default_square` is `True`, a single number will be resized to (size, size). Otherwise, the
        **smaller** edge of the image will be matched to `size` and the aspect ratio preserved.
    default_square :
        Control how to interpret single-number `size`. Set `True` to target a square, or `False` to
        preserve aspect ratio.
    letterbox_color :
        Provide a 0-255 (R, G, B) tuple to letterbox the image and use this color as the background
        for any unused area. Leave unset (`None`) to stretch the image to match the target size.
    max_size :
        Maximum allowed size for longer edge when using single-`size` mode with `default_square` =
        `False`. If the longer edge of the image is greater than `max_size` after initial resize,
        the image is again proportionally resized so that the longer edge is equal to `max_size`.
        As a result, `size` might be overruled, i.e the smaller edge may be shorter than `size`.
        Only used if `default_to_square` is `False` and `size` is a single number.
    resample :
        PIL.Image.Resampling method, defaults to BICUBIC
    """
    if not isinstance(image, PIL.Image.Image):
        raise ValueError(f"resize_image accepts PIL.Image only. Got: {type(image)}")

    if not hasattr(size, "__len__"):
        size = (size,)

    if len(size) == 1:
        if default_square:
            # Treat as square:
            size = (size[0], size[0])
        else:
            # Specified target shortest edge size:
            short = size[0]
            iw, ih = image.size
            ishort, ilong = (iw, ih) if iw <= ih else (ih, iw)

            if short == ishort:
                return image

            long = int(short * ilong / ishort)

            # Check longer edge max_size limit if provided:
            if max_size is not None:
                if max_size <= short:
                    raise ValueError(
                        f"max_size = {max_size} must be strictly greater than the requested "
                        f"size for the smaller edge = {short}"
                    )
                if long > max_size:
                    short, long = int(max_size * short / long), max_size

            size = (short, long) if iw <= ih else (long, short)

    if letterbox_color:
        # Letterbox the image to the normalized `size` with given background color:
        iw, ih = image.size
        w, h = size
        scale = min(w / iw, h / ih)
        nw = int(iw * scale)
        nh = int(ih * scale)
        result = PIL.Image.new("RGB", size, letterbox_color)
        return result.paste(
            image.resize((nw, nh), resample=resample),
            ((w - nw) // 2, (h - nh) // 2),
        )
    else:
        # Just stretch the image to fit:
        return image.resize(size, resample=resample)


def clean_document_for_img_ocr(
    rel_filepath: str,
    from_basepath: str,
    to_basepath: str,
    pdf_dpi: int = 300,
    pdf_image_format: str = "png",
    allowed_formats: Iterable[str] = ("jpg", "jpeg", "png"),
    preferred_image_format: str = "png",
    thumbs_basepath: Optional[str] = None,
    thumbs_size: Union[int, Tuple[int]] = (224, 224),
    thumbs_default_square: bool = True,
    thumbs_letterbox_color: Optional[Tuple[int, int, int]] = None,
    thumbs_max_size: Optional[int] = None,
    wait: float = 0,
) -> ImageExtractionResult:
    """Process an individual PDF or image for use with SageMaker Ground Truth image task UIs

    Extracts page images from PDFs, converts EXIF-rotated images to data-rotated.

    Parameters
    ----------
    rel_filepath : str
        Path (relative to from_basepath) to the raw document/image to be processed
    from_basepath : str
        Base input path which should be masked for mapping to `to_basepath`
    to_basepath : str
        Target path for converted files (subfolder structure will be preserved from input)
    pdf_dpi : int
        DPI resolution to extract images from PDFs (Default 300).
    pdf_image_format : str
        Format to extract images from PDFs (Default 'png').
    allowed_formats : Iterable[str]
        The set of permitted file formats for compatibility: Used to determine whether to convert
        source images in other formats which PIL may still have been able to successfully load.
        NOTE: Amazon Textract also supports 'tiff', but we left it out of the default list because
        TIFF images seemed to break the SageMaker Ground Truth built-in bounding box UI as of some
        tests in 2021-10. Default ('jpg', 'jpeg', 'png').
    preferred_image_format : str
        Format to be used when an image has been saved/converted (Default 'png').
    thumbs_basepath :
        If provided, also output resized thumbnail images to this target path (subfolder structure
        will be preserved like with to_basepath). See also other `thumbs_` options. If falsy,
        thumbnail images will not be generated.
    thumbs_size :
        Size of thumbnail images to generate if thumbnails enabled, as per `resize_image()`.
    thumbs_default_square :
        Default thumbnails aspect ratio treatment if thumbnails enabled, as per `resize_image()`.
    thumbs_letterbox_color :
        Set to letterbox thumbnails rather than stretch, if enabled, as per `resize_image()`.
    thumbs_max_size :
        Max thumbnail output dimension when aspect ratio is preserved, as per `resize_image()`.
    wait : float
        If !=0, this function will `time.sleep(wait)` before running. This is useful for batch jobs
        utilizing all available cores on a machine, where errors might arise if we don't provide a
        little room for any background processes.
    """
    if wait:
        time.sleep(wait)

    full_filepath = os.path.join(from_basepath, rel_filepath)
    filename = os.path.basename(rel_filepath)
    subfolder = os.path.dirname(rel_filepath)
    outfolder = os.path.join(to_basepath, subfolder)
    os.makedirs(outfolder, exist_ok=True)
    if thumbs_basepath:
        os.makedirs(os.path.join(thumbs_basepath, subfolder), exist_ok=True)
    basename, ext = split_filename(filename)
    ext_lower = ext.lower()
    result = ImageExtractionResult(
        rawpath=full_filepath,
        cleanpaths=[],
        cats=subfolder[1:].split(os.path.sep),  # Strip leading slash to avoid initial ''
    )
    if ext_lower == "pdf":
        logger.info(
            "Converting {} to {}/{}*.{}".format(
                full_filepath,
                outfolder,
                basename + "-",
                pdf_image_format,
            )
        )
        images = pdf2image.convert_from_path(
            full_filepath,
            output_folder=outfolder,
            output_file=basename + "-",
            # TODO: Use paths_only option if no thumbs, to return paths instead of image objs
            fmt=pdf_image_format,
            dpi=pdf_dpi,
        )
        if thumbs_basepath:
            for img in images:
                thumbpath = img.filename.replace(to_basepath, thumbs_basepath)
                resize_image(
                    img,
                    size=thumbs_size,
                    default_square=thumbs_default_square,
                    letterbox_color=thumbs_letterbox_color,
                    max_size=thumbs_max_size,
                ).save(thumbpath)

        result.cleanpaths = [i.filename for i in images]
        logger.info(
            "* PDF converted {}:\n    - {}".format(
                full_filepath,
                "\n    - ".join(result.cleanpaths),
            )
        )
        return result

    try:
        image = PIL.Image.open(full_filepath)
    except PIL.UnidentifiedImageError:
        logger.warning(f"* Ignoring incompatible file: {full_filepath}")
        return None

    # Some "image" formats (notably TIFF) support multiple pages as "frames":
    n_image_pages = getattr(image, "n_frames", 1)
    if n_image_pages > 1:
        logger.info("Extracting %s pages from file %s", n_image_pages, full_filepath)
    convert_format = not ext_lower in allowed_formats
    outpaths = []
    for ixpage in range(n_image_pages):
        if n_image_pages > 1:
            image.seek(ixpage)

        # Correct orientation from EXIF data:
        exif = dict((image.getexif() or {}).items())
        img_orientation = exif.get(ORIENTATION_EXIF_ID)
        if img_orientation == 3:
            image = image.rotate(180, expand=True)
            rotated = True
        elif img_orientation == 6:
            image = image.rotate(270, expand=True)
            rotated = True
        elif img_orientation == 8:
            image = image.rotate(90, expand=True)
            rotated = True
        else:
            rotated = False

        if n_image_pages == 1 and not (convert_format or rotated):
            # Special case where image file can just be copied across:
            outpath = os.path.join(outfolder, filename)
            shutil.copy2(full_filepath, outpath)
        else:
            outpath = os.path.join(
                outfolder,
                "".join(
                    (
                        basename,
                        "-%04i" % (ixpage + 1) if n_image_pages > 1 else "",
                        ".",
                        preferred_image_format if convert_format else ext,
                    )
                ),
            )
            image.save(outpath)

        if thumbs_basepath:
            thumbpath = outpath.replace(to_basepath, thumbs_basepath)
            resize_image(
                image,
                size=thumbs_size,
                default_square=thumbs_default_square,
                letterbox_color=thumbs_letterbox_color,
                max_size=thumbs_max_size,
            ).save(thumbpath)

        outpaths.append(outpath)
        logger.info(
            "* %s image %s%s (orientation %s) to %s",
            "Converted"
            if convert_format
            else ("Rotated" if rotated else ("Extracted" if n_image_pages > 1 else "Copied")),
            full_filepath,
            f" page {ixpage + 1}" if n_image_pages > 1 else "",
            img_orientation,
            outpath,
        )

    result.cleanpaths = outpaths
    return result


def clean_dataset_for_img_ocr(
    from_path: str,
    to_path: str,
    filepaths: Optional[Iterable[str]] = None,
    pdf_dpi: int = 300,
    pdf_image_format: str = "png",
    allowed_formats: Iterable[str] = ("jpg", "jpeg", "png"),
    preferred_image_format: str = "png",
    thumbs_basepath: Optional[str] = None,
    thumbs_size: Union[int, Tuple[int]] = (224, 224),
    thumbs_default_square: bool = True,
    thumbs_letterbox_color: Optional[Tuple[int, int, int]] = None,
    thumbs_max_size: Optional[int] = None,
) -> List[ImageExtractionResult]:
    """Process a mixed PDF/image dataset for use with SageMaker Ground Truth image task UIs

    This is a convenience method for processing documents directly in notebook, and is not used
    when run as a processing job. Extracts page images from PDFs, converts EXIF-rotated images to
    data-rotated.

    Parameters
    ----------
    from_path : str
        Base path of the raw/source dataset to be converted
    to_path : str
        Target path for converted files (subfolder structure will be preserved from source)
    filepaths : Optional[Iterable[str]]
        Paths (relative to from_path, no leading slash) to filter down the processing. If not
        provided, the whole from_path folder will be recursively crawled.
    pdf_dpi : int
        DPI resolution to extract images from PDFs (Default 300).
    pdf_image_format : str
        Format to extract images from PDFs (Default 'png').
    allowed_formats : Iterable[str]
        The set of permitted file formats for compatibility: Used to determine whether to convert
        source images in other formats which PIL may still have been able to successfully load.
        NOTE: Amazon Textract also supports 'tiff', but we left it out of the default list because
        TIFF images seemed to break the SageMaker Ground Truth built-in bounding box UI as of some
        tests in 2021-10. Default ('jpg', 'jpeg', 'png').
    preferred_image_format : str
        Format to be used when an image has been saved/converted (Default 'png').
    thumbs_size :
        Size of thumbnail images to generate if thumbnails enabled, as per `resize_image()`.
    thumbs_default_square :
        Default thumbnails aspect ratio treatment if thumbnails enabled, as per `resize_image()`.
    thumbs_letterbox_color :
        Set to letterbox thumbnails rather than stretch, if enabled, as per `resize_image()`.
    thumbs_max_size :
        Max thumbnail output dimension when aspect ratio is preserved, as per `resize_image()`.
    """
    results = []
    if not filepaths:
        # List input files automatically by walking the from_path dir:
        filepaths = [
            path[len(from_path) + 1 :]  # Convert to relative path (+1 for trailing '/')
            for path in filter(
                lambda path: "/." not in path,  # Ignore hidden stuff e.g. .ipynb_checkpoints
                (os.path.join(path, f) for path, _, files in os.walk(from_path) for f in files),
            )
        ]
    os.makedirs(to_path, exist_ok=True)

    for ixfilepath, filepath in enumerate(filepaths):
        logger.info("Doc %s of %s", ixfilepath + 1, len(filepaths))
        result = clean_document_for_img_ocr(
            filepath,
            from_path,
            to_path,
            pdf_dpi=pdf_dpi,
            pdf_image_format=pdf_image_format,
            allowed_formats=allowed_formats,
            preferred_image_format=preferred_image_format,
            thumbs_basepath=thumbs_basepath,
            thumbs_size=thumbs_size,
            thumbs_default_square=thumbs_default_square,
            thumbs_letterbox_color=thumbs_letterbox_color,
            thumbs_max_size=thumbs_max_size,
            wait=0,  # No need to pause in single-threaded for loop
        )
        if result:
            results.append(result)
        # Otherwise doc was skipped and warning already printed

    logger.info("Done!")
    return results


def parse_args():
    """Parse SageMaker Processing Job CLI arguments to job parameters"""
    parser = argparse.ArgumentParser(
        description="Split and standardize documents into images for OCR"
    )
    parser.add_argument(
        "--input",
        type=str,
        default="/opt/ml/processing/input/raw",
        help="Folder where raw input images/documents are stored",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="/opt/ml/processing/output/imgs-clean",
        help="Folder where cleaned output images should be saved",
    )
    default_thumbs_path = "/opt/ml/processing/output/thumbnails"
    parser.add_argument(
        "--thumbnails",
        type=str,
        default=default_thumbs_path if os.path.isdir(default_thumbs_path) else None,
        help=(
            "(Optional) folder where resized thumbnail images should be saved. Defaults to "
            f"{default_thumbs_path} IF this path exists, so the functionality can be enabled "
            "just by configuring the output in a SM Processing Job."
        ),
    )
    parser.add_argument(
        "--n-workers",
        type=int,
        default=cpu_count(),
        help="Number of worker processes to use for extraction (default #CPU cores)",
    )
    args = parser.parse_args()
    return args


def process_doc_in_worker(inputs: dict):
    return clean_document_for_img_ocr(
        rel_filepath=inputs["rel_filepath"],
        from_basepath=inputs["in_folder"],
        to_basepath=inputs["out_folder"],
        thumbs_basepath=inputs["thumbs_folder"],
        wait=0.5,
    )


if __name__ == "__main__":
    # Main processing job script:
    args = parse_args()
    logger.info(f"Parsed job args: %s", args)
    logger.info("Additional thumbnail output is %s", "ENABLED" if args.thumbnails else "DISABLED")

    logger.info("Reading raw files from %s", args.input)
    rel_filepaths_all = sorted(
        filter(
            lambda f: not (f.startswith(".") or "/." in f),  # (Exclude hidden dot-files)
            [
                os.path.join(currpath, name)[len(args.input) + 1 :]  # +1 for trailing '/'
                for currpath, dirs, files in os.walk(args.input)
                for name in files
            ],
        )
    )

    n_docs = len(rel_filepaths_all)
    logger.info("Processing %s files across %s processes", n_docs, args.n_workers)
    with Pool(args.n_workers) as pool:
        for ix, result in enumerate(
            pool.imap_unordered(
                process_doc_in_worker,
                [
                    {
                        "in_folder": args.input,
                        "out_folder": args.output,
                        "thumbs_folder": args.thumbnails,
                        "rel_filepath": path,
                    }
                    for path in rel_filepaths_all
                ],
            )
        ):
            logger.info("Processed doc %s of %s", ix + 1, n_docs)
    logger.info("All done!")


########  EXTRAS FOR REAL-TIME SAGEMAKER ENDPOINT PROCESSING  ########
# Items below this line are used for exposing the functionality on-demand via a SageMaker Endpoint

# Python Built-Ins:
import io
from tempfile import TemporaryDirectory
from typing import Any, Dict

# External Dependencies:
import numpy as np

# MIME type constants:
SINGLE_IMAGE_CONTENT_TYPES = {
    "image/jpeg": "JPG",
    "image/jpg": "JPG",
    "image/png": "PNG",
}
MULTI_IMAGE_CONTENT_TYPES = {
    "image/tiff": "TIFF",
}
PDF_CONTENT_TYPES = set(("application/pdf",))

# Endpoint environment variable configurations:
# When running in SM Endpoint, we can't use the usual processing job command line argument pattern
# to configure these extra parameters - so expose them via environment variables instead.
RT_THUMBNAIL_SIZE = tuple(int(x) for x in os.environ.get("RT_THUMBNAIL_SIZE", "224,224").split(","))
if len(RT_THUMBNAIL_SIZE) == 1:
    RT_THUMBNAIL_SIZE = RT_THUMBNAIL_SIZE[0]
RT_PDF_DPI = int(os.environ.get("RT_PDF_DPI", "300"))
RT_DEFAULT_SQUARE = os.environ.get("RT_DEFAULT_SQUARE", "true").lower()
if RT_DEFAULT_SQUARE in ("true", "t", "yes", "y", "1"):
    RT_DEFAULT_SQUARE = True
elif RT_DEFAULT_SQUARE in ("false", "f", "no", "n", "0"):
    RT_DEFAULT_SQUARE = False
else:
    raise ValueError("Environment variable RT_DEFAULT_SQUARE should be 'true', 'false', or not set")
RT_LETTERBOX_COLOR = os.environ.get("RT_LETTERBOX_COLOR")
if RT_LETTERBOX_COLOR:
    RT_LETTERBOX_COLOR = tuple(int(x) for x in RT_LETTERBOX_COLOR.split(","))
RT_MAX_SIZE = os.environ.get("RT_MAX_SIZE")
RT_MAX_SIZE = int(RT_MAX_SIZE) if RT_MAX_SIZE else None
RT_PREFERRED_IMAGE_FORMAT = os.environ.get("RT_PREFERRED_IMAGE_FORMAT", "png")


def model_fn(model_dir: str):
    """Dummy model loader: There is no "model" for this processing case

    So long as predict_fn is present (so the container doesn't try to use this as a PyTorch model),
    it doesn't really matter what we return here.
    """
    return lambda x: x


def input_fn(input_bytes: bytes, content_type: str) -> Dict:
    """Deserialize real-time processing requests

    Requests should be binary data (image or document), and this endpoint should typically be
    deployed as async to accommodate potentially large payload sizes.

    Returns
    -------
    result :
        Dict with "type" (an extension e.g. pdf, png, jpg) and **either** "image" (single loaded
        PIL image) **or** "doc" (raw document bytes for multi-page formats).
    """
    logger.debug("Deserializing request of content_type %s", content_type)
    if content_type in SINGLE_IMAGE_CONTENT_TYPES:
        logger.debug("Single image request")
        # Cannot `with` the buffer, because PIL requires the buffer to still be available later:
        buffer = io.BytesIO(input_bytes)
        img = PIL.Image.open(buffer)
        return {"image": img, "type": SINGLE_IMAGE_CONTENT_TYPES[content_type]}
    elif content_type in PDF_CONTENT_TYPES:
        logger.debug("PDF document request")
        return {"doc": input_bytes, "type": "pdf"}
    elif content_type in MULTI_IMAGE_CONTENT_TYPES:
        logger.debug("(Multi-page) TIFF image request")
        return {"doc": input_bytes, "type": MULTI_IMAGE_CONTENT_TYPES[content_type]}
    else:
        raise ValueError(
            "Unrecognised request content type {} not in supported list: {}".format(
                content_type,
                PDF_CONTENT_TYPES.union(SINGLE_IMAGE_CONTENT_TYPES),
            )
        )


def predict_fn(input_data: dict, model: Any):
    """Execute real-time processing requests

    Either resize an individual image, or run the full thumbnail extraction process for a document.
    Document processing is done in a temporary directory

    Returns
    -------
    result :
        A dict with either "image" (a single PIL image, for single-image requests) or "images" (a
        list of PIL images, for document format inputs)
    """
    if "image" in input_data:
        logger.info("Resizing single image")
        return {
            "image": resize_image(
                image=input_data["image"],
                size=RT_THUMBNAIL_SIZE,
                default_square=RT_DEFAULT_SQUARE,
                letterbox_color=RT_LETTERBOX_COLOR,
                max_size=RT_MAX_SIZE,
                # resample: int = PIL.Image.BICUBIC,
            ),
        }
    elif "doc" in input_data:
        logger.info("Collecting document page thumbnails")
        with TemporaryDirectory() as tmpdir:
            tmpdir_in = os.path.join(tmpdir, "in")
            os.makedirs(tmpdir_in, exist_ok=True)
            rel_filepath = f"doc.{input_data['type']}"
            inpath = os.path.join(tmpdir_in, rel_filepath)
            with open(inpath, "wb") as f:
                f.write(input_data["doc"])
            tmpdir_out = os.path.join(tmpdir, "out")
            os.makedirs(tmpdir_out, exist_ok=True)

            clean_document_for_img_ocr(
                rel_filepath=rel_filepath,
                from_basepath=tmpdir_in,
                to_basepath=tmpdir_out,
                pdf_dpi=RT_PDF_DPI,
                pdf_image_format=RT_PREFERRED_IMAGE_FORMAT,
                preferred_image_format=RT_PREFERRED_IMAGE_FORMAT,
                thumbs_basepath=tmpdir_out,
                thumbs_size=RT_THUMBNAIL_SIZE,
                thumbs_default_square=RT_DEFAULT_SQUARE,
                thumbs_letterbox_color=RT_LETTERBOX_COLOR,
                thumbs_max_size=RT_MAX_SIZE,
            )

            imgs = []
            for filename in os.listdir(tmpdir_out):
                with open(os.path.join(tmpdir_out, filename), "rb") as ftmp:
                    filebytes = ftmp.read()
                imgs.append(PIL.Image.open(io.BytesIO(filebytes)))
            logger.info("Prepared doc images")
            return {"images": imgs}
    else:
        logger.error("Expected 'image' or 'doc' in deserialized request object: %s", input_data)
        raise RuntimeError("Expected 'image' or 'doc' in deserialized request object")


def output_fn(prediction_output: Dict, accept: str) -> bytes:
    """Serialize results for real-time processing requests

    Image response 'Accept' types (e.g. image/png) are supported only for single-image requests.

    application/x-npy will return an *uncompressed* numpy array of either:
    - Pixel data for single-image type requests, or
    - PNG file bytestrings for document/multi-page type requests

    application/x-npz (preferred for multi-page documents) will return a *compressed* numpy archive
    including either:
    - "image": Pixel data for single-image type requests, or
    - "images": PNG file bytestrings for document/multi-page type requests
    """
    if accept in SINGLE_IMAGE_CONTENT_TYPES:
        logger.info("Preparing single-image response")
        if "image" in prediction_output:
            buffer = io.BytesIO()
            prediction_output["image"].save(buffer, format=SINGLE_IMAGE_CONTENT_TYPES[accept])
            return buffer.getvalue()
        else:
            raise ValueError(
                f"Requested content type {accept} can only be used for single-page images. "
                "Try application/x-npz for a compressed numpy array of PNG image bytes."
            )
    elif accept in ("application/x-npy", "application/x-npz"):
        is_npz = accept == "application/x-npz"
        logger.info("Preparing %snumpy response", "compressed " if is_npz else "")
        if "image" in prediction_output:
            arr = np.array(prediction_output["image"].convert("RGB"))
            buffer = io.BytesIO()
            if is_npz:
                np.savez_compressed(buffer, image=arr)
            else:
                np.save(buffer, arr)
            return buffer.getvalue()
        else:
            imgs = []
            for img in prediction_output["images"]:
                with io.BytesIO() as buffer:
                    img.save(buffer, format=RT_PREFERRED_IMAGE_FORMAT)
                    imgs.append(buffer.getvalue())
            imgs = np.array(imgs)
            buffer = io.BytesIO()
            if is_npz:
                np.savez_compressed(buffer, images=imgs)
            else:
                np.save(buffer, imgs)
            return buffer.getvalue()
    else:
        raise ValueError(
            f"Requested content type {accept} not recognised. Use application/x-npz (compressed) "
            "or application/x-npy - or (for single images) a supported image/... content type."
        )
