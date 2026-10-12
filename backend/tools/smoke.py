#!/usr/bin/env python3
"""End-to-end smoke test against a running SafeConvert stack.

Run it the way a user would — over HTTP, through nginx — rather than in-process.
That is the whole point: a converter can be perfect and the deployment still be
broken. This is the check that catches a bad proxy_pass, a missing CSP, an
upstream on the wrong port, or a container that starts before its backend.

    # against a local stack
    docker compose --profile test run --rm smoke

    # against anything reachable
    SC_TARGET=https://your-deployment.vercel.app \
      /home/agent/workspace/.venv/bin/python3 tools/smoke.py

Exit code is 0 only if every check passes. Prints one line per check so a
failure names itself.

Deliberately uses only the standard library. A test harness that needs
`requests` or `httpx` is one more thing to keep patched.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from dataclasses import field as dataclass_field

_TARGET_RAW = os.environ.get("SC_TARGET", "http://127.0.0.1:8080").rstrip("/")
if not _TARGET_RAW.startswith(("http://", "https://")):
    raise SystemExit(f"SC_TARGET must be an http(s) URL, got {_TARGET_RAW!r}")
TARGET = _TARGET_RAW
TIMEOUT = float(os.environ.get("SC_SMOKE_TIMEOUT", "90"))

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


@dataclass
class Result:
    passed: int = 0
    failed: int = 0
    failures: list[str] = dataclass_field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  {GREEN}PASS{RESET} {name}" + (f" {DIM}({detail}){RESET}" if detail else ""))
        else:
            self.failed += 1
            self.failures.append(f"{name}: {detail}")
            print(f"  {RED}FAIL{RESET} {name} {RED}{detail}{RESET}")
        return ok


R = Result()


class CaseInsensitiveHeaders(dict):
    """Header lookup that ignores case, as HTTP requires.

    Not cosmetic. A plain ``dict(resp.headers)`` keeps whatever case the
    server happened to use, and the two hops in this stack disagree: nginx
    emits lowercase field names while uvicorn emits canonical capitalisation.
    A test asserting ``headers["Content-Disposition"]`` therefore passed
    against the backend and failed against the full stack -- or worse, silently
    read ``None`` and "verified" a missing header was fine.
    """

    def __init__(self, message) -> None:
        super().__init__({k.lower(): v for k, v in message.items()})

    def get(self, key: str, default: str | None = None) -> str | None:  # type: ignore[override]
        return super().get(key.lower(), default)

    def __getitem__(self, key: str):
        return super().__getitem__(key.lower())

    def __contains__(self, key: object) -> bool:
        return super().__contains__(str(key).lower())


def post(path: str, files: dict[str, tuple[str, bytes, str]] | list[tuple[str, tuple[str, bytes, str]]], expect: int = 200):
    """Multipart POST. Returns (status, headers, body).

    `files` is a field name -> (filename, bytes, content-type) mapping.

    Pass a LIST OF TUPLES, not a dict, when the same field appears more than
    once. A dict literal with a repeated key keeps only the last value and
    raises no error, so ``{"files": a, "files": b}`` silently sends one file
    and looks completely correct at a glance -- which is how the two-image
    test ended up proving nothing at all. Ruff's F601 catches it; the type
    signature now requires the honest form.
    """
    boundary = f"----sc{uuid.uuid4().hex}"
    buf = io.BytesIO()
    items = files.items() if isinstance(files, dict) else files
    for field, (name, data, ctype) in items:
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{field}"; filename="{name}"\r\n'.encode())
        buf.write(f"Content-Type: {ctype}\r\n\r\n".encode())
        buf.write(data)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())

    # S310 fires on Request() as well as urlopen(); the scheme allowlist at
    # import time (see TARGET) is the control that matters.
    req = urllib.request.Request(  # noqa: S310
        f"{TARGET}{path}",
        data=buf.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        # S310: scheme is allowlisted at import time (see TARGET).
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310
            return resp.status, CaseInsensitiveHeaders(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, CaseInsensitiveHeaders(exc.headers), exc.read()


def get(path: str):
    try:
        # S310: scheme is allowlisted at import time (see TARGET).
        with urllib.request.urlopen(f"{TARGET}{path}", timeout=TIMEOUT) as resp:  # noqa: S310
            return resp.status, CaseInsensitiveHeaders(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, CaseInsensitiveHeaders(exc.headers), exc.read()


def build_pdf(pages: int = 2) -> bytes:
    """A real multi-page PDF, built without external libraries.

    Hand-rolled because the smoke test must not depend on the converters it is
    testing. Each page gets its OWN content stream, which keeps the object
    graph trivially checkable:

        1 catalog   2 page-tree   3 font   4,6,8... page objects
                                       5,7,9... that page's content stream

    An earlier version shared one content stream across all pages and pdfium
    could count the pages but not load them -- which the smoke test correctly
    reported as a failure. That turned out to be a genuine bug in the app (see
    the page-load handler in app/converters/pdf.py), so this fixture is now
    checked against pdfium by tests/test_smoke_fixtures.py before anyone relies
    on it.
    """
    if pages < 1:
        raise ValueError("pages must be >= 1")

    # obj 1 = catalog, 2 = page tree, 3 = font, then alternating
    # page object / its content stream.
    first_page_obj = 4
    kids = " ".join(f"{first_page_obj + i * 2} 0 R" for i in range(pages))

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}

    def emit(num: int, body: bytes) -> None:
        offsets[num] = len(out)
        out.extend(f"{num} 0 obj\n".encode() + body + b"\nendobj\n")

    emit(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    emit(2, f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    emit(3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for i in range(pages):
        page_obj = first_page_obj + i * 2
        stream_obj = page_obj + 1
        text = f"BT /F1 14 Tf 20 100 Td (smoke page {i + 1}) Tj ET".encode()
        emit(page_obj,
             f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
             f"/Resources << /Font << /F1 3 0 R >> >> /Contents {stream_obj} 0 R >>".encode())
        emit(stream_obj,
             b"<< /Length " + str(len(text)).encode() + b" >>\nstream\n" + text + b"\nendstream")

    last_obj = first_page_obj + pages * 2 - 1
    xref_at = len(out)
    out.extend(f"xref\n0 {last_obj + 1}\n".encode())
    out.extend(b"0000000000 65535 f \n")          # object 0, always free
    for num in range(1, last_obj + 1):
        out.extend(f"{offsets[num]:010d} 00000 n \n".encode())
    out.extend(
        f"trailer\n<< /Size {last_obj + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    )
    return bytes(out)


def build_docx() -> bytes:
    """A minimal but structurally valid .docx, built by hand with zipfile."""
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Smoke Heading</w:t></w:r></w:p>
<w:p><w:r><w:t>Smoke body paragraph.</w:t></w:r></w:p>
<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>
</w:body>
</w:document>"""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


