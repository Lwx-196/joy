"""Promotion SLO monitor — plan §P2.4 灰度发布自动回滚信号源。

设计原则：
1) **Read-only**：仅查询 `simulation_jobs` / `candidate_lineage` / `ops_audit_log`
   三张 Wave 1 落地的表，不写任何业务表，不动 manifest，不触发回滚动作。
   回滚 applier 是 Wave 3 范围（避免本 wave 撞 Agent Y 的 manifest_loader）。
2) **5 维 SLO**（plan §P2.4 + Wave 3 eval-auditor C-3）：
     - comfyui_failure_rate     ← simulation_jobs (failed/total)
     - vlm_disagreement_rate    ← candidate_lineage.vlm_judge_result_json
     - vlm_judge_missing_rate   ← candidate_lineage missing/malformed 比例
     - delivery_gate_rejection_rate ← ops_audit_log 失败 ratio
     - pre_render_gate_blocker_count ← simulation_jobs.audit_json.failure_stage
3) **样本量保护**（Wave 3 eval-auditor C-1 区分灰度 vs shadow）：
     - sample < `minimum_sample_size` + promotion_state ∈ {p10/p25/p50/p100}
       → `monitoring_paused`（保留状态等数据，within_slo=None）
     - sample < `minimum_sample_size` + promotion_state == 'shadow'（含
       fail-closed default 与 'rolled_back'）→ `insufficient_data`
4) **VLM judge missing rate 独立维度**（Wave 3 eval-auditor C-3）：
     - 分母 = 窗口内全量 candidate_lineage 行（不再只数 non-null judge 行）
     - 分子 missing = vlm_judge_result_json IS NULL 或 JSON 无法 parse
     - missing_rate > threshold（默认 0.10）→ violation
     - disagreement_rate 分母同步改全量行
5) **阈值可注入**：thresholds 可通过 kwarg 覆盖（测试 / 临时调参 / 多环境），
   未传则读 `case-workbench-ai/promotion/slo_thresholds.json` 默认。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

_logger = logging.getLogger(__name__)

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
    # Wave 3 C-3: missing/malformed payload rate cap (fallback default; may be
    # overridden via slo_thresholds.json `thresholds.vlm_judge_missing_rate_max`
    # — but we do NOT depend on the JSON having it. Agent C will reify the
    # threshold file separately.)
    "vlm_judge_missing_rate_max": 0.10,
    "delivery_gate_rejection_rate_multiplier_max": 1.05,
    "pre_render_gate_blocker_multiplier_max": 1.10,
}

_DEFAULT_BASELINE: dict[str, Any] = {
    "delivery_gate_rejection_rate": 0.10,
    "pre_render_gate_blocker_count": 5,
}

DEFAULT_MINIMUM_SAMPLE_SIZE = 30
DEFAULT_WINDOW_HOURS = 48

# Recommendation 常量（避免散落 magic string）
RECOMMENDATION_CONTINUE = "continue"
RECOMMENDATION_ROLLBACK = "rollback"
RECOMMENDATION_INSUFFICIENT_DATA = "insufficient_data"
# Wave 3 C-1: 灰度态小样本，保留状态等数据
RECOMMENDATION_MONITORING_PAUSED = "monitoring_paused"

# Wave 3 C-1: 灰度（已发布）态集合 — 这些 state 小样本走 monitoring_paused。
# `shadow` 与 `rolled_back` 是非灰度态（前者尚未发布，后者已撤回），均走
# insufficient_data；fail-closed 默认 'shadow' 也走 insufficient_data。
_PROMOTED_STATES: frozenset[str] = frozenset({"p10", "p25", "p50", "p100"})

# Baseline provenance gate — eval-auditor C-2 (Wave 3 Agent C).
# Required fields any thresholds JSON must carry; baseline > BASELINE_STALE_DAYS
# old → `baseline_stale` SLO violation (signals operator to re-calibrate).
BASELINE_STALE_DAYS = 60
_REQUIRED_PROVENANCE_FIELDS: tuple[str, ...] = (
    "measured_at",
    "window_hours",
    "sample_size",
    "computed_by",
    "computed_at_main_sha",
)
_SLO_TEST_MODE_ENV = "SLO_TEST_MODE"

# Wave 4 W4-1 — production deploy gate (release blocker).
# `computed_by` values that signal a placeholder / pre-calibration baseline.
# Production must refuse to load such thresholds (fail-closed) and any in-flight
# evaluation must surface `baseline_unmeasured` as an explicit violation so
# operators see it even if test-mode demotion silences the load-time check.
_PLACEHOLDER_COMPUTED_BY: frozenset[str] = frozenset(
    {"manual_seed", "placeholder", "seed"}
)
_PLACEHOLDER_SAMPLE_SIZE_THRESHOLD = 1  # sample_size < 1 == zero observed runs

# Wave 4 W4-2 — `monitoring_paused` time-axis stop-loss.
# Promoted state + sample<min for > PAUSED_STALE_DAYS days → escalate to
# `rollback` recommendation + emit `monitoring_paused_stale` violation. State
# is tracked in a sidecar JSON file so it survives restarts without touching
# the manifest schema (no Wave 1-3 contract churn).
PAUSED_STALE_DAYS = 7
_PAUSED_STATE_SCHEMA_VERSION = 1
_DEFAULT_PAUSED_STATE_FILE = (
    Path(__file__).resolve().parent.parent.parent
    / "case-workbench-ai"
    / "promotion"
    / "slo_paused_state.json"
)

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
    # Wave 3 C-1: `None` when recommendation='monitoring_paused' — small sample
    # under promoted state, decision is "保留状态等数据", neither pass nor fail.
    within_slo: bool | None
    violations: list[dict[str, Any]]
    evidence: dict[str, Any]
    # 'continue' | 'rollback' | 'insufficient_data' | 'monitoring_paused'
    recommendation: str
    window_hours: int
    sample_size: int
    # Wave 3 C-1: human-readable note (e.g. small_sample_in_promoted_state,
    # promotion_state echo). Empty string when no annotation needed.
    notes: str = ""
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Threshold 加载
# ---------------------------------------------------------------------------


def _is_test_mode() -> bool:
    """Permissive mode for tests / dev. Production keeps fail-closed semantics."""
    return os.environ.get(_SLO_TEST_MODE_ENV, "").strip() not in ("", "0", "false", "False")


def _validate_baseline_provenance(provenance: Any) -> dict[str, Any]:
    """Eval-auditor C-2: thresholds JSON must carry full provenance so baseline
    numbers are auditable + falsifiable. Five required fields + types.

    Production (`SLO_TEST_MODE` unset/0): missing/malformed → ValueError.
    Test (`SLO_TEST_MODE=1`): warn + return empty dict so legacy fixtures don't
    break. Callers downstream treat empty provenance as "stale unknown".
    """
    if not isinstance(provenance, dict):
        msg = (
            "invalid baseline_provenance: expected object, got "
            f"{type(provenance).__name__}"
        )
        if _is_test_mode():
            _logger.warning(msg)
            return {}
        raise ValueError(msg)

    errors: list[str] = []
    for field_name in _REQUIRED_PROVENANCE_FIELDS:
        if field_name not in provenance:
            errors.append(f"missing field '{field_name}'")

    if not errors:
        # Type checks
        measured_at = provenance.get("measured_at")
        if not isinstance(measured_at, str) or not measured_at:
            errors.append("'measured_at' must be non-empty ISO8601 string")
        else:
            try:
                parsed = datetime.fromisoformat(measured_at)
            except ValueError as exc:
                errors.append(f"'measured_at' is not ISO8601: {exc}")
            else:
                if parsed.tzinfo is None:
                    errors.append("'measured_at' must be timezone-aware")

        window_hours = provenance.get("window_hours")
        if not isinstance(window_hours, int) or isinstance(window_hours, bool) or window_hours <= 0:
            errors.append("'window_hours' must be positive int")

        sample_size = provenance.get("sample_size")
        if (
            not isinstance(sample_size, int)
            or isinstance(sample_size, bool)
            or sample_size < 0
        ):
            errors.append("'sample_size' must be non-negative int")

        computed_by = provenance.get("computed_by")
        if not isinstance(computed_by, str) or not computed_by.strip():
            errors.append("'computed_by' must be non-empty string")

        computed_sha = provenance.get("computed_at_main_sha")
        if not isinstance(computed_sha, str) or not computed_sha.strip():
            errors.append("'computed_at_main_sha' must be non-empty string")

    if errors:
        msg = "invalid baseline_provenance: " + "; ".join(errors)
        if _is_test_mode():
            _logger.warning(msg)
            return {}
        raise ValueError(msg)

    # Wave 4 W4-1: schema passed, but is this a placeholder baseline? A doc
    # with computed_by ∈ {manual_seed, placeholder, seed} + sample_size below
    # threshold means the threshold numbers were hand-seeded, not measured —
    # safe to load in test/dev (warn), unsafe in production (raise).
    computed_by = provenance.get("computed_by")
    sample_size = provenance.get("sample_size", 0)
    if (
        isinstance(computed_by, str)
        and computed_by in _PLACEHOLDER_COMPUTED_BY
        and isinstance(sample_size, int)
        and not isinstance(sample_size, bool)
        and sample_size < _PLACEHOLDER_SAMPLE_SIZE_THRESHOLD
    ):
        msg = (
            "baseline_provenance is a placeholder "
            f"(computed_by='{computed_by}', sample_size={sample_size}); "
            "production deploy refused. Run "
            "`python -m backend.scripts.calibrate_slo_baseline "
            "--window <N> --apply` to compute a real baseline from shadow data."
        )
        if _is_test_mode():
            _logger.warning(msg)
            # Returning the (validated-but-placeholder) provenance so downstream
            # `evaluate_window` can still emit the `baseline_unmeasured`
            # violation — visibility > silent pass in test mode too.
            return dict(provenance)
        raise ValueError(msg)

    return dict(provenance)


def load_default_thresholds(path: Path | None = None) -> dict[str, Any]:
    """Load thresholds JSON. Returns the full structure (thresholds + baseline +
    baseline_provenance + sample size + window). Raises ValueError if file is
    malformed or `baseline_provenance` is missing/invalid in production mode.
    """
    target = Path(path) if path else _DEFAULT_THRESHOLDS_FILE
    if not target.exists():
        # Pure code-default path. Provenance unknown; test-only fallback.
        return {
            "thresholds": dict(_DEFAULT_THRESHOLDS),
            "baseline": dict(_DEFAULT_BASELINE),
            "baseline_provenance": {},
            "minimum_sample_size": DEFAULT_MINIMUM_SAMPLE_SIZE,
            "default_window_hours": DEFAULT_WINDOW_HOURS,
        }
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid SLO thresholds JSON at {target}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(
            f"invalid SLO thresholds JSON at {target}: expected object root"
        )
    provenance = _validate_baseline_provenance(raw.get("baseline_provenance"))
    return {
        "thresholds": {**_DEFAULT_THRESHOLDS, **dict(raw.get("thresholds") or {})},
        "baseline": {**_DEFAULT_BASELINE, **dict(raw.get("baseline") or {})},
        "baseline_provenance": provenance,
        "minimum_sample_size": int(
            raw.get("minimum_sample_size", DEFAULT_MINIMUM_SAMPLE_SIZE)
        ),
        "default_window_hours": int(
            raw.get("default_window_hours", DEFAULT_WINDOW_HOURS)
        ),
    }


def _check_baseline_stale(
    provenance: dict[str, Any],
    *,
    now: datetime | None = None,
    stale_days: int = BASELINE_STALE_DAYS,
) -> dict[str, Any] | None:
    """If provenance.measured_at is older than `stale_days`, return a violation
    dict. Returns None when fresh or provenance unavailable (test mode)."""
    if not provenance:
        return None
    measured_at_raw = provenance.get("measured_at")
    if not isinstance(measured_at_raw, str) or not measured_at_raw:
        return None
    try:
        measured_at = datetime.fromisoformat(measured_at_raw)
    except ValueError:
        return None
    if measured_at.tzinfo is None:
        measured_at = measured_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    age = current - measured_at
    if age <= timedelta(days=stale_days):
        return None
    return {
        "dimension": "baseline_stale",
        "actual_days": round(age.total_seconds() / 86400.0, 3),
        "threshold_days": stale_days,
        "comparator": "<=",
        "context": {
            "measured_at": measured_at_raw,
            "computed_by": provenance.get("computed_by"),
            "computed_at_main_sha": provenance.get("computed_at_main_sha"),
        },
    }


def _code_default_thresholds() -> dict[str, Any]:
    """Pure code-default thresholds, no file I/O. Used as last-resort fallback
    when on-disk thresholds file is rejected by the W4-1 placeholder gate but
    callers supply a full override (e.g. tests, ad-hoc CLI sessions)."""
    return {
        "thresholds": dict(_DEFAULT_THRESHOLDS),
        "baseline": dict(_DEFAULT_BASELINE),
        "baseline_provenance": {},
        "minimum_sample_size": DEFAULT_MINIMUM_SAMPLE_SIZE,
        "default_window_hours": DEFAULT_WINDOW_HOURS,
    }


def _merge_thresholds(
    user_thresholds: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge user override on top of file defaults. user_thresholds may carry the
    full structure (`{thresholds, baseline, baseline_provenance, ...}`) OR a
    flat map of threshold names (treated as `thresholds` overlay).

    Wave 4 W4-1: when the on-disk thresholds file is rejected (placeholder
    provenance under production mode) BUT the caller has provided a full
    structured override with their own provenance, we degrade to code
    defaults rather than propagating the file rejection. This lets ad-hoc
    callers (tests, calibrate CLI's own sanity-load) work without
    SLO_TEST_MODE while production-path callers (no override) still
    fail-closed.
    """
    try:
        base = load_default_thresholds()
    except ValueError:
        if not user_thresholds:
            raise
        base = _code_default_thresholds()
    if not user_thresholds:
        return base
    if "thresholds" in user_thresholds or "baseline" in user_thresholds:
        # Full structured override
        overlay_provenance = user_thresholds.get("baseline_provenance")
        return {
            "thresholds": {
                **base["thresholds"],
                **dict(user_thresholds.get("thresholds") or {}),
            },
            "baseline": {
                **base["baseline"],
                **dict(user_thresholds.get("baseline") or {}),
            },
            "baseline_provenance": (
                dict(overlay_provenance)
                if isinstance(overlay_provenance, dict)
                else dict(base.get("baseline_provenance") or {})
            ),
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
        WHERE julianday(created_at) >= julianday(?)
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
    """Compute VLM disagreement rate **and** missing rate over all
    candidate_lineage rows within window.

    Wave 3 eval-auditor C-3 fix:
      - Pre-fix denominator was rows WHERE `vlm_judge_result_json IS NOT NULL`,
        which hid silent failure modes (judge call dropped, payload corrupted,
        agreement field missing). Operators got `sample_count=0` regardless of
        whether the judge actually ran 0 times or ran 100 times with all
        malformed output.
      - Post-fix denominator is **all** candidate_lineage rows in the window.
        Missing / malformed / unparseable / out-of-range payloads count toward
        `missing_count`; valid payloads contribute their `1 - agreement_rate`.
      - We return both signals so the orchestrator can emit independent
        violations for `vlm_disagreement_rate` and `vlm_judge_missing_rate`.

    Return keys:
      - total_rows:        full lineage row count in window
      - parsed_count:      rows w/ valid agreement_rate ∈ [0,1] (disagreement
                           denominator)
      - missing_count:     rows lacking usable agreement_rate
      - missing_rate:      missing_count / total_rows
      - mean_disagreement: avg(1 - agreement_rate) across parsed rows
      - sample_count:      alias of parsed_count, kept for backward-compatible
                           sample_size aggregation in evaluate_window
    """
    rows = conn.execute(
        """
        SELECT vlm_judge_result_json
        FROM candidate_lineage
        WHERE julianday(created_at) >= julianday(?)
        """,
        (cutoff_iso,),
    ).fetchall()
    total_rows = len(rows)
    disagreements: list[float] = []
    missing_count = 0
    for row in rows:
        raw = row["vlm_judge_result_json"]
        if not raw:
            missing_count += 1
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            missing_count += 1
            continue
        if not isinstance(payload, dict):
            missing_count += 1
            continue
        agreement = payload.get("agreement_rate")
        if agreement is None:
            missing_count += 1
            continue
        try:
            agreement_f = float(agreement)
        except (TypeError, ValueError):
            missing_count += 1
            continue
        if agreement_f < 0 or agreement_f > 1:
            missing_count += 1
            continue
        disagreements.append(1.0 - agreement_f)

    parsed_count = len(disagreements)
    mean_disagreement = (
        round(sum(disagreements) / parsed_count, 6) if parsed_count > 0 else 0.0
    )
    missing_rate = round(missing_count / total_rows, 6) if total_rows > 0 else 0.0
    return {
        "total_rows": total_rows,
        "parsed_count": parsed_count,
        "missing_count": missing_count,
        "missing_rate": missing_rate,
        "mean_disagreement": mean_disagreement,
        # Backward-compat alias used by sample_size aggregation. We deliberately
        # alias to total_rows (not parsed_count) — the lineage existence itself
        # is signal, and missing rows are now a first-class dimension.
        "sample_count": total_rows,
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
        WHERE julianday(created_at) >= julianday(?)
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
        WHERE julianday(created_at) >= julianday(?)
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
# Wave 4 W4-2 — paused state sidecar (file-based, atomic, optional path inject)
# ---------------------------------------------------------------------------


def _load_paused_state(path: Path | None = None) -> dict[str, Any] | None:
    """Read sidecar state file. Returns None if file missing or unreadable —
    callers treat None as "no prior paused window" and write fresh state.

    Defensive on JSON decode / schema drift: any error returns None (next
    write replaces the bad payload). We never crash the SLO loop on a
    corrupt sidecar — that would defeat the stop-loss purpose.
    """
    target = Path(path) if path else _DEFAULT_PAUSED_STATE_FILE
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _logger.warning("paused-state read failed (%s); treating as no state", exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def _write_paused_state(payload: dict[str, Any], path: Path | None = None) -> None:
    """Atomic write (tmp + os.replace) so concurrent SLO runs never read a
    half-flushed payload. mkdir parents to keep the seed path usable on
    fresh checkouts."""
    target = Path(path) if path else _DEFAULT_PAUSED_STATE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(serialized, encoding="utf-8")
    os.replace(tmp, target)


def _clear_paused_state(path: Path | None = None) -> None:
    """Remove sidecar when paused window resolves (sample recovered) or the
    promoted state transitions (counter must reset). Silent on missing file."""
    target = Path(path) if path else _DEFAULT_PAUSED_STATE_FILE
    try:
        target.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:  # pragma: no cover  (best-effort cleanup)
        _logger.warning("paused-state clear failed at %s: %s", target, exc)


def _evaluate_paused_state(
    *,
    promotion_state: str,
    sample_size: int,
    min_sample: int,
    paused_state_path: Path | None,
    stale_days: int = PAUSED_STALE_DAYS,
    now: datetime | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Apply Wave 4 W4-2 stop-loss policy. Returns ``(violation, notes_extra)``:

    * ``violation`` is non-None when the paused window has overstayed
      ``stale_days`` under the same promoted state — caller appends it to the
      report violations and escalates the recommendation to ``rollback``.
    * Always (re)writes / clears the sidecar so the next eval inherits a
      correct cursor.
    """
    current = now or datetime.now(timezone.utc)
    existing = _load_paused_state(paused_state_path)

    fresh_state: dict[str, Any] = {
        "schema_version": _PAUSED_STATE_SCHEMA_VERSION,
        "paused_since": current.isoformat(),
        "promotion_state_at_pause": promotion_state,
        "last_sample_size": int(sample_size),
        "minimum_sample_size": int(min_sample),
    }

    if existing is None:
        # First time we observe paused + small-sample under this state.
        _write_paused_state(fresh_state, paused_state_path)
        return None, f"paused_since={fresh_state['paused_since']}"

    prior_state_name = existing.get("promotion_state_at_pause")
    if prior_state_name != promotion_state:
        # Promotion bucket changed (e.g. p10 → p25); reset the timer so we
        # only stop-loss when the operator leaves a single state stuck.
        _write_paused_state(fresh_state, paused_state_path)
        return None, (
            f"paused_state_reset (prior={prior_state_name}, "
            f"current={promotion_state})"
        )

    paused_since_raw = existing.get("paused_since")
    if not isinstance(paused_since_raw, str) or not paused_since_raw:
        # Corrupt sidecar — rewrite with fresh anchor.
        _write_paused_state(fresh_state, paused_state_path)
        return None, "paused_state_repaired"

    try:
        paused_since = datetime.fromisoformat(paused_since_raw)
    except ValueError:
        _write_paused_state(fresh_state, paused_state_path)
        return None, "paused_state_repaired"
    if paused_since.tzinfo is None:
        paused_since = paused_since.replace(tzinfo=timezone.utc)

    duration = current - paused_since
    duration_days = round(duration.total_seconds() / 86400.0, 4)
    # Mutate existing payload to keep paused_since anchor but refresh
    # last_sample_size + minimum_sample_size (operator can audit recent obs).
    existing_update: dict[str, Any] = dict(existing)
    existing_update["last_sample_size"] = int(sample_size)
    existing_update["minimum_sample_size"] = int(min_sample)
    _write_paused_state(existing_update, paused_state_path)

    if duration > timedelta(days=stale_days):
        violation = {
            "dimension": "monitoring_paused_stale",
            "actual_days": duration_days,
            "threshold_days": stale_days,
            "comparator": "<=",
            "context": {
                "paused_since": paused_since_raw,
                "promotion_state": promotion_state,
                "last_sample_size": int(sample_size),
                "minimum_sample_size": int(min_sample),
            },
        }
        return violation, (
            f"paused_since={paused_since_raw}, "
            f"paused_duration_days={duration_days}"
        )

    return None, (
        f"paused_since={paused_since_raw}, "
        f"paused_duration_days={duration_days}"
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def evaluate_window(
    window_hours: int = DEFAULT_WINDOW_HOURS,
    *,
    conn: sqlite3.Connection | None = None,
    thresholds: dict[str, Any] | None = None,
    promotion_state: str | None = None,
    paused_state_path: Path | None = None,
) -> SLOReport:
    """Compute SLO report for the past `window_hours`. Returns immutable
    SLOReport DTO. If `conn` is None we open one from `backend.db`.

    Sample size = sum of all evidence denominators across dimensions; if
    < minimum_sample_size we short-circuit (Wave 3 C-1):
      - promotion_state ∈ {p10/p25/p50/p100} → `monitoring_paused`
        (within_slo=None, do NOT call this insufficient because there IS
        live promoted traffic — we just need more of it before deciding)
      - promotion_state ∈ {shadow, rolled_back, unknown} → `insufficient_data`
        (legacy semantics — no promoted traffic yet, normal cold-start case)

    `promotion_state` is read from
    `backend.services.promotion_manifest_loader.get_promotion_state()` unless
    explicitly overridden via the kwarg (tests / dry-run / multi-env).
    """
    if window_hours <= 0:
        raise ValueError("window_hours must be > 0")

    config = _merge_thresholds(thresholds)
    th = config["thresholds"]
    bl = config["baseline"]
    provenance: dict[str, Any] = dict(config.get("baseline_provenance") or {})
    min_sample = int(config["minimum_sample_size"])

    cutoff = _cutoff_iso(window_hours)

    # Wave 3 C-1: resolve promotion_state once at the top so it's available
    # both for the small-sample triage AND for evidence echo. Import is local
    # to keep the service importable without touching the manifest module at
    # import time.
    if promotion_state is None:
        try:
            from . import promotion_manifest_loader as _pml

            promotion_state = _pml.get_promotion_state()
        except Exception:  # pragma: no cover  — defensive; loader is fail-closed by design
            promotion_state = "shadow"
    resolved_state = str(promotion_state or "shadow")

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

    stale_violation = _check_baseline_stale(provenance)

    evidence = {
        "comfyui_failure": comfyui_evidence,
        "vlm_disagreement": vlm_evidence,
        "delivery_gate_rejection": delivery_evidence,
        "pre_render_gate_blocker": gate_evidence,
        "cutoff_iso": cutoff,
        "thresholds": th,
        "baseline": bl,
        "baseline_provenance": provenance,
        "minimum_sample_size": min_sample,
        "promotion_state": resolved_state,
    }

    if sample_size < min_sample:
        # Wave 3 C-1: triage on promotion_state
        if resolved_state in _PROMOTED_STATES:
            # Wave 4 W4-2 — apply paused-state stop-loss before returning.
            stop_loss_violation, paused_notes = _evaluate_paused_state(
                promotion_state=resolved_state,
                sample_size=sample_size,
                min_sample=min_sample,
                paused_state_path=paused_state_path,
            )
            notes_parts = ["small_sample_in_promoted_state"]
            if paused_notes:
                notes_parts.append(paused_notes)
            if stop_loss_violation is not None:
                # Overstayed paused window → escalate to rollback rec, surface
                # the violation. within_slo=False so downstream applier wires
                # this into the rollback signal.
                return SLOReport(
                    within_slo=False,
                    violations=[stop_loss_violation],
                    evidence=evidence,
                    recommendation=RECOMMENDATION_ROLLBACK,
                    window_hours=window_hours,
                    sample_size=sample_size,
                    notes="; ".join(notes_parts),
                )
            return SLOReport(
                within_slo=None,
                violations=[],
                evidence=evidence,
                recommendation=RECOMMENDATION_MONITORING_PAUSED,
                window_hours=window_hours,
                sample_size=sample_size,
                notes="; ".join(notes_parts),
            )
        # shadow / rolled_back / fail-closed default — cold start, no live
        # promoted traffic yet. Legacy semantics: pass-through + flag.
        # If we previously had a paused-state file (e.g. operator demoted
        # back to shadow), clear it so a future re-promotion starts fresh.
        _clear_paused_state(paused_state_path)
        return SLOReport(
            within_slo=True,
            violations=[],
            evidence=evidence,
            recommendation=RECOMMENDATION_INSUFFICIENT_DATA,
            window_hours=window_hours,
            sample_size=sample_size,
        )

    # Sample recovered — paused window resolved. Clear the sidecar so the
    # next paused episode (if any) starts its 7-day stop-loss clock fresh.
    _clear_paused_state(paused_state_path)

    violations: list[dict[str, Any]] = []

    # Wave 4 W4-1 (b): baseline_unmeasured — fires whenever the loaded
    # provenance is empty OR computed_by ∈ placeholder set. Independent of
    # baseline_stale (which keys on measured_at age); this one keys on
    # "did anyone actually measure this?". Even in SLO_TEST_MODE where (a)
    # demotes the load-time check, this violation makes the placeholder
    # state visible in every report — operators can't silently deploy onto
    # seed values.
    placeholder_computed_by: str | None = None
    prov_computed_by = provenance.get("computed_by") if provenance else None
    if isinstance(prov_computed_by, str) and prov_computed_by in _PLACEHOLDER_COMPUTED_BY:
        placeholder_computed_by = prov_computed_by
    if not provenance or placeholder_computed_by is not None:
        violations.append(
            {
                "dimension": "baseline_unmeasured",
                "comparator": "==",
                "context": {
                    "computed_by": placeholder_computed_by
                    or (provenance.get("computed_by") if provenance else None),
                    "sample_size": provenance.get("sample_size") if provenance else None,
                    "hint": (
                        "run `python -m backend.scripts.calibrate_slo_baseline "
                        "--window <N> --apply` before production deploy"
                    ),
                },
            }
        )

    if stale_violation is not None:
        violations.append(stale_violation)

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

    # 2) vlm disagreement rate — Wave 3 C-3: denominator is `parsed_count`
    # (rows with usable agreement_rate). When all rows are missing, parsed_count
    # is 0 and we don't fire a disagreement violation (missing_rate covers it).
    vd_max = float(th["vlm_disagreement_rate_max"])
    if vlm_evidence["parsed_count"] > 0 and vlm_evidence["mean_disagreement"] > vd_max:
        violations.append(
            {
                "dimension": "vlm_disagreement_rate",
                "actual": vlm_evidence["mean_disagreement"],
                "threshold": vd_max,
                "comparator": "<=",
                "context": vlm_evidence,
            }
        )

    # 2b) Wave 3 C-3: vlm_judge_missing_rate — fires when >= threshold of
    # candidate_lineage rows are missing or malformed VLM judge output. The
    # threshold is read from the thresholds map with a hardcoded fallback so
    # that older slo_thresholds.json files (pre-Agent-C reify) still work.
    vmr_max = float(th.get("vlm_judge_missing_rate_max", 0.10))
    if vlm_evidence["total_rows"] > 0 and vlm_evidence["missing_rate"] > vmr_max:
        violations.append(
            {
                "dimension": "vlm_judge_missing_rate",
                "actual": vlm_evidence["missing_rate"],
                "threshold": vmr_max,
                "comparator": "<=",
                "context": {
                    "missing_count": vlm_evidence["missing_count"],
                    "total_rows": vlm_evidence["total_rows"],
                    "parsed_count": vlm_evidence["parsed_count"],
                },
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
    recommendation = RECOMMENDATION_CONTINUE if within else RECOMMENDATION_ROLLBACK
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
    "BASELINE_STALE_DAYS",
    "PAUSED_STALE_DAYS",
    "DEFAULT_WINDOW_HOURS",
    "DEFAULT_MINIMUM_SAMPLE_SIZE",
    "RECOMMENDATION_CONTINUE",
    "RECOMMENDATION_ROLLBACK",
    "RECOMMENDATION_INSUFFICIENT_DATA",
    "RECOMMENDATION_MONITORING_PAUSED",
]


if __name__ == "__main__":  # pragma: no cover
    # Smoke print — used during dev. Real CLI is `backend.scripts.promotion_slo_check`.
    report = evaluate_window()
    json.dump(report.to_dict(), sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
