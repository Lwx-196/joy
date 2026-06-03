"""EXIF timeline extraction for medical-aesthetic case photo classification.

Extracts EXIF metadata from images, clusters photos into temporal sessions,
and infers before/after phase hints based on the timeline.  All functions
are fail-open: exceptions never propagate — callers always get a usable
(possibly empty) result.

Dependencies: Pillow only.  No DB, no network.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EXIF tag constants (decimal IDs from the EXIF 2.32 spec)
# ---------------------------------------------------------------------------
_TAG_IMAGE_WIDTH = 256
_TAG_IMAGE_HEIGHT = 257
_TAG_MODEL = 272
_TAG_ORIENTATION = 274
_TAG_EXIF_IFD = 0x8769
_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_DATETIME_DIGITIZED = 0x9004

# Pillow may expose pixel dimensions via these Exif IFD sub-tags when the
# main IFD width/height is absent (common in phone JPEGs).
_TAG_EXIF_IMAGE_WIDTH = 0xA002
_TAG_EXIF_IMAGE_HEIGHT = 0xA003

_EXIF_DATETIME_FMT = "%Y:%m:%d %H:%M:%S"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExifMeta:
    datetime_original: datetime | None
    datetime_digitized: datetime | None
    camera_model: str | None
    image_width: int | None
    image_height: int | None
    orientation: int | None


@dataclass(frozen=True)
class TemporalSession:
    session_id: int
    image_paths: tuple[Path, ...]
    earliest: datetime
    latest: datetime


@dataclass(frozen=True)
class TemporalPhaseHint:
    image_path: Path
    session_id: int
    phase_hint: str  # "before" | "after" | "unknown"
    confidence: float  # 0.0 – 0.30
    reasoning: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_exif_datetime(raw: str | None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip().rstrip("\x00")
    if not raw:
        return None
    try:
        return datetime.strptime(raw, _EXIF_DATETIME_FMT)
    except (ValueError, TypeError):
        return None


def _safe_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        v = int(value)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def extract_exif(image_path: Path) -> ExifMeta:
    """Extract EXIF metadata from *image_path*.

    Fail-open: any exception yields an ``ExifMeta`` with all-``None`` fields.
    """
    _empty = ExifMeta(
        datetime_original=None,
        datetime_digitized=None,
        camera_model=None,
        image_width=None,
        image_height=None,
        orientation=None,
    )
    try:
        with Image.open(image_path) as img:
            exif = img.getexif()
            if not exif:
                # Still capture pixel size from the image itself.
                w, h = img.size
                return ExifMeta(
                    datetime_original=None,
                    datetime_digitized=None,
                    camera_model=None,
                    image_width=w,
                    image_height=h,
                    orientation=None,
                )

            # Main IFD tags
            camera_model_raw = exif.get(_TAG_MODEL)
            camera_model = str(camera_model_raw).strip() if camera_model_raw else None
            orientation = _safe_int(exif.get(_TAG_ORIENTATION))

            # Pixel dimensions: prefer EXIF tags, fall back to Pillow decode
            width = _safe_int(exif.get(_TAG_IMAGE_WIDTH))
            height = _safe_int(exif.get(_TAG_IMAGE_HEIGHT))

            # Exif sub-IFD (DateTimeOriginal / DateTimeDigitized live here)
            exif_ifd = exif.get_ifd(_TAG_EXIF_IFD)

            datetime_original = _parse_exif_datetime(
                exif_ifd.get(_TAG_DATETIME_ORIGINAL) if exif_ifd else None
            )
            datetime_digitized = _parse_exif_datetime(
                exif_ifd.get(_TAG_DATETIME_DIGITIZED) if exif_ifd else None
            )

            if width is None or height is None:
                # Try Exif IFD sub-tags
                if exif_ifd:
                    width = width or _safe_int(exif_ifd.get(_TAG_EXIF_IMAGE_WIDTH))
                    height = height or _safe_int(exif_ifd.get(_TAG_EXIF_IMAGE_HEIGHT))
                # Ultimate fallback: decoded pixel size
                if width is None or height is None:
                    pw, ph = img.size
                    width = width or pw
                    height = height or ph

            return ExifMeta(
                datetime_original=datetime_original,
                datetime_digitized=datetime_digitized,
                camera_model=camera_model,
                image_width=width,
                image_height=height,
                orientation=orientation,
            )
    except Exception:
        logger.debug("EXIF extraction failed for %s", image_path, exc_info=True)
        return _empty


def cluster_sessions(
    images: list[tuple[Path, ExifMeta]],
    *,
    session_gap_minutes: int = 30,
) -> list[TemporalSession]:
    """Cluster *images* into temporal sessions.

    Two adjacent images belong to the same session when their timestamps
    differ by less than *session_gap_minutes*.  Images without a usable
    timestamp are silently excluded.
    """
    gap = timedelta(minutes=session_gap_minutes)

    # Collect images that carry a usable timestamp
    timed: list[tuple[datetime, Path]] = []
    for path, meta in images:
        ts = meta.datetime_original or meta.datetime_digitized
        if ts is not None:
            timed.append((ts, path))

    if not timed:
        return []

    timed.sort(key=lambda t: t[0])

    sessions: list[TemporalSession] = []
    cur_paths: list[Path] = [timed[0][1]]
    cur_earliest: datetime = timed[0][0]
    cur_latest: datetime = timed[0][0]
    sid = 0

    for ts, path in timed[1:]:
        if ts - cur_latest < gap:
            cur_paths.append(path)
            cur_latest = ts
        else:
            sessions.append(
                TemporalSession(
                    session_id=sid,
                    image_paths=tuple(cur_paths),
                    earliest=cur_earliest,
                    latest=cur_latest,
                )
            )
            sid += 1
            cur_paths = [path]
            cur_earliest = ts
            cur_latest = ts

    sessions.append(
        TemporalSession(
            session_id=sid,
            image_paths=tuple(cur_paths),
            earliest=cur_earliest,
            latest=cur_latest,
        )
    )
    return sessions


def infer_temporal_phase(
    sessions: list[TemporalSession],
    *,
    treatment_date: datetime | None = None,
) -> list[TemporalPhaseHint]:
    """Infer before/after phase hints from clustered sessions.

    Strategy:
    1. *treatment_date* provided — sessions before it are ``"before"``
       (confidence 0.30), sessions after are ``"after"`` (confidence 0.30).
    2. No *treatment_date* but >= 2 sessions — earliest → ``"before"``
       (0.15), latest → ``"after"`` (0.15), others → ``"unknown"`` (0.0).
    3. Single session or empty — ``"unknown"`` (0.0).
    """
    if not sessions:
        return []

    hints: list[TemporalPhaseHint] = []

    if treatment_date is not None:
        for sess in sessions:
            if sess.latest < treatment_date:
                phase, conf, reason = (
                    "before",
                    0.30,
                    f"session ends {sess.latest} before treatment {treatment_date}",
                )
            elif sess.earliest > treatment_date:
                phase, conf, reason = (
                    "after",
                    0.30,
                    f"session starts {sess.earliest} after treatment {treatment_date}",
                )
            else:
                phase, conf, reason = (
                    "unknown",
                    0.0,
                    f"session spans treatment date {treatment_date}",
                )
            for path in sess.image_paths:
                hints.append(
                    TemporalPhaseHint(
                        image_path=path,
                        session_id=sess.session_id,
                        phase_hint=phase,
                        confidence=conf,
                        reasoning=reason,
                    )
                )
        return hints

    if len(sessions) >= 2:
        sorted_sessions = sorted(sessions, key=lambda s: s.earliest)
        earliest_sid = sorted_sessions[0].session_id
        latest_sid = sorted_sessions[-1].session_id

        for sess in sessions:
            if sess.session_id == earliest_sid:
                phase, conf, reason = (
                    "before",
                    0.15,
                    "earliest session (no treatment date)",
                )
            elif sess.session_id == latest_sid:
                phase, conf, reason = (
                    "after",
                    0.15,
                    "latest session (no treatment date)",
                )
            else:
                phase, conf, reason = (
                    "unknown",
                    0.0,
                    "middle session (no treatment date)",
                )
            for path in sess.image_paths:
                hints.append(
                    TemporalPhaseHint(
                        image_path=path,
                        session_id=sess.session_id,
                        phase_hint=phase,
                        confidence=conf,
                        reasoning=reason,
                    )
                )
        return hints

    # Single session — cannot determine phase
    for sess in sessions:
        for path in sess.image_paths:
            hints.append(
                TemporalPhaseHint(
                    image_path=path,
                    session_id=sess.session_id,
                    phase_hint="unknown",
                    confidence=0.0,
                    reasoning="single session, phase indeterminate",
                )
            )
    return hints
