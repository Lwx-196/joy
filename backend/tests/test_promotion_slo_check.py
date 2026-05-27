"""Tests for `backend.scripts.promotion_slo_check` CLI rendering layer.

Wave 4 K-7 hardening: pre-K-7 the markdown renderer hard-coded
``v['actual']`` / ``v['threshold']`` and crashed on Wave 4 violations
that use different field names. Post-K-7 the renderer is schema-adaptive
via ``_render_violation_row``.
"""

from __future__ import annotations

from backend.scripts.promotion_slo_check import (
    EXIT_OK,
    EXIT_ROLLBACK,
    EXIT_STOP_LOSS_HALT,
    _exit_code_for,
    _format_markdown,
    _render_violation_row,
)
from backend.services.promotion_slo_monitor import (
    RECOMMENDATION_CONTINUE,
    RECOMMENDATION_ROLLBACK,
    RECOMMENDATION_STOP_LOSS_HALT,
    SLOReport,
)


def _make_report(*, violations: list[dict], recommendation: str = "rollback") -> SLOReport:
    return SLOReport(
        within_slo=False if recommendation == "rollback" else True,
        violations=violations,
        evidence={"comfyui_failure": {"rate": 0.1}},
        recommendation=recommendation,
        window_hours=48,
        sample_size=100,
    )


# K-7 — _render_violation_row schema adaptation -----------------------------


def test_k7_renders_legacy_violation_with_actual_threshold() -> None:
    """K-7: legacy SLO violations (comfyui_failure_rate, vlm_disagreement_rate,
    etc.) use `actual` + `threshold` fields — must render unchanged."""
    v = {
        "dimension": "comfyui_failure_rate",
        "actual": 0.12,
        "threshold": 0.05,
        "comparator": "<=",
    }
    row = _render_violation_row(v)
    assert "comfyui_failure_rate" in row
    assert "actual=0.12" in row
    assert "threshold=0.05" in row


def test_k7_renders_monitoring_paused_stale() -> None:
    """K-7: monitoring_paused_stale uses actual_days / threshold_days —
    must render those fields (not crash on KeyError 'actual')."""
    v = {
        "dimension": "monitoring_paused_stale",
        "actual_days": 8.4,
        "threshold_days": 7,
        "comparator": "<=",
        "context": {"promotion_state": "p10"},
    }
    row = _render_violation_row(v)
    assert "monitoring_paused_stale" in row
    assert "actual=8.4" in row
    assert "threshold=7" in row


def test_k7_renders_baseline_unmeasured_with_na_placeholders() -> None:
    """K-7: baseline_unmeasured has no actual/threshold (categorical
    violation) — must render N/A placeholders, not KeyError."""
    v = {
        "dimension": "baseline_unmeasured",
        "comparator": "==",
        "context": {
            "computed_by": "manual_seed",
            "sample_size": 0,
            "hint": "run calibrate_slo_baseline",
        },
    }
    row = _render_violation_row(v)
    assert "baseline_unmeasured" in row
    assert "N/A" in row  # placeholder for missing actual/threshold
    assert "calibrate_slo_baseline" in row  # hint surfaced


def test_k7_renders_baseline_undersampled() -> None:
    """K-7: baseline_undersampled (K-4 new) has comparator='>=' + no
    actual/threshold top-level fields — must render gracefully."""
    v = {
        "dimension": "baseline_undersampled",
        "comparator": ">=",
        "context": {
            "computed_by": "calibrate_cli",
            "sample_size": 5,
            "minimum_sample_size": 30,
            "hint": "re-run calibrate with larger window",
        },
    }
    row = _render_violation_row(v)
    assert "baseline_undersampled" in row
    assert "N/A" in row
    assert ">=" in row


def test_k7_renders_paused_state_unreadable_with_none_actual() -> None:
    """K-7: K-6's paused_state_unreadable has actual=None — must render
    as N/A, not crash on attribute access."""
    v = {
        "dimension": "paused_state_unreadable",
        "actual_days": None,
        "threshold_days": 7,
        "comparator": "<=",
        "context": {"hint": "verify permissions"},
    }
    row = _render_violation_row(v)
    assert "paused_state_unreadable" in row
    # actual_days is None → renderer treats as N/A
    assert "N/A" in row or "actual=None" in row
    assert "threshold=7" in row


def test_k7_format_markdown_mixed_violations_does_not_crash() -> None:
    """K-7: full markdown render with mixed legacy + Wave 4 violations
    must succeed (regression guard against pre-K-7 KeyError on the first
    non-legacy violation in the list)."""
    report = _make_report(
        violations=[
            {
                "dimension": "comfyui_failure_rate",
                "actual": 0.12,
                "threshold": 0.05,
                "comparator": "<=",
            },
            {
                "dimension": "monitoring_paused_stale",
                "actual_days": 8.4,
                "threshold_days": 7,
                "comparator": "<=",
                "context": {"promotion_state": "p10"},
            },
            {
                "dimension": "baseline_unmeasured",
                "comparator": "==",
                "context": {"computed_by": "manual_seed"},
            },
        ]
    )
    md = _format_markdown(report)
    assert "comfyui_failure_rate" in md
    assert "monitoring_paused_stale" in md
    assert "baseline_unmeasured" in md
    # No KeyError'd "'actual'" in the output
    assert "KeyError" not in md


# K-5 / K-7 — exit code for STOP_LOSS_HALT ----------------------------------


def test_k5_exit_code_stop_loss_halt() -> None:
    """K-5: STOP_LOSS_HALT → distinct exit code 3 (not 1, not 0). Cron
    `|| rollback_runner` chains must NOT trigger on this signal."""
    report = _make_report(violations=[], recommendation=RECOMMENDATION_STOP_LOSS_HALT)
    assert _exit_code_for(report) == EXIT_STOP_LOSS_HALT
    assert EXIT_STOP_LOSS_HALT != EXIT_ROLLBACK
    assert EXIT_STOP_LOSS_HALT != EXIT_OK


def test_k7_exit_code_rollback_still_1() -> None:
    """K-5/K-7: real ROLLBACK still gets exit code 1 (back-compat)."""
    report = _make_report(violations=[], recommendation=RECOMMENDATION_ROLLBACK)
    assert _exit_code_for(report) == EXIT_ROLLBACK


def test_k7_exit_code_continue_still_0() -> None:
    """K-7: continue still exit 0 (back-compat)."""
    report = _make_report(violations=[], recommendation=RECOMMENDATION_CONTINUE)
    assert _exit_code_for(report) == EXIT_OK
