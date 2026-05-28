"""Tests for `backend.scripts.promotion_slo_check` CLI rendering layer.

Wave 4 K-7 hardening: pre-K-7 the markdown renderer hard-coded
``v['actual']`` / ``v['threshold']`` and crashed on Wave 4 violations
that use different field names. Post-K-7 the renderer is schema-adaptive
via ``_render_violation_row``.

Wave 6 followup A-1: ``--baseline-stale-days`` CLI flag tests at the
bottom of this file exercise argparse + threshold_override routing without
hitting the real DB (the flag layer is pure argument-shape logic — the
underlying override path is covered exhaustively in test_promotion_slo_monitor).
"""

from __future__ import annotations

import pytest

from backend.scripts.promotion_slo_check import (
    EXIT_OK,
    EXIT_ROLLBACK,
    EXIT_STOP_LOSS_HALT,
    EXIT_USAGE_ERROR,
    _exit_code_for,
    _format_markdown,
    _render_violation_row,
    main,
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


# Wave 6 followup A-1 — --baseline-stale-days CLI flag ----------------------


def test_w6_baseline_stale_days_zero_rejected_at_cli(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """W6-A: ``--baseline-stale-days 0`` must exit ``EXIT_USAGE_ERROR=2``
    at the argparse / validation layer, BEFORE reaching ``evaluate_window``
    (where the same value would also raise — but cron operators get a
    cleaner stderr line by failing fast at the CLI layer).
    """
    exit_code = main(["--baseline-stale-days", "0"])
    captured = capsys.readouterr()
    assert exit_code == EXIT_USAGE_ERROR
    assert "--baseline-stale-days must be positive integer" in captured.err


def test_w6_baseline_stale_days_negative_rejected_at_cli(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """W6-A: negative override is rejected at the CLI layer (parallel to
    ``--window`` rejecting non-positive)."""
    exit_code = main(["--baseline-stale-days", "-5"])
    captured = capsys.readouterr()
    assert exit_code == EXIT_USAGE_ERROR
    assert "--baseline-stale-days must be positive integer" in captured.err


def test_w6_baseline_stale_days_routes_through_structured_override(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """W6-A: ``--baseline-stale-days N`` (without ``--thresholds``) must
    construct a structured override (``{"thresholds": {}, ...}``) so
    ``_merge_thresholds`` routes the field through the *structured branch*
    (which respects top-level ``baseline_stale_days``) instead of the *flat
    branch* (which would misroute the key into the ``thresholds`` sub-dict).
    Closes a footgun where CLI override would silently no-op."""
    captured_kwargs: dict[str, object] = {}

    def _fake_evaluate(*args: object, **kwargs: object) -> SLOReport:
        captured_kwargs.update(kwargs)
        return SLOReport(
            within_slo=True,
            violations=[],
            evidence={"comfyui_failure": {"rate": 0.01}},
            recommendation=RECOMMENDATION_CONTINUE,
            window_hours=int(kwargs.get("window_hours", 48)),
            sample_size=100,
        )

    monkeypatch.setattr(
        "backend.scripts.promotion_slo_check.evaluate_window",
        _fake_evaluate,
    )

    exit_code = main(["--baseline-stale-days", "90"])
    capsys.readouterr()  # discard JSON stdout
    assert exit_code == EXIT_OK

    override = captured_kwargs.get("thresholds")
    assert isinstance(override, dict), (
        f"CLI must construct an override dict; got {override!r}"
    )
    # Structured-branch marker present.
    assert (
        "thresholds" in override or "baseline" in override
    ), (
        f"CLI override must trip the structured branch in _merge_thresholds; "
        f"got keys={list(override)}"
    )
    # Top-level value forwarded.
    assert override["baseline_stale_days"] == 90, (
        f"override must carry top-level baseline_stale_days=90; got {override!r}"
    )


def test_w6_baseline_stale_days_augments_existing_thresholds_dict(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    """W6-A: ``--baseline-stale-days N`` + ``--thresholds <path>`` must
    *augment* the on-disk override with the CLI value (CLI wins over the
    file's own baseline_stale_days), not replace it wholesale. Mirrors the
    documented "kwarg > JSON > module constant" priority — here the CLI
    flag IS the kwarg channel.
    """
    # Build a minimal valid thresholds JSON with its own baseline_stale_days=120.
    import json

    thresholds_file = tmp_path / "custom.json"
    thresholds_file.write_text(
        json.dumps(
            {
                "thresholds": {
                    "comfyui_failure_rate_max": 0.05,
                    "vlm_disagreement_rate_max": 0.10,
                    "vlm_judge_missing_rate_max": 0.10,
                    "delivery_gate_rejection_rate_multiplier_max": 1.05,
                    "pre_render_gate_blocker_multiplier_max": 1.10,
                },
                "baseline": {
                    "delivery_gate_rejection_rate": 0.10,
                    "pre_render_gate_blocker_count": 5,
                },
                "baseline_provenance": {
                    "measured_at": "2026-05-01T00:00:00+00:00",
                    "window_hours": 24,
                    "sample_size": 100,
                    "computed_by": "calibrate_cli",
                    "computed_at_main_sha": "abc1234",
                },
                "minimum_sample_size": 30,
                "default_window_hours": 48,
                "paused_stale_days": 7,
                "baseline_stale_days": 120,
            }
        ),
        encoding="utf-8",
    )

    captured_kwargs: dict[str, object] = {}

    def _fake_evaluate(*args: object, **kwargs: object) -> SLOReport:
        captured_kwargs.update(kwargs)
        return SLOReport(
            within_slo=True,
            violations=[],
            evidence={"comfyui_failure": {"rate": 0.01}},
            recommendation=RECOMMENDATION_CONTINUE,
            window_hours=48,
            sample_size=100,
        )

    monkeypatch.setattr(
        "backend.scripts.promotion_slo_check.evaluate_window",
        _fake_evaluate,
    )

    exit_code = main(
        [
            "--thresholds",
            str(thresholds_file),
            "--baseline-stale-days",
            "90",
        ]
    )
    capsys.readouterr()
    assert exit_code == EXIT_OK

    override = captured_kwargs.get("thresholds")
    assert isinstance(override, dict)
    # CLI flag must win over file value (90, not 120).
    assert override["baseline_stale_days"] == 90, (
        f"CLI --baseline-stale-days 90 must override file value 120; "
        f"got effective={override.get('baseline_stale_days')!r}"
    )
    # File-supplied structured fields preserved (thresholds + baseline + provenance).
    assert "thresholds" in override and override["thresholds"], (
        f"file thresholds must survive augmentation; got {override!r}"
    )
    assert "baseline" in override


def test_w6_baseline_stale_days_default_does_not_inject_override(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """W6-A BC: invoking the CLI WITHOUT ``--baseline-stale-days`` must
    pass ``thresholds=None`` to ``evaluate_window`` (the pre-W6 contract).
    Closes a regression risk where the new flag silently injected an empty
    override dict on every invocation."""
    captured_kwargs: dict[str, object] = {}

    def _fake_evaluate(*args: object, **kwargs: object) -> SLOReport:
        captured_kwargs.update(kwargs)
        return SLOReport(
            within_slo=True,
            violations=[],
            evidence={"comfyui_failure": {"rate": 0.01}},
            recommendation=RECOMMENDATION_CONTINUE,
            window_hours=48,
            sample_size=100,
        )

    monkeypatch.setattr(
        "backend.scripts.promotion_slo_check.evaluate_window",
        _fake_evaluate,
    )

    exit_code = main([])
    capsys.readouterr()
    assert exit_code == EXIT_OK
    assert captured_kwargs.get("thresholds") is None, (
        f"absent flag must pass thresholds=None (BC); got "
        f"{captured_kwargs.get('thresholds')!r}"
    )
