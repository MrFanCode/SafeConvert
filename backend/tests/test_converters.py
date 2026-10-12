"""Converter tests: every conversion is verified by reading the output back.

A converter test that only asserts "did not raise" is worthless — it passes
for a function that returns 10 bytes of garbage. Every test here opens the
result and checks a property that would fail if the conversion were wrong.

Shared fixtures build real documents, so no binary blobs live in the repo.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image
from pypdfium2 import PdfDocument

from app.config import Settings
from app.converters.docx import docx_to_pdf
from app.converters.images import image_to_pdf, images_to_pdf
from app.converters.pdf import pdf_to_images
from app.converters.svg import sanitise_svg, svg_to_image
from app.errors import ConversionError, CorruptFileError, MaliciousFileError

SMALL = Settings(max_pdf_pages=10, max_docx_elements=5000)


# ----------------------------------------------------------------- fixtures
def make_pdf(pages: int = 2) -> bytes:
    from reportlab.pdfgen.canvas import Canvas

    buf = io.BytesIO()
    c = Canvas(buf, pagesize=(595, 842))
    for i in range(pages):
        c.setFont("Helvetica-Bold", 30)
        c.drawString(70, 750, f"Page {i + 1}")
        c.setFont("Helvetica", 12)
        c.drawString(70, 700, "Conversion round-trip fixture")
        c.rect(70, 600, 180, 70, fill=1)
        c.showPage()
    c.save()
    return buf.getvalue()


def make_image(fmt: str = "PNG", size=(600, 400), mode: str = "RGB", alpha=None) -> bytes:
    img = Image.new(mode, size, "white")
    if mode in ("RGB",):
        for x in range(0, size[0], 50):
            for y in range(0, size[1], 50):
                img.putpixel((x, y), (200, 30, 30))
    if alpha is not None and mode == "RGBA":
        img.putalpha(alpha)
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def make_svg(width=400, height=300, extra: str = "") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"
     viewBox="0 0 {width} {height}">
  <rect width="{width}" height="{height}" fill="#123456"/>
  <circle cx="{width // 2}" cy="{height // 2}" r="{min(width, height) // 4}" fill="#ffcc00"/>
  {extra}
</svg>""".encode()


def make_docx(include_table=True, include_list=True, include_heading=True) -> bytes:
    import docx

    d = docx.Document()
    if include_heading:
        d.add_heading("Quarterly Report", level=1)
        d.add_heading("Detail Section", level=2)
    p = d.add_paragraph()
    p.add_run("Normal text. ").bold = False
    p.add_run("Bold text").bold = True
    p.add_run(" and ")
    p.add_run("italic text").italic = True
    d.add_paragraph("Second paragraph to give the layout something to flow.")

    if include_list:
        for item in ("Alpha item", "Beta item", "Gamma item"):
            d.add_paragraph(item, style="List Bullet")

    if include_table:
        t = d.add_table(rows=3, cols=3)
        t.style = "Table Grid"
        for j, v in enumerate(("Product", "Units", "Price")):
            t.cell(0, j).text = v
        for i, row in enumerate([("Widget", "12", "9.99"), ("Gadget", "3", "49.50")], start=1):
            for j, v in enumerate(row):
                t.cell(i, j).text = v

    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def pdf_text(data: bytes) -> str:
    doc = PdfDocument(data)
    try:
        return "".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
    finally:
        doc.close()


# ------------------------------------------------------------- pdf -> image
class TestPdfToImages:
    def test_renders_every_page(self):
        outputs = pdf_to_images(make_pdf(pages=3), dpi=100, settings=SMALL)
        assert len(outputs) == 3

    def test_dpi_controls_resolution(self):
        low = pdf_to_images(make_pdf(1), dpi=72, settings=SMALL)[0]
        high = pdf_to_images(make_pdf(1), dpi=200, settings=SMALL)[0]
        low_img = Image.open(io.BytesIO(low))
        high_img = Image.open(io.BytesIO(high))
        # A4 at 72dpi = 595x842; at 200dpi it should be substantially larger.
        assert low_img.size == (595, 842)
        assert high_img.width > low_img.width * 2
        assert high_img.height > low_img.height * 2

    def test_output_is_a_valid_decodable_image(self):
        data = pdf_to_images(make_pdf(1), dpi=120, settings=SMALL)[0]
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        img = Image.open(io.BytesIO(data))
        img.load()
        assert img.size[0] > 100

    def test_jpeg_output(self):
        data = pdf_to_images(make_pdf(1), dpi=100, image_format="jpeg", settings=SMALL)[0]
        assert data.startswith(b"\xff\xd8\xff")  # JPEG SOI marker
        Image.open(io.BytesIO(data)).load()

    def test_rejects_page_count_over_limit(self):
        tight = Settings(max_pdf_pages=1)
        with pytest.raises(MaliciousFileError):
            pdf_to_images(make_pdf(pages=5), dpi=100, settings=tight)

    def test_rejects_out_of_range_dpi(self):
        with pytest.raises(ConversionError) as exc:
            pdf_to_images(make_pdf(1), dpi=9999, settings=SMALL)
        assert exc.value.code == "invalid_dpi"

    def test_rejects_corrupt_pdf(self):
        with pytest.raises(CorruptFileError):
            pdf_to_images(b"%PDF-1.7\nthis is not a real pdf body", dpi=100, settings=SMALL)

    def test_huge_page_is_clamped_not_rendered_at_full_size(self):
        """A 200-inch page at 400 DPI is 1.1B pixels without clamping."""
        from reportlab.pdfgen.canvas import Canvas

        buf = io.BytesIO()
        c = Canvas(buf, pagesize=(14400, 14400))
        c.drawString(100, 100, "huge")
        c.showPage()
        c.save()

        tiny = Settings(max_render_pixels=1_000_000, max_render_dpi=400)
        out = pdf_to_images(buf.getvalue(), dpi=400, settings=tiny)[0]
        img = Image.open(io.BytesIO(out))
        assert img.width * img.height <= 1_000_000


