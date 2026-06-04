"""Create a scope manifest that separates T42 edits from preexisting dirt."""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _normalize_path(path: str) -> str:
    value = str(path or "").strip()
    if " -> " in value:
        value = value.split(" -> ", 1)[1].strip()
    while value.startswith("./"):
        value = value[2:]
    return value


def parse_porcelain(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in lines:
        line = str(raw).rstrip("\n")
        if not line.strip() or len(line) < 4:
            continue
        status = line[:2]
        path = _normalize_path(line[3:])
        if path:
            out[path] = status
    return out


def _sorted(values: set[str]) -> list[str]:
    return sorted(values, key=str)


def build_scope_manifest(
    baseline_lines: list[str],
    current_lines: list[str],
    *,
    t42_paths: list[str],
    scope_label: str = "t42",
) -> dict[str, Any]:
    baseline = parse_porcelain(baseline_lines)
    current = parse_porcelain(current_lines)
    touched = {_normalize_path(path) for path in t42_paths if _normalize_path(path)}
    baseline_paths = set(baseline)
    current_paths = set(current)
    new_dirty_not_declared = current_paths - baseline_paths - touched
    label = str(scope_label or "t42").strip().lower()
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": f"{label}_dirty_scope_manifest_v1",
        "scope_status": "ok" if not new_dirty_not_declared else "needs_review",
        "baseline_dirty_count": len(baseline_paths),
        "current_dirty_count": len(current_paths),
        "t42_touched_count": len(touched),
        "task_touched_count": len(touched),
        "baseline_dirty_paths": dict(sorted(baseline.items())),
        "current_dirty_paths": dict(sorted(current.items())),
        "t42_touched_paths": _sorted(touched),
        "task_touched_paths": _sorted(touched),
        "t42_only_paths": _sorted((current_paths - baseline_paths) & touched),
        "task_only_paths": _sorted((current_paths - baseline_paths) & touched),
        "t42_touched_preexisting_dirty_paths": _sorted(baseline_paths & current_paths & touched),
        "task_touched_preexisting_dirty_paths": _sorted(baseline_paths & current_paths & touched),
        "preexisting_dirty_untouched_paths": _sorted(baseline_paths - touched),
        "new_dirty_not_declared_paths": _sorted(new_dirty_not_declared),
        "notes": [
            "This manifest is only a scope boundary; it does not revert or clean preexisting changes.",
            f"Paths listed in task_touched_preexisting_dirty_paths were dirty before {label.upper()} and also touched in this task.",
        ],
    }
    return payload


def git_status_lines(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.splitlines()


def _load_lines(path: Path | None) -> list[str]:
    if not path:
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("raw_git_status_porcelain") if isinstance(data, dict) else None
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if isinstance(data, list):
        return [str(item) for item in data]
    return []


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Write a dirty scope manifest for the current repo.")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--capture-baseline", action="store_true")
    parser.add_argument("--t42-path", action="append", default=[])
    parser.add_argument("--task-path", action="append", default=[])
    parser.add_argument("--scope-label", default="t42")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    current_lines = git_status_lines(args.repo)
    if args.capture_baseline:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "scope": f"{str(args.scope_label or 't42').strip().lower()}_dirty_scope_baseline_v1",
            "raw_git_status_porcelain": current_lines,
        }
    else:
        baseline_lines = _load_lines(args.baseline) if args.baseline else current_lines
        task_paths = [str(path) for path in [*args.t42_path, *args.task_path]]
        payload = build_scope_manifest(
            baseline_lines,
            current_lines,
            t42_paths=task_paths,
            scope_label=str(args.scope_label),
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
