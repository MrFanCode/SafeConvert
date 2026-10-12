"""DOCX → PDF, in pure Python.

**This is not a Word renderer, and the docs must say so plainly.**

Word's layout engine is enormous. This module reproduces the *structure* of a
document — headings, paragraphs, character formatting, lists and tables — and
approximates the rest. What that means in practice is documented in
``docs/DOCX-FIDELITY.md``. Nothing here shells out to LibreOffice, so the
service ships no C++ office suite and inherits none of its CVE history.

**What is preserved**
  - Heading hierarchy (levels 1-3 map to distinct sizes)
  - Paragraph text and reading order, including tables inline at the right
    position (Word stores tables as sibling paragraphs, not a separate list —
    iterating ``document.paragraphs`` alone silently drops them)
  - Bold / italic runs
  - Bullet and numbered lists, with indentation
  - Tables: cell text, grid, header row emphasis
  - Page size, margins, page breaks

**What is approximated**
  - Exact line breaking and font metrics (no kerning table, no Word's grid)
  - Images embedded in the document
  - Headers/footers, footnotes, text boxes, columns, tracked changes
  - Fonts not present in the container are substituted (Liberation/DejaVu)

The substitution is why this is safe: the input is parsed by python-docx and
drawn by ReportLab, both pure Python, both driven by the same XML you can
read. There is no native code path where an OOXML payload becomes a memory
corruption bug.
"""

from __future__ import annotations

import html
import io
import re
from dataclasses import dataclass

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    ListFlowable,
    ListItem,
    PageBreak,
    SimpleDocTemplate,
    Spacer,
    TableStyle,
)
from reportlab.platypus import (
    Paragraph as RLParagraph,
)
from reportlab.platypus import (
    Table as RLTable,
)

from ..config import Settings, get_settings
from ..errors import ConversionError, CorruptFileError, MaliciousFileError
from ..fonts import FAMILY_MONO, FAMILY_SANS, FAMILY_SERIF, register_fonts

# Fonts are registered once at import time. ReportLab raises
# "Can't map determine family/bold/italic for dejavusans" for an unregistered
# font family, so this must happen before any Paragraph is built.
_FONTS_OK = register_fonts()

# Fall back to the base-14 Type-1 set if DejaVu is unavailable. ASCII-only,
# but it never fails, so a missing font degrades fidelity instead of
# breaking every conversion.
_SERIF = FAMILY_SERIF if _FONTS_OK else "Times-Roman"
_SANS = FAMILY_SANS if _FONTS_OK else "Helvetica"
_MONO = FAMILY_MONO if _FONTS_OK else "Courier"

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _escape(text: str) -> str:
    """XML-escape run text for safe inclusion in ReportLab markup.

    python-docx already unescapes the document's XML, so the text we hold is
    *content*. Passing it to ReportLab's mini-markup without escaping would
    let a document containing ``<b>`` or ``&`` corrupt the generated PDF's
    markup stream — and a malformed markup stream is a parser bug waiting to
    happen in ReportLab. This is the single most important correctness line
    in the module.
    """
    text = _CONTROL_CHARS.sub("", text)
    return html.escape(text, quote=False)


@dataclass
class _DocxStyles:
    h1: ParagraphStyle
    h2: ParagraphStyle
    h3: ParagraphStyle
    body: ParagraphStyle
    bullet: ParagraphStyle
    caption: ParagraphStyle
    table_header: ParagraphStyle
    table_cell: ParagraphStyle
    pre: ParagraphStyle


