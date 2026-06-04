"""Shared case source-file safety helpers."""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from .. import scanner

TRASH_DIR_NAME = ".case-workbench-trash"


def resolve_existing_source(case_dir: Path | str, filename: str | None) -> Path:
    """Resolve a relative source image inside a case directory.

    This mirrors the route-layer guard but lives in services so compute/select
    code can validate real files without importing `backend.routes.*`.
    """
    if not filename or filename in {".", ".."}:
        raise HTTPException(400, "existing image filename is required")
    rel = Path(str(filename))
    if rel.is_absolute() or ".." in rel.parts:
        raise HTTPException(400, "invalid image path")
    if TRASH_DIR_NAME in rel.parts:
        raise HTTPException(400, "trashed images cannot be used as active source images")
    base = Path(case_dir).resolve()
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(400, "invalid image path")
    if target.suffix.lower() not in scanner.IMAGE_EXTS:
        raise HTTPException(400, f"unsupported image extension: {target.suffix}")
    if not target.is_file():
        raise HTTPException(404, f"image not found: {filename}")
    return target
