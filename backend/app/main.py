"""FastAPI application factory.

Middleware order matters here and is deliberate:

1. **Security headers** — outermost, so they are present on every response
   including 429s and 500s produced further in.
2. **Rate limit** — before body parsing, so a flood is rejected without
   buffering the bytes.
3. **Body size** — a final backstop. The per-endpoint reader already caps
   uploads; this catches anything that reaches a route without going
   through it.

Nothing parses a request body before the size limit is known.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartParser
from starlette.responses import JSONResponse, Response

from .config import Settings, get_settings
from .errors import (
    ConversionError,
    conversion_error_handler,
    http_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from .fonts import register_fonts
from .rate_limit import client_key, get_limiter
from .routers import convert

logger = logging.getLogger("safeconvert")

DESCRIPTION = """
A privacy-first file conversion service.

**Files are processed in memory and never written to disk, never stored, and
never shared.** Nothing on this service requires an account.

### Security properties

* Uploaded files are validated by content, not by filename or declared
  extension.
* Zip bombs, oversized canvases, page-count and pixel-count ceilings are
  enforced **before** any expensive work begins.
* Output is always PNG, JPEG or PDF. **SVG is never returned**, which removes
  the converter-XSS class of attack entirely.
* The SVG renderer has no network access, so a document cannot make this
  server issue requests on its behalf.
* No LibreOffice, no office automation, no native document-parsing C code.
"""


def _configure_logging(settings: Settings) -> None:
    level = logging.DEBUG if settings.environment == "development" else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    # Uvicorn's own access log is noisy and logs client IPs; keep it at WARNING.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()
    _configure_logging(s)

    # Register fonts before the first request so the first conversion is not
    # the one that pays for a font scan.
    register_fonts()

    # Raise Starlette's multipart part cap to match our own limit. This is the
    # class-level default, applied before any request creates a parser — the
    # per-request `max_part_size` kwarg would otherwise default to 1 MiB and
    # turn a large-but-legal upload into a 422. The `guard` middleware below
    # still rejects the genuinely oversized request with a correct 413 before
    # this cap is reached.
    MultiPartParser.max_part_size = s.max_upload_bytes + 1024 * 1024

    app = FastAPI(
        title=s.app_name,
        version=s.version,
        description=DESCRIPTION,
        docs_url="/docs" if s.enable_docs else None,
        redoc_url="/redoc" if s.enable_docs else None,
        openapi_url="/openapi.json" if s.enable_docs else None,
    )

    # ------------------------------------------------------------ errors
    app.add_exception_handler(ConversionError, conversion_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    # -------------------------------------------------------------- cors
    # Same-origin by default (nginx proxies /api), so no CORS headers are sent
    # unless origins are explicitly configured.
    origins = s.cors_origin_list
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
            max_age=600,
        )
    else:
        logger.info("CORS: no origins configured (same-origin only)")

    # ---------------------------------------------------- security headers
    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        # nosniff stops a browser from overriding a declared content type.
        response.headers["X-Content-Type-Options"] = "nosniff"
        # The API only ever returns binary downloads and JSON; it never needs
        # to frame, embed, or make subrequests.
        response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        # Do not advertise the server stack.
        if "server" in response.headers:
            del response.headers["server"]
        if "x-powered-by" in response.headers:
            del response.headers["x-powered-by"]
        return response

    # ------------------------------------------------------------- rate limit
    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Reject an over-large body before anything parses it.

        Two limits have to be enforced at different layers, because Starlette's
        multipart parser enforces its own:

        * ``MultiPartParser.max_part_size`` is a *constructor argument*
          defaulting to 1 MiB. Setting the class attribute after the app is
          built does not change parsers that were already constructed, and
          exceeding it raises ``MultiPartException``, which FastAPI surfaces
          as 422 — the wrong status for "too large". So the class default is
          raised at import time (below) *and* Content-Length is checked here.

        * ``_read_upload`` remains the authoritative per-file check, and fires
          when Content-Length is absent or lying (chunked encoding).
        """
        content_length = request.headers.get("content-length")
        content_length = request.headers.get("content-length")
        # Allow a generous margin for multipart boundaries and headers, so a
        # legal upload that sits just under the limit is not rejected on
        # framing overhead alone.
        if (
            content_length
            and content_length.isdigit()
            and int(content_length) > s.max_upload_bytes + 2 * 1024 * 1024
        ):
            return JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": "file_too_large",
                        "message": "That file is larger than the upload limit.",
                        "limit_mb": s.max_upload_bytes // (1024 * 1024),
                    }
                },
                headers={"Retry-After": "0"},
            )
        return await call_next(request)

    @app.middleware("http")
    async def rate_limit(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # The probe must never be throttled, or a load balancer restart would
        # mark a healthy instance unhealthy.
        if request.url.path in ("/api/health", "/health"):
            return await call_next(request)

        limiter = get_limiter(s)
        key = client_key(request)
        allowed, remaining, retry_after = limiter.check(key)

        if not allowed:
            logger.warning("rate limit hit: path=%s", request.url.path)
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "rate_limited",
                        "message": "Too many requests. Please wait a moment and try again.",
                        "retry_after_seconds": retry_after,
                    }
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limiter.limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-RateLimit-Limit"] = str(limiter.limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.1f}"
        return response

    # ------------------------------------------------------------- routes
    # Only mounted under /api. nginx proxies /api/* to this service and serves
    # the SPA everywhere else, so there is no reason to expose a second copy
    # of every route at the root — duplicated paths also mean two entries per
    # endpoint in the OpenAPI schema.
    app.include_router(convert.router, prefix="/api", tags=["conversion"])

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": s.app_name,
            "version": s.version,
            "docs": "/docs" if s.enable_docs else "disabled",
            "health": "/api/health",
        }

    logger.info(
        "%s %s ready (env=%s, docs=%s, upload_limit=%dMB, pdf_pages=%d)",
        s.app_name,
        s.version,
        s.environment,
        s.enable_docs,
        s.max_upload_bytes // (1024 * 1024),
        s.max_pdf_pages,
    )
    return app


app = create_app()