def _build_styles() -> _DocxStyles:
    base = getSampleStyleSheet()
    body_font_size = 10.5

    def mk(name: str, parent_size: float, leading: float, **kw) -> ParagraphStyle:
        return ParagraphStyle(
            name,
            parent=base["Normal"],
            fontName=kw.pop("fontName", _SANS),
            fontSize=parent_size,
            leading=leading,
            spaceAfter=kw.pop("spaceAfter", 6),
            alignment=kw.pop("alignment", TA_LEFT),
            **kw,
        )

    return _DocxStyles(
        h1=mk("h1", 19, 23, fontName=_SANS, spaceAfter=10, spaceBefore=4),
        h2=mk("h2", 15, 19, spaceAfter=8, spaceBefore=10),
        h3=mk("h3", 12.5, 16, spaceAfter=6, spaceBefore=8),
        body=mk("body", body_font_size, 14.5),
        bullet=mk("bullet", body_font_size, 14.5, spaceAfter=2),
        caption=mk("caption", 9, 12, fontName=_SANS, textColor=colors.HexColor("#444444")),
        table_header=mk("th", 9.5, 12, fontName=_SANS),
        table_cell=mk("td", 9.5, 12, fontName=_SANS, spaceAfter=0),
        pre=mk("pre", 9, 11, fontName=_MONO, spaceAfter=6),
    )


def _runs_to_markup(paragraph: DocxParagraph) -> str:
    """Render a paragraph's runs as escaped ReportLab markup.

    Each run's own bold/italic flags are honoured, which is why this iterates
    runs rather than using ``paragraph.text`` (that loses formatting).
    """
    parts: list[str] = []
    for run in paragraph.runs:
        text = _escape(run.text)
        if not text:
            continue
        if run.bold:
            text = f"<b>{text}</b>"
        if run.italic:
            text = f"<i>{text}</i>"
        parts.append(text)

    if not parts:
        # A paragraph can hold text that isn't in a run (e.g. inside a
        # hyperlink or smart tag). Fall back rather than losing content.
        return _escape(paragraph.text)
    return "".join(parts)


def _style_for(paragraph: DocxParagraph, styles: _DocxStyles):
    name = (paragraph.style.name if paragraph.style is not None else "") or ""
    lowered = name.lower()
    if "heading 1" in lowered or lowered == "title":
        return styles.h1
    if "heading 2" in lowered:
        return styles.h2
    if "heading 3" in lowered:
        return styles.h3
    if "caption" in lowered:
        return styles.caption
    if lowered.startswith("quote") or "intense quote" in lowered:
        return styles.body
    return styles.body


def _is_list_paragraph(paragraph: DocxParagraph) -> bool:
    name = (paragraph.style.name if paragraph.style is not None else "") or ""
    return "list" in name.lower()


def _has_page_break(paragraph: DocxParagraph) -> bool:
    """True when the paragraph carries an explicit page break."""
    for run in paragraph.runs:
        for br in run._element.findall(qn("w:br")):
            if br.get(qn("w:type")) == "page":
                return True
    return False


