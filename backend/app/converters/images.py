"""JPEG / PNG / WebP / GIF → PDF.

Engine: **Pillow** for decode, **ReportLab** for PDF assembly.

Two decisions worth stating:

1. **Multiple files become multiple pages**, in upload order. Each image is
   fitted to A4 preserving its aspect ratio and centred, never stretched or
   cropped — silently altering someone's document is worse than a margin.
2. **Images are re-encoded as JPEG inside the PDF** (except when the source
   is already a JPEG at a comparable quality). A PNG screenshot pasted into a
   PDF is losslessly re-compressed and can be 10x larger for no visible gain.
   Set ``compress=False`` to embed losslessly instead.
"""

from __future__ import annotations

import io

from PIL import Image, ImageFile, UnidentifiedImageError
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from ..config import Settings, get_settings
from ..errors import ConversionError, CorruptFileError, MaliciousFileError

# Truncated files are common in the wild (interrupted uploads). Without this,
# PIL raises on a partially-written image that a user would still accept.
ImageFile.LOAD_TRUNCATED_IMAGES = False

SUPPORTED_INPUT = {"JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF", "MPO"}


def _flatten_to_rgb(image: Image.Image, background: tuple[int, int, int]) -> Image.Image:
    """Convert any mode to RGB, compositing alpha onto a solid background.

    A PDF page is opaque. A transparent PNG rendered naively either comes out
    with a black background (Pillow's default RGB conversion) or raises
    outright. Compositing onto white is what every print pipeline does.
    """
    if image.mode == "RGB":
        return image
    if image.mode in ("RGBA", "LA", "PA"):
        rgba = image.convert("RGBA")
        canvas_img = Image.new("RGBA", rgba.size, (*background, 255))
        return Image.alpha_composite(canvas_img, rgba).convert("RGB")
    if image.mode == "P":
        # Palette images can carry transparency in the palette itself.
        if "transparency" in image.info:
            return _flatten_to_rgb(image.convert("RGBA"), background)
        return image.convert("RGB")
    if image.mode in ("L", "1", "I", "F", "CMYK", "YCbCr", "RGBX", "LAB", "HSV"):
        return image.convert("RGB")
    return image.convert("RGB")


def _open_image(data: bytes, settings: Settings) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise CorruptFileError() from exc

    # Guard against decompression bombs. PIL's own check uses MAX_IMAGE_PIXELS,
    # but we set our own ceiling *before* decoding, and do it on both axes:
    # a 1x100_000_000 strip passes a pixel-count check yet still explodes.
    if image.width * image.height > settings.max_image_pixels:
        raise MaliciousFileError("That image has too many pixels to process safely.")
    if max(image.width, image.height) > settings.max_image_dimension:
        raise MaliciousFileError("That image's dimensions are too large.")

    # Force the decode inside the try block: PIL defers pixel loading until
    # load(), and that is where a malformed file actually explodes.
    image.load()
    return image


def images_to_pdf(
    images: list[bytes],
    page_size: tuple[float, float] = A4,
    margin_mm: float = 12.0,
    compress: bool = True,
    settings: Settings | None = None,
) -> bytes:
    """Build a single PDF, one page per input image."""
    s = settings or get_settings()
    if not images:
        raise ConversionError("No images were provided.", code="no_input")
    if len(images) > s.max_pdf_pages:
        raise MaliciousFileError(f"At most {s.max_pdf_pages} images are allowed.")

    page_w, page_h = page_size
    margin = margin_mm * mm
    available_w = page_w - 2 * margin
    available_h = page_h - 2 * margin

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=page_size)

    for raw in images:
        image = _open_image(raw, s)

        if image.format and image.format.upper() not in SUPPORTED_INPUT:
            raise ConversionError(
                f"{image.format} images are not supported.",
                code="unsupported_image",
            )

        rgb = _flatten_to_rgb(image, background=(255, 255, 255))

        # fit-within, preserving aspect ratio, then centre
        scale = min(available_w / rgb.width, available_h / rgb.height)
        draw_w = rgb.width * scale
        draw_h = rgb.height * scale
        x = (page_w - draw_w) / 2
        y = (page_h - draw_h) / 2

        # reportlab needs an ImageReader, NOT a raw PIL Image — passing PIL
        # directly raises TypeError inside PDFImageXObject.
        reader = ImageReader(rgb)
        if compress and image.format != "JPEG":
            # Re-encode to JPEG: much smaller PDF, visually identical for
            # photographic content.
            jpeg_buf = io.BytesIO()
            rgb.save(jpeg_buf, "JPEG", quality=s.jpeg_quality, optimize=True)
            jpeg_buf.seek(0)
            reader = ImageReader(jpeg_buf)

        pdf.drawImage(reader, x, y, width=draw_w, height=draw_h)
        pdf.showPage()
        image.close()

    pdf.save()
    return buffer.getvalue()


def image_to_pdf(data: bytes, **kwargs) -> bytes:
    """Single-image convenience wrapper."""
    return images_to_pdf([data], **kwargs)