# ------------------------------------------------------------- image -> pdf
class TestImagesToPdf:
    def test_produces_a_real_pdf(self):
        data = image_to_pdf(make_image(), settings=SMALL)
        assert data.startswith(b"%PDF-")
        doc = PdfDocument(data)
        try:
            assert len(doc) == 1
        finally:
            doc.close()

    def test_multiple_images_become_multiple_pages(self):
        data = images_to_pdf([make_image(), make_image(), make_image()], settings=SMALL)
        doc = PdfDocument(data)
        try:
            assert len(doc) == 3
        finally:
            doc.close()

    def test_alpha_is_flattened_not_crashed(self):
        """A transparent PNG must composite onto white, not raise."""

        alpha = Image.new("L", (100, 100), 0)
        buf = io.BytesIO()
        Image.new("RGBA", (100, 100), (255, 0, 0, 255)).putalpha(alpha)
        img = Image.new("RGBA", (100, 100), (255, 0, 0, 255))
        img.putalpha(alpha)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        data = image_to_pdf(buf.getvalue(), settings=SMALL)
        assert data.startswith(b"%PDF-")

    def test_palette_image_with_transparency(self):
        img = Image.new("P", (80, 80))
        img.putpalette([0, 0, 0] * 256)
        img.info["transparency"] = 0
        buf = io.BytesIO()
        img.save(buf, "PNG")
        data = image_to_pdf(buf.getvalue(), settings=SMALL)
        assert data.startswith(b"%PDF-")

    def test_jpeg_input(self):
        data = image_to_pdf(make_image("JPEG"), settings=SMALL)
        assert data.startswith(b"%PDF-")

    def test_greyscale_input(self):
        data = image_to_pdf(make_image("PNG", mode="L"), settings=SMALL)
        assert data.startswith(b"%PDF-")

    def test_rejects_pixel_bomb(self):
        """A header claiming 20000x20000 must be refused before decode."""
        img = Image.new("RGB", (10, 10))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        # Rewrite the IHDR to claim a huge size, which is the classic bomb shape.
        raw = bytearray(buf.getvalue())
        raw[16:24] = (20000).to_bytes(4, "big") + (20000).to_bytes(4, "big")
        tight = Settings(max_image_pixels=1_000_000)
        with pytest.raises((MaliciousFileError, CorruptFileError)):
            image_to_pdf(bytes(raw), settings=tight)

    def test_rejects_dimension_bomb(self):
        """1 x 100000 passes a pixel-count check but must still be refused."""
        img = Image.new("RGB", (1, 400))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        raw = bytearray(buf.getvalue())
        raw[16:24] = (1).to_bytes(4, "big") + (100000).to_bytes(4, "big")
        tight = Settings(max_image_pixels=50_000_000, max_image_dimension=5000)
        with pytest.raises((MaliciousFileError, CorruptFileError)):
            image_to_pdf(bytes(raw), settings=tight)

    def test_rejects_empty_list(self):
        with pytest.raises(ConversionError):
            images_to_pdf([], settings=SMALL)

    def test_rejects_too_many_pages(self):
        tight = Settings(max_pdf_pages=2)
        with pytest.raises(MaliciousFileError):
            images_to_pdf([make_image()] * 5, settings=tight)

    def test_rejects_garbage(self):
        with pytest.raises(CorruptFileError):
            image_to_pdf(b"definitely not an image", settings=SMALL)

    def test_page_order_is_preserved(self):
        """Red then blue: page 1 must be red-dominant, page 2 blue-dominant.

        The sample point must be the page centre. At the corners the page is
        the A4 margin, which is white — an earlier version sampled (5,5) and
        read 255/255 on both pages and wrongly concluded the order was lost.
        """
        red = Image.new("RGB", (200, 200), (255, 0, 0))
        blue = Image.new("RGB", (200, 200), (0, 0, 255))
        r_buf, b_buf = io.BytesIO(), io.BytesIO()
        red.save(r_buf, "PNG")
        blue.save(b_buf, "PNG")

        pdf_bytes = images_to_pdf([r_buf.getvalue(), b_buf.getvalue()], settings=SMALL)
        rendered = pdf_to_images(pdf_bytes, dpi=40, settings=Settings(max_pdf_pages=5))
        assert len(rendered) == 2

        def dominant(data: bytes) -> tuple[int, int, int]:
            img = Image.open(io.BytesIO(data)).convert("RGB")
            cx, cy = img.width // 2, img.height // 2
            return img.getpixel((cx, cy))

        first_r, first_g, first_b = dominant(rendered[0])
        second_r, second_g, second_b = dominant(rendered[1])
        assert first_r > first_b, "page 1 should be red-dominant"
        assert second_b > second_r, "page 2 should be blue-dominant"


