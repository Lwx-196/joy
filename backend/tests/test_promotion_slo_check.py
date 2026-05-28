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


# Wave 7 followup A-1 — --paused-stale-days CLI flag (symmetric W6-A) ------
#
# W7-A mirrors W6-A's contract one-for-one. Both flags now route through
# `_inject_structured_field`, so any structural-routing footgun caught by
# the W6 tests is automatically guarded for paused too. Tests below focus
# on (a) W7-specific surface (--paused-stale-days), (b) helper extraction
# correctness, and (c) the dual-flag combo case (both flags together must
# both land in the override).


def _fake_evaluator(captured: dict, recommendation: str = "continue"):
    def _evaluate(*args: object, **kwargs: object) -> SLOReport:
        captured.update(kwargs)
        return SLOReport(
            within_slo=True,
            violations=[],
            evidence={"comfyui_failure": {"rate": 0.01}},
            recommendation=recommendation,
            window_hours=int(kwargs.get("window_hours", 48)),
            sample_size=100,
        )
    return _evaluate


def test_w7_paused_stale_days_zero_rejected_at_cli(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """W7-A: --paused-stale-days 0 must EXIT_USAGE_ERROR=2 at CLI layer
    (symmetric to W6-A baseline behavior)."""
    exit_code = main(["--paused-stale-days", "0"])
    captured = capsys.readouterr()
    assert exit_code == EXIT_USAGE_ERROR
    assert "--paused-stale-days must be positive integer" in captured.err


def test_w7_paused_stale_days_negative_rejected_at_cli(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """W7-A: --paused-stale-days -3 must EXIT_USAGE_ERROR=2 (symmetric to W6-A)."""
    exit_code = main(["--paused-stale-days", "-3"])
    captured = capsys.readouterr()
    assert exit_code == EXIT_USAGE_ERROR
    assert "--paused-stale-days must be positive integer" in captured.err


def test_w7_paused_stale_days_routes_through_structured_override(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """W7-A: --paused-stale-days N (alone) must construct a structured
    override carrying top-level `paused_stale_days`, NOT misroute it into
    the `thresholds` sub-dict. Mirror of the W6-A footgun test."""
    captured_kwargs: dict[str, object] = {}
    monkeypatch.setattr(
        "backend.scripts.promotion_slo_check.evaluate_window",
        _fake_evaluator(captured_kwargs),
    )

    exit_code = main(["--paused-stale-days", "14"])
    capsys.readouterr()
    assert exit_code == EXIT_OK

    override = captured_kwargs.get("thresholds")
    assert isinstance(override, dict), (
        f"CLI must construct an override dict; got {override!r}"
    )
    assert "thresholds" in override or "baseline" in override, (
        f"CLI override must trip the structured branch; got keys={list(override)}"
    )
    assert override["paused_stale_days"] == 14, (
        f"override must carry top-level paused_stale_days=14; got {override!r}"
    )
    # Critically, the value must NOT be inside the `thresholds` sub-dict
    # (where _merge_thresholds' flat branch would otherwise misroute it).
    assert "paused_stale_days" not in override.get("thresholds", {}), (
        "paused_stale_days must NOT be inside thresholds sub-dict "
        "(would be silently no-op'd by the structured branch)"
    )


def test_w7_dual_flag_both_take_effect_together(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """W7-A: passing BOTH --baseline-stale-days AND --paused-stale-days
    in one invocation must land BOTH in the override. Closes a regression
    risk where the second helper call could clobber the first if the
    structured-marker injection logic ran twice incorrectly."""
    captured_kwargs: dict[str, object] = {}
    monkeypatch.setattr(
        "backend.scripts.promotion_slo_check.evaluate_window",
        _fake_evaluator(captured_kwargs),
    )

    exit_code = main(
        [
            "--baseline-stale-days",
            "75",
            "--paused-stale-days",
            "10",
        ]
    )
    capsys.readouterr()
    assert exit_code == EXIT_OK

    override = captured_kwargs.get("thresholds")
    assert isinstance(override, dict)
    assert override.get("baseline_stale_days") == 75
    assert override.get("paused_stale_days") == 10
    # Structured marker still present, not duplicated (must be a dict).
    assert isinstance(override.get("thresholds"), dict)


def test_w7_inject_structured_field_helper_idempotent_marker() -> None:
    """W7-A helper-unit test: _inject_structured_field must NOT clobber an
    existing `thresholds` sub-dict (e.g. one loaded from --thresholds file)
    when called a second time for a different field."""
    from backend.scripts.promotion_slo_check import _inject_structured_field

    # Start with a file-supplied override carrying real thresholds + baseline.
    base_override = {
        "thresholds": {"comfyui_failure_rate_max": 0.05},
        "baseline": {"delivery_gate_rejection_rate": 0.10},
    }
    out = _inject_structured_field(base_override, "paused_stale_days", 14)
    assert out["paused_stale_days"] == 14
    # File-supplied thresholds preserved (not overwritten by empty marker).
    assert out["thresholds"] == {"comfyui_failure_rate_max": 0.05}
    assert out["baseline"] == {"delivery_gate_rejection_rate": 0.10}

    # Second call for a different field augments rather than clobbers.
    out2 = _inject_structured_field(out, "baseline_stale_days", 90)
    assert out2["paused_stale_days"] == 14
    assert out2["baseline_stale_days"] == 90
    assert out2["thresholds"] == {"comfyui_failure_rate_max": 0.05}


def test_w7_inject_structured_field_helper_creates_marker_from_none() -> None:
    """W7-A helper-unit test: passing override=None creates a fresh
    structured dict with empty thresholds marker + top-level field."""
    from backend.scripts.promotion_slo_check import _inject_structured_field

    out = _inject_structured_field(None, "paused_stale_days", 14)
    assert out == {"thresholds": {}, "paused_stale_days": 14}


def test_w7_inject_structured_field_helper_flat_dict_adds_marker() -> None:
    """W7-A helper-unit test: passing a flat thresholds-only override
    (rare but plausible from hand-built tests) must inject the empty
    `thresholds` marker so _merge_thresholds takes the structured branch."""
    from backend.scripts.promotion_slo_check import _inject_structured_field

    flat = {"comfyui_failure_rate_max": 0.05}  # NO 'thresholds' or 'baseline' key
    out = _inject_structured_field(flat, "paused_stale_days", 14)
    assert out["paused_stale_days"] == 14
    # Original flat fields preserved.
    assert out["comfyui_failure_rate_max"] == 0.05
    # Empty marker injected.
    assert out["thresholds"] == {}


# Wave 9 (resolves W7 reviewer Info #1) — defensive copy-on-write -----------
#
# Pre-W9 `_inject_structured_field` mutated the caller-supplied dict in place.
# Today's two CLI callers chain the return value and never reuse the input,
# so behavior was correct. But a future third caller passing a *shared* dict
# (e.g. a cached config object reused across calls) would see surprise
# mutation. W9 adds a shallow top-level copy so input dicts are never
# mutated. Tests below pin the contract.


def test_w9_helper_does_not_mutate_caller_dict_with_structured_marker() -> None:
    """W9: caller-supplied structured override (carrying real `thresholds`
    and `baseline` sub-dicts) must come back UNCHANGED after the helper
    runs — only the returned dict carries the injected field."""
    from backend.scripts.promotion_slo_check import _inject_structured_field

    caller_dict = {
        "thresholds": {"comfyui_failure_rate_max": 0.05},
        "baseline": {"delivery_gate_rejection_rate": 0.10},
    }
    # Snapshot a stable serialization for byte-identical comparison.
    import json
    pre_snapshot = json.dumps(caller_dict, sort_keys=True)

    out = _inject_structured_field(caller_dict, "paused_stale_days", 14)

    post_snapshot = json.dumps(caller_dict, sort_keys=True)
    assert pre_snapshot == post_snapshot, (
        "caller's dict must NOT be mutated by helper (W9 copy-on-write); "
        f"pre={pre_snapshot!r} post={post_snapshot!r}"
    )
    assert "paused_stale_days" not in caller_dict, (
        "caller_dict still carries injected field — copy-on-write broken"
    )
    # Returned dict has the injection.
    assert out["paused_stale_days"] == 14
    # Returned dict is a DIFFERENT object than the input.
    assert out is not caller_dict, (
        "helper must return a fresh dict, not the input reference"
    )


def test_w9_helper_does_not_mutate_caller_flat_dict() -> None:
    """W9: caller-supplied flat dict (rare hand-built test fixture) — same
    contract. Input untouched; marker + field appear only on the return."""
    from backend.scripts.promotion_slo_check import _inject_structured_field

    flat = {"comfyui_failure_rate_max": 0.05}
    import json
    pre_snapshot = json.dumps(flat, sort_keys=True)

    out = _inject_structured_field(flat, "baseline_stale_days", 90)

    assert json.dumps(flat, sort_keys=True) == pre_snapshot
    assert "baseline_stale_days" not in flat
    assert "thresholds" not in flat, (
        "caller's flat dict must NOT have marker injected (W9 copy-on-write)"
    )
    assert out["baseline_stale_days"] == 90
    assert out["thresholds"] == {}
    assert out is not flat


def test_w9_helper_shared_input_reuse_does_not_cross_contaminate() -> None:
    """W9 most realistic regression test: a hypothetical future caller that
    passes the same `base_config` to multiple helper calls (e.g. to build
    two distinct overrides for parallel evaluation) must NOT see one call's
    injection leak into the second.

    Pre-W9 this would fail because the first call mutates `base_config` in
    place, so the second call sees `paused_stale_days=14` from the first.
    """
    from backend.scripts.promotion_slo_check import _inject_structured_field

    base_config = {"thresholds": {}}

    # Caller scenario: two parallel evaluators want different paused windows
    # for an A/B test, both starting from the same cached `base_config`.
    override_a = _inject_structured_field(base_config, "paused_stale_days", 7)
    override_b = _inject_structured_field(base_config, "paused_stale_days", 14)

    assert override_a["paused_stale_days"] == 7
    assert override_b["paused_stale_days"] == 14, (
        "second call inherited mutation from first — copy-on-write broken; "
        f"override_b={override_b!r}"
    )
    # base_config still pristine.
    assert "paused_stale_days" not in base_config, (
        "base_config mutated by helper calls — W9 copy-on-write broken"
    )
