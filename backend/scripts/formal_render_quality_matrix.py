"""Run and report a real formal render quality matrix for T59.

This runner only reads the live case-workbench DB and real local source files.
If a real render cannot be enqueued or observed, it writes a blocked report
instead of fabricating quality records.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/Users/a1234/Desktop/案例生成器/case-workbench")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import source_images  # noqa: E402

DEFAULT_DB_PATH = ROOT / "case-workbench.db"
DEFAULT_JSON_OUTPUT = ROOT / "tasks" / "t59_formal_render_quality_matrix.json"
DEFAULT_MARKDOWN_OUTPUT = ROOT / "tasks" / "t59_formal_render_quality_matrix.md"
TERMINAL_STATUSES = {"done", "done_with_issues", "blocked", "failed", "cancelled"}
UNVERIFIED = "未验证/无法获取"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(raw: str | None, default: Any) -> Any:
    try:
        data = json.loads(raw or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return data


def _json_dict(raw: str | None) -> dict[str, Any]:
    data = _load_json(raw, {})
    return data if isinstance(data, dict) else {}


def _case_rows(db_path: Path, case_ids: list[int] | None = None) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if case_ids:
            placeholders = ",".join("?" * len(case_ids))
            rows = conn.execute(
                f"SELECT * FROM cases WHERE trashed_at IS NULL AND id IN ({placeholders})",
                case_ids,
            ).fetchall()
            by_id = {int(row["id"]): row for row in rows}
            return [by_id[cid] for cid in case_ids if cid in by_id]
        return conn.execute(
            "SELECT * FROM cases WHERE trashed_at IS NULL ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _image_files_from_meta(raw: str | None) -> list[str]:
    meta = _json_dict(raw)
    return [str(item) for item in (meta.get("image_files") or []) if item]


def _phase_for_case_file(case_dir: Path, filename: str) -> str | None:
    case_name = case_dir.name
    contextual = str(Path(case_name) / filename) if case_name else filename
    return source_images._phase_from_filename(filename) or source_images._phase_from_filename(contextual)


def _has_distinct_before_after_sources(case_dir: Path, image_files: list[str]) -> bool:
    split = source_images.existing_source_image_files(str(case_dir), image_files)
    before_paths: set[Path] = set()
    after_paths: set[Path] = set()
    for filename in [str(item) for item in split["existing"]]:
        phase = _phase_for_case_file(case_dir, filename)
        if phase not in {"before", "after"}:
            continue
        resolved = (case_dir / filename).resolve(strict=False)
        if phase == "before":
            before_paths.add(resolved)
        else:
            after_paths.add(resolved)
    return bool(before_paths and after_paths and before_paths.isdisjoint(after_paths))


def _invalid_case_reason(profile: dict[str, Any], case_dir: Path, image_files: list[str]) -> str | None:
    source_kind = str(profile.get("source_kind") or "")
    if source_kind != "ready_source":
        if int(profile.get("missing_source_count") or 0) > 0 or source_kind == "missing_source_files":
            return "missing_source_files"
        return source_kind or "source_not_ready"
    if not _has_distinct_before_after_sources(case_dir, image_files):
        return "no_distinct_before_after_sources"
    return None


def select_live_render_cases(
    db_path: Path,
    *,
    max_cases: int,
    case_ids: list[int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select real cases with readable source photos and before/after evidence."""
    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in _case_rows(db_path, case_ids):
        case_id = int(row["id"])
        case_dir = Path(str(row["abs_path"]))
        image_files = _image_files_from_meta(row["meta_json"] if "meta_json" in row.keys() else None)
        profile = source_images.classify_existing_case_source_profile(str(case_dir), image_files)
        reason = _invalid_case_reason(profile, case_dir, image_files)
        case_item = {
            "case_id": case_id,
            "abs_path": str(case_dir),
            "customer_raw": row["customer_raw"] if "customer_raw" in row.keys() else None,
            "template_tier": row["template_tier"] if "template_tier" in row.keys() else None,
            "manual_template_tier": row["manual_template_tier"] if "manual_template_tier" in row.keys() else None,
            "source_profile": profile,
        }
        if reason:
            rejected.append({**case_item, "reason": reason})
            continue
        selected.append(case_item)
        if len(selected) >= max_cases:
            break
    return selected, rejected