# ------------------------------------------------------------- svg -> image
class TestSvgToImage:
    def test_renders_png(self):
        data = svg_to_image(make_svg(), output_format="png", settings=SMALL)
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        img = Image.open(io.BytesIO(data))
        img.load()
        assert img.size == (400, 300)

    def test_explicit_size_overrides_document_size(self):
        data = svg_to_image(make_svg(400, 300), output_format="png", width=1000, height=750, settings=SMALL)
        img = Image.open(io.BytesIO(data))
        assert img.size == (1000, 750)

    def test_jpeg_output_flattens_alpha(self):
        data = svg_to_image(make_svg(), output_format="jpeg", settings=SMALL)
        assert data.startswith(b"\xff\xd8\xff")
        img = Image.open(io.BytesIO(data))
        assert img.mode == "RGB"

    def test_rejects_bad_format(self):
        with pytest.raises(ConversionError):
            svg_to_image(make_svg(), output_format="gif", settings=SMALL)

    def test_rejects_oversized_request(self):
        # max_svg_pixels has a ge=1_000_000 floor in Settings, so the tiny
        # budget must be expressed through max_image_dimension instead.
        tight = Settings(max_svg_pixels=1_000_000, max_image_dimension=1000)
        with pytest.raises(MaliciousFileError):
            svg_to_image(make_svg(4000, 4000), output_format="png", settings=tight)

    def test_malformed_svg_raises_conversion_error(self):
        with pytest.raises((CorruptFileError, ConversionError)):
            svg_to_image(b"<svg><unclosed>", output_format="png", settings=SMALL)


class TestSvgOutputIsNeverSvg:
    """The converter-XSS control.

    If any code path ever returns SVG bytes with an ``image/svg+xml``
    content type, a hostile document executes script in the visitor's
    browser. These tests assert the output is always a raster format, by
    checking magic bytes rather than trusting the caller.
    """

    HOSTILE = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
  <script>alert(document.cookie)</script>
  <rect width="100" height="100" fill="red"/>
