"""Stress-test mode helpers.

The normal workbench operates directly on the production-ish local SQLite DB
and writes render artifacts into real case folders.  Stress mode deliberately
keeps those writes in an isolated DB/output root while still using real case
metadata and real source images.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from .config import REPO_ROOT, get_settings

DEFAULT_DB_PATH = REPO_ROOT / "case-workbench.db"


def configured_db_path() -> Path:
    return get_settings().db_path()


def output_root() -> Path | None:
    return get_settings().output_root()


def is_stress_mode() -> bool:
    return get_settings().case_workbench_stress_mode


def stress_run_id() -> str | None:
    return get_settings().stress_run_id()


def allow_destructive_actions() -> bool:
    return get_settings().case_workbench_stress_allow_destructive


def allow_external_ai() -> bool:
    return get_settings().case_workbench_ai_allow_external


def assert_destructive_allowed(action: str) -> None:
    if is_stress_mode() and not allow_destructive_actions():
        raise HTTPException(
            403,
            f"{action} is disabled in CASE_WORKBENCH_STRESS_MODE; "
            "set CASE_WORKBENCH_STRESS_ALLOW_DESTRUCTIVE=1 only for an explicit destructive drill",
        )


def _case_output_key(case_dir: Path) -> str:
    digest = hashlib.sha1(str(case_dir.resolve()).encode("utf-8")).hexdigest()[:12]
    name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in case_dir.name)
    return f"{name or 'case'}-{digest}"


def render_output_root(case_dir: Path, brand: str, template: str) -> Path:
    root = output_root()
    if root is None:
        return case_dir / ".case-layout-output" / brand / template / "render"
    return root / "render" / _case_output_key(case_dir) / brand / template / "render"


def simulation_root(default_root: Path) -> Path:
    return get_settings().simulation_root(default_root)


def tag_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(payload or {})
    if is_stress_mode():
        meta = data.get("_stress")
        if not isinstance(meta, dict):
            meta = {}
        meta.update(
            {
                "enabled": True,
                "run_id": stress_run_id(),
                "db_path": str(configured_db_path()),
                "output_root": str(output_root()) if output_root() else None,
            }
        )
        data["_stress"] = meta
    return data


def is_path_allowed_artifact(path: Path, case_dir: Path | None = None) -> bool:
    target = path.expanduser().resolve()
    roots: list[Path] = []
    root = output_root()
    if root is not None:
        roots.append(root)
    if case_dir is not None:
        roots.append(case_dir.expanduser().resolve())
    for allowed in roots:
        try:
            target.relative_to(allowed)
            return True
        except ValueError:
            continue
    return False


def status_payload(*, db_path: Path, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    root = output_root()
    payload: dict[str, Any] = {
        "stress_mode": is_stress_mode(),
        "stress_run_id": stress_run_id(),
        "repo_root": str(REPO_ROOT),
        "expected_repo_root": str(REPO_ROOT),
        "db_path": str(db_path),
        "default_db_path": str(DEFAULT_DB_PATH),
        "db_is_default": db_path.resolve() == DEFAULT_DB_PATH.resolve(),
        "output_root": str(root) if root else None,
        "destructive_allowed": allow_destructive_actions(),
        "external_ai_allowed": allow_external_ai(),
    }
    if extra:
        payload.update(extra)
    return payload
