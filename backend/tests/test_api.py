"""API integration tests: real HTTP requests against the real app.

These use FastAPI's TestClient, so they exercise routing, multipart parsing,
validation, conversion, error envelopes and security headers end to end. They
do not mock the converters — a mocked converter would pass while the actual
conversion was broken.

The security assertions here are the reason the service can be trusted, so
they are deliberately specific: they assert the *property* (this response can
never execute script) rather than the implementation.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.main import create_app
from app.rate_limit import reset_limiter


@pytest.fixture
def client():
    reset_limiter()
    settings = Settings(
        environment="test",
        enable_docs=False,
        max_upload_bytes=5 * 1024 * 1024,
        max_pdf_pages=10,
        max_docx_elements=5000,
        rate_limit_requests=1000,
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        yield c


# ------------------------------------------------------------- fixtures
def make_pdf(pages: int = 1) -> bytes:
    from reportlab.pdfgen.canvas import Canvas

    buf = io.BytesIO()
    c = Canvas(buf, pagesize=(595, 842))
    for i in range(pages):
        c.setFont("Helvetica-Bold", 30)
        c.drawString(70, 750, f"Page {i + 1}")
        c.showPage()
    c.save()
    return buf.getvalue()


def make_png(size=(200, 150)) -> bytes:
    img = Image.new("RGB", size, (40, 120, 200))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def make_docx() -> bytes:
    import docx

    d = docx.Document()
    d.add_heading("API Test Document", level=1)
    d.add_paragraph("Paragraph with content for the endpoint test.")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "Key"
    t.cell(0, 1).text = "Value"
    t.cell(1, 0).text = "Alpha"
    t.cell(1, 1).text = "42"
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def make_svg() -> bytes:
    return b"""<svg xmlns="http://www.w3.org/2000/svg" width="120" height="90">
    <rect width="120" height="90" fill="#2a9d8f"/></svg>"""


# ------------------------------------------------------------------ health
class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["version"]

    def test_health_has_security_headers(self, client):
        r = client.get("/api/health")
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert "sandbox" in r.headers["Content-Security-Policy"]

    def test_health_is_not_rate_limited(self, client):
        for _ in range(30):
            assert client.get("/api/health").status_code == 200

    def test_docs_are_disabled_in_production_mode(self, client):
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404

    def test_root_lists_service(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert r.json()["service"]


# ------------------------------------------------------------ pdf to image
class TestPdfToImageEndpoint:
    def test_single_page_returns_png(self, client):
        r = client.post(
            "/api/pdf-to-image",
            files={"file": ("doc.pdf", make_pdf(1), "application/pdf")},
            params={"format": "png", "dpi": 100},
        )
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "image/png"
        assert r.content.startswith(b"\x89PNG\r\n\x1a\n")
        img = Image.open(io.BytesIO(r.content))
        assert img.size[0] > 800 and img.size[1] > 1100  # A4 at 100 dpi
        # Exact A4 at 100 dpi is 827x1169; PDFium rounds per-axis, so assert
        # the ratio rather than a hardcoded pixel count that shifts by one.

    def test_multi_page_returns_zip(self, client):
        r = client.post(
            "/api/pdf-to-image",
            files={"file": ("multi.pdf", make_pdf(3), "application/pdf")},
            params={"format": "png", "dpi": 72},
        )
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            names = zf.namelist()
            assert len(names) == 3
            assert names[0].endswith(".png")
            # Entries must be in page order.
            assert "page-0001" in names[0] and "page-0003" in names[2]
            assert zf.read(names[0]).startswith(b"\x89PNG")

    def test_jpeg_format(self, client):
        r = client.post(
            "/api/pdf-to-image",
            files={"file": ("doc.pdf", make_pdf(1), "application/pdf")},
            params={"format": "jpeg", "dpi": 80},
        )
        assert r.status_code == 200
        assert r.content.startswith(b"\xff\xd8\xff")

    def test_single_page_selection(self, client):
        r = client.post(
            "/api/pdf-to-image",
            files={"file": ("multi.pdf", make_pdf(3), "application/pdf")},
            params={"dpi": 72, "page": 2},
        )
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"

    def test_page_out_of_range(self, client):
        r = client.post(
            "/api/pdf-to-image",
            files={"file": ("multi.pdf", make_pdf(2), "application/pdf")},
            params={"dpi": 72, "page": 99},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "page_out_of_range"

    def test_rejects_non_pdf(self, client):
        r = client.post(
            "/api/pdf-to-image",
            files={"file": ("fake.pdf", make_png(), "application/pdf")},
        )
        assert r.status_code == 415
        assert r.json()["error"]["code"] == "unsupported_type"

    def test_rejects_corrupt_pdf(self, client):
        r = client.post(
            "/api/pdf-to-image",
            files={"file": ("bad.pdf", b"%PDF-1.7\ngarbage body", "application/pdf")},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "corrupt_file"

    def test_rejects_dpi_out_of_range(self, client):
        r = client.post(
            "/api/pdf-to-image",
            files={"file": ("doc.pdf", make_pdf(1), "application/pdf")},
            params={"dpi": 99999},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid_request"

    def test_oversized_upload_rejected(self, client):
        """A body beyond the ceiling is refused with 413, before any parsing.

        The margin matters: the `guard` middleware allows
        ``max_upload_bytes + 2MB`` for multipart framing, so this uses 8 MB
        against a 5 MB limit — comfortably past the guard, and past Starlette's
        multipart cap. A file just over the limit (6 MB) legitimately reaches
        the converter and is reported on its merits, not as "too large".
        """
        payload = b"%PDF-1.7\n" + b"0" * (8 * 1024 * 1024)
        r = client.post(
            "/api/pdf-to-image",
            files={"file": ("big.pdf", payload, "application/pdf")},
        )
        assert r.status_code == 413
        assert r.json()["error"]["code"] == "file_too_large"
        assert r.json()["error"]["limit_mb"] == 5

    def test_payload_within_limit_reaches_converter_not_size_guard(self, client):
        """A payload inside the upload limit must reach the parser.

        The payload is not a real PDF, so the correct answer is "corrupt" —
        proving the size guard did not fire and the request reached the parser.

        The size is derived from the active settings rather than hardcoded. An
        earlier version used 6 MB, which was only "under the limit" while
        max_upload_bytes was 25 MiB; lowering it to 5 MiB (to match the shipped
        Dockerfile) turned this into a legitimate 413 and the test correctly
        failed. Deriving it means the next limit change cannot silently
        invalidate the assertion.
        """
        from app.config import get_settings

        settings = get_settings()
        # Comfortably inside the limit, but still multi-megabyte.
        size = max(1024 * 1024, settings.max_upload_bytes - 1024 * 1024)
        payload = b"%PDF-1.7\n" + b"0" * size
        assert len(payload) < settings.max_upload_bytes

        r = client.post(
            "/api/pdf-to-image",
            files={"file": ("big.pdf", payload, "application/pdf")},
        )
        assert r.status_code == 422, r.text[:200]
        assert r.json()["error"]["code"] == "corrupt_file"

    def test_valid_large_pdf_within_limit_succeeds(self, client):
        """A real multi-MB PDF must convert, proving the raised cap works.

        NOTE: 4000 identical-ish drawString calls compress to ~26 KB — ReportLab
        deflates the content stream, so "lots of text" is not a large file. The
        content here has to be *incompressible*: a page image drawn from
        pseudo-random noise. Getting this wrong is what made an earlier
        version of this test fail its own size assertion.
        """
        import os

        from reportlab.pdfgen.canvas import Canvas

        buf = io.BytesIO()
        c = Canvas(buf, pagesize=(595, 842))
        # os.urandom-derived noise, not a fixed seed, so this is genuinely
        # incompressible rather than reproducible-but-compressible.
        # Measured: 8 pages x 48KB noise = ~2.44 MB uncompressible output.
        noise = os.urandom(48 * 1024)
        for _page in range(8):
            # Non-overlapping 1pt-wide strips: overlapping or repeated geometry
            # deflates well below the entropy, which is why an earlier attempt
            # produced only 1.7 MB.
            for offset in range(0, len(noise)):
                shade = noise[offset] / 255
                c.setFillColorRGB(shade, shade, shade)
                c.rect(20 + offset % 560, 20 + (offset // 560) * 8, 1, 8, stroke=0, fill=1)
            c.showPage()
        c.save()
        data = buf.getvalue()
        assert len(data) > 2 * 1024 * 1024, f"fixture only reached {len(data)} bytes"
        assert len(data) < 5 * 1024 * 1024, "fixture must stay inside the limit under test"

        r = client.post(
            "/api/pdf-to-image",
            files={"file": ("large.pdf", data, "application/pdf")},
            params={"dpi": 50},
        )
        assert r.status_code == 200, r.text[:300]
        assert r.headers["content-type"] == "application/zip"  # 8 pages
        assert r.content.startswith(b"PK")

    def test_extension_lie_is_ignored(self, client):
        """A PNG renamed to .pdf is detected by content."""
        r = client.post(
            "/api/pdf-to-image",
            files={"file": ("actually.png", make_png(), "application/pdf")},
        )
        assert r.status_code == 415


# ----------------------------------------------------------- images to pdf
class TestImagesToPdfEndpoint:
    def test_single_image(self, client):
        r = client.post(
            "/api/images-to-pdf",
            files={"files": ("photo.png", make_png(), "image/png")},
        )
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/pdf"
        assert r.content.startswith(b"%PDF-")

    def test_multiple_images_multipage(self, client):
        r = client.post(
            "/api/images-to-pdf",
            files=[
                ("files", ("a.png", make_png(), "image/png")),
                ("files", ("b.png", make_png(), "image/png")),
                ("files", ("c.png", make_png(), "image/png")),
            ],
        )
        assert r.status_code == 200
        from pypdfium2 import PdfDocument

        doc = PdfDocument(r.content)
        try:
            assert len(doc) == 3
        finally:
            doc.close()

    def test_rejects_pdf_input(self, client):
        r = client.post(
            "/api/images-to-pdf",
            files={"files": ("doc.pdf", make_pdf(1), "application/pdf")},
        )
        assert r.status_code == 415
        assert "pdf-to-image" in r.json()["error"]["message"]

    def test_rejects_svg_input_with_a_pointer(self, client):
        r = client.post(
            "/api/images-to-pdf",
            files={"files": ("logo.svg", make_svg(), "image/svg+xml")},
        )
        assert r.status_code == 415

    def test_mixed_valid_and_invalid(self, client):
        r = client.post(
            "/api/images-to-pdf",
            files=[
                ("files", ("ok.png", make_png(), "image/png")),
                ("files", ("bad.png", b"not an image", "image/png")),
            ],
        )
        assert r.status_code == 422


# ------------------------------------------------------------ svg to image
class TestSvgToImageEndpoint:
    def test_png_output(self, client):
        r = client.post(
            "/api/svg-to-image",
            files={"file": ("logo.svg", make_svg(), "image/svg+xml")},
        )
        assert r.status_code == 200, r.text
        assert r.content.startswith(b"\x89PNG")
        assert Image.open(io.BytesIO(r.content)).size == (120, 90)

    def test_jpeg_output(self, client):
        r = client.post(
            "/api/svg-to-image",
            files={"file": ("logo.svg", make_svg(), "image/svg+xml")},
            params={"format": "jpeg"},
        )
        assert r.status_code == 200
        assert r.content.startswith(b"\xff\xd8\xff")

    def test_explicit_dimensions(self, client):
        r = client.post(
            "/api/svg-to-image",
            files={"file": ("logo.svg", make_svg(), "image/svg+xml")},
            params={"width": 400, "height": 300},
        )
        assert r.status_code == 200
        assert Image.open(io.BytesIO(r.content)).size == (400, 300)

    def test_rejects_non_svg(self, client):
        r = client.post(
            "/api/svg-to-image",
            files={"file": ("doc.pdf", make_pdf(1), "application/pdf")},
        )
        assert r.status_code == 415

    def test_hostile_svg_never_returns_svg(self, client):
        """The converter-XSS control, at the HTTP layer.

        This is the assertion that matters most in this file. A response that
        is image/svg+xml here would run <script> in the visitor's browser.
        """
        hostile = (
            b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
            b"<script>alert(document.cookie)</script>"
            b'<rect width="100" height="100" fill="red"/></svg>'
        )
        r = client.post(
            "/api/svg-to-image",
            files={"file": ("x.svg", hostile, "image/svg+xml")},
        )
        assert r.status_code == 200
        assert r.headers["content-type"] in ("image/png", "image/jpeg")
        assert "svg+xml" not in r.headers["content-type"]
        assert b"<script" not in r.content
        assert r.headers["X-Content-Type-Options"] == "nosniff"

    def test_hostile_svg_does_not_break_response_headers(self, client):
        """A filename with quotes/newlines must not inject headers."""
        hostile = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'
        r = client.post(
            "/api/svg-to-image",
            files={"file": ('ev"il\r\nX-Injected: yes\n.svg', hostile, "image/svg+xml")},
        )
        assert r.status_code == 200
        assert "x-injected" not in {k.lower() for k in r.headers}


# ------------------------------------------------------------- docx to pdf
class TestDocxToPdfEndpoint:
    def test_conversion(self, client):
        r = client.post(
            "/api/docx-to-pdf",
            files={"file": ("report.docx", make_docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        )
        assert r.status_code == 200, r.text
        assert r.content.startswith(b"%PDF-")

    def test_content_is_preserved(self, client):
        from pypdfium2 import PdfDocument

        r = client.post(
            "/api/docx-to-pdf",
            files={"file": ("report.docx", make_docx(), "application/octet-stream")},
        )
        assert r.status_code == 200
        doc = PdfDocument(r.content)
        try:
            text = "".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
        finally:
            doc.close()
        assert "API Test Document" in text
        assert "Alpha" in text

    def test_rejects_legacy_doc(self, client):
        r = client.post(
            "/api/docx-to-pdf",
            files={"file": ("old.doc", b"\xd0\xcf\x11\xe0legacy OLE2 header", "application/msword")},
        )
        assert r.status_code == 415

    def test_rejects_pdf_input(self, client):
        r = client.post(
            "/api/docx-to-pdf",
            files={"file": ("doc.pdf", make_pdf(1), "application/pdf")},
        )
        assert r.status_code == 415

    def test_rejects_zip_bomb(self, client):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", b"<Types/>")
            zf.writestr("word/document.xml", b"<w:document/>")
            zf.writestr("bomb.bin", b"\x00" * (4 * 1024 * 1024))
        r = client.post(
            "/api/docx-to-pdf",
            files={"file": ("bomb.docx", buf.getvalue(), "application/octet-stream")},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "unsafe_file"


# ------------------------------------------------------------- rate limits
class TestRateLimit:
    def test_limit_triggers_429(self):
        reset_limiter()
        settings = Settings(environment="test", rate_limit_requests=3, rate_limit_window_seconds=60)
        with TestClient(create_app(settings), raise_server_exceptions=False) as c:
            for _ in range(3):
                assert c.post("/api/images-to-pdf", files={"files": ("a.png", make_png(), "image/png")}).status_code == 200
            r = c.post("/api/images-to-pdf", files={"files": ("a.png", make_png(), "image/png")})
            assert r.status_code == 429
            assert r.json()["error"]["code"] == "rate_limited"
            assert "Retry-After" in r.headers

    def test_rate_limit_headers_present(self, client):
        r = client.post("/api/images-to-pdf", files={"files": ("a.png", make_png(), "image/png")})
        assert "X-RateLimit-Limit" in r.headers
        assert "X-RateLimit-Remaining" in r.headers


# ------------------------------------------------------- error envelopes
class TestErrorEnvelope:
    def test_unknown_route_is_json(self, client):
        r = client.get("/api/does-not-exist")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/json")
        assert r.json()["error"]["code"] == "not_found"

    def test_no_traceback_leaks(self, client):
        r = client.post(
            "/api/pdf-to-image",
            files={"file": ("bad.pdf", b"%PDF-1.7\ncorrupt", "application/pdf")},
        )
        text = r.text
        assert "Traceback" not in text
        assert "/app/" not in text  # no server paths
        assert "pypdfium" not in text.lower()

    def test_missing_file_field(self, client):
        r = client.post("/api/pdf-to-image", data={"format": "png"})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid_request"