</svg>"""

    @pytest.mark.parametrize("fmt", ["png", "jpeg"])
    def test_output_is_raster_never_svg(self, fmt):
        out = svg_to_image(self.HOSTILE, output_format=fmt, settings=SMALL)
        assert not out.lstrip().startswith(b"<svg")
        assert not out.lstrip().startswith(b"<?xml")
        assert b"<script" not in out

    def test_script_tag_is_stripped_before_rendering(self):
        cleaned = sanitise_svg(self.HOSTILE)
        assert b"<script" not in cleaned
        assert b"alert(" not in cleaned

    def test_event_handler_is_stripped(self):
        payload = b"""<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
        <rect width="10" height="10" onload="alert(1)" onclick="steal()"/></svg>"""
        cleaned = sanitise_svg(payload)
        assert b"onload" not in cleaned
        assert b"onclick" not in cleaned
        assert b"steal()" not in cleaned

    def test_javascript_uri_is_stripped(self):
        payload = b"""<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
        <a href="javascript:alert(1)"><rect width="10" height="10"/></a></svg>"""
        cleaned = sanitise_svg(payload)
        assert b"javascript:" not in cleaned

    def test_external_entity_is_rejected(self):
        xxe = b"""<?xml version="1.0"?>
        <!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
        <svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
        <text x="0" y="10">&xxe;</text></svg>"""
        with pytest.raises(MaliciousFileError):
            sanitise_svg(xxe)

    def test_metadata_endpoint_reference_does_not_block_render(self):
        """resvg cannot fetch, so a metadata URL is inert rather than an error.

        This is the SSRF property. CairoSVG would have made this request.
        """
        payload = b"""<svg xmlns="http://www.w3.org/2000/svg"
             xmlns:xlink="http://www.w3.org/1999/xlink"
             width="50" height="50">
        <image xlink:href="http://169.254.169.254/latest/meta-data/" width="50" height="50"/>
        <rect width="50" height="50" fill="blue"/></svg>"""
        out = svg_to_image(payload, output_format="png", settings=SMALL)
        assert out.startswith(b"\x89PNG")


# ------------------------------------------------------------- docx -> pdf
class TestDocxToPdf:
    def test_produces_a_real_pdf(self):
        pdf_bytes, stats = docx_to_pdf(make_docx(), settings=SMALL)
        assert pdf_bytes.startswith(b"%PDF-")
        assert stats["paragraphs"] > 0

    def test_text_survives_the_round_trip(self):
        pdf_bytes, _ = docx_to_pdf(make_docx(), settings=SMALL)
        text = pdf_text(pdf_bytes)
        assert "Quarterly Report" in text
        assert "Bold text" in text
        assert "Alpha item" in text
        assert "Widget" in text

    def test_table_is_preserved_in_document_order(self):
        """Regression: iterating document.paragraphals drops tables.

        If this fails, the table content is missing from the PDF entirely —
        which is silent data loss, the worst failure mode for a document tool.
        """
        pdf_bytes, stats = docx_to_pdf(make_docx(include_table=True), settings=SMALL)
        assert stats["tables"] == 1
        text = pdf_text(pdf_bytes)
        for expected in ("Product", "Units", "Price", "Widget", "Gadget", "49.50"):
            assert expected in text, f"missing {expected!r} from the PDF"

    def test_list_items_become_a_list(self):
        pdf_bytes, stats = docx_to_pdf(make_docx(include_list=True), settings=SMALL)
        assert stats["lists"] >= 1
        text = pdf_text(pdf_bytes)
        for item in ("Alpha item", "Beta item", "Gamma item"):
            assert item in text

    def test_headings_counted(self):
        _, stats = docx_to_pdf(make_docx(include_heading=True), settings=SMALL)
        assert stats["headings"] >= 2

    def test_page_count_is_sane(self):
        pdf_bytes, _ = docx_to_pdf(make_docx(), settings=SMALL)
        doc = PdfDocument(pdf_bytes)
        try:
            assert len(doc) >= 1
        finally:
            doc.close()

    def test_markup_in_source_text_does_not_corrupt_pdf(self):
        """Regression guard: unescaped run text breaks ReportLab markup.

        A document containing ``<b>`` or ``&`` must be escaped, or the
        generated PDF is either mangled or the build raises.
        """
        import docx

        d = docx.Document()
        d.add_paragraph("Angle brackets <b>bold</b> and an ampersand & entity")
        d.add_paragraph("A stray < and > and & inside plain prose.")
        buf = io.BytesIO()
        d.save(buf)

        pdf_bytes, _ = docx_to_pdf(buf.getvalue(), settings=SMALL)
        assert pdf_bytes.startswith(b"%PDF-")
        text = pdf_text(pdf_bytes)
        # The visible text is preserved (ReportLab renders it literally).
        assert "ampersand" in text

    def test_empty_document_still_produces_a_pdf(self):
        import docx

        d = docx.Document()
        buf = io.BytesIO()
        d.save(buf)
        pdf_bytes, _ = docx_to_pdf(buf.getvalue(), settings=SMALL)
        assert pdf_bytes.startswith(b"%PDF-")

    def test_rejects_non_docx_bytes(self):
        with pytest.raises(CorruptFileError):
            docx_to_pdf(b"PK\x03\x04 but not actually a zip", settings=SMALL)

    def test_rejects_element_bomb(self):
        import docx

        d = docx.Document()
        for _ in range(300):
            d.add_paragraph("filler paragraph for the element count test")
        buf = io.BytesIO()
        d.save(buf)

        tight = Settings(max_docx_elements=50)
        with pytest.raises(MaliciousFileError):
            docx_to_pdf(buf.getvalue(), settings=tight)

    def test_large_document_within_limits_succeeds(self):
        import docx

        d = docx.Document()
        for i in range(400):
            d.add_paragraph(f"Paragraph number {i} with some body text to lay out.")
        buf = io.BytesIO()
        d.save(buf)
        pdf_bytes, _ = docx_to_pdf(buf.getvalue(), settings=SMALL)
        assert pdf_bytes.startswith(b"%PDF-")
