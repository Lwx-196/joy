"""VLM classifier output distribution collapse detector.

If the classifier produces highly concentrated outputs (one class dominates
>= 90% of the window) AND confidence p50 is very high (>= 0.9), we treat the
model as collapsed (`uncalibrated`). The source classifier must then fall
back to live-no-apply mode and skip writes to `image_observations` until the
distribution recovers.

The detector accepts a plain list of dict records so the lib has no DB or
schema coupling; the CLI wrapper (`backend/scripts/vlm_calibration_report.py`)
is responsible for fetching the window from SQLite.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Literal, Sequence

CalibrationStatusKind = Literal["ok", "warn", "uncalibrated"]
AlertSeverity = Literal["warn", "uncalibrated"]

COLLAPSE_RATIO_THRESHOLD = 0.90
COLLAPSE_CONFIDENCE_P50_THRESHOLD = 0.90
WARN_RATIO_THRESHOLD = 0.80
WARN_CONFIDENCE_P50_THRESHOLD = 0.85

DEFAULT_DIMENSIONS: tuple[str, ...] = ("phase", "view", "body_part")


@dataclass(frozen=True)
class DimensionAlert:
    dimension: str
    dominant_class: str | None
    dominant_ratio: float
    confidence_p50: float | None
    severity: AlertSeverity


@dataclass(frozen=True)
class CalibrationStatus:
    status: CalibrationStatusKind
    sample_size: int
    evidence: list[DimensionAlert]
    recommendation: str


def _confidence_p50_for_class(
    records: Sequence[dict], dimension: str, target_class: str
) -> float | None:
    values: list[float] = []
    for r in records:
        if r.get(dimension) != target_class:
            continue
        c = r.get("confidence")
        if c is None:
            continue
        try:
            values.append(float(c))
        except (TypeError, ValueError):
            continue
    return statistics.median(values) if values else None


def _classify_severity(ratio: float, confidence_p50: float | None) -> AlertSeverity | None:
    if ratio >= COLLAPSE_RATIO_THRESHOLD and (
        confidence_p50 is not None and confidence_p50 >= COLLAPSE_CONFIDENCE_P50_THRESHOLD
    ):
        return "uncalibrated"
    if ratio >= WARN_RATIO_THRESHOLD and (
        confidence_p50 is not None and confidence_p50 >= WARN_CONFIDENCE_P50_THRESHOLD
    ):
        return "warn"
    return None


def detect_distribution_collapse(
    records: Sequence[dict],
    dimensions: Sequence[str] = DEFAULT_DIMENSIONS,
) -> CalibrationStatus:
    sample_size = len(records)
    if sample_size == 0:
        return CalibrationStatus(
            status="ok",
            sample_size=0,
            evidence=[],
            recommendation="empty window; no calibration signal",
        )

    alerts: list[DimensionAlert] = []
    overall: CalibrationStatusKind = "ok"

    for dim in dimensions:
        values = [r.get(dim) for r in records if r.get(dim) is not None]
        if not values:
            continue

        counts: dict[str, int] = {}
        for v in values:
            counts[v] = counts.get(v, 0) + 1

        # A dimension with only one observed class is structurally
        # single-valued (e.g. body_part="face" across a face-skill case set)
        # and cannot be interpreted as "collapsed". Skip.
        if len(counts) <= 1:
            continue

        dominant_class, dominant_count = max(counts.items(), key=lambda kv: kv[1])
        dominant_ratio = dominant_count / len(values)
        confidence_p50 = _confidence_p50_for_class(records, dim, dominant_class)
        severity = _classify_severity(dominant_ratio, confidence_p50)

        if severity is None:
            continue

        alerts.append(
            DimensionAlert(
                dimension=dim,
                dominant_class=str(dominant_class),
                dominant_ratio=round(dominant_ratio, 4),
                confidence_p50=(
                    round(confidence_p50, 4) if confidence_p50 is not None else None
                ),
                severity=severity,
            )
        )
        if severity == "uncalibrated":
            overall = "uncalibrated"
        elif overall == "ok":
            overall = "warn"

    if overall == "uncalibrated":
        recommendation = (
            "Model output collapsed; downgrade classifier to live-no-apply mode "
            "and skip image_observations writes until distribution recovers."
        )
    elif overall == "warn":
        recommendation = (
            "Output distribution skewed but not collapsed; monitor and consider "
            "widening sample window before next apply pass."
        )
    else:
        recommendation = "distribution within healthy bounds"

    return CalibrationStatus(
        status=overall,
        sample_size=sample_size,
        evidence=alerts,
        recommendation=recommendation,
    )
