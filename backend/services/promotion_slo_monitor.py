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

import contextlib
import json
import logging
import os
import sqlite3
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

try:  # POSIX-only; macOS / Linux are the primary targets. Windows: degrade no-op.
    import fcntl  # type: ignore[import-not-found]

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover  (non-POSIX)
    fcntl = None  # type: ignore[assignment]
    _HAS_FCNTL = False

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
# Wave 4 K-5: 灰度长时间 stop-loss escalate — 不真撤回灰度（语义是"流量不足
# 无法判定 SLO"），让 applier 写 audit alert 但保 manifest 不变；这与
# `RECOMMENDATION_ROLLBACK`（SLO 真违反 → applier 撤回）严格区分。
RECOMMENDATION_STOP_LOSS_HALT = "stop_loss_halt"

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

# Wave 4 K-3 — explicit truthy allowlist for SLO_TEST_MODE. Pre-K-3 the check
# was `value not in ("", "0", "false", "False")` which silently enabled
# test-mode on ANY other value — e.g. `SLO_TEST_MODE=production`,
# `SLO_TEST_MODE=off`, `SLO_TEST_MODE=disable` all flipped fail-closed off.
# Strict whitelist closes that footgun: only the strings below count as
# "enabled", everything else (including unknown literals, whitespace, empty,
# typos) is treated as production mode.
_TEST_MODE_TRUTHY: frozenset[str] = frozenset(
    {"1", "true", "True", "yes", "Yes", "TRUE", "YES"}
)

# Wave 4 W4-1 — production deploy gate (release blocker).
# `computed_by` values that signal a placeholder / pre-calibration baseline.
# Production must refuse to load such thresholds (fail-closed) and any in-flight
# evaluation must surface `baseline_unmeasured` as an explicit violation so
# operators see it even if test-mode demotion silences the load-time check.
_PLACEHOLDER_COMPUTED_BY: frozenset[str] = frozenset(
    {"manual_seed", "placeholder", "seed"}
)
_PLACEHOLDER_SAMPLE_SIZE_THRESHOLD = 1  # sample_size < 1 == zero observed runs

# Wave 4 K-4 — producer allowlist. Only `computed_by` values listed here may
# load thresholds in production. Future calibration v2/v3 algorithms append
# their own labels here; unknown labels are rejected by name so a typo or
# rogue producer can't sneak past `_PLACEHOLDER_COMPUTED_BY` (which only
# rejects three explicit seed labels).
_LEGITIMATE_COMPUTED_BY: frozenset[str] = frozenset({"calibrate_cli"})

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
    """Permissive mode for tests / dev. Production keeps fail-closed semantics.

    Wave 4 K-3: STRICT whitelist — only the canonical truthy literals enable
    test mode. Any other value (incl. ``"production"``, ``"off"``, ``"2"``,
    ``" "``, ``"disable"``) is treated as production. This closes a footgun
    where operators thought they were turning test-mode off by writing
    ``SLO_TEST_MODE=production`` and instead silently enabled it.
    """
    return os.environ.get(_SLO_TEST_MODE_ENV, "").strip() in _TEST_MODE_TRUTHY


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

    # Wave 4 K-4 — producer allowlist. ``computed_by`` outside the union of
    # legitimate (``_LEGITIMATE_COMPUTED_BY``) and explicit-placeholder
    # (``_PLACEHOLDER_COMPUTED_BY``) sets means an unknown / rogue / typo
    # producer wrote this thresholds file. Fail-closed in production. We do
    # NOT silently accept on the assumption "if it has full provenance it's
    # fine" — a typo'd `computed_by="ops_seed"` would otherwise bypass the
    # placeholder gate (since "ops_seed" is not in the placeholder set) AND
    # still load with full schema. K-4 forces an explicit allowlist update
    # for every new producer.
    if (
        isinstance(computed_by, str)
        and computed_by not in _LEGITIMATE_COMPUTED_BY
        and computed_by not in _PLACEHOLDER_COMPUTED_BY
    ):
        msg = (
            f"unknown computed_by '{computed_by}' — must be one of "
            f"{sorted(_LEGITIMATE_COMPUTED_BY)}. If you added a new "
            "calibration producer, append its label to "
            "promotion_slo_monitor._LEGITIMATE_COMPUTED_BY."
        )
        if _is_test_mode():
            _logger.warning(msg)
            return dict(provenance)
        raise ValueError(msg)

    return dict(provenance)


