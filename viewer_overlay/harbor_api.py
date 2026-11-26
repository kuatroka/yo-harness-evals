"""The one place the overlay touches Harbor's internals.

Harbor exposes no stable API for embedding its viewer. Everything the overlay
imports from the installed package lives here, so an independent Harbor upgrade
fails in one module with a version and a fix, not somewhere inside a route.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from fastapi import FastAPI

SUPPORTED_MAJOR_MINOR = (0, 26)


class HarborAPIError(RuntimeError):
    """The installed Harbor is outside the range the overlay was built for."""


def installed_version() -> str:
    try:
        return version("harbor")
    except PackageNotFoundError:
        return "unknown"


def ensure_supported(found: str | None = None) -> str:
    """Return the Harbor version, or raise with the file to check next."""
    found = found or installed_version()
    parts = found.split(".")
    try:
        major_minor = (int(parts[0]), int(parts[1]))
    except (IndexError, ValueError):
        raise HarborAPIError(
            f"cannot read the harbor version {found!r}; reinstall with `uv sync`"
        ) from None
    if major_minor != SUPPORTED_MAJOR_MINOR:
        supported = ".".join(str(part) for part in SUPPORTED_MAJOR_MINOR)
        raise HarborAPIError(
            f"viewer_overlay supports harbor {supported}.x, found {found}. "
            "Check the viewer routes against the new release, then raise "
            "SUPPORTED_MAJOR_MINOR in viewer_overlay/harbor_api.py."
        )
    return found


def create_app(jobs_dir: Path, jobs_root: str, **kwargs: Any) -> FastAPI:
    ensure_supported()
    from harbor.viewer.server import create_app as harbor_create_app

    return harbor_create_app(jobs_dir, jobs_root, **kwargs)


def static_dir() -> Path:
    ensure_supported()
    from harbor.cli.view import STATIC_DIR

    return Path(STATIC_DIR)
