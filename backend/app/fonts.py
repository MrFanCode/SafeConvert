"""Font registration.

ReportLab's built-in fonts are the Type-1 base-14 set (Helvetica, Times,
Courier). Those cover ASCII only and, more importantly, they make output
depend on nothing being installed — which is the wrong trade for a service
that must handle Malay, Chinese, Arabic and emoji without turning a document
into a column of ``#`` characters.

So we register the DejaVu family (SIL Open Font License 1.1, redistributable
and embeddable) from the paths present in the container image. DejaVu covers
Latin, Greek, Cyrillic, Hebrew, Arabic and a large symbol set — that is why
it ships with every Linux distro and every LibreOffice install.

If the fonts are missing the module still imports; ``registered`` is False and
the converters fall back to the base-14 set. A missing font is a fidelity
problem, not a crash.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFError, TTFont

logger = logging.getLogger("safeconvert.fonts")

# Names must match the constants in app.converters.docx.
FAMILY_SERIF = "DejaVuSerif"
FAMILY_SANS = "DejaVuSans"
FAMILY_MONO = "DejaVuSansMono"

_SEARCH_ROOTS = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/app/fonts",
)

_FILES: dict[str, dict[str, str]] = {
    FAMILY_SERIF: {
        "": "DejaVuSerif.ttf",
        "-Bold": "DejaVuSerif-Bold.ttf",
        "-Italic": "DejaVuSerif-Italic.ttf",
        "-BoldItalic": "DejaVuSerif-BoldItalic.ttf",
    },
    FAMILY_SANS: {
        "": "DejaVuSans.ttf",
        "-Bold": "DejaVuSans-Bold.ttf",
        "-Italic": "DejaVuSans-Oblique.ttf",
        "-BoldItalic": "DejaVuSans-BoldOblique.ttf",
    },
    FAMILY_MONO: {
        "": "DejaVuSansMono.ttf",
        "-Bold": "DejaVuSansMono-Bold.ttf",
        "-Italic": "DejaVuSansMono-Oblique.ttf",
        "-BoldItalic": "DejaVuSansMono-BoldOblique.ttf",
    },
}


@lru_cache(maxsize=1)
def _find(filename: str) -> Path | None:
    for root in _SEARCH_ROOTS:
        base = Path(root)
        if not base.is_dir():
            continue
        try:
            for path in base.rglob(filename):
                if path.is_file():
                    return path
        except (PermissionError, OSError):
            continue
    return None


@lru_cache(maxsize=1)
def register_fonts() -> bool:
    """Register the DejaVu family. Returns True when fully available.

    Only the *regular* face of each family is required. Bold and italic are
    optional: a host may ship a subset (a minimal container has only 8 of the
    12 faces, for example). When a variant is missing its face name is
    aliased to the regular face, so ``<b>`` and ``<i>`` markup still renders
    — as regular weight rather than bold — instead of raising.

    That trade is deliberate. "Bold text renders as regular" is a fidelity
    problem the user can see; "every DOCX conversion fails" is an outage.
    """
    regular_ok = 0
    families: list[str] = []

    for family, variants in _FILES.items():
        regular_path = _find(variants[""])
        if regular_path is None:
            logger.warning("font family unavailable: %s (continuing)", family)
            continue
        regular_ok += 1
        families.append(family)

        try:
            pdfmetrics.registerFont(TTFont(family, str(regular_path)))
        except (TTFError, OSError) as exc:
            logger.warning("could not register %s: %s", family, exc)
            regular_ok -= 1
            families.remove(family)
            continue

        for variant, filename in variants.items():
            if not variant:
                continue
            face = f"{family}-{variant.lstrip('-')}"
            path = _find(filename)
            if path is None:
                # Alias to the regular face so markup resolves.
                logger.info("font variant missing (%s); aliasing to regular", filename)
                pdfmetrics.registerFontFamily(
                    family, normal=family, bold=family, italic=family, boldItalic=family
                )
                break
            try:
                pdfmetrics.registerFont(TTFont(face, str(path)))
            except (TTFError, OSError) as exc:
                logger.warning("could not register %s: %s", filename, exc)
                pdfmetrics.registerFontFamily(
                    family, normal=family, bold=family, italic=family, boldItalic=family
                )
                break
        else:
            pdfmetrics.registerFontFamily(
                family,
                normal=family,
                bold=f"{family}-Bold",
                italic=f"{family}-Italic",
                boldItalic=f"{family}-BoldItalic",
            )

    complete = regular_ok == len(_FILES)
    if not complete:
        logger.warning(
            "%d/%d font families available; converters fall back to base-14",
            regular_ok,
            len(_FILES),
        )
    return complete


@lru_cache(maxsize=1)
def is_available() -> bool:
    return register_fonts()


def available_families() -> list[str]:
    """Families actually usable right now (regular face registered)."""
    register_fonts()
    names = set(pdfmetrics.getRegisteredFontNames())
    return [f for f in _FILES if f in names]
