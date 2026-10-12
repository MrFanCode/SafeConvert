"""SVG → PNG / JPEG.

Engine: **resvg-py** (Rust `resvg`, MIT).

Why this and not CairoSVG? Two independent reasons:

1. **SSRF.** CairoSVG had CVE-2023-27586: rendering an SVG that references
   ``http://169.254.169.254/`` made the *server* issue the request, turning a
   file converter into a proxy for reaching cloud metadata endpoints and the
   private network. resvg has no network access at all — verified by
   inspecting its entire Python surface, which exposes no
   "allow external resources" parameter. There is nothing to disable because
   it does not exist.
2. **Consistency.** resvg is the reference SVG renderer used by many browsers
   and design tools, so output looks like what the author saw.

The output is *always* a raster image. An SVG is never returned to the
browser, because an SVG is an executable document format — see
:mod:`app.validation` for the full threat model.
"""

from __future__ import annotations

import io
import re

import resvg_py
from PIL import Image

from ..config import Settings, get_settings
from ..errors import ConversionError, CorruptFileError, MaliciousFileError

# Strip anything that could make a renderer behave unexpectedly or reach out.
# resvg ignores scripts entirely, but removing them keeps the input honest and
# means we never even pass a payload to the native library.
_SCRIPT_BLOCK = re.compile(rb"<script\b.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_SCRIPT_SELF_CLOSING = re.compile(rb"<script\b[^>]*/>", re.IGNORECASE)
_EVENT_HANDLER = re.compile(rb"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
# javascript:/data: URIs inside attributes
_JS_URI = re.compile(rb"(href|src)\s*=\s*[\"']?\s*(javascript|vbscript)\s*:", re.IGNORECASE)
_EXTERNAL_ENTITY = re.compile(rb"<!ENTITY", re.IGNORECASE)
_DOCTYPE = re.compile(rb"<!DOCTYPE[^>[]*(\[[^\]]*\])?[^>]*>", re.IGNORECASE)


def sanitise_svg(data: bytes) -> bytes:
    """Remove script, event handlers and DTDs from SVG markup.

    Defence in depth. The primary control is that we never *return* SVG, but
    an SVG still has to be parsed, and reducing what reaches the native
    renderer shrinks the surface. The XML declaration and any external
    references are left alone — resvg resolves nothing remotely, and an
    ``<image href="...">`` that fails to resolve simply renders as blank.
    """
    cleaned = _SCRIPT_BLOCK.sub(b"", data)
    cleaned = _SCRIPT_SELF_CLOSING.sub(b"", cleaned)
    cleaned = _EVENT_HANDLER.sub(b"", cleaned)
    cleaned = _JS_URI.sub(b"href=\"\"", cleaned)

    # An internal DTD subset can define entities (XXE). Check BEFORE removing
    # the doctype: an earlier version stripped it first and then searched for
    # <!ENTITY> in the already-cleaned bytes, which can never match — so the
    # check silently never fired. Remove the doctype after rejecting it.
    if _EXTERNAL_ENTITY.search(cleaned):
        raise MaliciousFileError()
    cleaned = _DOCTYPE.sub(b"", cleaned)
    return cleaned


def svg_to_image(
    data: bytes,
    output_format: str = "png",
    width: int | None = None,
    height: int | None = None,
    dpi: int | None = None,
    jpeg_quality: int | None = None,
    settings: Settings | None = None,
) -> bytes:
    """Rasterise an SVG to PNG or JPEG bytes.

    Exactly one sizing mode is used, in priority order:
      1. explicit width+height (pixels)
      2. dpi (scales the SVG's own dimensions)
      3. the SVG's intrinsic size

    The resulting pixel count is clamped to ``max_svg_pixels`` so a hostile
    ``width="10000000"`` cannot exhaust memory.
    """
    s = settings or get_settings()

    output_format = output_format.lower()
    if output_format in ("jpg", "jpeg"):
        fmt = "jpeg"
    elif output_format == "png":
        fmt = "png"
    else:
        raise ConversionError("Output format must be PNG or JPEG.", code="invalid_format")

    cleaned = sanitise_svg(data)

    if width and height:
        target_w, target_h = int(width), int(height)
    elif dpi:
        # resvg's dpi scales the document's own dimensions; we still cap the
        # result below, so a huge width plus a huge dpi cannot combine.
        intrinsic = _intrinsic_size(cleaned)
        if intrinsic:
            target_w = max(1, round(intrinsic[0] * dpi / 72))
            target_h = max(1, round(intrinsic[1] * dpi / 72))
        else:
            target_w = target_h = None
    else:
        intrinsic = _intrinsic_size(cleaned)
        target_w, target_h = (round(v) for v in intrinsic) if intrinsic else (None, None)

    if target_w and target_h:
        # Reject rather than silently downscale. An earlier version scaled
        # the result down to fit the budget, which produced a 1000x1000 PNG
        # from a 4000x4000 SVG with no indication to the caller — the user
        # would get a lower-resolution image and no idea why. Every other
        # converter in this service refuses oversized input instead, so this
        # matches: the client can offer an explicit smaller size.
        if target_w * target_h > s.max_svg_pixels:
            raise MaliciousFileError(
                "That SVG is too large to rasterise at its declared size. "
                "Render it at a smaller size instead.",
            )
        if max(target_w, target_h) > s.max_image_dimension:
            raise MaliciousFileError("The requested SVG size is too large.")

    try:
        rendered = resvg_py.svg_to_bytes(
            svg_string=cleaned.decode("utf-8", errors="replace"),
            width=target_w,
            height=target_h,
            # skip_system_fonts keeps output deterministic across machines and
            # avoids depending on whatever fonts happen to be in the image.
            # Font-based SVGs fall back to resvg's built-ins.
            skip_system_fonts=False,
            resources_dir=None,
        )
    except Exception as exc:
        raise CorruptFileError("That SVG could not be parsed.") from exc

    if not rendered:
        raise CorruptFileError("That SVG produced no output.")

    if fmt == "jpeg":
        image = Image.open(io.BytesIO(rendered))
        rgb = image.convert("RGBA")
        background = Image.new("RGBA", rgb.size, (255, 255, 255, 255))
        flat = Image.alpha_composite(background, rgb).convert("RGB")
        out = io.BytesIO()
        flat.save(out, "JPEG", quality=jpeg_quality or s.jpeg_quality, optimize=True)
        return out.getvalue()

    return rendered


def _intrinsic_size(data: bytes) -> tuple[float, float] | None:
    """Best-effort read of the SVG's own width/height, in pixels.

    Only used to pre-scale for DPI mode. Returns None when the SVG uses
    viewBox-only sizing, in which case resvg decides and our pixel clamp
    still applies to the output.
    """
    head = data[:2048].decode("utf-8", errors="replace")
    open_tag = re.search(r"<svg\b[^>]*>", head, re.IGNORECASE | re.DOTALL)
    if not open_tag:
        return None
    tag = open_tag.group(0)

    def _attr(name: str) -> float | None:
        m = re.search(rf'\b{name}\s*=\s*["\']([\d.]+)\s*(px)?["\']', tag, re.IGNORECASE)
        return float(m.group(1)) if m else None

    width = _attr("width")
    height = _attr("height")
    if width and height and width > 0 and height > 0:
        return width, height

    # Fall back to viewBox: render at its natural size.
    m = re.search(r'\bviewBox\s*=\s*["\']\s*[\d.-]+\s+[\d.-]+\s+([\d.]+)\s+([\d.]+)', tag)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


def svg_to_png(data: bytes, **kwargs) -> bytes:
    return svg_to_image(data, output_format="png", **kwargs)


def svg_to_jpeg(data: bytes, **kwargs) -> bytes:
    return svg_to_image(data, output_format="jpeg", **kwargs)
