"""Tests for backend.services.exif_extractor — EXIF timeline extraction."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from backend.services.exif_extractor import (
    ExifMeta,
    TemporalSession,
    cluster_sessions,
    extract_exif,
    infer_temporal_phase,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_jpeg_with_exif(
    dest: Path,
    *,
    datetime_original: str | None = None,
    datetime_digitized: str | None = None,
    camera_model: str | None = None,
    orientation: int | None = None,
    width: int = 120,
    height: int = 80,
) -> Path:
    """Create a minimal JPEG at *dest* with the requested EXIF tags."""
    img = Image.new("RGB", (width, height), "blue")
    exif = img.getexif()

    if camera_model is not None:
        exif[272] = camera_model  # Model
    if orientation is not None:
        exif[274] = orientation  # Orientation

    ifd = exif.get_ifd(0x8769)
    if datetime_original is not None:
        ifd[0x9003] = datetime_original
    if datetime_digitized is not None:
        ifd[0x9004] = datetime_digitized

    img.save(dest, format="JPEG", exif=exif.tobytes())
    return dest


def _make_jpeg_no_exif(dest: Path, *, width: int = 64, height: int = 48) -> Path:
    """Create a JPEG with no EXIF data at all."""
    img = Image.new("RGB", (width, height), "green")
    img.save(dest, format="JPEG")
    return dest


# ===================================================================
# extract_exif
# ===================================================================
class TestExtractExif:
    def test_full_exif(self, tmp_path: Path) -> None:
        p = _make_jpeg_with_exif(
            tmp_path / "full.jpg",
            datetime_original="2025:03:15 10:30:00",
            datetime_digitized="2025:03:15 10:30:05",
            camera_model="iPhone 15 Pro",
            orientation=6,
        )
        meta = extract_exif(p)

        assert meta.datetime_original == datetime(2025, 3, 15, 10, 30, 0)
        assert meta.datetime_digitized == datetime(2025, 3, 15, 10, 30, 5)
        assert meta.camera_model == "iPhone 15 Pro"
        assert meta.orientation == 6
        assert meta.image_width == 120
        assert meta.image_height == 80

    def test_partial_exif_no_datetime(self, tmp_path: Path) -> None:
        p = _make_jpeg_with_exif(
            tmp_path / "partial.jpg",
            camera_model="Canon EOS R5",
        )
        meta = extract_exif(p)

        assert meta.datetime_original is None
        assert meta.datetime_digitized is None
        assert meta.camera_model == "Canon EOS R5"

    def test_no_exif_still_gets_size(self, tmp_path: Path) -> None:
        p = _make_jpeg_no_exif(tmp_path / "plain.jpg", width=200, height=150)
        meta = extract_exif(p)

        assert meta.datetime_original is None
        assert meta.camera_model is None
        assert meta.image_width == 200
        assert meta.image_height == 150

    def test_nonexistent_path(self, tmp_path: Path) -> None:
        meta = extract_exif(tmp_path / "does_not_exist.jpg")

        assert meta.datetime_original is None
        assert meta.camera_model is None
        assert meta.image_width is None
        assert meta.image_height is None

    def test_corrupted_file(self, tmp_path: Path) -> None:
        bad = tmp_path / "corrupt.jpg"
        bad.write_bytes(b"\xff\xd8\xff\xe0garbage_not_a_real_jpeg")
        meta = extract_exif(bad)

        # fail-open: all None
        assert meta.datetime_original is None
        assert meta.camera_model is None

    def test_orientation_value(self, tmp_path: Path) -> None:
        p = _make_jpeg_with_exif(
            tmp_path / "orient.jpg",
            orientation=3,
        )
        meta = extract_exif(p)
        assert meta.orientation == 3

    def test_frozen_dataclass(self, tmp_path: Path) -> None:
        p = _make_jpeg_with_exif(tmp_path / "frozen.jpg")
        meta = extract_exif(p)
        with pytest.raises(AttributeError):
            meta.camera_model = "hacked"  # type: ignore[misc]


# ===================================================================
# cluster_sessions
# ===================================================================
class TestClusterSessions:
    def test_empty_list(self) -> None:
        assert cluster_sessions([]) == []

    def test_single_image(self, tmp_path: Path) -> None:
        p = tmp_path / "a.jpg"
        meta = ExifMeta(
            datetime_original=datetime(2025, 1, 1, 9, 0, 0),
            datetime_digitized=None,
            camera_model=None,
            image_width=100,
            image_height=100,
            orientation=None,
        )
        sessions = cluster_sessions([(p, meta)])
        assert len(sessions) == 1
        assert sessions[0].session_id == 0
        assert sessions[0].image_paths == (p,)
        assert sessions[0].earliest == datetime(2025, 1, 1, 9, 0, 0)

    def test_same_session_within_gap(self, tmp_path: Path) -> None:
        base = datetime(2025, 6, 1, 14, 0, 0)
        images = []
        for i in range(4):
            p = tmp_path / f"img_{i}.jpg"
            meta = ExifMeta(
                datetime_original=base + timedelta(minutes=5 * i),
                datetime_digitized=None,
                camera_model=None,
                image_width=100,
                image_height=100,
                orientation=None,
            )
            images.append((p, meta))

        sessions = cluster_sessions(images, session_gap_minutes=30)
        assert len(sessions) == 1
        assert len(sessions[0].image_paths) == 4
        assert sessions[0].earliest == base
        assert sessions[0].latest == base + timedelta(minutes=15)

    def test_multiple_sessions(self, tmp_path: Path) -> None:
        t1 = datetime(2025, 3, 1, 9, 0, 0)
        t2 = datetime(2025, 3, 1, 9, 10, 0)
        t3 = datetime(2025, 3, 1, 11, 0, 0)  # > 30 min gap
        t4 = datetime(2025, 3, 1, 11, 5, 0)

        images = []
        for i, ts in enumerate([t1, t2, t3, t4]):
            p = tmp_path / f"s_{i}.jpg"
            meta = ExifMeta(
                datetime_original=ts,
                datetime_digitized=None,
                camera_model=None,
                image_width=100,
                image_height=100,
                orientation=None,
            )
            images.append((p, meta))

        sessions = cluster_sessions(images)
        assert len(sessions) == 2
        assert len(sessions[0].image_paths) == 2
        assert len(sessions[1].image_paths) == 2
        assert sessions[0].session_id == 0
        assert sessions[1].session_id == 1

    def test_no_exif_images_skipped(self, tmp_path: Path) -> None:
        p1 = tmp_path / "has_ts.jpg"
        p2 = tmp_path / "no_ts.jpg"
        meta_with = ExifMeta(
            datetime_original=datetime(2025, 1, 1, 12, 0, 0),
            datetime_digitized=None,
            camera_model=None,
            image_width=100,
            image_height=100,
            orientation=None,
        )
        meta_without = ExifMeta(
            datetime_original=None,
            datetime_digitized=None,
            camera_model=None,
            image_width=100,
            image_height=100,
            orientation=None,
        )

        sessions = cluster_sessions([(p1, meta_with), (p2, meta_without)])
        assert len(sessions) == 1
        assert sessions[0].image_paths == (p1,)

    def test_all_images_without_exif(self, tmp_path: Path) -> None:
        meta = ExifMeta(
            datetime_original=None,
            datetime_digitized=None,
            camera_model=None,
            image_width=None,
            image_height=None,
            orientation=None,
        )
        sessions = cluster_sessions([(tmp_path / "a.jpg", meta)])
        assert sessions == []

    def test_falls_back_to_datetime_digitized(self, tmp_path: Path) -> None:
        p = tmp_path / "digitized.jpg"
        meta = ExifMeta(
            datetime_original=None,
            datetime_digitized=datetime(2025, 5, 20, 8, 0, 0),
            camera_model=None,
            image_width=100,
            image_height=100,
            orientation=None,
        )
        sessions = cluster_sessions([(p, meta)])
        assert len(sessions) == 1
        assert sessions[0].earliest == datetime(2025, 5, 20, 8, 0, 0)

    def test_custom_gap(self, tmp_path: Path) -> None:
        t1 = datetime(2025, 1, 1, 10, 0, 0)
        t2 = datetime(2025, 1, 1, 10, 20, 0)  # 20 min gap

        images = []
        for i, ts in enumerate([t1, t2]):
            p = tmp_path / f"g_{i}.jpg"
            meta = ExifMeta(
                datetime_original=ts,
                datetime_digitized=None,
                camera_model=None,
                image_width=100,
                image_height=100,
                orientation=None,
            )
            images.append((p, meta))

        # 30 min gap: same session
        assert len(cluster_sessions(images, session_gap_minutes=30)) == 1
        # 10 min gap: two sessions
        assert len(cluster_sessions(images, session_gap_minutes=10)) == 2

    def test_frozen_session(self, tmp_path: Path) -> None:
        p = tmp_path / "x.jpg"
        meta = ExifMeta(
            datetime_original=datetime(2025, 1, 1, 9, 0, 0),
            datetime_digitized=None,
            camera_model=None,
            image_width=100,
            image_height=100,
            orientation=None,
        )
        sessions = cluster_sessions([(p, meta)])
        with pytest.raises(AttributeError):
            sessions[0].session_id = 99  # type: ignore[misc]


# ===================================================================
# infer_temporal_phase
# ===================================================================
class TestInferTemporalPhase:
    @staticmethod
    def _session(
        sid: int,
        earliest: datetime,
        latest: datetime,
        paths: tuple[Path, ...] | None = None,
    ) -> TemporalSession:
        if paths is None:
            paths = (Path(f"/fake/{sid}.jpg"),)
        return TemporalSession(
            session_id=sid,
            image_paths=paths,
            earliest=earliest,
            latest=latest,
        )

    def test_empty_sessions(self) -> None:
        assert infer_temporal_phase([]) == []

    def test_with_treatment_date_before(self) -> None:
        s = self._session(0, datetime(2025, 1, 1, 9, 0), datetime(2025, 1, 1, 9, 30))
        treatment = datetime(2025, 6, 1, 12, 0)
        hints = infer_temporal_phase([s], treatment_date=treatment)

        assert len(hints) == 1
        assert hints[0].phase_hint == "before"
        assert hints[0].confidence == 0.30

    def test_with_treatment_date_after(self) -> None:
        s = self._session(0, datetime(2025, 8, 1, 9, 0), datetime(2025, 8, 1, 10, 0))
        treatment = datetime(2025, 6, 1, 12, 0)
        hints = infer_temporal_phase([s], treatment_date=treatment)

        assert len(hints) == 1
        assert hints[0].phase_hint == "after"
        assert hints[0].confidence == 0.30

    def test_with_treatment_date_spanning(self) -> None:
        s = self._session(0, datetime(2025, 5, 31, 9, 0), datetime(2025, 6, 2, 10, 0))
        treatment = datetime(2025, 6, 1, 12, 0)
        hints = infer_temporal_phase([s], treatment_date=treatment)

        assert hints[0].phase_hint == "unknown"
        assert hints[0].confidence == 0.0

    def test_with_treatment_date_mixed(self) -> None:
        s_before = self._session(0, datetime(2025, 1, 1, 9, 0), datetime(2025, 1, 1, 10, 0))
        s_after = self._session(1, datetime(2025, 8, 1, 9, 0), datetime(2025, 8, 1, 10, 0))
        treatment = datetime(2025, 6, 1, 12, 0)
        hints = infer_temporal_phase([s_before, s_after], treatment_date=treatment)

        assert len(hints) == 2
        before_hints = [h for h in hints if h.phase_hint == "before"]
        after_hints = [h for h in hints if h.phase_hint == "after"]
        assert len(before_hints) == 1
        assert len(after_hints) == 1

    def test_no_treatment_two_sessions(self) -> None:
        s1 = self._session(0, datetime(2025, 1, 1, 9, 0), datetime(2025, 1, 1, 10, 0))
        s2 = self._session(1, datetime(2025, 6, 1, 9, 0), datetime(2025, 6, 1, 10, 0))
        hints = infer_temporal_phase([s1, s2])

        by_phase = {h.phase_hint for h in hints}
        assert "before" in by_phase
        assert "after" in by_phase
        assert all(h.confidence == 0.15 for h in hints)

    def test_no_treatment_three_sessions_middle_unknown(self) -> None:
        s1 = self._session(0, datetime(2025, 1, 1, 9, 0), datetime(2025, 1, 1, 10, 0))
        s2 = self._session(1, datetime(2025, 3, 1, 9, 0), datetime(2025, 3, 1, 10, 0))
        s3 = self._session(2, datetime(2025, 6, 1, 9, 0), datetime(2025, 6, 1, 10, 0))
        hints = infer_temporal_phase([s1, s2, s3])

        by_sid = {h.session_id: h for h in hints}
        assert by_sid[0].phase_hint == "before"
        assert by_sid[1].phase_hint == "unknown"
        assert by_sid[2].phase_hint == "after"

    def test_single_session_unknown(self) -> None:
        s = self._session(0, datetime(2025, 3, 1, 9, 0), datetime(2025, 3, 1, 10, 0))
        hints = infer_temporal_phase([s])

        assert len(hints) == 1
        assert hints[0].phase_hint == "unknown"
        assert hints[0].confidence == 0.0

    def test_multi_image_session_all_get_hints(self) -> None:
        paths = (Path("/a.jpg"), Path("/b.jpg"), Path("/c.jpg"))
        s = self._session(
            0,
            datetime(2025, 1, 1, 9, 0),
            datetime(2025, 1, 1, 9, 15),
            paths=paths,
        )
        hints = infer_temporal_phase([s])
        assert len(hints) == 3
        assert all(h.phase_hint == "unknown" for h in hints)

    def test_hint_frozen(self) -> None:
        s = self._session(0, datetime(2025, 1, 1, 9, 0), datetime(2025, 1, 1, 10, 0))
        hints = infer_temporal_phase([s])
        with pytest.raises(AttributeError):
            hints[0].phase_hint = "before"  # type: ignore[misc]


# ===================================================================
# Integration: extract → cluster → infer
# ===================================================================
class TestEndToEnd:
    def test_full_pipeline_two_sessions(self, tmp_path: Path) -> None:
        # Session 1: two images taken 5 min apart (before treatment)
        p1 = _make_jpeg_with_exif(
            tmp_path / "s1_a.jpg",
            datetime_original="2025:01:10 09:00:00",
            camera_model="iPhone 15",
        )
        p2 = _make_jpeg_with_exif(
            tmp_path / "s1_b.jpg",
            datetime_original="2025:01:10 09:05:00",
        )
        # Session 2: one image taken months later (after treatment)
        p3 = _make_jpeg_with_exif(
            tmp_path / "s2_a.jpg",
            datetime_original="2025:06:20 14:00:00",
        )

        images = [(p, extract_exif(p)) for p in [p1, p2, p3]]
        sessions = cluster_sessions(images)
        assert len(sessions) == 2

        treatment = datetime(2025, 3, 1, 12, 0, 0)
        hints = infer_temporal_phase(sessions, treatment_date=treatment)
        assert len(hints) == 3

        before_hints = [h for h in hints if h.phase_hint == "before"]
        after_hints = [h for h in hints if h.phase_hint == "after"]
        assert len(before_hints) == 2
        assert len(after_hints) == 1
        assert all(h.confidence == 0.30 for h in hints)

    def test_full_pipeline_no_treatment_date(self, tmp_path: Path) -> None:
        p1 = _make_jpeg_with_exif(
            tmp_path / "early.jpg",
            datetime_original="2025:02:01 10:00:00",
        )
        p2 = _make_jpeg_with_exif(
            tmp_path / "late.jpg",
            datetime_original="2025:09:01 10:00:00",
        )

        images = [(p, extract_exif(p)) for p in [p1, p2]]
        sessions = cluster_sessions(images)
        hints = infer_temporal_phase(sessions)

        by_phase = {h.phase_hint for h in hints}
        assert by_phase == {"before", "after"}
        assert all(h.confidence == 0.15 for h in hints)

    def test_full_pipeline_mixed_exif_and_no_exif(self, tmp_path: Path) -> None:
        p1 = _make_jpeg_with_exif(
            tmp_path / "has_exif.jpg",
            datetime_original="2025:04:01 08:00:00",
        )
        p2 = _make_jpeg_no_exif(tmp_path / "no_exif.jpg")

        images = [(p, extract_exif(p)) for p in [p1, p2]]
        sessions = cluster_sessions(images)
        # Only 1 image had EXIF → 1 session
        assert len(sessions) == 1
        hints = infer_temporal_phase(sessions)
        assert len(hints) == 1
        assert hints[0].phase_hint == "unknown"
