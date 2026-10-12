"""The smoke test's own fixtures must be valid.

A test harness with a broken fixture is worse than no test: it either fails
constantly (noise, ignored) or passes for the wrong reason (worse).

This exists because the first version of tools/smoke.py hand-rolled a PDF with
one shared content stream across all pages. pdfium reported the correct page
count and then refused to load a page, so the smoke test failed on a perfectly
good PDF. That failure turned out to be a real bug in the app as well (a page
LOAD error escaping as a 500), which is the best kind of bug to find -- but the
fixture still had to be fixed, and this file stops anyone re-introducing a
broken one.

These assertions read the fixtures with the same libraries the converters use,
so "the fixture is valid" means "the converter can actually read it".
"""

from __future__ import annotations

import io
import zipfile

import pypdfium2 as pdfium
from PIL import Image

from tools.smoke import PNG_1PX, build_docx, build_pdf


def test_png_fixture_is_a_real_png() -> None:
    assert PNG_1PX[:8] == b"\x89PNG\r\n\x1a\n"
    image = Image.open(io.BytesIO(PNG_1PX))
    image.load()  # forces a full decode, not just a header read
    assert image.format == "PNG"
    assert image.size == (1, 1)


def test_pdf_fixture_parses_and_every_page_loads() -> None:
    for pages in (1, 2, 3, 5):
        data = build_pdf(pages)
        document = pdfium.PdfDocument(data)
        try:
            assert len(document) == pages, f"asked for {pages} pages, got {len(document)}"
            # The assertion the original bug needed: counting pages is not the
            # same as being able to load them.
            for index in range(pages):
                page = document[index]
                width, height = page.get_size()
                assert width > 0 and height > 0, f"page {index + 1} has a zero dimension"
        finally:
            document.close()


def test_pdf_fixture_renders() -> None:
    data = build_pdf(2)
    document = pdfium.PdfDocument(data)
    try:
        for index in range(len(document)):
            page = document[index]
            bitmap = page.render(scale=1.0)
            image = bitmap.to_pil()
            assert image.width > 0 and image.height > 0
    finally:
        document.close()


def test_pdf_fixture_rejects_a_bad_page_count() -> None:
    try:
        build_pdf(0)
    except ValueError:
        return
    raise AssertionError("build_pdf(0) should raise rather than emit an empty PDF")


def test_docx_fixture_is_a_valid_package() -> None:
    data = build_docx()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        # The exact entries app/validation.py requires of an OOXML package.
        assert "[Content_Types].xml" in names
        assert "word/document.xml" in names
        assert zf.testzip() is None, "zip CRC failure"

    # And it must be readable by the library the converter uses.
    import docx

    document = docx.Document(io.BytesIO(data))
    texts = [p.text for p in document.paragraphs if p.text.strip()]
    assert texts, "no paragraph text survived the round-trip"
    assert any("Smoke" in t for t in texts), texts


def test_svg_hostile_fixtures_are_well_formed() -> None:
    # The XXE payload in smoke.py must be parseable XML *with* the entity, or
    # the test would pass because the parser choked early rather than because
    # the entity check fired.
    from xml.dom import minidom

    xxe = (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        '<text x="0" y="20">&xxe;</text></svg>'
    )
    # minidom will not expand the external entity, but it must parse the
    # doctype declaration and the entity reference without error.
    # S318 is the POINT: this asserts the hostile DOCTYPE is well-formed XML,
    # which is why the app must never hand user input to minidom. The parser
    # under test here is the stdlib one, on a literal defined in this file.
    parsed = minidom.parseString(xxe)  # noqa: S318
    assert parsed.doctype is not None
    assert parsed.doctype.entities.length == 1
    # minidom's Entity exposes nodeName, not name -- there is no .name
    # attribute. Asserting on .name would pass vacuously via AttributeError
    # being swallowed, or fail confusingly.
    entity = parsed.doctype.entities.item(0)
    assert entity is not None
    assert entity.nodeName == "xxe"
    # And crucially the SYSTEM id is the thing the service must refuse to fetch.
    assert entity.systemId == "file:///etc/passwd"
