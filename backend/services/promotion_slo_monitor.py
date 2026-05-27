"""Promotion SLO monitor — plan §P2.4 灰度发布自动回滚信号源。

设计原则：
1) **Read-only**：仅查询 `simulation_jobs` / `candidate_lineage` / `ops_audit_log`
   三张 Wave 1 落地的表，不写任何业务表，不动 manifest，不触发回滚动作。
   回滚 applier 是 Wave 3 范围（避免本 wave 撞 Agent Y 的 manifest_loader）。
2) **4 维 SLO**（plan §P2.4）：
     - comfyui_failure_rate     ← simulation_jobs (failed/total)
     - vlm_disagreement_rate    ← candidate_lineage.vlm_judge_result_json
     - delivery_gate_rejection_rate ← ops_audit_log 失败 ratio
     - pre_render_gate_blocker_count ← simulation_jobs.audit_json.failure_stage
3) **样本量保护**：window 内 sample < `minimum_sample_size` 一律
   recommendation='insufficient_data'，避免冷启动 / 灰度初期 false positive。
4) **阈值可注入**：thresholds 可通过 kwarg 覆盖（测试 / 临时调参 / 多环境），
   未传则读 `case-workbench-ai/promotion/slo_thresholds.json` 默认。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Threshold / config 默认值（与 slo_thresholds.json 同源；JSON 为权威，本常量
# 仅作 sentinel + 兜底）
# ---------------------------------------------------------------------------

_DEFAULT_THRESHOLDS_FILE = (
    Path(__file__).resolve().parent.parent.parent
    / "case-workbench-ai"
    / "promotion"
    / "slo_thresholds.json"
)

_DEFAULT_THRESHOLDS: dict[str, Any] = {
    "comfyui_failure_rate_max": 0.05,
    "vlm_disagreement_rate_max": 0.10,
    "delivery_gate_rejection_rate_multiplier_max": 1.05,
    "pre_render_gate_blocker_multiplier_max": 1.10,
}

_DEFAULT_BASELINE: dict[str, Any] = {
    "delivery_gate_rejection_rate": 0.10,
    "pre_render_gate_blocker_count": 5,
}

DEFAULT_MINIMUM_SAMPLE_SIZE = 30
DEFAULT_WINDOW_HOURS = 48

# Endpoint 关键字 — `ops_audit_log.endpoint` 匹配 delivery / gate / batch-rerun 三类
_DELIVERY_GATE_ENDPOINT_KEYWORDS: tuple[str, ...] = (
    "delivery",
    "pre_render_gate",
    "batch-rerun",
)

# outcome ∈ {ok|partial|error|dry_run}（参 backend/routes/render.py
# ALLOWED_OPS_OUTCOMES）；error / partial 计作"被拒"；dry_run 不计 total。
_REJECTED_OUTCOMES: frozenset[str] = frozenset({"error", "partial"})
_COUNTED_OUTCOMES: frozenset[str] = frozenset({"ok", "error", "partial"})


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SLOReport:
    within_slo: bool
    violations: list[dict[str, Any]]
    evidence: dict[str, Any]
    recommendation: str  # 'continue' | 'rollback' | 'insufficient_data'
    window_hours: int
    sample_size: int
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Threshold 加载
# ---------------------------------------------------------------------------


def load_default_thresholds(path: Path | None = None) -> dict[str, Any]:
    """Load thresholds JSON. Returns the full structure (thresholds + baseline +
    sample size + window). Raises if file unreadable / malformed."""
    target = Path(path) if path else _DEFAULT_THRESHOLDS_FILE
    if not target.exists():
        return {
            "thresholds": dict(_DEFAULT_THRESHOLDS),
            "baseline": dict(_DEFAULT_BASELINE),
            "minimum_sample_size": DEFAULT_MINIMUM_SAMPLE_SIZE,
            "default_window_hours": DEFAULT_WINDOW_HOURS,
        }
    raw = json.loads(target.read_text(encoding="utf-8"))
    return {
        "thresholds": {**_DEFAULT_THRESHOLDS, **dict(raw.get("thresholds") or {})},
        "baseline": {**_DEFAULT_BASELINE, **dict(raw.get("baseline") or {})},
        "minimum_sample_size": int(
            raw.get("minimum_sample_size", DEFAULT_MINIMUM_SAMPLE_SIZE)
        ),
        "default_window_hours": int(
            raw.get("default_window_hours", DEFAULT_WINDOW_HOURS)
        ),
    }


def _merge_thresholds(
    user_thresholds: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge user override on top of file defaults. user_thresholds may carry the
    full structure (`{thresholds, baseline, minimum_sample_size}`) OR a flat
    map of threshold names (treated as `thresholds` overlay)."""
    base = load_default_thresholds()
    if not user_thresholds:
        return base
    if "thresholds" in user_thresholds or "baseline" in user_thresholds:
        # Full structured override
        return {
            "thresholds": {
                **base["thresholds"],
                **dict(user_thresholds.get("thresholds") or {}),
            },
            "baseline": {
                **base["baseline"],
                **dict(user_thresholds.get("baseline") or {}),
            },
            "minimum_sample_size": int(
                user_thresholds.get("minimum_sample_size", base["minimum_sample_size"])
            ),
            "default_window_hours": int(
                user_thresholds.get("default_window_hours", base["default_window_hours"])
            ),
        }
    # Flat override → assumed thresholds-only
    return {
        **base,
        "thresholds": {**base["thresholds"], **dict(user_thresholds)},
    }