def _validate_positive_int(
    raw_value: Any,
    *,
    default: int,
    field_name: str,
) -> int:
    """Wave 5 followup #2 (resolves followup-1 I-1): shared validator for the
    handful of positive-int JSON fields under ``slo_thresholds.json``:
    ``paused_stale_days`` (Wave 5 #1), ``baseline_stale_days`` (Wave 5 #2,
    eval-M1 sibling), and ``minimum_sample_size`` (pre-Wave 5, upgraded from
    bare ``int()`` cast to match the other two for defense-in-depth).

    Contract:
      * Absent (``None``) → return ``default`` (BC: legacy JSON files that
        predate any given field still load successfully without ceremony).
      * ``bool`` → always invalid (``True``/``False`` are technically ``int``
        subclasses in CPython but operator intent for a count-of-days /
        sample-size is nonsensical; a JSON literal ``true`` would silently
        parse as 1, ``false`` as 0 — both anti-patterns the explicit check
        closes).
      * Non-int type (string / float / list / dict / …) → invalid.
      * Value ``<= 0`` → invalid (zero days = fire on first observation,
        zero sample size = no minimum protection at all; neither matches
        operator intent).

    Mode dispatch (mirrors ``_validate_baseline_provenance``):
      * Production (``SLO_TEST_MODE`` unset / not in
        :data:`_TEST_MODE_TRUTHY`): invalid → ``ValueError`` (fail-closed).
      * Test (``SLO_TEST_MODE=1``): invalid → log warning + return
        ``default`` (escape hatch so legacy fixtures / placeholder JSONs
        load).

    ``field_name`` is included in error messages + log records so a caller
    using this helper for multiple fields can grep / regex on the canonical
    name (e.g. ``paused_stale_days.*must be positive int``).
    """
    if raw_value is None:
        return default
    # ``bool`` check MUST precede the ``int`` check — Python's ``True``/``False``
    # satisfy ``isinstance(x, int)`` so a naked int check would silently accept
    # the bool and produce a paused_stale_days=1 / minimum_sample_size=0 footgun.
    if isinstance(raw_value, bool):
        msg = (
            f"invalid {field_name}: must be positive int, "
            f"got {raw_value!r} ({type(raw_value).__name__})"
        )
        if _is_test_mode():
            _logger.warning("%s; falling back to default %d", msg, default)
            return default
        raise ValueError(msg)
    if not isinstance(raw_value, int) or raw_value <= 0:
        msg = (
            f"invalid {field_name}: must be positive int, "
            f"got {raw_value!r} ({type(raw_value).__name__})"
        )
        if _is_test_mode():
            _logger.warning("%s; falling back to default %d", msg, default)
            return default
        raise ValueError(msg)
    return int(raw_value)


def _validate_paused_stale_days(raw_value: Any) -> int:
    """Wave 5 followup #1 (eval-M1): validate the optional `paused_stale_days`
    JSON field. Thin wrapper over :func:`_validate_positive_int` preserved for
    BC / readability (call sites read more naturally as
    ``_validate_paused_stale_days(...)`` than the generic helper).
    """
    return _validate_positive_int(
        raw_value,
        default=PAUSED_STALE_DAYS,
        field_name="paused_stale_days",
    )


def _validate_baseline_stale_days(raw_value: Any) -> int:
    """Wave 5 followup #2 (eval-M1 sibling): validate the optional
    ``baseline_stale_days`` JSON field. Same contract as
    :func:`_validate_paused_stale_days` — thin wrapper over
    :func:`_validate_positive_int` for readable call sites.

    Promotes ``BASELINE_STALE_DAYS = 60`` from a hard-coded module constant to
    an operator-tunable JSON field so the "baseline calibration is too old"
    threshold can be raised / lowered per environment without code edit +
    redeploy.
    """
    return _validate_positive_int(
        raw_value,
        default=BASELINE_STALE_DAYS,
        field_name="baseline_stale_days",
    )


