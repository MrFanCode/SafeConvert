"""Security validation. This module is the reason the service can be trusted.

Threat model
------------
An attacker uploads a file that is crafted to do one of these things:

1. **Exhaust memory/CPU** — a decompression bomb (a 40 KB file that inflates
   to 4 GB), a 100000-page PDF, an SVG with a 10^9-pixel canvas, an image
   with absurd dimensions. *Mitigated by:* explicit size/pixel/ratio ceilings
   checked BEFORE the expensive operation, not after.
2. **Read the server's filesystem or make outbound requests** — an SVG that
   references ``http://169.254.169.254/``, a DOCX/XLSX XML with an external
   entity, a PDF with a JavaScript action. *Mitigated by:* parsers that
   cannot fetch (resvg), entity-stripping XML parsing, and never returning a
   converted file in a form the browser will execute.
3. **Execute script in the visitor's browser** — the classic "converter XSS":
   we take a hostile SVG and hand it back as ``image/svg+xml``, the browser
   runs its ``<script>``, and it steals the user's session. *Mitigated by:*
   SVG output is **never** produced. Every conversion returns PNG/JPEG/PDF.
4. **Zip-slip / path traversal** — ``../../etc/passwd`` as a ZIP entry name.
   *Mitigated by:* we never write extracted entries to disk by name; the
   DOCX reader is fed in-memory bytes.

Every check here fails closed and raises a client-safe error.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass

from .config import Settings, get_settings
from .errors import CorruptFileError, MaliciousFileError, UnsupportedTypeError

# Magic-byte signatures. Extension is user-controlled and proves nothing, so
# the declared type is always confirmed against the actual bytes.
_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "pdf": (b"%PDF-",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "webp": (b"RIFF",),
    "gif": (b"GIF87a", b"GIF89a"),
    "zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
}

# A DOCX is a ZIP. We check the container, then require the OOXML parts.
# ZipFile.namelist() yields str, so this marker is a str too — comparing it to
# entry names as bytes is a silent TypeError, not a match failure.
_OOXML_MARKER = "word/document.xml"

# Characters that must never appear in a filename we echo back to a client.
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class UploadInfo:
    """What the validators concluded about an uploaded file."""

    kind: str  # pdf | image | svg | docx
    filename: str  # sanitised, safe to echo back
    size: int


def sanitise_filename(name: str | None, fallback: str) -> str:
    """Reduce an arbitrary client filename to something safe to emit.

    Strips directory components (both separators), collapses anything outside
    ``[A-Za-z0-9._-]``, and caps the length. Without this, a filename like
    ``../../etc/passwd`` becomes a real path traversal the moment anything
    downstream treats it as a path.
    """
    if not name:
        return fallback
    # Take only the last component, for both POSIX and Windows separators.
    base = re.split(r"[\\/]", name)[-1].strip()
    base = _UNSAFE_FILENAME.sub("_", base).lstrip(".")
    if not base:
        return fallback
    stem, dot, ext = base.rpartition(".")
    if not dot:
        # No extension at all: the entire name is the stem. Returning the
        # fallback here would rename a legitimate extensionless upload.
        return base[:80]
    if len(stem) > 80:
        stem = stem[:80]
    if not stem:
        return fallback
    return f"{stem}.{ext}"


def sniff_kind(data: bytes, filename: str) -> str:
    """Identify the real type from content, falling back to the extension.

    The extension is only trusted to *disambiguate* formats that share
    magic bytes (e.g. ZIP-based DOCX vs a generic ZIP); it never establishes
    that a file is safe to parse.
    """
    if not data:
        raise CorruptFileError("The uploaded file is empty.")

    for kind, sigs in _SIGNATURES.items():
        if any(data.startswith(sig) for sig in sigs):
            if kind == "zip":
                return "docx" if _looks_like_docx(data) else "zip"
            if kind in ("png", "jpg", "webp", "gif"):
                return "image"
            return kind

    # SVG is text; look past a BOM/XML prolog/whitespace/comments.
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    if b"<svg" in head.lower():
        return "svg"

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "svg":
        # Claimed SVG but no magic and no <svg in the head: treat as hostile
        # rather than handing arbitrary bytes to an XML parser.
        raise MaliciousFileError()
    if ext in ("png", "jpg", "jpeg", "webp", "gif"):
        raise CorruptFileError("This file does not look like a valid image.")

    raise UnsupportedTypeError(
        "Unsupported file type. Upload a PDF, PNG, JPEG, WebP, GIF, SVG or DOCX file."
    )


def _looks_like_docx(data: bytes) -> bool:
    """True when the ZIP container holds the OOXML parts a .docx requires."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            if not names:
                return False
            has_marker = any(_OOXML_MARKER in n for n in names)
            # [Content_Types].xml is present in every OOXML package.
            has_types = "[Content_Types].xml" in names
            return has_marker and has_types
    except zipfile.BadZipFile:
        return False


def check_zip_bomb(data: bytes, settings: Settings | None = None) -> int:
    """Reject a ZIP whose declared contents are implausible.

    Reads each entry's ``file_size`` from the central directory *without*
    extracting anything, so this is cheap and safe. Enforces:
      - total uncompressed bytes
      - per-entry and overall compression ratio (a 1000:1 ratio is a bomb)
      - entry count
    """
    s = settings or get_settings()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            if len(infos) > s.max_zip_entries:
                raise MaliciousFileError("That document contains too many embedded files.")

            total = 0
            for info in infos:
                if info.file_size > s.max_uncompressed_bytes:
                    raise MaliciousFileError()
                total += info.file_size
                if total > s.max_uncompressed_bytes:
                    raise MaliciousFileError()
                # A zero-length entry has no meaningful ratio.
                if info.file_size > 0 and info.compress_size > 0:
                    ratio = info.file_size / info.compress_size
                    if ratio > s.max_compression_ratio:
                        raise MaliciousFileError()
    except zipfile.BadZipFile as exc:
        raise CorruptFileError() from exc
    return len(infos)


def validate_upload(data: bytes, filename: str, settings: Settings | None = None) -> UploadInfo:
    """Entry point: size check, type detection, and format-specific guards.

    Call this before handing bytes to any parser. Raises on anything that
    should not be parsed.
    """
    s = settings or get_settings()

    if len(data) > s.max_upload_bytes:
        from .errors import FileTooLargeError

        raise FileTooLargeError()
    if not data:
        raise CorruptFileError("The uploaded file is empty.")

    kind = sniff_kind(data, filename)
    clean_name = sanitise_filename(filename, f"upload.{_default_ext(kind)}")

    if kind == "docx":
        check_zip_bomb(data, s)
    elif kind == "svg" and len(data) > s.max_svg_bytes:
        raise MaliciousFileError("That SVG is larger than the SVG limit.")

    return UploadInfo(kind=kind, filename=clean_name, size=len(data))


def _default_ext(kind: str) -> str:
    return {
        "pdf": "pdf",
        "image": "png",
        "svg": "svg",
        "docx": "docx",
        "zip": "zip",
    }.get(kind, "bin")