# ---------------------------------------------------------------------------
# Per-dimension SLO 计算
# ---------------------------------------------------------------------------


def _cutoff_iso(window_hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()


def _compute_comfyui_failure_rate(
    conn: sqlite3.Connection, cutoff_iso: str
) -> dict[str, Any]:
    """`simulation_jobs` failed / total within window. status ∈ done/failed/...
    we count `failed` against everything except `pending` / `running` (only
    terminal states are observable)."""
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS n
        FROM simulation_jobs
        WHERE created_at >= ?
        GROUP BY status
        """,
        (cutoff_iso,),
    ).fetchall()
    status_counts: dict[str, int] = {}
    for row in rows:
        try:
            status_counts[str(row["status"])] = int(row["n"])
        except (KeyError, TypeError, ValueError):
            continue
    terminal_total = sum(
        n for status, n in status_counts.items() if status not in {"pending", "running"}
    )
    failed = status_counts.get("failed", 0)
    rate = (failed / terminal_total) if terminal_total > 0 else 0.0
    return {
        "failed": failed,
        "terminal_total": terminal_total,
        "rate": round(rate, 6),
        "status_breakdown": status_counts,
    }


def _compute_vlm_disagreement_rate(
    conn: sqlite3.Connection, cutoff_iso: str
) -> dict[str, Any]:
    """Average `1 - agreement_rate` over candidate_lineage rows with non-null
    vlm_judge_result_json within window. Missing / malformed payloads are
    skipped (don't count toward denominator)."""
    rows = conn.execute(
        """
        SELECT vlm_judge_result_json
        FROM candidate_lineage
        WHERE created_at >= ?
          AND vlm_judge_result_json IS NOT NULL
        """,
        (cutoff_iso,),
    ).fetchall()
    disagreements: list[float] = []
    for row in rows:
        raw = row["vlm_judge_result_json"]
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        agreement = payload.get("agreement_rate")
        if agreement is None:
            continue
        try:
            agreement_f = float(agreement)
        except (TypeError, ValueError):
            continue
        if agreement_f < 0 or agreement_f > 1:
            continue
        disagreements.append(1.0 - agreement_f)
    if not disagreements:
        return {"sample_count": 0, "mean_disagreement": 0.0}
    mean = sum(disagreements) / len(disagreements)
    return {
        "sample_count": len(disagreements),
        "mean_disagreement": round(mean, 6),
    }


def _compute_delivery_gate_rejection_rate(
    conn: sqlite3.Connection,
    cutoff_iso: str,
    keywords: Iterable[str] = _DELIVERY_GATE_ENDPOINT_KEYWORDS,
) -> dict[str, Any]:
    """ops_audit_log endpoint 含 delivery / pre_render_gate / batch-rerun 关键字
    的行：rejected = outcome ∈ {error, partial} / counted = ok+error+partial。
    dry_run 不计入 denominator（dry_run 是规划态，不代表真实流量）。"""
    rows = conn.execute(
        """
        SELECT endpoint, outcome
        FROM ops_audit_log
        WHERE created_at >= ?
        """,
        (cutoff_iso,),
    ).fetchall()
    rejected = 0
    counted = 0
    for row in rows:
        endpoint = str(row["endpoint"] or "")
        if not any(kw in endpoint for kw in keywords):
            continue
        outcome = str(row["outcome"] or "")
        if outcome not in _COUNTED_OUTCOMES:
            continue
        counted += 1
        if outcome in _REJECTED_OUTCOMES:
            rejected += 1
    rate = (rejected / counted) if counted > 0 else 0.0
    return {
        "rejected": rejected,
        "counted": counted,
        "rate": round(rate, 6),
    }


def _compute_pre_render_gate_blocker_count(
    conn: sqlite3.Connection, cutoff_iso: str
) -> dict[str, Any]:
    """simulation_jobs.audit_json.failure.failure_stage == 'pre_render_gate'
    在 window 内的计数。这是 plan §P2.4 的 absolute count（vs baseline
    multiplier），不是 rate。"""
    rows = conn.execute(
        """
        SELECT audit_json
        FROM simulation_jobs
        WHERE created_at >= ?
          AND status = 'failed'
        """,
        (cutoff_iso,),
    ).fetchall()
    blocker_count = 0
    for row in rows:
        raw = row["audit_json"] or "{}"
        try:
            audit = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            audit = {}
        if not isinstance(audit, dict):
            continue
        failure = audit.get("failure")
        if not isinstance(failure, dict):
            continue
        stage = str(failure.get("failure_stage") or "")
        if stage == "pre_render_gate":
            blocker_count += 1
    return {"blocker_count": blocker_count}


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def evaluate_window(
    window_hours: int = DEFAULT_WINDOW_HOURS,
    *,
    conn: sqlite3.Connection | None = None,
    thresholds: dict[str, Any] | None = None,
) -> SLOReport:
    """Compute SLO report for the past `window_hours`. Returns immutable
    SLOReport DTO. If `conn` is None we open one from `backend.db`.

    Sample size = sum of all evidence denominators across 4 dimensions; if
    < minimum_sample_size we short-circuit to `insufficient_data`.
    """
    if window_hours <= 0:
        raise ValueError("window_hours must be > 0")

    config = _merge_thresholds(thresholds)
    th = config["thresholds"]
    bl = config["baseline"]
    min_sample = int(config["minimum_sample_size"])

    cutoff = _cutoff_iso(window_hours)

    owns_conn = False
    if conn is None:
        # Lazy import — keep service importable in unit tests w/o DB
        from .. import db as _db  # pragma: no cover

        conn = _db.get_conn()
        owns_conn = True

    try:
        comfyui_evidence = _compute_comfyui_failure_rate(conn, cutoff)
        vlm_evidence = _compute_vlm_disagreement_rate(conn, cutoff)
        delivery_evidence = _compute_delivery_gate_rejection_rate(conn, cutoff)
        gate_evidence = _compute_pre_render_gate_blocker_count(conn, cutoff)
    finally:
        if owns_conn:
            conn.close()

    sample_size = (
        comfyui_evidence["terminal_total"]
        + vlm_evidence["sample_count"]
        + delivery_evidence["counted"]
        + gate_evidence["blocker_count"]
    )

    evidence = {
        "comfyui_failure": comfyui_evidence,
        "vlm_disagreement": vlm_evidence,
        "delivery_gate_rejection": delivery_evidence,
        "pre_render_gate_blocker": gate_evidence,
        "cutoff_iso": cutoff,
        "thresholds": th,
        "baseline": bl,
        "minimum_sample_size": min_sample,
    }

    if sample_size < min_sample:
        return SLOReport(
            within_slo=True,
            violations=[],
            evidence=evidence,
            recommendation="insufficient_data",
            window_hours=window_hours,
            sample_size=sample_size,
        )

    violations: list[dict[str, Any]] = []

    # 1) comfyui failure rate
    cr_max = float(th["comfyui_failure_rate_max"])
    if comfyui_evidence["rate"] > cr_max:
        violations.append(
            {
                "dimension": "comfyui_failure_rate",
                "actual": comfyui_evidence["rate"],
                "threshold": cr_max,
                "comparator": "<=",
                "context": comfyui_evidence,
            }
        )

    # 2) vlm disagreement rate
    vd_max = float(th["vlm_disagreement_rate_max"])
    if vlm_evidence["sample_count"] > 0 and vlm_evidence["mean_disagreement"] > vd_max:
        violations.append(
            {
                "dimension": "vlm_disagreement_rate",
                "actual": vlm_evidence["mean_disagreement"],
                "threshold": vd_max,
                "comparator": "<=",
                "context": vlm_evidence,
            }
        )

    # 3) delivery gate rejection rate vs baseline * multiplier
    dg_mult = float(th["delivery_gate_rejection_rate_multiplier_max"])
    dg_baseline = float(bl["delivery_gate_rejection_rate"])
    dg_cap = dg_baseline * dg_mult
    if delivery_evidence["counted"] > 0 and delivery_evidence["rate"] > dg_cap:
        violations.append(
            {
                "dimension": "delivery_gate_rejection_rate",
                "actual": delivery_evidence["rate"],
                "threshold": round(dg_cap, 6),
                "baseline": dg_baseline,
                "multiplier": dg_mult,
                "comparator": "<=",
                "context": delivery_evidence,
            }
        )

    # 4) pre_render_gate blocker count vs baseline * multiplier
    pg_mult = float(th["pre_render_gate_blocker_multiplier_max"])
    pg_baseline = float(bl["pre_render_gate_blocker_count"])
    pg_cap = pg_baseline * pg_mult
    if gate_evidence["blocker_count"] > pg_cap:
        violations.append(
            {
                "dimension": "pre_render_gate_blocker_count",
                "actual": gate_evidence["blocker_count"],
                "threshold": round(pg_cap, 6),
                "baseline": pg_baseline,
                "multiplier": pg_mult,
                "comparator": "<=",
                "context": gate_evidence,
            }
        )

    within = len(violations) == 0
    recommendation = "continue" if within else "rollback"
    return SLOReport(
        within_slo=within,
        violations=violations,
        evidence=evidence,
        recommendation=recommendation,
        window_hours=window_hours,
        sample_size=sample_size,
    )


__all__ = [
    "SLOReport",
    "evaluate_window",
    "load_default_thresholds",
    "DEFAULT_WINDOW_HOURS",
    "DEFAULT_MINIMUM_SAMPLE_SIZE",
]


if __name__ == "__main__":  # pragma: no cover
    # Smoke print — used during dev. Real CLI is `backend.scripts.promotion_slo_check`.
    report = evaluate_window()
    json.dump(report.to_dict(), sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