def _validate_minimum_sample_size(raw_value: Any) -> int:
    """Wave 5 followup #2 (resolves I-1 BC consolidation): the
    ``minimum_sample_size`` field has existed since Wave 3 but was loaded with
    a bare ``int()`` cast which silently accepted ``bool`` (``True`` → 1,
    ``False`` → 0), negatives, and other anti-patterns. This wrapper aligns
    its validation depth with :data:`PAUSED_STALE_DAYS` /
    :data:`BASELINE_STALE_DAYS` so the same fail-closed / test-mode-fallback
    semantics apply across all three positive-int JSON fields.

    BC: the module-level :data:`DEFAULT_MINIMUM_SAMPLE_SIZE` (=30) remains the
    fallback default for absent / test-mode-rejected values.
    """
    return _validate_positive_int(
        raw_value,
        default=DEFAULT_MINIMUM_SAMPLE_SIZE,
        field_name="minimum_sample_size",
    )


def load_default_thresholds(path: Path | None = None) -> dict[str, Any]:
    """Load thresholds JSON. Returns the full structure (thresholds + baseline +
    baseline_provenance + sample size + window + paused_stale_days +
    baseline_stale_days). Raises ValueError if file is malformed or
    `baseline_provenance` is missing/invalid in production mode.

    Wave 5 followup #2: ``baseline_stale_days`` is now JSON-tunable (parallel
    to ``paused_stale_days`` introduced in followup #1). ``minimum_sample_size``
    validation depth was upgraded to match (bool / negative / non-int now
    rejected in prod instead of silently coerced).
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
            "paused_stale_days": PAUSED_STALE_DAYS,
            "baseline_stale_days": BASELINE_STALE_DAYS,
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
        "minimum_sample_size": _validate_minimum_sample_size(
            raw.get("minimum_sample_size")
        ),
        "default_window_hours": int(
            raw.get("default_window_hours", DEFAULT_WINDOW_HOURS)
        ),
        "paused_stale_days": _validate_paused_stale_days(
            raw.get("paused_stale_days")
        ),
        "baseline_stale_days": _validate_baseline_stale_days(
            raw.get("baseline_stale_days")
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
    callers supply a full override (e.g. tests, ad-hoc CLI sessions).

    Wave 5 followup #2: includes ``baseline_stale_days`` parallel to
    ``paused_stale_days``.
    """
    return {
        "thresholds": dict(_DEFAULT_THRESHOLDS),
        "baseline": dict(_DEFAULT_BASELINE),
        "baseline_provenance": {},
        "minimum_sample_size": DEFAULT_MINIMUM_SAMPLE_SIZE,
        "default_window_hours": DEFAULT_WINDOW_HOURS,
        "paused_stale_days": PAUSED_STALE_DAYS,
        "baseline_stale_days": BASELINE_STALE_DAYS,
    }


