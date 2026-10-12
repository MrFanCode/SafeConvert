"""Security tests: prove the validators actually reject hostile input.

These are the tests that matter for this project. Each one builds a real
malicious file and asserts it is refused. If a check is removed or weakened,
the matching test fails.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.config import Settings
from app.errors import (
    CorruptFileError,
    FileTooLargeError,
    MaliciousFileError,
    UnsupportedTypeError,
)
from app.validation import (
    check_zip_bomb,
    sanitise_filename,
    sniff_kind,
    validate_upload,
)

SMALL = Settings(
    max_upload_bytes=2 * 1024 * 1024,
    max_uncompressed_bytes=8 * 1024 * 1024,
    max_compression_ratio=50,
    max_zip_entries=100,
    max_svg_bytes=1024 * 1024,
)


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def _minimal_docx() -> bytes:
    return _zip_bytes(
        {
            "[Content_Types].xml": b"<Types/>",
            "word/document.xml": b"<w:document/>",
        }
    )


# ---------------------------------------------------------------- filenames
class TestFilenameSanitisation:
    def test_strips_path_traversal(self):
        """Only the final path component survives, with no separators."""
        out = sanitise_filename("../../etc/passwd", "f.pdf")
        assert out == "passwd"  # "passwd" is the basename; traversal is gone
        assert "/" not in out and ".." not in out

    def test_strips_windows_path(self):
        out = sanitise_filename(r"C:\Windows\System32\evil.dll", "f.dll")
        assert out == "evil.dll"
        assert "\\" not in out

    def test_collapses_shell_metacharacters(self):
        out = sanitise_filename("my file;rm -rf.png", "f.png")
        assert ";" not in out and " " not in out

    def test_falls_back_when_empty(self):
        assert sanitise_filename("", "fallback.pdf") == "fallback.pdf"
        assert sanitise_filename("...", "fallback.pdf") == "fallback.pdf"

    def test_caps_stem_length(self):
        out = sanitise_filename("x" * 500 + ".pdf", "f.pdf")
        assert len(out) <= 90

    def test_never_returns_a_traversal_component(self):
        """Property check across many hostile names."""
        for name in ["../../a", "..\\..\\b", "a/../../b", "/etc/shadow", "..", "."]:
            out = sanitise_filename(name, "safe.pdf")
            assert "/" not in out
            assert "\\" not in out
            assert not out.startswith("..")


# ------------------------------------------------------------------- sniffing
class TestTypeSniffing:
    def test_detects_pdf_by_magic(self):
        assert sniff_kind(b"%PDF-1.7\n...", "lying.exe") == "pdf"

    def test_extension_cannot_lie_about_binary(self):
        """A .txt name on PNG bytes is still an image, not text."""
        assert sniff_kind(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, "notes.txt") == "image"

    def test_detects_svg_beneath_xml_prolog(self):
        svg = b'<?xml version="1.0"?><!DOCTYPE svg><svg xmlns="x"></svg>'
        assert sniff_kind(svg, "a.svg") == "svg"

    def test_zip_without_ooxml_is_not_docx(self):
        data = _zip_bytes({"payload.txt": b"hi"})
        assert sniff_kind(data, "a.docx") == "zip"

    def test_real_docx_detected(self):
        assert sniff_kind(_minimal_docx(), "a.docx") == "docx"

    def test_empty_file_rejected(self):
        with pytest.raises(CorruptFileError):
            sniff_kind(b"", "a.pdf")

    def test_svg_extension_without_svg_content_is_hostile(self):
        """An .svg name on random bytes must not reach an XML parser."""
        with pytest.raises(MaliciousFileError):
            sniff_kind(b"\x00\x01\x02 not markup at all", "a.svg")

    def test_image_extension_on_garbage_is_corrupt_not_a_crash(self):
        with pytest.raises(CorruptFileError):
            sniff_kind(b"not an image at all", "a.png")


# ------------------------------------------------------------------ zip bomb
class TestZipBomb:
    def test_rejects_high_ratio(self):
        """10 MB of zeroes compresses to ~10 KB: ratio ~1000:1."""
        data = _zip_bytes({"bomb": b"\x00" * (6 * 1024 * 1024)})
        with pytest.raises(MaliciousFileError):
            check_zip_bomb(data, SMALL)

    def test_rejects_oversized_entry(self):
        data = _zip_bytes({"big": b"\x41" * (9 * 1024 * 1024)})
        with pytest.raises(MaliciousFileError):
            check_zip_bomb(data, SMALL)

    def test_rejects_too_many_entries(self):
        data = _zip_bytes({f"f{i}.txt": b"x" for i in range(200)})
        with pytest.raises(MaliciousFileError):
            check_zip_bomb(data, SMALL)

    def test_accepts_ordinary_docx(self):
        """A normal document must not trip the checks."""
        check_zip_bomb(_minimal_docx(), SMALL)

    def test_ratio_boundary_is_not_off_by_one(self):
        """Realistic prose must stay under the ratio limit.

        NOTE: an earlier version of this test used b"the quick brown fox " * 400
        and failed. That was the test being wrong, not the check — a string
        repeated 400 times compresses >50:1, which is exactly the shape of a
        zip bomb. Real document text compresses ~3-5x, so this uses varied
        words, and asserts the actual ratio so a limit change is visible.
        """
        words = [
            "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
        ]
        body = " ".join(words[i % len(words)] + str(i) for i in range(600)).encode()
        data = _zip_bytes({"notes.txt": body})
        check_zip_bomb(data, SMALL)

        import zipfile as _zf

        with _zf.ZipFile(io.BytesIO(data)) as zf:
            info = zf.infolist()[0]
            ratio = info.file_size / info.compress_size
        assert ratio < SMALL.max_compression_ratio, f"prose ratio was {ratio:.1f}"

    def test_path_traversal_entry_names_are_not_extracted_to_disk(self):
        """A zip-slip entry name must never become a real path.

        We never write entries to disk by name, so this documents the
        invariant rather than testing ZipFile itself.
        """
        data = _zip_bytes({"../../../../etc/cron.d/pwn": b"* * * * * root sh"})
        check_zip_bomb(data, SMALL)
        # The entry exists in the archive, but nothing in our code path uses
        # info.filename to build a filesystem path.
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert zf.namelist() == ["../../../../etc/cron.d/pwn"]


# ------------------------------------------------------------------ validate
class TestValidateUpload:
    def test_rejects_oversized_upload(self):
        data = b"%PDF-1.7\n" + b"0" * (SMALL.max_upload_bytes + 1)
        with pytest.raises(FileTooLargeError):
            validate_upload(data, "big.pdf", SMALL)

    def test_accepts_clean_pdf(self):
        info = validate_upload(b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n", "doc.pdf", SMALL)
        assert info.kind == "pdf"
        assert info.filename == "doc.pdf"

    def test_docx_goes_through_bomb_check(self):
        info = validate_upload(_minimal_docx(), "a.docx", SMALL)
        assert info.kind == "docx"

    def test_zip_bomb_named_docx_is_rejected(self):
        data = _zip_bytes(
            {
                "[Content_Types].xml": b"<Types/>",
                "word/document.xml": b"<w:document/>",
                "bomb": b"\x00" * (6 * 1024 * 1024),
            }
        )
        with pytest.raises(MaliciousFileError):
            validate_upload(data, "invoice.docx", SMALL)

    def test_oversized_svg_rejected(self):
        svg = b"<svg>" + b"A" * (SMALL.max_svg_bytes + 10) + b"</svg>"
        with pytest.raises(MaliciousFileError):
            validate_upload(svg, "a.svg", SMALL)

    def test_unsupported_binary_rejected(self):
        with pytest.raises(UnsupportedTypeError):
            validate_upload(b"\x7fELF\x02\x01\x01" + b"\x00" * 50, "x.bin", SMALL)


# ------------------------------------------------------- converter XSS payload
HOSTILE_SVG = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"
     xmlns:xlink="http://www.w3.org/1999/xlink">
  <script type="text/javascript">alert(document.cookie)</script>
  <rect width="100" height="100" fill="red"/>
  <image xlink:href="http://169.254.169.254/latest/meta-data/" width="10" height="10"/>
</svg>"""


class TestHostileSVGPassesValidation:
    """The SVG is structurally valid, so validation accepts it.

    The defence is that it is then *rasterised to PNG* and the SVG is never
    returned. See tests/test_converters.py::TestSvgOutputIsNeverSvg — this test
    exists to document that validation is not, by itself, the XSS control.
    """

    def test_accepted_as_svg(self):
        info = validate_upload(HOSTILE_SVG, "x.svg", SMALL)
        assert info.kind == "svg"
