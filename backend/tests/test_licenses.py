"""Dependency licence assertions.

This project makes explicit licensing claims — most importantly that it avoids
AGPL, because a hosted service linking AGPL code is distributing an AGPL work
over the network. That claim is only worth something if it is checked rather
than asserted, because the failure mode is completely silent: nothing breaks,
no test fails, and the obligation appears years later.

Three claims were wrong when first written, from memory rather than from
metadata:

    Pillow           claimed HPND,          actual MIT-CMU
    python-multipart claimed MIT,           actual Apache-2.0
    resvg-py         claimed MPL-2.0,       upstream resvg is Apache-2.0 OR MIT

All three would have shipped. None would have been noticed.

Run with `--live` to re-check against PyPI instead of the installed
distribution metadata:

    pytest tests/test_licenses.py --live
"""

from __future__ import annotations

import json
import re
import urllib.request
from importlib.metadata import metadata, version

import pytest

# SPDX ids or licence strings, matched case-insensitively against the
# distribution's declared metadata. Order matters: `in` substring matching.
EXPECTED = {
    "pypdfium2": ("bsd-3-clause", "apache-2.0"),
    "pillow": ("mit-cmu", "hpnd", "mit"),
    "reportlab": ("bsd",),
    "python-docx": ("mit",),
    "defusedxml": ("psf", "psfl"),
    "fastapi": ("mit",),
    "starlette": ("bsd",),
    "uvicorn": ("bsd",),
    "pydantic": ("mit",),
    "pydantic-settings": ("mit",),
    "python-multipart": ("apache-2.0", "apache 2.0"),
}

# Packages with no usable licence metadata. Listed explicitly so the exception
# is visible and reviewable rather than a silently skipped assertion.
#
# resvg-py publishes NO licence metadata at all — no License-Expression, no
# License field, no licence classifier. The expected licence below is that of
# the upstream crate it wraps (linebender/resvg, declared Apache-2.0 upstream),
# which is an inference, not a fact read from the distribution. Treat it as
# needing confirmation against the source distribution before a compliance
# review.
NO_METADATA = {
    "resvg-py": ("apache-2.0 or mit", "linebender/resvg, the wrapped crate"),
}


def declared_licence(dist: str) -> str:
    """All licence signals a distribution declares, lowercased."""
    m = metadata(dist)
    parts = [
        m.get("License-Expression") or "",  # PEP 639, preferred
        m.get("License") or "",
    ]
    parts.extend(c for c in (m.get_all("Classifier") or []) if c.startswith("License"))
    return " | ".join(p for p in parts if p).lower()


@pytest.mark.parametrize("dist,allowed", sorted(EXPECTED.items()))
def test_licence_matches_what_the_docs_claim(dist: str, allowed: tuple[str, ...]) -> None:
    declared = declared_licence(dist)
    assert declared, f"{dist} declares no licence metadata at all — update LICENSE"
    assert any(a in declared for a in allowed), (
        f"{dist}@{version(dist)} licence is {declared!r}, expected one of {allowed}. "
        "If the licence really changed, update LICENSE and docs/SECURITY.md — "
        "do not just relax this assertion."
    )


@pytest.mark.parametrize("dist", sorted(NO_METADATA))
def test_no_metadata_dependencies_are_documented_as_such(dist: str) -> None:
    """A dependency with no licence metadata must be called out in LICENSE."""
    from pathlib import Path

    licence = (Path(__file__).resolve().parent.parent.parent / "LICENSE").read_text()
    assert dist in licence
    assert "no licence metadata" in licence.lower(), (
        f"{dist} ships no licence metadata; LICENSE must say so explicitly rather "
        "than asserting a licence it does not declare"
    )


def test_no_dependency_is_agpl() -> None:
    """The central licensing claim, asserted rather than promised.

    An AGPL dependency in a publicly hosted service is a real obligation, not a
    formality. This is the check that makes the README's "no AGPL" true.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent
    text = (root / "backend" / "requirements.txt").read_text().lower()

    # Parse real requirement lines only. Matching the raw file text means the
    # comment explaining that PyMuPDF is deliberately avoided trips the check
    # -- which it did, on the first run of this very test.
    requirements = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name = re.split(r"[=<>!~\[]", line, maxsplit=1)[0].strip()
        if name:
            requirements[name] = line

    agpl_markers = ("agpl", "gnu affero", "affero general public")
    offenders = [r for r in requirements.values() if any(m in r for m in agpl_markers)]
    assert not offenders, f"AGPL dependency in requirements.txt: {offenders}"

    # PyMuPDF is the trap: the most common PDF renderer in Python, and AGPL.
    # Its PyPI name is pymupdf and it also imports as `fitz`.
    assert "pymupdf" not in requirements, "PyMuPDF is AGPL — use pypdfium2"
    assert "fitz" not in requirements
    assert "pypdfium2" in requirements, "the permissive-licence PDF engine should be present"


def test_source_does_not_import_pymupdf() -> None:
    """A transitive dependency could reintroduce it; check the source too."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent / "backend"
    offenders = []
    for py in root.rglob("*.py"):
        text = py.read_text()
        if re.search(r"^\s*(import|from)\s+(fitz|pymupdf)\b", text, re.M):
            offenders.append(str(py.relative_to(root)))
    assert not offenders, f"PyMuPDF (AGPL) imported in: {offenders}"


@pytest.mark.skipif(
    not __import__("os").environ.get("SC_LIVE_LICENCE_CHECK"),
    reason="set SC_LIVE_LICENCE_CHECK=1 to re-check against PyPI",
)
def test_licences_still_match_pypi() -> None:
    """Belt and braces: upstream cannot change without the docs noticing."""
    for dist in list(EXPECTED) + list(NO_METADATA):
        with urllib.request.urlopen(f"https://pypi.org/pypi/{dist}/json", timeout=20) as r:
            info = json.load(r)["info"]
        declared = " | ".join(
            filter(
                None,
                [
                    info.get("license_expression") or "",
                    info.get("license") or "",
                    *[c for c in info.get("classifiers", []) if c.startswith("License")],
                ],
            )
        ).lower()
        expected = EXPECTED.get(dist) or (NO_METADATA.get(dist) or ("", ""))[0:1]
        assert any(e and e in declared for e in expected), (
            f"{dist} on PyPI now declares {declared!r}; expected {expected}. "
            "Update LICENSE and docs/SECURITY.md."
        )
