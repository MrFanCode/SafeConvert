"""Regression tests for the PDF page-load failure.

Found by the end-to-end smoke test rather than by any unit test, which is the
point of having one. A PDF whose page tree parses but whose page object does
not is enough to make PDFium report a plausible page count and then refuse to
load a page. Before this was handled, `document[index]` sat outside every
`try` block and the resulting `PdfiumError` escaped as an opaque
`500 internal_error` — telling the user the service was broken when their file
was merely damaged, and telling an attacker that a malformed input reached an
unhandled path.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.converters.pdf import pdf_to_images
from app.errors import ConversionError, CorruptFileError

SETTINGS = get_settings()


def _dangling_kids_pdf(pages: int = 2) -> bytes:
    """A PDF that OPENS and REPORTS a page count, but whose pages will not load.

    This distinction is the whole test. My first attempt truncated a valid PDF,
    which makes pdfium fail to open the document at all -- so it was caught by
    the pre-existing "cannot open" guard and never reached the page-load path
    the fix is about. The regression test therefore passed even with the fix
    removed, which is worse than no test.

    This fixture keeps the cross-reference table valid and points /Kids at
    object numbers that do not exist, so `len(document)` succeeds while
    `document[i]` raises. Verified: opens, len == 2, both pages fail to load.
    """
    from tools.smoke import build_pdf

    good = build_pdf(pages)
    real_kids = b" ".join(
        f"{4 + i * 2} 0 R".encode() for i in range(pages)
    )
    dangling = b" ".join(f"{900 + i} 0 R".encode() for i in range(pages))
    assert b"/Kids [" + real_kids + b"]" in good, "fixture shape changed"
    return good.replace(b"/Kids [" + real_kids + b"]", b"/Kids [" + dangling + b"]")


def test_fixture_really_does_reach_the_page_load_path() -> None:
    """Guard the guard: if pdfium changes behaviour this test must say so.

    Otherwise the tests below could quietly start passing for the wrong reason
    again, which is exactly the failure mode this file was written to prevent.
    """
    import pypdfium2 as pdfium

    data = _dangling_kids_pdf(2)
    document = pdfium.PdfDocument(data)
    try:
        assert len(document) == 2, "fixture no longer reports a page count"
        with pytest.raises(Exception):  # noqa: B017 - any pdfium error will do
            document[0]
    finally:
        document.close()


def test_page_counted_but_page_unloadable_is_a_corrupt_file() -> None:
    data = _dangling_kids_pdf()
    with pytest.raises(CorruptFileError):
        pdf_to_images(data, 150, "png", None, SETTINGS)


def test_damaged_pdf_does_not_leak_internals() -> None:
    """The user-facing message must not name a parser, library or path."""
    data = _dangling_kids_pdf()
    with pytest.raises(CorruptFileError) as exc:
        pdf_to_images(data, 150, "png", None, SETTINGS)
    message = str(exc.value)
    assert message, "an empty error message tells the user nothing"
    for leak in ("pdfium", "PdfiumError", "Traceback", "/usr/", "site-packages"):
        assert leak not in message, f"message leaks internals: {leak!r}"


def test_truncated_pdf_is_a_corrupt_file() -> None:
    """A PDF that will not even open is handled by the pre-existing guard."""
    from tools.smoke import build_pdf

    data = build_pdf(3)
    head = data[: data.index(b"4 0 obj")] + b"\n%%EOF\n"
    with pytest.raises(CorruptFileError):
        pdf_to_images(head, 150, "png", None, SETTINGS)


def test_completely_bogus_pdf_is_a_corrupt_file() -> None:
    with pytest.raises(CorruptFileError):
        pdf_to_images(b"%PDF-1.4\nnot really a pdf at all\n%%EOF\n", 150, "png", None, SETTINGS)


def test_oversized_page_count_is_still_a_malicious_file() -> None:
    """The page-count guard must not be shadowed by the new page-load handler."""
    from tools.smoke import build_pdf

    data = build_pdf(2)
    tight = SETTINGS.model_copy(update={"max_pdf_pages": 1})
    with pytest.raises(ConversionError) as exc:
        pdf_to_images(data, 150, "png", None, tight)
    assert "limit" in str(exc.value).lower()


def test_valid_pdf_still_converts() -> None:
    """Guard against the fix breaking the happy path."""
    from tools.smoke import build_pdf

    out = pdf_to_images(build_pdf(2), 150, "png", None, SETTINGS)
    assert len(out) == 2
    assert all(image[:8] == b"\x89PNG\r\n\x1a\n" for image in out)
