"""Shared decisions for the platform starter scripts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from . import config

NO_NPM_WARNING = "npm not found; using the existing build in frontend/dist."
NO_BUILD_ERROR = "npm not found and no build in frontend/dist. Install Node.js 20 or newer."


@dataclass(frozen=True)
class FrontendPlan:
    install_dependencies: bool = False
    build: bool = False
    warning: str | None = None
    error: str | None = None


def python_is_supported(version_info: Sequence[int]) -> bool:
    """Whether an interpreter satisfies the application's declared floor."""
    return tuple(version_info[:2]) >= config.PYTHON_VERSION_FLOOR


def python_floor_label() -> str:
    return ".".join(str(part) for part in config.PYTHON_VERSION_FLOOR)


def frontend_is_stale(root: Path) -> bool:
    """Whether a developer checkout's frontend build is behind its sources."""
    frontend = root / "frontend"
    dist = frontend / "dist" / "index.html"
    if not dist.is_file():
        return True
    if not (frontend / "node_modules").is_dir():
        return False

    sources = [frontend / "index.html", frontend / "vite.config.ts"]
    source_root = frontend / "src"
    if source_root.is_dir():
        sources.append(source_root)
        sources.extend(source_root.rglob("*"))
    return any(path.exists() and path.stat().st_mtime > dist.stat().st_mtime for path in sources)


def frontend_plan(
    root: Path,
    *,
    npm_available: bool,
    dev: bool = False,
    update: bool = False,
    skip_build: bool = False,
) -> FrontendPlan:
    """Choose the setup work a starter needs before it launches the server."""
    if skip_build:
        return FrontendPlan()

    dist_exists = (root / "frontend" / "dist" / "index.html").is_file()
    stale = frontend_is_stale(root)
    if not npm_available:
        if dist_exists:
            return FrontendPlan(warning=NO_NPM_WARNING)
        return FrontendPlan(error=NO_BUILD_ERROR)

    install_dependencies = dev or update or not dist_exists or stale
    return FrontendPlan(
        install_dependencies=install_dependencies,
        build=not dev and stale,
    )


def dependency_warning(root: Path, launcher: str) -> str | None:
    """Warn when an existing developer installation predates the lock file."""
    frontend = root / "frontend"
    node_modules = frontend / "node_modules"
    stamp = node_modules / ".install-stamp"
    lock = frontend / "package-lock.json"
    if not node_modules.is_dir() or not lock.is_file():
        return None
    if not stamp.is_file() or lock.stat().st_mtime > stamp.stat().st_mtime:
        return (
            "Frontend dependencies are behind package-lock.json. "
            f"Run {launcher} --update or npm --prefix frontend install."
        )
    return None