def _merge_thresholds(
    user_thresholds: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge user override on top of file defaults. user_thresholds may carry the
    full structure (`{thresholds, baseline, baseline_provenance, ...}`) OR a
    flat map of threshold names (treated as `thresholds` overlay).

    Wave 4 K-2 — fallback contract (post-hardening):
      The on-disk thresholds file may be rejected at load time (W4-1
      placeholder gate or K-4 unknown-producer gate). In that case we fall
      back to `_code_default_thresholds()` ONLY when one of the two safety
      conditions holds:

        (a) `_is_test_mode()` is True (developer / CI escape hatch), or
        (b) The caller has supplied a structured override whose own
            `baseline_provenance` passes `_validate_baseline_provenance`
            (i.e. the caller is taking explicit responsibility for the
            measured baseline; production calibrate CLI's own sanity-load
            uses this branch).

      Any other situation — production mode + user override with no
      provenance (or with placeholder provenance, or with a flat
      thresholds-only override) — re-raises the original ValueError so the
      file rejection is propagated. This protects against a footgun where
      a caller could silently bypass W4-1 by passing a single threshold
      override that happens to skip the fallback condition.
    """
    try:
        base = load_default_thresholds()
    except ValueError:
        # Decide whether fallback is safe:
        if _is_test_mode():
            base = _code_default_thresholds()
        elif user_thresholds and isinstance(
            user_thresholds.get("baseline_provenance"), dict
        ):
            # Caller-supplied provenance — validate before allowing fallback.
            try:
                _validate_baseline_provenance(
                    user_thresholds.get("baseline_provenance")
                )
            except ValueError:
                raise  # caller's provenance is itself bad — propagate
            base = _code_default_thresholds()
        else:
            # No safe path — fall through to the original file rejection.
            raise
    if not user_thresholds:
        return base
    if "thresholds" in user_thresholds or "baseline" in user_thresholds:
        # Full structured override
        overlay_provenance = user_thresholds.get("baseline_provenance")
        # Wave 5 followup #1+#2: positive-int field propagation. If the caller
        # supplied a field explicitly, validate (same rules as JSON load) and
        # use the override; otherwise inherit base. Flat overrides (else branch
        # below) just `**base` so they inherit automatically.
        if "paused_stale_days" in user_thresholds:
            paused_stale_days = _validate_paused_stale_days(
                user_thresholds.get("paused_stale_days")
            )
        else:
            paused_stale_days = int(
                base.get("paused_stale_days", PAUSED_STALE_DAYS)
            )
        if "baseline_stale_days" in user_thresholds:
            baseline_stale_days = _validate_baseline_stale_days(
                user_thresholds.get("baseline_stale_days")
            )
        else:
            baseline_stale_days = int(
                base.get("baseline_stale_days", BASELINE_STALE_DAYS)
            )
        # Wave 5 followup #2: ``minimum_sample_size`` also goes through the
        # shared validator so a bool / negative override is rejected with the
        # same fail-closed semantics as the JSON load path.
        if "minimum_sample_size" in user_thresholds:
            minimum_sample_size = _validate_minimum_sample_size(
                user_thresholds.get("minimum_sample_size")
            )
        else:
            minimum_sample_size = int(base["minimum_sample_size"])
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
            "minimum_sample_size": minimum_sample_size,
            "default_window_hours": int(
                user_thresholds.get("default_window_hours", base["default_window_hours"])
            ),
            "paused_stale_days": paused_stale_days,
            "baseline_stale_days": baseline_stale_days,
        }
    # Flat override → assumed thresholds-only. ``**base`` propagates
    # ``paused_stale_days`` / ``baseline_stale_days`` / ``minimum_sample_size``
    # / ``default_window_hours`` automatically.
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


# K-6 status tokens for `_load_paused_state` tri-state return. Pre-K-6 the
# loader collapsed missing / corrupt / unreadable to a single `None`, which
# meant a transient OSError (permission flip / disk hiccup) silently reset
# the stop-loss clock — operator could never see the 7-day window elapse.
# Post-K-6 we surface the cause so callers can branch.
PAUSED_STATE_STATUS_OK = "ok"
PAUSED_STATE_STATUS_MISSING = "missing"
PAUSED_STATE_STATUS_CORRUPT = "corrupt"
PAUSED_STATE_STATUS_UNREADABLE = "unreadable"


@contextlib.contextmanager
def _paused_state_lock(target_path: Path) -> Iterator[int | None]:
    """K-1 — exclusive non-blocking flock around sidecar read-modify-write.

    Pre-K-1 the sidecar suffered two race conditions:

      1) Fixed ``<target>.tmp`` filename meant concurrent cron writes could
         overwrite each other's tmp file mid-flight (corrupt payload land in
         the canonical path).
      2) Even with unique tmps, the read-modify-write of the sidecar payload
         (load → mutate → write) was unprotected — two evaluators racing
         could both observe "no paused_state" and both write a fresh
         paused_since=now, losing the older anchor.

    This context manager acquires ``fcntl.flock(LOCK_EX | LOCK_NB)`` on a
    sibling lock file (``<sidecar>.lock``). On Windows (no fcntl) we degrade
    to a no-op — the macOS / Linux deployment targets cover all production
    crons; CI runs serially per-test under the autouse isolation fixture.

    On lock contention we yield ``None`` (caller may skip the write) rather
    than blocking — stop-loss MUST not stall the SLO monitor loop. The
    caller logs and proceeds with evaluation; the other process's write
    will land first and the next eval cycle will reconcile.
    """
    if not _HAS_FCNTL:  # pragma: no cover  (POSIX-only path)
        yield None
        return
    lock_path = Path(str(target_path) + ".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _logger.warning("paused-state lock-dir mkdir failed (%s); proceeding lockless", exc)
        yield None
        return
    fd: int | None = None
    try:
        try:
            fd = os.open(
                str(lock_path), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644
            )
        except OSError as exc:
            _logger.warning("paused-state lock open failed (%s); proceeding lockless", exc)
            yield None
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[union-attr]
        except BlockingIOError:
            _logger.warning(
                "paused-state lock held by another process at %s; "
                "skipping this write cycle",
                lock_path,
            )
            yield None
            return
        yield fd
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[union-attr]
            except OSError:  # pragma: no cover
                _logger.warning("flock LOCK_UN failed (non-fatal)", exc_info=True)
            try:
                os.close(fd)
            except OSError:  # pragma: no cover
                _logger.warning("paused-state lock fd close failed (non-fatal)", exc_info=True)


def _load_paused_state(
    path: Path | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """K-6: tri-state read.

    Returns ``(payload, status)``:
      * ``(None, "missing")``     — file does not exist (first paused episode)
      * ``(None, "corrupt")``     — JSON parse failed or root is not a dict
      * ``(None, "unreadable")``  — OSError (permission / I/O); caller MUST
                                    NOT treat this as "no prior pause" or
                                    the stop-loss clock would be reset by a
                                    transient disk error.
      * ``(payload, "ok")``       — valid sidecar payload

    Pre-K-6 all four cases returned ``None``, meaning a transient unreadable
    sidecar silently rewrote a fresh ``paused_since=now`` anchor — the
    7-day stop-loss timer was effectively infinitely deferred on any
    permission flip.
    """
    target = Path(path) if path else _DEFAULT_PAUSED_STATE_FILE
    if not target.exists():
        return None, PAUSED_STATE_STATUS_MISSING
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        _logger.warning(
            "paused-state unreadable at %s (%s); preserving prior anchor "
            "(NOT resetting stop-loss clock)",
            target,
            exc,
        )
        return None, PAUSED_STATE_STATUS_UNREADABLE
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        _logger.warning(
            "paused-state corrupt at %s (%s); will rewrite fresh anchor",
            target,
            exc,
        )
        return None, PAUSED_STATE_STATUS_CORRUPT
    if not isinstance(data, dict):
        _logger.warning(
            "paused-state malformed at %s (root not object); "
            "will rewrite fresh anchor",
            target,
        )
        return None, PAUSED_STATE_STATUS_CORRUPT
    return data, PAUSED_STATE_STATUS_OK


def _write_paused_state(payload: dict[str, Any], path: Path | None = None) -> None:
    """Atomic write (unique tmp + os.replace) so concurrent SLO runs never
    read a half-flushed payload AND never collide on the tmp filename.

    K-1 fix: tmp name now embeds pid + uuid so two parallel cron invocations
    each write to their own tmp file (no in-flight overwrite). The lock
    held by ``_paused_state_lock`` serializes the os.replace landing —
    callers MUST hold the lock when calling this function.
    """
    target = Path(path) if path else _DEFAULT_PAUSED_STATE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = target.name + f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    tmp = target.parent / tmp_name
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        tmp.write_text(serialized, encoding="utf-8")
        os.replace(tmp, target)
    except Exception:
        # Best-effort cleanup of the tmp; never mask the original error.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:  # pragma: no cover
            pass
        raise


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
      report violations and escalates the recommendation to
      :data:`RECOMMENDATION_STOP_LOSS_HALT` (K-5).
    * Always (re)writes / clears the sidecar so the next eval inherits a
      correct cursor. K-1: all sidecar mutations happen under exclusive
      flock so concurrent crons can't lose the paused_since anchor.
    * K-6: ``OSError`` during sidecar read no longer silently resets the
      stop-loss clock; we surface ``paused_state_unreadable`` and treat it
      as a synthetic stale violation (conservative — operator must
      investigate before deploy).
    """
    current = now or datetime.now(timezone.utc)
    target = Path(paused_state_path) if paused_state_path else _DEFAULT_PAUSED_STATE_FILE

    fresh_state: dict[str, Any] = {
        "schema_version": _PAUSED_STATE_SCHEMA_VERSION,
        "paused_since": current.isoformat(),
        "promotion_state_at_pause": promotion_state,
        "last_sample_size": int(sample_size),
        "minimum_sample_size": int(min_sample),
    }

    with _paused_state_lock(target) as lock_fd:
        existing, status = _load_paused_state(target)

        # K-6: unreadable sidecar — preserve the stop-loss clock. Surface a
        # synthetic violation so the operator sees we can't read the
        # canonical anchor; conservative (treat as if paused window
        # exceeded) so we don't silently green-light a stale promotion.
        if status == PAUSED_STATE_STATUS_UNREADABLE:
            violation = {
                "dimension": "paused_state_unreadable",
                "actual_days": None,
                "threshold_days": stale_days,
                "comparator": "<=",
                "context": {
                    "paused_state_path": str(target),
                    "promotion_state": promotion_state,
                    "last_sample_size": int(sample_size),
                    "minimum_sample_size": int(min_sample),
                    "hint": (
                        "could not read sidecar file (OSError); verify "
                        "permissions on promotion/ — refusing to reset the "
                        "stop-loss clock until operator confirms"
                    ),
                },
            }
            return violation, "paused_state_unreadable"

        if status == PAUSED_STATE_STATUS_MISSING:
            # First time we observe paused + small-sample under this state.
            if lock_fd is None:
                # Contention: another process is currently writing. Skip this
                # cycle's write — the holder will land a fresh anchor and the
                # next eval will reconcile.
                return None, "paused_state_lock_contention"
            _write_paused_state(fresh_state, target)
            return None, f"paused_since={fresh_state['paused_since']}"

        if status == PAUSED_STATE_STATUS_CORRUPT:
            # Corrupt sidecar — rewrite with fresh anchor + diagnostic note.
            if lock_fd is None:
                return None, "paused_state_lock_contention"
            _write_paused_state(fresh_state, target)
            return None, "paused_state_repaired"

        # status == OK — proceed with anchor reconciliation
        assert existing is not None

        prior_state_name = existing.get("promotion_state_at_pause")
        if prior_state_name != promotion_state:
            # Promotion bucket changed (e.g. p10 → p25); reset the timer so we
            # only stop-loss when the operator leaves a single state stuck.
            if lock_fd is None:
                return None, "paused_state_lock_contention"
            _write_paused_state(fresh_state, target)
            return None, (
                f"paused_state_reset (prior={prior_state_name}, "
                f"current={promotion_state})"
            )

        paused_since_raw = existing.get("paused_since")
        if not isinstance(paused_since_raw, str) or not paused_since_raw:
            # Schema drift on a critical field — rewrite anchor.
            if lock_fd is None:
                return None, "paused_state_lock_contention"
            _write_paused_state(fresh_state, target)
            return None, "paused_state_repaired"

        try:
            paused_since = datetime.fromisoformat(paused_since_raw)
        except ValueError:
            if lock_fd is None:
                return None, "paused_state_lock_contention"
            _write_paused_state(fresh_state, target)
            return None, "paused_state_repaired"
        if paused_since.tzinfo is None:
            paused_since = paused_since.replace(tzinfo=timezone.utc)

        duration = current - paused_since
        duration_days = round(duration.total_seconds() / 86400.0, 4)
        # Mutate existing payload to keep paused_since anchor but refresh
        # last_sample_size + minimum_sample_size (operator can audit recent obs).
        if lock_fd is not None:
            existing_update: dict[str, Any] = dict(existing)
            existing_update["last_sample_size"] = int(sample_size)
            existing_update["minimum_sample_size"] = int(min_sample)
            _write_paused_state(existing_update, target)

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
    # Wave 5 followup #1 (eval-M1): configurable stop-loss window. Fallback to
    # module constant when the merge result is missing the key (shouldn't
    # happen post-Wave5, but defensive in case of cross-version thresholds
    # injection from older callers).
    paused_stale_days = int(
        config.get("paused_stale_days", PAUSED_STALE_DAYS)
    )
    # Wave 5 followup #2 (eval-M1 sibling): configurable baseline-stale window.
    # Same defensive fallback for cross-version threshold dicts that predate
    # the field. Read at top so it's available for both the `_check_baseline_stale`
    # call below AND the evidence echo (so operators can confirm config-driven
    # override took effect).
    baseline_stale_days = int(
        config.get("baseline_stale_days", BASELINE_STALE_DAYS)
    )

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

    # Wave 5 followup #2: pass operator-tunable stale_days through. Pre-W5#2
    # the function always used the module constant; now an operator can lower
    # it (e.g. 30 days to force more frequent recalibration in a noisy env) or
    # raise it (e.g. 90 days for a stable pipeline).
    stale_violation = _check_baseline_stale(provenance, stale_days=baseline_stale_days)

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
        # Wave 5 followup #1: surface effective stop-loss window so operators
        # can confirm config-driven override took effect (vs module constant).
        "paused_stale_days": paused_stale_days,
        # Wave 5 followup #2: parallel evidence echo for the baseline-stale
        # window so operators can audit both tunables from a single SLO report.
        "baseline_stale_days": baseline_stale_days,
    }

    if sample_size < min_sample:
        # Wave 3 C-1: triage on promotion_state
        if resolved_state in _PROMOTED_STATES:
            # Wave 4 W4-2 — apply paused-state stop-loss before returning.
            # Wave 5 followup #1 — stale_days is now configurable per JSON.
            stop_loss_violation, paused_notes = _evaluate_paused_state(
                promotion_state=resolved_state,
                sample_size=sample_size,
                min_sample=min_sample,
                paused_state_path=paused_state_path,
                stale_days=paused_stale_days,
            )
            notes_parts = ["small_sample_in_promoted_state"]
            if paused_notes:
                notes_parts.append(paused_notes)
            if stop_loss_violation is not None:
                # K-5: overstayed paused window → emit
                # ``RECOMMENDATION_STOP_LOSS_HALT`` (NOT ``ROLLBACK``). The
                # semantics here are "灰度流量长时间不足 — 无法 SLO 评估"
                # rather than "SLO 真违反"; the applier writes an audit
                # alert + keeps the manifest intact (operator review
                # required). ``within_slo=False`` so the report is
                # actionable; downstream applier branches on the
                # recommendation, not within_slo.
                return SLOReport(
                    within_slo=False,
                    violations=[stop_loss_violation],
                    evidence=evidence,
                    recommendation=RECOMMENDATION_STOP_LOSS_HALT,
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
    else:
        # K-4: legitimate (non-placeholder) provenance but the calibration
        # window itself was undersampled. Surface as an independent
        # violation — operators should re-calibrate with a larger window
        # before trusting the baseline numbers. Parallel to
        # ``baseline_unmeasured`` semantically (both indicate the baseline
        # is not trustworthy) but distinct in cause (this one means "we
        # tried to measure, but didn't see enough traffic").
        prov_sample = provenance.get("sample_size")
        if (
            isinstance(prov_sample, int)
            and not isinstance(prov_sample, bool)
            and prov_sample < min_sample
        ):
            violations.append(
                {
                    "dimension": "baseline_undersampled",
                    "comparator": ">=",
                    "context": {
                        "computed_by": provenance.get("computed_by"),
                        "sample_size": prov_sample,
                        "minimum_sample_size": min_sample,
                        "hint": (
                            "baseline calibrated on too few rows; re-run "
                            "`python -m backend.scripts.calibrate_slo_baseline "
                            "--window <N> --apply` with a larger N"
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
    "RECOMMENDATION_STOP_LOSS_HALT",
]


if __name__ == "__main__":  # pragma: no cover
    # Smoke print — used during dev. Real CLI is `backend.scripts.promotion_slo_check`.
    report = evaluate_window()
    json.dump(report.to_dict(), sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