def resolve_render_template(case_info: dict[str, Any], *, requested_template: str) -> str:
    """Resolve `auto` to the case's audited template tier."""
    requested = str(requested_template or "").strip()
    if requested and requested != "auto":
        return requested
    tier = str(case_info.get("manual_template_tier") or case_info.get("template_tier") or "").strip()
    return {
        "single": "single-compare",
        "single-compare": "single-compare",
        "bi": "bi-compare",
        "bi-compare": "bi-compare",
        "tri": "tri-compare",
        "tri-compare": "tri-compare",
        "body-dual-compare": "body-dual-compare",
    }.get(tier, "tri-compare")


def _path_info(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {"path": None, "exists": False, "size_bytes": None}
    path = Path(raw)
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "exists": False, "size_bytes": None}
    return {"path": str(path), "exists": path.is_file(), "size_bytes": stat.st_size if path.is_file() else None}


def _same_existing_path(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)


def _metrics_gate_blockers(metrics: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    for action in metrics.get("action_suggestions") or []:
        if not isinstance(action, dict):
            continue
        gate = action.get("publish_gate")
        if isinstance(gate, dict) and gate.get("can_publish_after_acceptance") is False:
            blockers.append("render_quality.metrics_publish_gate")
    for alert in metrics.get("composition_alerts") or []:
        if not isinstance(alert, dict):
            continue
        code = str(alert.get("code") or "").strip()
        recommended = str(alert.get("recommended_action") or "").strip()
        if code == "front_source_crop_touches_frame" or recommended == "reselect_front_source_keep_unpublishable":
            blockers.append(f"composition_alert:{code or recommended}")
    return blockers


def _quality_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return False


def build_render_record(
    *,
    job: dict[str, Any],
    quality: dict[str, Any] | None,
    case_info: dict[str, Any],
) -> dict[str, Any]:
    output = _path_info(job.get("output_path"))
    manifest = _path_info(job.get("manifest_path"))
    same_artifact_path = _same_existing_path(job.get("output_path"), job.get("manifest_path"))
    if same_artifact_path:
        artifact_status = "invalid_same_artifact_path"
    elif output["exists"] and manifest["exists"]:
        artifact_status = "ok"
    elif not output["exists"] and not manifest["exists"]:
        artifact_status = "missing_final_board_and_manifest"
    elif not output["exists"]:
        artifact_status = "missing_final_board"
    else:
        artifact_status = "missing_manifest"

    metrics = quality.get("metrics") if isinstance(quality, dict) and isinstance(quality.get("metrics"), dict) else {}
    hard_blockers: list[str] = []
    status = str(job.get("status") or "")
    if status in {"blocked", "failed", "cancelled"}:
        hard_blockers.append(f"render_job.status:{status}")
    if quality is None:
        hard_blockers.append("render_quality.missing")
    else:
        quality_status = str(quality.get("quality_status") or "")
        if quality_status and quality_status != "done":
            hard_blockers.append(f"render_quality.quality_status:{quality_status}")
        if int(quality.get("blocking_count") or 0) > 0:
            hard_blockers.append("render_quality.blocking_count")
        manifest_status = str(quality.get("manifest_status") or "")
        if manifest_status in {"missing", "error", "failed"}:
            hard_blockers.append(f"render_quality.manifest_status:{manifest_status}")
    if artifact_status != "ok":
        if artifact_status == "invalid_same_artifact_path":
            hard_blockers.append("same_artifact_path")
        elif "final_board" in artifact_status:
            hard_blockers.append("final_board_missing")
        if "manifest" in artifact_status:
            hard_blockers.append("manifest_missing")
    for item in metrics.get("blocking_issues") or []:
        text = str(item).strip()
        if text:
            hard_blockers.append(f"blocking_issue:{text[:80]}")
    hard_blockers.extend(_metrics_gate_blockers(metrics))
    hard_blockers = list(dict.fromkeys(hard_blockers))

    can_publish = _quality_bool(quality.get("can_publish")) if isinstance(quality, dict) else False
    can_deliver = bool(status == "done" and can_publish and artifact_status == "ok" and not hard_blockers)
    reasons = hard_blockers[:]
    if not can_deliver and not reasons:
        reasons.append("render_quality.can_publish is false")
    delivery_envelope = {
        "class": "formal_ready" if can_deliver else "experimental_blocked",
        "can_deliver": can_deliver,
        "source": "render_quality",
        "gate_status": "ready_to_publish" if can_deliver else "blocked",
        "reasons": reasons,
        "evidence": {
            "artifact_mode": quality.get("artifact_mode") if isinstance(quality, dict) else None,
            "quality_status": quality.get("quality_status") if isinstance(quality, dict) else None,
            "job_status": status,
        },
    }
    return {
        "job_id": int(job.get("id") or 0),
        "case_id": int(job.get("case_id") or case_info.get("case_id") or 0),
        "case": case_info,
        "brand": job.get("brand"),
        "template": job.get("template"),
        "render_mode": job.get("render_mode") or "ai",
        "status": status,
        "enqueued_at": job.get("enqueued_at"),
        "finished_at": job.get("finished_at"),
        "error_message": job.get("error_message"),
        "artifact_integrity": {
            "status": artifact_status,
            "final_board": output,
            "manifest": manifest,
        },
        "quality": quality or None,
        "hard_blockers": hard_blockers,
        "delivery_envelope": delivery_envelope,
    }


def _counter_dict(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def summarize_matrix(
    *,
    run_id: str,
    selected_cases: list[dict[str, Any]],
    rejected_cases: list[dict[str, Any]],
    enqueued_job_ids: list[int],
    records: list[dict[str, Any]],
    run_status: str,
) -> dict[str, Any]:
    terminal_count = sum(1 for item in records if str(item.get("status") or "") in TERMINAL_STATUSES)
    formal_ready_count = sum(1 for item in records if (item.get("delivery_envelope") or {}).get("class") == "formal_ready")
    hard_blockers = []
    for record in records:
        hard_blockers.extend([str(item) for item in record.get("hard_blockers") or []])
    if not records or not enqueued_job_ids:
        decision = f"{UNVERIFIED}: 没有真实正式 render 终态记录。"
    elif run_status.startswith("timeout"):
        decision = f"{UNVERIFIED}: render 矩阵超时，仅有部分真实终态记录。"
    else:
        decision = "真实正式 render 矩阵已生成；是否达到交付质量以 formal_ready_count 和 hard_blockers 为准。"
    return {
        "generated_at": _now(),
        "run_id": run_id,
        "run_status": run_status,
        "used_mock_data": False,
        "decision": decision,
        "policy": {
            "source": "formal render chain only",
            "path": "cases -> render_jobs -> case-layout-board -> final-board.jpg -> manifest.final.json -> render_quality",
            "comfyui_included": False,
            "delivery_rule": "delivery_envelope.class == formal_ready and can_deliver == true",
            "done_with_issues_publishable": False,
        },
        "summary": {
            "selected_case_count": len(selected_cases),
            "rejected_case_count": len(rejected_cases),
            "enqueued_job_count": len(enqueued_job_ids),
            "record_count": len(records),
            "terminal_job_count": terminal_count,
            "formal_ready_count": formal_ready_count,
            "not_deliverable_count": len(records) - formal_ready_count,
            "final_board_exists_count": sum(
                1 for item in records if ((item.get("artifact_integrity") or {}).get("final_board") or {}).get("exists")
            ),
            "manifest_exists_count": sum(
                1 for item in records if ((item.get("artifact_integrity") or {}).get("manifest") or {}).get("exists")
            ),
            "by_job_status": _counter_dict([str(item.get("status") or "unknown") for item in records]),
            "by_quality_status": _counter_dict(
                [str(((item.get("quality") or {}).get("quality_status") or "missing")) for item in records]
            ),
            "by_artifact_integrity": _counter_dict(
                [str((item.get("artifact_integrity") or {}).get("status") or "unknown") for item in records]
            ),
            "hard_blocker_counts": _counter_dict(hard_blockers),
        },
        "selected_cases": selected_cases,
        "rejected_cases": rejected_cases[:80],
        "enqueued_job_ids": enqueued_job_ids,
        "records": records,
    }


def blocked_report(
    *,
    run_id: str,
    reason: str,
    selected_cases: list[dict[str, Any]],
    rejected_cases: list[dict[str, Any]],
) -> dict[str, Any]:
    report = summarize_matrix(
        run_id=run_id,
        selected_cases=selected_cases,
        rejected_cases=rejected_cases,
        enqueued_job_ids=[],
        records=[],
        run_status="blocked_no_real_render_execution",
    )
    report["blocked_reason"] = reason
    report["decision"] = f"{UNVERIFIED}: {reason}"
    return report


def _connect_rows(db_path: Path, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _case_info_by_id(selected_cases: list[dict[str, Any]], db_path: Path, job_case_ids: list[int]) -> dict[int, dict[str, Any]]:
    out = {int(item["case_id"]): item for item in selected_cases}
    missing = [cid for cid in job_case_ids if cid not in out]
    if missing:
        placeholders = ",".join("?" * len(missing))
        for row in _connect_rows(db_path, f"SELECT * FROM cases WHERE id IN ({placeholders})", tuple(missing)):
            out[int(row["id"])] = {
                "case_id": int(row["id"]),
                "abs_path": row["abs_path"],
                "customer_raw": row["customer_raw"] if "customer_raw" in row.keys() else None,
            }
    return out


def load_render_records(
    db_path: Path,
    *,
    selected_cases: list[dict[str, Any]],
    job_ids: list[int],
) -> list[dict[str, Any]]:
    if not job_ids:
        return []
    placeholders = ",".join("?" * len(job_ids))
    rows = _connect_rows(
        db_path,
        f"""
        SELECT
          j.*,
          rq.quality_status,
          rq.quality_score,
          rq.can_publish,
          rq.artifact_mode,
          rq.manifest_status,
          rq.blocking_count,
          rq.warning_count,
          rq.metrics_json,
          rq.review_verdict,
          rq.reviewer,
          rq.review_note,
          rq.reviewed_at
        FROM render_jobs j
        LEFT JOIN render_quality rq ON rq.render_job_id = j.id
        WHERE j.id IN ({placeholders})
        ORDER BY j.id
        """,
        tuple(job_ids),
    )
    case_info = _case_info_by_id(selected_cases, db_path, [int(row["case_id"]) for row in rows])
    records: list[dict[str, Any]] = []
    for row in rows:
        quality = None
        if row["quality_status"] is not None:
            quality = {
                "quality_status": row["quality_status"],
                "quality_score": row["quality_score"],
                "can_publish": bool(row["can_publish"]),
                "artifact_mode": row["artifact_mode"],
                "manifest_status": row["manifest_status"],
                "blocking_count": row["blocking_count"],
                "warning_count": row["warning_count"],
                "metrics": _json_dict(row["metrics_json"]),
                "review_verdict": row["review_verdict"],
                "reviewer": row["reviewer"],
                "review_note": row["review_note"],
                "reviewed_at": row["reviewed_at"],
            }
        records.append(
            build_render_record(
                job={key: row[key] for key in row.keys()},
                quality=quality,
                case_info=case_info.get(int(row["case_id"]), {"case_id": int(row["case_id"])}),
            )
        )
    return records


def wait_for_jobs(db_path: Path, job_ids: list[int], *, timeout_sec: int, poll_sec: float) -> str:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        records = load_render_records(db_path, selected_cases=[], job_ids=job_ids)
        statuses = {str(item.get("status") or "") for item in records}
        if len(records) == len(job_ids) and statuses and statuses.issubset(TERMINAL_STATUSES):
            return "completed_real_render_matrix"
        time.sleep(poll_sec)
    return "timeout_partial_real_render_matrix"


def _set_db_path_for_queue(db_path: Path) -> None:
    os.environ["CASE_WORKBENCH_DB_PATH"] = str(db_path.resolve())
    from backend import db  # noqa: PLC0415

    db.DB_PATH = db_path.resolve()


def ensure_live_db_schema(db_path: Path) -> None:
    """Apply additive schema migrations before touching the live render queue."""
    _set_db_path_for_queue(db_path)
    from backend import db  # noqa: PLC0415

    db.init_schema()


def enqueue_real_render_jobs(
    db_path: Path,
    selected_cases: list[dict[str, Any]],
    *,
    brand: str,
    template: str,
    semantic_judge: str,
) -> list[int]:
    ensure_live_db_schema(db_path)
    from backend.render_queue import RENDER_QUEUE  # noqa: PLC0415

    job_ids: list[int] = []
    for item in selected_cases:
        resolved_template = resolve_render_template(item, requested_template=template)
        item["resolved_template"] = resolved_template
        job_id = RENDER_QUEUE.enqueue(
            case_id=int(item["case_id"]),
            brand=brand,
            template=resolved_template,
            semantic_judge=semantic_judge,
            render_mode="ai",
        )
        job_ids.append(job_id)
    return job_ids


def live_db_snapshot(db_path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        def count(table: str) -> int | str:
            try:
                return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
            except sqlite3.Error:
                return UNVERIFIED

        status_rows = []
        try:
            status_rows = conn.execute("SELECT status, COUNT(*) AS n FROM render_jobs GROUP BY status").fetchall()
        except sqlite3.Error:
            status_rows = []
        quality_rows = []
        try:
            quality_rows = conn.execute(
                "SELECT quality_status, can_publish, COUNT(*) AS n FROM render_quality GROUP BY quality_status, can_publish"
            ).fetchall()
        except sqlite3.Error:
            quality_rows = []
        return {
            "cases": count("cases"),
            "render_jobs": count("render_jobs"),
            "render_quality": count("render_quality"),
            "render_jobs_by_status": {str(row["status"]): int(row["n"]) for row in status_rows},
            "render_quality_by_status_and_publish": [
                {
                    "quality_status": row["quality_status"],
                    "can_publish": bool(row["can_publish"]),
                    "count": int(row["n"]),
                }
                for row in quality_rows
            ],
        }
    finally:
        conn.close()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    summary = report.get("summary") or {}
    blockers = summary.get("hard_blocker_counts") or {}
    top_blockers = sorted(blockers.items(), key=lambda item: (-int(item[1]), str(item[0])))[:12]
    lines = [
        "# T59 真实正式出图质量矩阵",
        "",
        f"- run_status: `{report.get('run_status')}`",
        f"- decision: {report.get('decision')}",
        f"- selected_case_count: {summary.get('selected_case_count')}",
        f"- enqueued_job_count: {summary.get('enqueued_job_count')}",
        f"- terminal_job_count: {summary.get('terminal_job_count')}",
        f"- formal_ready_count: {summary.get('formal_ready_count')}",
        f"- not_deliverable_count: {summary.get('not_deliverable_count')}",
        f"- final_board_exists_count: {summary.get('final_board_exists_count')}",
        f"- manifest_exists_count: {summary.get('manifest_exists_count')}",
        "",
        "## Job Status",
        "",
    ]
    for key, value in (summary.get("by_job_status") or {}).items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Quality Status", ""])
    for key, value in (summary.get("by_quality_status") or {}).items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Top Hard Blockers", ""])
    if top_blockers:
        for key, value in top_blockers:
            lines.append(f"- `{key}`: {value}")
    else:
        lines.append("- none")
    lines.extend(["", "## Records", ""])
    for record in report.get("records") or []:
        env = record.get("delivery_envelope") or {}
        quality = record.get("quality") or {}
        lines.append(
            "- "
            f"job `{record.get('job_id')}` case `{record.get('case_id')}` "
            f"status `{record.get('status')}` quality `{quality.get('quality_status')}` "
            f"score `{quality.get('quality_score')}` delivery `{env.get('class')}`"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _renderer_available() -> tuple[bool, str | None]:
    root = Path.home() / "Desktop" / "飞书Claude" / "skills" / "case-layout-board"
    missing = [
        str(path)
        for path in (root / "scripts" / "case_layout_board.py", root / "scripts" / "render_brand_clean.py")
        if not path.is_file()
    ]
    if missing:
        return False, f"case-layout-board renderer missing: {missing}"
    return True, None


def run_matrix(args: argparse.Namespace) -> dict[str, Any]:
    db_path = Path(args.db).expanduser().resolve()
    run_id = args.run_id or f"t59-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    case_ids = [int(item) for item in args.case_ids.split(",") if item.strip()] if args.case_ids else None
    try:
        ensure_live_db_schema(db_path)
    except Exception as exc:  # noqa: BLE001
        return blocked_report(
            run_id=run_id,
            reason=f"live DB schema migration failed: {exc}",
            selected_cases=[],
            rejected_cases=[],
        )
    selected, rejected = select_live_render_cases(db_path, max_cases=int(args.max_cases), case_ids=case_ids)
    renderer_ok, renderer_reason = _renderer_available()
    if not renderer_ok:
        report = blocked_report(run_id=run_id, reason=renderer_reason or "renderer unavailable", selected_cases=selected, rejected_cases=rejected)
        report["live_db_snapshot"] = live_db_snapshot(db_path)
        return report
    if not selected:
        report = blocked_report(run_id=run_id, reason="no eligible live cases with readable before/after source photos", selected_cases=selected, rejected_cases=rejected)
        report["live_db_snapshot"] = live_db_snapshot(db_path)
        return report
    if args.dry_run:
        report = blocked_report(run_id=run_id, reason="dry-run requested; no real render was enqueued", selected_cases=selected, rejected_cases=rejected)
        report["run_status"] = "dry_run_no_real_render_execution"
        report["live_db_snapshot"] = live_db_snapshot(db_path)
        return report
    try:
        job_ids = enqueue_real_render_jobs(
            db_path,
            selected,
            brand=str(args.brand),
            template=str(args.template),
            semantic_judge=str(args.semantic_judge),
        )
    except Exception as exc:  # noqa: BLE001
        report = blocked_report(run_id=run_id, reason=f"render enqueue failed: {exc}", selected_cases=selected, rejected_cases=rejected)
        report["live_db_snapshot"] = live_db_snapshot(db_path)
        return report
    run_status = wait_for_jobs(db_path, job_ids, timeout_sec=int(args.timeout_sec), poll_sec=float(args.poll_sec))
    records = load_render_records(db_path, selected_cases=selected, job_ids=job_ids)
    report = summarize_matrix(
        run_id=run_id,
        selected_cases=selected,
        rejected_cases=rejected,
        enqueued_job_ids=job_ids,
        records=records,
        run_status=run_status,
    )
    report["live_db_snapshot"] = live_db_snapshot(db_path)
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run T59 real formal render quality matrix.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--max-cases", type=int, default=20)
    parser.add_argument("--case-ids", default="")
    parser.add_argument("--brand", default="fumei")
    parser.add_argument("--template", default="tri-compare")
    parser.add_argument("--semantic-judge", default="auto", choices=["auto", "off"])
    parser.add_argument("--timeout-sec", type=int, default=3600)
    parser.add_argument("--poll-sec", type=float, default=2.0)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN_OUTPUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    report = run_matrix(args)
    write_json(Path(args.output), report)
    write_markdown(Path(args.markdown_output), report)
    print(json.dumps({"run_status": report["run_status"], "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
