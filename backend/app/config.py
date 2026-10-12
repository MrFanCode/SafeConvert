"""Application settings.

Every limit that protects the process lives here, in one place, so an
auditor can read the whole security posture from a single file instead of
hunting through the converters.

All values are overridable by environment variable (prefix ``SC_``) so the
same image can run with tighter limits in CI or on Vercel.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SC_",
        env_file=".env",
        extra="ignore",
    )

    # ---------------------------------------------------------------- app
    app_name: str = "SafeConvert"
    version: str = "1.0.0"
    environment: str = "production"
    # When false, /docs and /redoc are not registered. Off by default in
    # production: the OpenAPI UI is an unnecessary attack surface and it
    # documents every endpoint for a scanner.
    enable_docs: bool = False

    # ------------------------------------------------------------ uploads
    # Hard ceiling on a single upload. Checked while streaming, so an
    # oversized body is rejected before it is fully buffered.
    max_upload_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)
    # Ceiling on the decompressed size of any container format (DOCX is a
    # ZIP). A 40 KB .docx that expands to 4 GB is a zip bomb.
    max_uncompressed_bytes: int = Field(default=200 * 1024 * 1024, ge=1024)
    # Largest ratio of compressed:uncompressed size tolerated in a ZIP entry.
    max_compression_ratio: int = Field(default=120, ge=1)
    # Max entries in a ZIP/DOCX container.
    max_zip_entries: int = Field(default=3000, ge=1)

    # --------------------------------------------------------------- pdf
    max_pdf_pages: int = Field(default=200, ge=1)
    # Rendering a page at 600 DPI on A4 is ~35M pixels. This caps the
    # pixel count regardless of the DPI/area combination requested.
    max_render_pixels: int = Field(default=40_000_000, ge=1_000_000)
    min_render_dpi: int = Field(default=36, ge=1)
    max_render_dpi: int = Field(default=400, ge=1)

    # ------------------------------------------------------------- images
    # Pillow's own default is ~178M pixels; ours is deliberately lower
    # because we also decode and re-encode in the same process.
    max_image_pixels: int = Field(default=50_000_000, ge=1000)
    max_image_dimension: int = Field(default=20_000, ge=16)
    jpeg_quality: int = Field(default=92, ge=30, le=100)

    # --------------------------------------------------------------- svg
    max_svg_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)
    # Pixel ceiling for SVG rasterisation, same reasoning as PDF pages.
    max_svg_pixels: int = Field(default=40_000_000, ge=1000_000)

    # --------------------------------------------------------------- docx
    max_docx_elements: int = Field(default=150_000, ge=1)

    # ------------------------------------------------------------- limits
    # Per-IP token bucket. 20 conversions per minute is generous for a human
    # and cheap to serve.
    rate_limit_requests: int = Field(default=20, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=1)
    # Wall-clock ceiling for a single conversion, enforced in a worker thread
    # so a pathological file cannot pin a request forever.
    conversion_timeout_seconds: int = Field(default=30, ge=1)

    # ------------------------------------------------------------ origins
    # Comma-separated list of allowed CORS origins. Empty = same-origin only,
    # which is the correct default when nginx proxies /api on the same host.
    cors_origins: str = ""

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
