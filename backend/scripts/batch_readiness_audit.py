"""Batch readiness audit for case-workbench renders.

Diagnose why each case fails to enter the render pipeline. Runs ZERO AI
calls — pure read + gate evaluation.

The case-workbench render pipeline has TWO independent gates before any
quota is spent:

  1. backend/services/pre_render_gate.py::evaluate_pre_render_gate
       Checks: identity_embedding presence, render slot fill, before/after
       pair availability, skill metadata source profile.

  2. backend/render_queue.py::_classification_blocking_preflight (called
     inside _execute_render_impl)
       Checks: every image has resolved phase+view (from skill metadata
       OR manual_phase override OR filename token fallback).

A case must pass BOTH to actually invoke run_render. Conventional wisdom
("74 unrendered are waiting for quota") is wrong — quota is not the gating
factor. Data completeness is.

Usage:
    python -m backend.scripts.batch_readiness_audit              # all unrendered
    python -m backend.scripts.batch_readiness_audit --case-ids 88,136
    python -m backend.scripts.batch_readiness_audit --format json
    python -m backend.scripts.batch_readiness_audit --only-ready

Exit codes:
    0  - audit completed (regardless of how many cases are blocked)
    1  - usage error
    2  - DB / fatal error
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend import db, render_queue, source_images
from backend.services.pre_render_gate import evaluate_pre_render_gate


# Tokens whose presence in a case_dir name will poison filename phase detection.
# When case_dir contains "术前"/"术后", every file path under it inherits the
# substring — _phase_from_filename does substring match and returns the FIRST
# match (BEFORE_TOKENS is checked first), incorrectly labeling all images.
POISONING_TOKENS = ("术前", "术后", "治疗前", "治疗后", "before", "after", "pre", "post")


@dataclass
class CaseAudit:
    case_id: int
    customer: str | None
    category: str | None
    abs_path: str
    n_image_files: int = 0
    n_skill_entries: int = 0
    n_with_identity_embedding: int = 0
    n_with_phase: int = 0
    n_with_view: int = 0
    n_manual_overrides: int = 0
    blocking_issues_count: int = 0
    case_dir_phase_pollution: list[str] = field(default_factory=list)
    pre_render_gate_passes: bool = False
    pre_render_gate_tickets: list[str] = field(default_factory=list)
    classification_eff_after: int = 0  # images effective phase resolves to 'after'
    classification_eff_before: int = 0
    classification_unresolved: int = 0
    status: str = "unknown"
    fix_actions: list[str] = field(default_factory=list)


def _detect_dir_pollution(abs_path: str) -> list[str]:
    """Detect phase tokens in case_dir name that poison filename-based phase
    detection for every image under the case."""
    case_name = Path(abs_path).name.lower()
    return [tok for tok in POISONING_TOKENS if tok.lower() in case_name]


def _resolve_effective_phase_view(
    filename: str,
    skill_meta: dict[str, Any] | None,
    override: dict[str, Any] | None,
    case_dir_polluted: bool,
) -> tuple[str | None, str | None]:
    """Replicate _metadata_phase_view's resolution chain so we can audit it
    without invoking the queue. The order: override > skill > filename token.

    If the case_dir name is polluted with phase tokens, the filename fallback
    is poisoned — disable it in the audit to surface this as a problem.
    """
    o = override or {}
    sm = skill_meta or {}
    phase = o.get("phase") if o.get("phase") in ("before", "after") else None
    view = o.get("view") if o.get("view") in ("front", "oblique", "side") else None
    if phase is None:
        p = sm.get("phase")
        if p in ("before", "after"):
            phase = p
    if view is None:
        v = sm.get("view_bucket") or sm.get("angle")
        if v in ("front", "oblique", "side"):
            view = v
    if phase is None and not case_dir_polluted:
        phase = source_images._phase_from_filename(filename)
    return phase, view


def _classify_status(audit: CaseAudit) -> tuple[str, list[str]]:
    """Bucketize and produce concrete fix actions, in priority order."""
    actions: list[str] = []

    if audit.blocking_issues_count > 0:
        actions.append(
            f"清理 {audit.blocking_issues_count} 项显式 blocking_issues "
            "(operator 在 Cases UI 解决, e.g. 补术前正面照片)"
        )

    if audit.case_dir_phase_pollution:
        actions.append(
            f"重命名 case_dir (含污染 token: {audit.case_dir_phase_pollution}) — "
            "phase token 出现在目录名会让所有子文件被误判 'before' (BEFORE_TOKENS 先匹配)"
        )

    if audit.n_image_files > 0 and audit.n_with_identity_embedding < audit.n_image_files:
        missing = audit.n_image_files - audit.n_with_identity_embedding
        actions.append(
            f"补 identity_embedding {missing}/{audit.n_image_files} 张 — "
            "跑 `backend/scripts/identity_embedding_enrichment.py` (T72)"
        )

    if audit.classification_unresolved > 0:
        actions.append(
            f"补 phase/view 元数据 {audit.classification_unresolved} 张 — "
            "operator 在 ImageWorkbench UI 手动标 (会写 case_image_overrides)"
        )

    if audit.classification_eff_after == 0 and audit.n_image_files > 0:
        actions.append(
            "0 张 effective 'after' 图 — 渲染没东西可对比; "
            "若实际有术后照, 检查上面 phase pollution / metadata 缺失"
        )

    if not audit.pre_render_gate_tickets and audit.blocking_issues_count == 0 and audit.classification_unresolved == 0:
        return "ready", actions

    if "identity_not_verified" in audit.pre_render_gate_tickets:
        return "fix_identity", actions

    if audit.case_dir_phase_pollution:
        return "fix_naming", actions

    if audit.classification_unresolved > 0:
        return "fix_metadata", actions

    if audit.blocking_issues_count > 0:
        return "fix_blocking", actions

    return "fix_other", actions


def audit_case(conn, case_id: int) -> CaseAudit | None:
    row = conn.execute(
        """
        SELECT id, customer_raw, category, abs_path,
               meta_json, skill_image_metadata_json, blocking_issues_json
        FROM cases WHERE id = ? AND trashed_at IS NULL
        """,
        (case_id,),
    ).fetchone()
    if not row:
        return None

    audit = CaseAudit(
        case_id=int(row["id"]),
        customer=row["customer_raw"],
        category=row["category"],
        abs_path=row["abs_path"] or "",
    )

    case_meta = json.loads(row["meta_json"] or "{}")
    skill = json.loads(row["skill_image_metadata_json"] or "[]")
    blocking = json.loads(row["blocking_issues_json"] or "[]")
    image_files = case_meta.get("image_files") or []
    audit.n_image_files = len(image_files)
    audit.blocking_issues_count = len(blocking) if isinstance(blocking, list) else 0

    skill_by_file = {
        (m.get("filename") or m.get("relative_path")): m
        for m in skill if isinstance(m, dict)
    }
    audit.n_skill_entries = len(skill_by_file)
    audit.n_with_identity_embedding = sum(
        1 for m in skill_by_file.values()
        if m.get("identity_embedding")
    )
    audit.n_with_phase = sum(
        1 for m in skill_by_file.values()
        if m.get("phase") in ("before", "after")
    )
    audit.n_with_view = sum(
        1 for m in skill_by_file.values()
        if (m.get("view_bucket") or m.get("angle")) in ("front", "oblique", "side")
    )

    overrides = render_queue._fetch_case_image_overrides(conn, audit.case_id)
    audit.n_manual_overrides = len([
        v for v in overrides.values() if v.get("phase") in ("before", "after")
    ])

    audit.case_dir_phase_pollution = _detect_dir_pollution(audit.abs_path)

    # Effective phase/view resolution per image
    polluted = bool(audit.case_dir_phase_pollution)
    for filename in image_files:
        sm = skill_by_file.get(filename) or skill_by_file.get(Path(filename).name) or {}
        ov = overrides.get(filename) or overrides.get(Path(filename).name) or {}
        phase, view = _resolve_effective_phase_view(filename, sm, ov, polluted)
        if phase == "after":
            audit.classification_eff_after += 1
        elif phase == "before":
            audit.classification_eff_before += 1
        if not phase or not view:
            audit.classification_unresolved += 1

    # Pre-render gate
    try:
        result = evaluate_pre_render_gate(
            audit.case_id, template="single-compare", conn=conn,
        )
        tickets = result.get("tickets") or []
        audit.pre_render_gate_tickets = [
            str(t.get("reason_code") or "unknown") for t in tickets
        ]
        audit.pre_render_gate_passes = not tickets
    except Exception as exc:
        audit.pre_render_gate_tickets = [f"gate_eval_error:{type(exc).__name__}"]

    audit.status, audit.fix_actions = _classify_status(audit)
    return audit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--case-ids", type=str, default="",
        help="Comma-separated case ids. Default: all unrendered live cases.",
    )
    parser.add_argument(
        "--format", choices=("table", "markdown", "json"), default="table",
    )
    parser.add_argument(
        "--only-ready", action="store_true",
        help="Print only cases with status='ready'.",
    )
    parser.add_argument(
        "--summary-only", action="store_true",
        help="Skip per-case detail, just print status histogram.",
    )
    args = parser.parse_args(argv)

    if args.case_ids:
        try:
            target_ids = [int(x.strip()) for x in args.case_ids.split(",") if x.strip()]
        except ValueError:
            parser.error("invalid --case-ids; must be comma-separated integers")
            return 1
    else:
        target_ids = []

    try:
        with db.connect() as conn:
            if not target_ids:
                rows = conn.execute(
                    """
                    SELECT id FROM cases
                    WHERE trashed_at IS NULL
                      AND id NOT IN (SELECT case_id FROM render_jobs WHERE status='done')
                    ORDER BY id
                    """
                ).fetchall()
                target_ids = [int(r["id"]) for r in rows]

            audits: list[CaseAudit] = []
            for cid in target_ids:
                a = audit_case(conn, cid)
                if a is None:
                    print(f"!! case {cid} not found / trashed", file=sys.stderr)
                    continue
                audits.append(a)
    except Exception as exc:
        print(f"FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    # Status histogram
    histogram: dict[str, int] = {}
    for a in audits:
        histogram[a.status] = histogram.get(a.status, 0) + 1

    if args.format == "json":
        payload = {
            "total": len(audits),
            "histogram": histogram,
            "cases": [a.__dict__ for a in audits] if not args.summary_only else [],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    # Table / markdown
    print(f"\nBatch readiness audit — {len(audits)} cases\n")
    print("Status histogram:")
    for status, n in sorted(histogram.items(), key=lambda x: -x[1]):
        print(f"  {n:4d}  {status}")

    if args.summary_only:
        return 0

    if args.only_ready:
        audits = [a for a in audits if a.status == "ready"]

    print("\n" + "=" * 80)
    for a in audits:
        print(f"\ncase {a.case_id}  customer={a.customer}  category={a.category}")
        print(f"  status: {a.status}")
        print(f"  abs_path: {a.abs_path}")
        print(f"  images: {a.n_image_files} files | skill: {a.n_skill_entries} entries"
              f" | overrides: {a.n_manual_overrides}")
        print(f"  identity_embedding: {a.n_with_identity_embedding}/{a.n_image_files}")
        print(f"  skill phase coverage: {a.n_with_phase}/{a.n_image_files}"
              f" | view coverage: {a.n_with_view}/{a.n_image_files}")
        print(f"  effective: before={a.classification_eff_before}"
              f" after={a.classification_eff_after}"
              f" unresolved={a.classification_unresolved}")
        print(f"  blocking_issues: {a.blocking_issues_count}")
        print(f"  case_dir phase pollution: {a.case_dir_phase_pollution or '(none)'}")
        gate_tag = "PASS" if a.pre_render_gate_passes else (
            ",".join(a.pre_render_gate_tickets) or "BLOCK"
        )
        print(f"  pre_render_gate: {gate_tag}")
        if a.fix_actions:
            print(f"  fix:")
            for fix in a.fix_actions:
                print(f"    - {fix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
