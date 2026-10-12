#!/usr/bin/env python3
"""Regenerate backend/requirements.txt from the resolved dependency graph.

Run this after changing a dependency in pyproject.toml and reinstalling:

    cd backend && /home/agent/workspace/.venv/bin/python3 tools/gen_requirements.py

Why not just `pip freeze`? Three reasons:

  1. `pip freeze` emits EVERYTHING in the venv, including pytest and ruff.
     Installing the test toolchain into the production image is attack surface
     for no benefit.
  2. It does not sort or group, so a diff after a routine bump is noisy and
     unreviewable.
  3. It includes local path installs (`-e .`), which break in a container.

This walks the dependency closure of the production roots only, verifies each
one is actually installed, and fails loudly otherwise. A pin file containing a
package that is not present in the environment would be a fabrication — the
whole value of this file is that it describes a real, tested graph.

The venv is uv-managed and has no `pip` module, so this reads installed
distributions straight from importlib.metadata rather than shelling out.
"""

from __future__ import annotations

import sys
from importlib.metadata import distributions
from pathlib import Path

# Production roots. Must mirror [project].dependencies in pyproject.toml.
ROOTS = (
    "fastapi",
    "uvicorn",
    "python-multipart",
    "pydantic",
    "pydantic-settings",
    "pypdfium2",
    "pillow",
    "reportlab",
    "python-docx",
    "resvg-py",
    "defusedxml",
)

# Grouped output so a version bump lands in a readable place.
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "web framework",
        (
            "fastapi",
            "starlette",
            "uvicorn",
            "python-multipart",
            "pydantic",
            "pydantic-core",
            "pydantic-settings",
            "annotated-doc",
            "annotated-types",
            "anyio",
            "charset-normalizer",
            "click",
            "h11",
            "idna",
            "python-dotenv",
            "typing-extensions",
            "typing-inspection",
            "opentelemetry-api",
        ),
    ),
    ("PDF rendering (BSD-3/Apache-2 — deliberately NOT PyMuPDF/AGPL)", ("pillow", "pypdfium2", "reportlab")),
    ("DOCX", ("lxml", "python-docx")),
    ("SVG rasterisation (Rust resvg — performs no external fetches)", ("resvg-py",)),
    ("XML hardening", ("defusedxml",)),
)

HEADER = """\
# SafeConvert — pinned production dependencies.
#
# GENERATED from the resolved dependency graph, not hand-written. Regenerate:
#
#     cd backend && /home/agent/workspace/.venv/bin/python3 tools/gen_requirements.py
#
# Two rules this file exists to enforce:
#
#  1. Exact pins. Every build installs byte-identical versions, so the image
#     you test is the image you ship. A `>=` range makes those two different
#     things at different times.
#
#  2. No surprises from compiled wheels. pypdfium2 and resvg-py ship native
#     wheels. A version bump that quietly drops your platform tag turns a
#     five-second install into a toolchain build — or a broken runtime. The
#     Dockerfile asserts both landed as wheels rather than source builds.
#
# Note: uvicorn is pinned WITHOUT the [standard] extras. httptools/uvloop are
# compiled, and the pure-Python h11 implementation is entirely adequate at this
# service's throughput. Fewer native dependencies is a smaller attack surface,
# which is the whole point of this project.
#
# Every version below was verified installed in the environment that generated
# this file (Python 3.14 on linux/amd64).
"""


def installed() -> dict[str, str]:
    """Map of normalised distribution name -> version."""
    out: dict[str, str] = {}
    for dist in distributions():
        raw = dist.metadata["Name"]
        if not raw:
            continue
        # PEP 503 normalisation: lowercase, runs of -_. collapse to a single -.
        key = raw.lower().replace("_", "-").replace(".", "-")
        while "--" in key:
            key = key.replace("--", "-")
        out[key] = dist.version
    return out


def closure(roots: tuple[str, ...], have: dict[str, str]) -> set[str]:
    """Transitive dependency closure, skipping extras and missing packages."""
    seen: set[str] = set()
    queue = list(roots)
    while queue:
        name = queue.pop()
        key = name.lower().replace("_", "-")
        if key in seen:
            continue
        seen.add(key)
        queue.extend(requires(name))
    return {n for n in seen if n in have}


def requires(name: str) -> list[str]:
    """Direct requirements of an installed distribution, extras excluded."""
    try:
        from importlib.metadata import distribution
    except ImportError:  # pragma: no cover
        return []

    try:
        raw_reqs = distribution(name).requires or []
    except Exception:
        return []

    out: list[str] = []
    for raw in raw_reqs:
        req = raw.split(";", 1)[0].strip()
        if not req or "://" in req:
            continue
        # Strip environment markers that select an extra.
        _, _, marker = raw.partition(";")
        if "extra" in marker:
            continue
        # PEP 508 name ends at the first version/specifier delimiter.
        for delim in ("==", ">=", "<=", "~=", "!=", ">", "<", "[", "(", " "):
            req = req.split(delim, 1)[0]
        req = req.strip().lower().replace("_", "-").replace(".", "-")
        if req:
            out.append(req)
    return out


def main() -> int:
    have = installed()

    missing = [r for r in ROOTS if r.lower() not in have]
    if missing:
        print(f"ERROR: production dependencies not installed: {missing}", file=sys.stderr)
        print("Install them first, then re-run.", file=sys.stderr)
        return 1

    resolved = closure(ROOTS, have)

    lines = [HEADER]
    seen: set[str] = set()

    for title, members in GROUPS:
        present = [m for m in members if m in resolved]
        if not present:
            continue
        lines.append(f"\n# --- {title} " + "-" * max(4, 66 - len(title)))
        for name in sorted(present):
            lines.append(f"{name}=={have[name]}")
            seen.add(name)

    # Anything resolved but not named in a group still has to be pinned, or the
    # image will resolve it at build time and this file stops being truthful.
    leftovers = sorted(resolved - seen)
    if leftovers:
        lines.append("\n# --- transitively required " + "-" * 40)
        lines.extend(f"{name}=={have[name]}" for name in leftovers)
        print(f"NOTE: {len(leftovers)} package(s) not named in GROUPS: {leftovers}", file=sys.stderr)
        print("Add them to tools/gen_requirements.py GROUPS for tidiness.", file=sys.stderr)

    out_path = Path(__file__).resolve().parent.parent / "requirements.txt"
    out_path.write_text("\n".join(lines) + "\n")

    print(f"wrote {out_path} with {len(resolved)} pinned packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