# PNG with correct CRCs, written by hand so this file needs no image library.
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080200000090"
    "7753de0000000c4944415408d763f8cfc0000003010100189dd923000000"
    "0049454e44ae426082"
)


def _pdf_page_count(data: bytes) -> int:
    """Count pages in a PDF without a PDF library.

    Looks for /Type /Page (not /Pages) and /Count in the page tree. Good enough
    for a smoke assertion on a ReportLab-produced file; not a general parser,
    and deliberately not used as one.
    """
    pages = len(re.findall(rb"/Type\s*/Page[^s]", data))
    if pages:
        return pages
    match = re.search(rb"/Count\s+(\d+)", data)
    return int(match.group(1)) if match else 0


def main() -> int:
    print(f"\nSafeConvert smoke test → {TARGET}\n")

    # ---- health ----------------------------------------------------------
    print("health")
    status, headers, body = get("/api/health")
    R.check("GET /api/health returns 200", status == 200, f"got {status}")
    if status == 200:
        try:
            data = json.loads(body)
            R.check("health payload has status=ok", data.get("status") == "ok", str(data)[:80])
        except json.JSONDecodeError as exc:
            R.check("health payload is JSON", False, str(exc))

    # ---- security headers ------------------------------------------------
    print("\nsecurity headers on the HTML document")
    status, headers, body = get("/")
    csp = headers.get("content-security-policy") or ""
    # These are the headers that silently disappear when a location block
    # declares its own add_header. See the vault note: nginx's add_header does
    # not merge across levels, and this exact check is what caught the bug.
    for name, expect in (
        ("Content-Security-Policy", "default-src 'self'"),
        ("X-Frame-Options", "DENY"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
    ):
        got = headers.get(name) or ""
        R.check(f"{name} present", bool(got), "MISSING" if not got else got[:48])
        if expect and got:
            R.check(f"{name} contains {expect!r}", expect in got, got[:64])
    R.check("CSP forbids object-src", "object-src 'none'" in csp)
    R.check("CSP forbids framing", "frame-ancestors 'none'" in csp)

    # ---- SVG is never returned as SVG ------------------------------------
    print("\nSVG endpoint never returns image/svg+xml")
    hostile = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        b"<script>window.__xss=1</script><rect width='100' height='100' fill='red'/></svg>"
    )
    status, headers, body = post(
        "/api/svg-to-image?format=png", {"file": ("x.svg", hostile, "image/svg+xml")}
    )
    R.check("SVG converts successfully", status == 200, f"got {status}")
    ct = headers.get("content-type") or ""
    R.check("content-type is NOT svg", "svg" not in ct.lower(), ct)
    R.check("content-type is png", "png" in ct.lower(), ct)
    R.check("body starts with PNG magic", body[:8] == PNG_1PX[:8], repr(body[:8]))
    R.check("no script survived", b"script" not in body.lower())

    # ---- hostile SVG is refused ------------------------------------------
    print("\nhostile SVG with an external entity is refused")
    xxe = (
        b'<?xml version="1.0"?>\n'
        b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        b'<text x="0" y="20">&xxe;</text></svg>'
    )
    status, headers, body = post(
        "/api/svg-to-image?format=png", {"file": ("x.svg", xxe, "image/svg+xml")}
    )
    R.check("XXE payload rejected (4xx)", 400 <= status < 500, f"got {status}")
    R.check("no /etc/passwd content leaked", b"root:" not in body)

    # ---- PDF -> PNG ------------------------------------------------------
    print("\nPDF → PNG")
    status, headers, body = post(
        "/api/pdf-to-image?format=png", {"file": ("smoke.pdf", build_pdf(2), "application/pdf")}
    )
    R.check("returns 200", status == 200, f"got {status}")
    ct = headers.get("content-type") or ""
    R.check("content-type is zip", "zip" in ct.lower(), ct)
    if status == 200:
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as zf:
                names = zf.namelist()
                R.check("zip contains 2 PNGs", len(names) == 2, str(names))
                R.check(
                    "first entry is a PNG",
                    bool(names) and zf.read(names[0])[:8] == PNG_1PX[:8],
                )
        except zipfile.BadZipFile as exc:
            R.check("body is a valid zip", False, str(exc))

    # ---- images -> PDF ---------------------------------------------------
    print("\nimages → PDF")
    status, headers, body = post(
        "/api/images-to-pdf",
        # LIST form on purpose -- see post()'s docstring. A dict here would
        # send one file, not two, and the assertion below would be a no-op.
        [("files", ("a.png", PNG_1PX, "image/png")), ("files", ("b.png", PNG_1PX, "image/png"))],
    )
    R.check("returns 200", status == 200, f"got {status}")
    R.check("content-type is pdf", "pdf" in (headers.get("content-type") or "").lower())
    R.check("body starts with %PDF-", body[:5] == b"%PDF-", repr(body[:5]))
    # Two images in means TWO pages out. Checking the magic bytes alone is not
    # enough — that was the gap that let the duplicate-dict-key bug pass.
    R.check(
        "both images became pages",
        _pdf_page_count(body) == 2,
        f"got {_pdf_page_count(body)} pages",
    )

    # ---- DOCX -> PDF -----------------------------------------------------
    print("\nDOCX → PDF")
    status, headers, body = post(
        "/api/docx-to-pdf",
        {
            "file": (
                "smoke.docx",
                build_docx(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    R.check("returns 200", status == 200, f"got {status}")
    R.check("content-type is pdf", "pdf" in (headers.get("content-type") or "").lower())
    R.check("body starts with %PDF-", body[:5] == b"%PDF-", repr(body[:5]))

    # ---- size limit returns a parseable error ----------------------------
    print("\noversized upload produces a structured error, not HTML")
    status, headers, body = post(
        "/api/images-to-pdf", {"files": ("big.png", b"\x89PNG\r\n\x1a\n" + os.urandom(9 * 1024 * 1024), "image/png")}
    )
    R.check("rejected with 413", status == 413, f"got {status}")
    ct = headers.get("content-type") or ""
    R.check("error body is JSON, not nginx HTML", "json" in ct.lower(), ct)
    if "json" in ct.lower():
        try:
            err = json.loads(body)
            R.check(
                "error uses the shared envelope",
                isinstance(err.get("error"), dict) and "code" in err["error"],
                body[:80].decode(errors="replace"),
            )
        except json.JSONDecodeError as exc:
            R.check("error body parses as JSON", False, str(exc))

    # ---- Content-Disposition is safe -------------------------------------
    print("\nresponse headers are safe")
    # A real PNG whose NAME is a traversal attempt. The earlier version of this
    # check sent PDF bytes named a.png, so validation rejected it with 422 and
    # the assertion trivially passed against a response that had no
    # Content-Disposition at all -- the test proved nothing.
    status, headers, body = post(
        "/api/images-to-pdf", {"files": ("../../etc/passwd.png", PNG_1PX, "image/png")}
    )
    R.check("traversal-named upload still converts", status == 200, f"got {status}")
    disp = headers.get("content-disposition") or ""
    R.check("Content-Disposition is present", bool(disp), disp[:64] or "header absent")
    R.check("no path traversal in filename", ".." not in disp, disp[:80])
    R.check("no raw slash in filename", "/" not in disp, disp[:80])

    # ---- summary ---------------------------------------------------------
    print(f"\n{'=' * 60}")
    total = R.passed + R.failed
    if R.failed:
        print(f"{RED}{R.failed} of {total} checks FAILED{RESET}")
        for f in R.failures:
            print(f"  {RED}·{RESET} {f}")
        return 1
    print(f"{GREEN}all {total} checks passed{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
