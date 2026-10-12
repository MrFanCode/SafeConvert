"""Error types and the exception handler that turns them into JSON.

A converter must never leak a traceback or a library message to the client:
internal paths and parser internals are reconnaissance. Every failure the
client can trigger maps to a stable, deliberately vague message; the detail
goes to the log only.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("safeconvert")


class ConversionError(Exception):
    """A user-correctable problem: bad file, wrong type, too many pages.

    ``code`` is a stable machine-readable token the frontend can branch on.
    ``message`` is written for a human and never includes internal detail.
    """

    status_code = 422

    def __init__(self, message: str, code: str = "conversion_failed") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class FileTooLargeError(ConversionError):
    status_code = 413

    def __init__(self, message: str = "That file is larger than the upload limit.") -> None:
        super().__init__(message, code="file_too_large")


class UnsupportedTypeError(ConversionError):
    status_code = 415

    def __init__(self, message: str = "That file type is not supported for this conversion.") -> None:
        super().__init__(message, code="unsupported_type")


class CorruptFileError(ConversionError):
    status_code = 422

    def __init__(self, message: str = "That file could not be read. It may be corrupt or truncated.") -> None:
        super().__init__(message, code="corrupt_file")


class MaliciousFileError(ConversionError):
    """Rejected by a security check: zip bomb, XXE, oversized canvas, etc.

    Deliberately does not say *which* check fired on the public path — the
    detail is useful to an attacker probing for thresholds. It is logged.
    """

    status_code = 422

    def __init__(self, message: str = "That file was rejected by a safety check.") -> None:
        super().__init__(message, code="unsafe_file")


class RateLimitError(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


def _error_body(code: str, message: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if extra:
        body["error"].update(extra)
    return body


async def conversion_error_handler(_: Request, exc: ConversionError) -> JSONResponse:
    logger.info("conversion rejected: code=%s message=%s", exc.code, exc.message)
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(exc.code, exc.message),
    )


async def http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = "not_found" if exc.status_code == 404 else "http_error"
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(code, detail),
        headers=getattr(exc, "headers", None),
    )


async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    # Echoing pydantic's per-field errors is safe and genuinely helpful
    # (e.g. "dpi must be between 36 and 400"), but strip anything that could
    # carry a server path.
    fields = [
        {
            "field": ".".join(str(p) for p in err.get("loc", ())[1:]) or "request",
            "problem": str(err.get("msg", "invalid value")),
        }
        for err in exc.errors()[:5]
    ]
    return JSONResponse(
        status_code=422,
        content=_error_body("invalid_request", "The request was not valid.", fields=fields),
    )


async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    # Never leak the exception text to the client.
    logger.exception("unhandled error: %s", type(exc).__name__, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content=_error_body("internal_error", "Something went wrong while processing that file."),
    )
