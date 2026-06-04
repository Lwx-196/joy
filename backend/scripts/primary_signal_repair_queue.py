"""T70 repair queue for primary-signal enrichment gaps.

The queue is evidence-only: it enumerates real missing files and real CV read
failures from the live DB. Missing source references are not deleted here.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/Users/a1234/Desktop/案例生成器/case-workbench")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import scanner, source_images  # noqa: E402
from backend.scripts import primary_signal_enrichment as enrich  # noqa: E402

DEFAULT_DB_PATH = ROOT / "case-workbench.db"
DEFAULT_JSON_OUTPUT = ROOT / "tasks" / "t70_primary_signal_repair_queue.json"
DEFAULT_MARKDOWN_OUTPUT = ROOT / "tasks" / "t70_primary_signal_repair_queue.md"
UNVERIFIED = "未验证/无法获取"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_load(raw: str | None, default: Any) -> Any:
    try:
        parsed = json.loads(raw or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed


def _case_image_files(row: sqlite3.Row) -> list[str]:
    meta = _json_load(row["meta_json"] if "meta_json" in row.keys() else None, {})
    if not isinstance(meta, dict):
        return []
    return source_images.filter_source_image_files([str(item) for item in (meta.get("image_files") or []) if str(item)])


def _replacement_candidates(case_dir: Path, filename: str) -> list[str]:
    expected = Path(filename)
    stem = expected.stem.lower()
    suffix = expected.suffix.lower()
    candidates: list[str] = []
    if not case_dir.is_dir():
        return candidates
    try:
        for child in case_dir.iterdir():
            if not child.is_file():
                continue
            if not source_images.is_source_image_file(child.name):
                continue
            child_stem = child.stem.lower()
            if child.name == expected.name or (stem and child_stem == stem) or (stem and stem in child_stem):
                candidates.append(child.name)
            elif suffix and child.suffix.lower() == suffix and stem[:6] and stem[:6] in child_stem:
                candidates.append(child.name)
    except OSError:
        return []
    return sorted(dict.fromkeys(candidates))[:8]


def identity_provider_decision() -> dict[str, Any]:
    readiness = enrich.identity_provider_readiness()
    dependencies = {
        "numpy": importlib.util.find_spec("numpy") is not None,
        "insightface": importlib.util.find_spec("insightface") is not None,
        "onnxruntime": importlib.util.find_spec("onnxruntime") is not None,
        "face_recognition": importlib.util.find_spec("face_recognition") is not None,
    }
    if readiness.get("can_generate_embeddings"):
        return {
            "status": "ready",
            "decision": "connect_existing_provider",
            "readiness": "ready",
            "fail_closed": False,
            "provider_readiness": readiness,
            "dependencies": dependencies,
            "recommended_next_step": "connect_existing_provider",
        }
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return {
        "status": "not_ready",
        "decision": "do_not_install_in_current_venv_yet",
        "readiness": UNVERIFIED,
        "fail_closed": True,
        "provider_readiness": readiness,
        "dependencies": dependencies,
        "runtime": {
            "python": python_version,
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "recommended_next_step": "keep_human_review_fallback",
        "install_candidate": {
            "provider": "InsightFace/ArcFace-compatible local embedding",
            "packages_to_prove_in_isolated_env": ["numpy", "onnxruntime", "insightface"],
            "why_not_current_venv": "heavy native dependencies are absent; install should be proven in an isolated env before touching the active production venv.",
        },
    }


def build_repair_queue(db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, Any]:
    db_path = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    missing: list[dict[str, Any]] = []
    cv_failures: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    cv_source_counts: Counter[str] = Counter()
    try:
        rows = conn.execute(
            "SELECT id, abs_path, meta_json FROM cases WHERE trashed_at IS NULL ORDER BY id"
        ).fetchall()
        for row in rows:
            case_id = int(row["id"])
            case_dir = Path(str(row["abs_path"] or ""))
            for filename in _case_image_files(row):
                path = (case_dir / filename).resolve()
                if not path.is_file():
                    status_counts["missing_file"] += 1
                    missing.append(
                        {
                            "case_id": case_id,
                            "case_abs_path": str(case_dir),
                            "filename": filename,
                            "expected_path": str(path),
                            "replacement_candidates": _replacement_candidates(case_dir, filename),
                            "auto_action": "none",
                            "recommended_action": "manual_restore_or_rescan",
                            "readiness": UNVERIFIED,
                        }
                    )
                    continue
                try:
                    signals = enrich.compute_local_cv_signals(path)
                except Exception as exc:  # noqa: BLE001
                    status_counts["cv_unavailable"] += 1
                    cv_failures.append(
                        {
                            "case_id": case_id,
                            "case_abs_path": str(case_dir),
                            "filename": filename,
                            "path": str(path),
                            "suffix": path.suffix.lower(),
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:500],
                            "auto_action": "none",
                            "recommended_action": "manual_convert_or_replace_source",
                            "readiness": UNVERIFIED,
                        }
                    )
                else:
                    status_counts["cv_readable"] += 1
                    cv_source_counts[str(signals.get("source") or "unknown")] += 1
    finally:
        conn.close()

    missing_by_case = dict(sorted(Counter(int(item["case_id"]) for item in missing).items()))
    cv_failure_by_case = dict(sorted(Counter(int(item["case_id"]) for item in cv_failures).items()))
    identity = identity_provider_decision()
    summary = {
        "case_count": len(rows),
        "missing_file_count": len(missing),
        "cv_failure_count": len(cv_failures),
        "cv_readable_count": int(status_counts.get("cv_readable", 0)),
        "missing_by_case": missing_by_case,
        "cv_failure_by_case": cv_failure_by_case,
        "status_counts": dict(sorted(status_counts.items())),
        "cv_source_counts": dict(sorted(cv_source_counts.items())),
    }
    if not missing and not cv_failures:
        queue_status = "clear"
        decision = "CV 失败已清零；缺失源文件也已清零。"
    elif not cv_failures:
        queue_status = "missing_sources_require_manual_repair"
        decision = "CV 失败已清零；缺失源文件仍需人工恢复或重扫源目录，未静默删除。"
    else:
        queue_status = "repair_required"
        decision = f"{UNVERIFIED}: 仍存在 CV 失败或缺失源文件，不能视为整理信号完整。"
    return {
        "generated_at": _now(),
        "run_status": "readonly_primary_signal_repair_queue",
        "queue_status": queue_status,
        "decision": decision,
        "used_mock_data": False,
        "db_path": str(db_path),
        "summary": summary,
        "missing_source_files": missing,
        "cv_failures": cv_failures,
        "identity_provider_decision": identity,
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone()
    return bool(row)


def _audit_before(conn: sqlite3.Connection, case_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not case_ids or not (_table_exists(conn, "case_revisions") and _table_exists(conn, "cases")):
        return {}
    try:
        from backend import audit  # noqa: PLC0415

        return audit.snapshot_before(conn, case_ids)
    except Exception:  # noqa: BLE001
        return {}


def _audit_after(conn: sqlite3.Connection, case_ids: list[int], before: dict[int, dict[str, Any]]) -> list[int]:
    if not before:
        return []
    try:
        from backend import audit  # noqa: PLC0415

        return audit.record_after(
            conn,
            case_ids,
            before,
            op="t70_missing_source_safe_rescan",
            source_route="backend/scripts/primary_signal_repair_queue.py",
            actor="codex-t70",
        )
    except Exception:  # noqa: BLE001
        return []


def apply_safe_missing_source_repairs(db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Clear stale metadata only when a case directory currently has zero real sources."""
    db_path = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    results: list[dict[str, Any]] = []
    try:
        rows = conn.execute(
            "SELECT id, abs_path, meta_json FROM cases WHERE trashed_at IS NULL ORDER BY id"
        ).fetchall()
        for row in rows:
            case_id = int(row["id"])
            case_dir = Path(str(row["abs_path"] or ""))
            meta_files = _case_image_files(row)
            if not meta_files:
                continue
            missing_files = [filename for filename in meta_files if not (case_dir / filename).is_file()]
            if not missing_files:
                continue
            if not case_dir.is_dir():
                results.append(
                    {
                        "case_id": case_id,
                        "status": "manual_required_missing_case_dir",
                        "missing_count": len(missing_files),
                        "actual_source_count": 0,
                        "case_abs_path": str(case_dir),
                    }
                )
                continue
            actual_sources = scanner._iter_case_image_files(case_dir) if case_dir.is_dir() else []
            if actual_sources or len(missing_files) != len(meta_files):
                results.append(
                    {
                        "case_id": case_id,
                        "status": "manual_required",
                        "missing_count": len(missing_files),
                        "actual_source_count": len(actual_sources),
                    }
                )
                continue
            before = _audit_before(conn, [case_id])
            scanner.rescan_one(conn, case_id)
            conn.execute(
                """
                UPDATE cases
                SET skill_image_metadata_json = ?,
                    skill_blocking_detail_json = NULL,
                    skill_warnings_json = NULL
                WHERE id = ?
                """,
                ("[]", case_id),
            )
            revisions = _audit_after(conn, [case_id], before)
            results.append(
                {
                    "case_id": case_id,
                    "status": "safe_rescanned_empty_source_dir",
                    "missing_count": len(missing_files),
                    "actual_source_count": 0,
                    "revision_ids": revisions,
                }
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    status_counts = dict(sorted(Counter(str(item.get("status") or "") for item in results).items()))
    manual_required_count = sum(
        count for status, count in status_counts.items() if str(status).startswith("manual_required")
    )
    return {
        "generated_at": _now(),
        "run_status": "applied_safe_missing_source_repairs",
        "used_mock_data": False,
        "db_path": str(db_path),
        "summary": {
            "result_count": len(results),
            "safe_rescan_count": status_counts.get("safe_rescanned_empty_source_dir", 0),
            "manual_required_count": manual_required_count,
            "missing_case_dir_count": status_counts.get("manual_required_missing_case_dir", 0),
            "status_counts": status_counts,
        },
        "results": results,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    summary = report.get("summary") or {}
    identity = report.get("identity_provider_decision") or {}
    lines = [
        "# T70 Primary Signal Repair Queue",
        "",
        f"- run_status: `{report.get('run_status')}`",
        f"- queue_status: `{report.get('queue_status')}`",
        f"- decision: {report.get('decision')}",
        f"- used_mock_data: `{report.get('used_mock_data')}`",
        f"- cv_failure_count: {summary.get('cv_failure_count')}",
        f"- missing_file_count: {summary.get('missing_file_count')}",
        f"- cv_source_counts: `{json.dumps(summary.get('cv_source_counts') or {}, ensure_ascii=False)}`",
        "",
        "## Identity Provider Decision",
        "",
        f"- status: `{identity.get('status')}`",
        f"- decision: `{identity.get('decision')}`",
        f"- readiness: {identity.get('readiness')}",
        f"- fail_closed: `{identity.get('fail_closed')}`",
        f"- recommended_next_step: `{identity.get('recommended_next_step')}`",
        "",
        "## Missing Source Files",
        "",
    ]
    for item in report.get("missing_source_files") or []:
        lines.append(
            f"- case {item.get('case_id')} `{item.get('filename')}`: {item.get('recommended_action')} / {item.get('readiness')}"
        )
    lines.extend(["", "## CV Failures", ""])
    for item in report.get("cv_failures") or []:
        lines.append(
            f"- case {item.get('case_id')} `{item.get('filename')}`: {item.get('error_type')} / {item.get('readiness')}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build T70 primary signal repair queue.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN_OUTPUT)
    parser.add_argument("--apply-safe-rescan-empty-dirs", action="store_true")
    parser.add_argument("--apply-output", type=Path, default=ROOT / "tasks" / "t70_missing_source_safe_repair_report.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    apply_report = None
    if args.apply_safe_rescan_empty_dirs:
        apply_report = apply_safe_missing_source_repairs(Path(args.db))
        write_json(Path(args.apply_output), apply_report)
    report = build_repair_queue(Path(args.db))
    if apply_report is not None:
        report["applied_repair_report"] = apply_report
    write_json(Path(args.output), report)
    write_markdown(Path(args.markdown_output), report)
    print(json.dumps({"queue_status": report["queue_status"], "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
