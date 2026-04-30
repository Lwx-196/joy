"""Unit tests for `render_executor` archive + restore.

Targets the file-system-only paths:
  - `_archive_existing_final_board(out_root)` — copy current final-board.jpg
    to `.history/<ts>.jpg`, prune to RENDER_HISTORY_MAX_VERSIONS, return ts or None.
  - `restore_archived_final_board(out_root, archived_at)` — copy snapshot
    back to final-board.jpg AFTER auto-archiving the current one.

The mediapipe / cv2 render execution path is out of scope (other tests already
exercise the route layer + audit through monkeypatch).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from backend import render_executor


def _write_final(out_root: Path, content: bytes = b"jpeg") -> Path:
    out_root.mkdir(parents=True, exist_ok=True)
    p = out_root / "final-board.jpg"
    p.write_bytes(content)
    return p


# ----------------------------------------------------------------------
# _archive_existing_final_board
# ----------------------------------------------------------------------


def test_archive_returns_none_when_no_final_board(tmp_path):
    out_root = tmp_path / "out"
    out_root.mkdir()
    assert render_executor._archive_existing_final_board(out_root) is None


def test_archive_copies_existing_final_to_history(tmp_path):
    out_root = tmp_path / "out"
    _write_final(out_root, content=b"original")

    ts = render_executor._archive_existing_final_board(out_root)
    assert ts is not None
    snapshot = out_root / ".history" / f"{ts}.jpg"
    assert snapshot.is_file()
    assert snapshot.read_bytes() == b"original"
    # Original final-board still in place — archive is a copy, not move.
    assert (out_root / "final-board.jpg").read_bytes() == b"original"


def test_archive_ts_format_is_strict_iso_basic(tmp_path):
    """The ts must match `YYYYMMDDTHHMMSSZ` exactly so the route's regex
    `\\d{8}T\\d{6}Z` accepts it for restore.
    """
    import re

    out_root = tmp_path / "out"
    _write_final(out_root)
    ts = render_executor._archive_existing_final_board(out_root)
    assert re.fullmatch(r"\d{8}T\d{6}Z", ts) is not None


def test_archive_prunes_to_max_versions(tmp_path, monkeypatch):
    """Set MAX=3, archive 5 times → only the 3 newest kept."""
    out_root = tmp_path / "out"
    monkeypatch.setattr(render_executor, "RENDER_HISTORY_MAX_VERSIONS", 3)

    history = out_root / ".history"
    history.mkdir(parents=True, exist_ok=True)
    # Pre-populate older snapshots with monotonically-increasing names so
    # sorted(..., reverse=True) gives a deterministic prune order.
    for i in range(5):
        ts = f"2026010{i+1}T120000Z"
        (history / f"{ts}.jpg").write_bytes(b"old")

    _write_final(out_root, b"new")
    new_ts = render_executor._archive_existing_final_board(out_root)
    assert new_ts is not None

    remaining = sorted(p.name for p in history.iterdir())
    # 5 old + 1 new = 6 total before prune → keep top 3 by name desc
    assert len(remaining) == 3


# ----------------------------------------------------------------------
# restore_archived_final_board
# ----------------------------------------------------------------------


def test_restore_404_when_snapshot_missing(tmp_path):
    out_root = tmp_path / "out"
    out_root.mkdir()
    with pytest.raises(FileNotFoundError):
        render_executor.restore_archived_final_board(out_root, "20260101T120000Z")


def test_restore_copies_snapshot_back_to_final(tmp_path):
    out_root = tmp_path / "out"
    history = out_root / ".history"
    history.mkdir(parents=True)
    snapshot = history / "20260101T120000Z.jpg"
    snapshot.write_bytes(b"archived-content")

    result = render_executor.restore_archived_final_board(out_root, "20260101T120000Z")
    assert result["restored_from"] == "20260101T120000Z"
    assert (out_root / "final-board.jpg").read_bytes() == b"archived-content"


def test_restore_archives_existing_final_first(tmp_path):
    """Restore is reversible: before overwriting final-board.jpg, the current
    one must be archived too.
    """
    out_root = tmp_path / "out"
    history = out_root / ".history"
    history.mkdir(parents=True)
    (history / "20260101T120000Z.jpg").write_bytes(b"old")
    _write_final(out_root, b"current")

    result = render_executor.restore_archived_final_board(out_root, "20260101T120000Z")
    # previous_archived_at must reflect the just-archived current final
    assert result["previous_archived_at"] is not None
    archived = history / f"{result['previous_archived_at']}.jpg"
    assert archived.read_bytes() == b"current"
    # final-board now holds the restored snapshot
    assert (out_root / "final-board.jpg").read_bytes() == b"old"


def test_restore_previous_archived_at_none_when_no_current_final(tmp_path):
    """No current final → previous_archived_at is None (nothing to archive)."""
    out_root = tmp_path / "out"
    history = out_root / ".history"
    history.mkdir(parents=True)
    (history / "20260101T120000Z.jpg").write_bytes(b"snapshot")

    result = render_executor.restore_archived_final_board(out_root, "20260101T120000Z")
    assert result["previous_archived_at"] is None


def test_restore_uses_copy_not_copy2_so_mtime_is_now(tmp_path):
    """`copy` (not `copy2`) makes the restored final-board's mtime reflect the
    restore moment, not the original archive time. The route relies on this
    to invalidate the frontend's <img src> cache.
    """
    out_root = tmp_path / "out"
    history = out_root / ".history"
    history.mkdir(parents=True)
    snapshot = history / "20260101T120000Z.jpg"
    snapshot.write_bytes(b"x")
    # Backdate snapshot to a known older time
    old_time = time.time() - 86400  # 1 day ago
    import os

    os.utime(snapshot, (old_time, old_time))

    before_restore = time.time()
    render_executor.restore_archived_final_board(out_root, "20260101T120000Z")
    final_mtime = (out_root / "final-board.jpg").stat().st_mtime
    # mtime reflects the restore, not the snapshot — must be near `now`
    assert final_mtime >= before_restore