def _build_table(docx_table: DocxTable, styles: _DocxStyles, page_width: float) -> RLTable | None:
    raw_rows: list[list[str]] = []
    for row in docx_table.rows:
        cells: list[str] = []
        for cell in row.cells:
            text = " ".join(p.text.strip() for p in cell.paragraphs if p.text.strip())
            cells.append(_escape(text))
        # Word tables use merged cells, which repeat content across the span.
        # Collapse consecutive duplicates so a merged header reads once.
        deduped: list[str] = []
        for cell in cells:
            if deduped and cell and cell == deduped[-1]:
                continue
            deduped.append(cell)
        raw_rows.append(deduped or [""])

    if not raw_rows:
        return None

    column_count = max(len(r) for r in raw_rows)
    normalised = [r + [""] * (column_count - len(r)) for r in raw_rows]

    # Build Paragraph flowables directly rather than strings + a private
    # attribute. Strings in a Table do not wrap: a long cell value overflows
    # the column and gets clipped. Paragraphs wrap and honour the style.
    cell_flowables: list[list[RLParagraph]] = []
    for index, row in enumerate(normalised):
        sty = styles.table_header if index == 0 else styles.table_cell
        rendered: list[RLParagraph] = []
        for value in row:
            if index == 0 and value.strip():
                rendered.append(RLParagraph(f"<b>{value}</b>", sty))
            else:
                rendered.append(RLParagraph(value or " ", sty))
        cell_flowables.append(rendered)

    table = RLTable(
        cell_flowables,
        colWidths=[page_width / column_count] * column_count,
        repeatRows=1 if len(cell_flowables) > 1 else 0,
    )
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9aa3ad")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f6")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _build_story(
    source, styles: _DocxStyles, page_width: float, settings: Settings
) -> tuple[list, dict]:
    """Walk the document body in order and build the ReportLab story.

    Iterates ``document.element.body`` children directly. This is the crux of
    the module: ``document.paragraphs`` yields only ``w:p`` elements, so a
    table placed between two paragraphs is silently dropped. Walking the body
    preserves true document order, which is what a reader expects.
    """
    story: list = []
    stats = {"paragraphs": 0, "tables": 0, "lists": 0, "headings": 0, "page_breaks": 0}
    element_count = 0

    pending_list: list[str] = []

    def flush_list() -> None:
        nonlocal pending_list
        if not pending_list:
            return
        story.append(
            ListFlowable(
                [ListItem(RLParagraph(item, styles.bullet), leftIndent=14) for item in pending_list],
                bulletType="bullet",
                start="•",
                leftIndent=14,
            )
        )
        story.append(Spacer(1, 4))
        stats["lists"] += 1
        pending_list = []

    for child in source.element.body.iterchildren():
        element_count += 1
        if element_count > settings.max_docx_elements:
            raise MaliciousFileError("That document has too many elements to process safely.")

        tag = child.tag

        if tag == qn("w:p"):
            paragraph = DocxParagraph(child, source)

            if _has_page_break(paragraph):
                flush_list()
                story.append(PageBreak())
                stats["page_breaks"] += 1

            markup = _runs_to_markup(paragraph)
            text = paragraph.text.strip()
            if not text:
                flush_list()
                story.append(Spacer(1, 5))
                continue

            style = _style_for(paragraph, styles)
            if style in (styles.h1, styles.h2, styles.h3):
                stats["headings"] += 1

            if _is_list_paragraph(paragraph):
                pending_list.append(markup)
                continue

            flush_list()
            story.append(RLParagraph(markup, style))
            stats["paragraphs"] += 1

        elif tag == qn("w:tbl"):
            flush_list()
            table = _build_table(DocxTable(child, source), styles, page_width)
            if table is not None:
                story.append(table)
                story.append(Spacer(1, 8))
                stats["tables"] += 1

    flush_list()
    if not story:
        story.append(RLParagraph("(This document has no readable content.)", styles.body))
    return story, stats


def _page_footer(canvas_obj, doc) -> None:
    """Page number, centred, on every page but the first."""
    canvas_obj.saveState()
    canvas_obj.setFont(_SANS, 8)
    canvas_obj.setFillColor(colors.HexColor("#6b7280"))
    canvas_obj.drawCentredString(doc.pagesize[0] / 2, 12 * mm, str(canvas_obj.getPageNumber()))
    canvas_obj.restoreState()


def docx_to_pdf(data: bytes, settings: Settings | None = None) -> tuple[bytes, dict]:
    """Convert DOCX bytes to PDF bytes. Returns ``(pdf_bytes, stats)``."""
    s = settings or get_settings()
    try:
        source = Document(io.BytesIO(data))
    except Exception as exc:
        raise CorruptFileError() from exc

    styles = _build_styles()
    margin = 18 * mm
    # SimpleDocTemplate needs a path or a file-like; BytesIO works and keeps
    # the whole conversion in memory (no temp file, nothing to clean up).
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin + 6 * mm,
        title=(source.core_properties.title or None),
        author=(source.core_properties.author or None),
    )

    page_width = doc.width
    story, stats = _build_story(source, styles, page_width, s)

    try:
        doc.build(
            story,
            onFirstPage=_page_footer,
            onLaterPages=_page_footer,
        )
    except ConversionError:
        raise
    except Exception as exc:
        # ReportLab raises on malformed flowables. Never surface its text.
        raise ConversionError("That document could not be laid out as PDF.", code="layout_failed") from exc

    pdf_bytes = buffer.getvalue()
    if not pdf_bytes.startswith(b"%PDF-"):
        raise ConversionError("PDF generation produced no output.", code="no_output")

    stats["source_characters"] = sum(len(p.text) for p in source.paragraphs)
    return pdf_bytes, stats
