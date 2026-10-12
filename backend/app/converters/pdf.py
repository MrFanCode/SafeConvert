"""PDF → PNG / JPEG.

Engine: **pypdfium2** (PDFium bindings, BSD-3-Clause + Apache-2.0).

Why not PyMuPDF? Because PyMuPDF is **AGPL-3.0**. A public web service that
links it into a larger program is distributing an AGPL work over the
network — the exact case the AGPL exists to prevent. It is the single most
common licensing mistake in this category of project. pypdfium2 gives the same
rendering capability under a permissive licence.

PDFium is also a deliberately small attack surface: it renders, it does not
execute JavaScript, and it does not fetch external resources.
"""

from __future__ import annotations

import io
import math

import pypdfium2 as pdfium

from ..config import Settings, get_settings
from ..errors import ConversionError, CorruptFileError, MaliciousFileError


def _compute_scale(page_width_pt: float, page_height_pt: float, dpi: int, settings: Settings) -> float:
    """Convert a requested DPI into a render scale, then clamp the result.

    The clamp is the important part. DPI alone is not a safe control: a PDF
    page can declare a MediaBox of 14400x14400 points (200 inches), where
    even 150 DPI means 30 million pixels. So the pixel count is computed
    first and the scale is reduced to fit, rather than trusting the caller.

    Note the ordering below. An earlier version applied a `max(scale,
    min_scale)` floor *after* the pixel clamp, which silently undid the clamp
    for oversized pages — a 14400pt page still rendered 51.8M pixels instead
    of the 1M budget. The floor is now only ever used to raise a scale that
    would otherwise be absurdly small, and it is itself capped by the pixel
    budget.
    """
    points_per_inch = 72.0
    scale = dpi / points_per_inch

    # Absolute pixel ceiling, independent of the caller's DPI.
    max_by_pixels = math.sqrt(settings.max_render_pixels / (page_width_pt * page_height_pt))
    scale = min(scale, max_by_pixels)

    # A very large page can drive the clamped scale below the minimum useful
    # DPI. That floor is advisory for normal pages; when it would exceed the
    # pixel budget the budget wins, because the budget is the DoS control.
    floor = settings.min_render_dpi / points_per_inch
    if floor * page_width_pt * floor * page_height_pt <= settings.max_render_pixels:
        scale = max(scale, floor)
    return max(scale, 0.01)


def pdf_to_images(
    data: bytes,
    dpi: int = 150,
    image_format: str = "png",
    jpeg_quality: int | None = None,
    settings: Settings | None = None,
) -> list[bytes]:
    """Render every page of a PDF to one encoded image per page.

    Returns a list because a PDF is not a single image. The router decides
    how to deliver multiple pages (ZIP).
    """
    s = settings or get_settings()
    if not (s.min_render_dpi <= dpi <= s.max_render_dpi):
        raise ConversionError(
            f"Resolution must be between {s.min_render_dpi} and {s.max_render_dpi} DPI.",
            code="invalid_dpi",
        )

    image_format = image_format.lower()
    if image_format not in ("png", "jpg", "jpeg"):
        raise ConversionError("Output format must be PNG or JPEG.", code="invalid_format")
    fmt = "jpeg" if image_format in ("jpg", "jpeg") else "png"

    try:
        # autoclose=False so an exception mid-render still closes the handle
        # via the explicit close() in the finally block.
        document = pdfium.PdfDocument(data, autoclose=False)
    except Exception as exc:  # pdfium raises a variety of exception types
        raise CorruptFileError() from exc

    try:
        page_count = len(document)
        if page_count == 0:
            raise CorruptFileError("That PDF has no pages.")
        if page_count > s.max_pdf_pages:
            raise MaliciousFileError(
                f"That PDF has {page_count} pages; the limit is {s.max_pdf_pages}.",
            )

        outputs: list[bytes] = []
        for index in range(page_count):
            try:
                page = document[index]
            except Exception as exc:
                # Page LOADING sits outside the render try/except below, so an
                # exception here previously escaped every handler and surfaced
                # as an opaque 500 internal_error. That is reachable with a
                # merely malformed PDF -- pdfium reports len(document) == 2 but
                # then fails to load a page, because the page tree parsed while
                # the page object itself did not.
                #
                # "Corrupt file" is the honest answer: it parsed far enough for
                # pdfium to count pages, and then failed. Not a server fault.
                raise CorruptFileError(
                    f"Page {index + 1} of that PDF could not be read; the file is damaged."
                ) from exc

            try:
                width_pt, height_pt = page.get_size()

                scale = _compute_scale(width_pt, height_pt, dpi, s)
                # pypdfium2 annotates scale as int, but the underlying PDFium
                # C API takes a float and the docs' own examples pass
                # `scale=dpi/72`. Verified working with scale=200/72 producing
                # 1653x2339 at 200 DPI on an A4 page. Passing a float is
                # correct; the annotation is a known over-restriction.
                bitmap = page.render(scale=scale)  # type: ignore[arg-type]
                image = bitmap.to_pil().convert("RGB")
            except Exception as exc:
                # A single unrenderable page should fail the request rather
                # than produce a half-empty ZIP that looks like success.
                raise ConversionError(
                    f"Page {index + 1} could not be rendered.",
                    code="page_render_failed",
                ) from exc

            buffer = io.BytesIO()
            if fmt == "jpeg":
                image.save(
                    buffer,
                    "JPEG",
                    quality=jpeg_quality or s.jpeg_quality,
                    optimize=True,
                    progressive=True,
                )
            else:
                # A PDF page can contain transparency (a mask/alpha channel).
                # JPEG has no alpha, so PNG is the honest default for pages.
                image.save(buffer, "PNG", optimize=True)
            outputs.append(buffer.getvalue())
        return outputs
    finally:
        document.close()
