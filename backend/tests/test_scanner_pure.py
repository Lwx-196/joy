"""Unit tests for backend.scanner pure functions.

Targets the file-system-light functions:
  - `_iter_image_files` — image extension filtering, sorted output
  - `_should_skip_dir` — generated artefact directory pruning
  - `infer_category` — the category decision tree (body / unsupported /
    fragment_only / non_labeled / standard_face + tier)
  - `extract_customer_raw` — first-segment extraction from a known root
  - `discover_case_dirs` — leaf directory discovery walking through fixtures

Heavy mediapipe / cv2 paths are intentionally not exercised here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend import scanner


# ----------------------------------------------------------------------
# _iter_image_files
# ----------------------------------------------------------------------


def test_iter_image_files_filters_by_extension(tmp_path: Path):
    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.png").write_bytes(b"x")
    (tmp_path / "c.txt").write_text("x")
    (tmp_path / "d.JPEG").write_bytes(b"x")  # case-insensitive
    out = scanner._iter_image_files(tmp_path)
    assert out == ["a.jpg", "b.png", "d.JPEG"]


def test_iter_image_files_skips_generated_boards(tmp_path: Path):
    (tmp_path / "术前-正面.jpg").write_bytes(b"x")
    (tmp_path / "陈莹-正式品牌版-三联图.jpg").write_bytes(b"x")
    (tmp_path / "preview.jpg").write_bytes(b"x")

    out = scanner._iter_image_files(tmp_path)

    assert out == ["术前-正面.jpg"]


def test_iter_image_files_returns_empty_for_nonexistent_dir(tmp_path: Path):
    """Permission/OS errors must degrade to []; the scanner can't crash on
    one bad directory and abort the whole scan.
    """
    bogus = tmp_path / "does-not-exist"
    assert scanner._iter_image_files(bogus) == []


def test_iter_image_files_ignores_subdirectories(tmp_path: Path):
    (tmp_path / "img.jpg").write_bytes(b"x")
    (tmp_path / "subdir").mkdir()
    out = scanner._iter_image_files(tmp_path)
    assert out == ["img.jpg"]  # subdir not listed


# ----------------------------------------------------------------------
# _should_skip_dir
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, expected",
    [
        # Exact-name matches
        (".case-layout-classify", True),
        (".case-layout-pick", True),
        (".case-layout-organize", True),
        (".case-layout-render", True),
        (".case-workbench-trash", True),
        # Prefix matches (any .case-layout-* not in exact list)
        (".case-layout-output", True),
        (".case-layout-anything", True),
        (".case-workbench-temp", True),
        ("_download-inbox", True),
        (".cache", True),
        (".DS_Store", True),
        # Real case dirs must not be skipped
        ("张三", False),
        ("normal", False),
        ("case-001", False),
    ],
)
def test_should_skip_dir(name, expected):
    assert scanner._should_skip_dir(name) is expected


# ----------------------------------------------------------------------
# infer_category — the decision tree
# ----------------------------------------------------------------------


def test_infer_no_images_returns_unsupported_with_blocking():
    cat, tier, blocking = scanner.infer_category(Path("/tmp/x"), [])
    assert cat == "unsupported"
    assert tier is None
    assert blocking == [{"code": "no_images", "files": [], "severity": "block"}]


def test_infer_body_keyword_in_path_short_circuits():
    """A body keyword in the path beats label / fragment heuristics —
    BODY check fires before any image-content branching.
    """
    cat, tier, blocking = scanner.infer_category(
        Path("/tmp/颈纹/case-1"), ["frame_001.jpg", "frame_002.jpg"]
    )
    assert cat == "body"
    assert tier == "body-dual-compare"
    assert blocking == []


def test_infer_all_frames_returns_fragment_only_with_first5_bound():
    files = [f"frame_{i:03d}.jpg" for i in range(20)]
    cat, _tier, blocking = scanner.infer_category(Path("/tmp/case-x"), files)
    assert cat == "fragment_only"
    assert blocking[0]["code"] == "no_labeled_sources"
    # First 5 frames bound for rename UI
    assert blocking[0]["files"] == files[:5]


def test_infer_no_labeled_returns_non_labeled():
    cat, tier, blocking = scanner.infer_category(
        Path("/tmp/case-y"), ["random1.jpg", "random2.jpg", "random3.jpg"]
    )
    assert cat == "non_labeled"
    assert tier is None
    assert blocking[0]["code"] == "no_labeled_sources"


def test_infer_standard_face_tier_tri_with_6plus_labeled():
    files = [
        "术前-正面.jpg", "术前-左45.jpg", "术前-右45.jpg",
        "术后-正面.jpg", "术后-左45.jpg", "术后-右45.jpg",
    ]
    cat, tier, blocking = scanner.infer_category(Path("/tmp/case-z"), files)
    assert cat == "standard_face"
    assert tier == "tri"
    assert blocking == []


def test_infer_standard_face_tier_bi_with_4labeled():
    files = ["术前-正面.jpg", "术前-左45.jpg", "术后-正面.jpg", "术后-左45.jpg"]
    cat, tier, _blocking = scanner.infer_category(Path("/tmp/case"), files)
    assert tier == "bi"


def test_infer_standard_face_tier_single_with_2labeled():
    files = ["术前-正面.jpg", "术后-正面.jpg"]
    cat, tier, _blocking = scanner.infer_category(Path("/tmp/case"), files)
    assert tier == "single"


def test_infer_standard_face_tier_unsupported_with_1labeled():
    files = ["术前-正面.jpg", "random.jpg"]
    cat, tier, blocking = scanner.infer_category(Path("/tmp/case"), files)
    assert cat == "standard_face"
    assert tier == "unsupported"
    assert any(b["code"] == "missing_oblique" for b in blocking)


def test_infer_recognizes_english_label_tokens():
    """LABELED_TOKENS includes both Chinese and English."""
    files = ["before-front.jpg", "after-front.jpg"]
    cat, tier, _ = scanner.infer_category(Path("/tmp/case"), files)
    assert cat == "standard_face"
    assert tier == "single"


# ----------------------------------------------------------------------
# extract_customer_raw
# ----------------------------------------------------------------------


def test_extract_customer_raw_first_segment_under_root(tmp_path: Path):
    root = tmp_path / "library"
    case_dir = root / "张三" / "2025-01-01"
    raw = scanner.extract_customer_raw(case_dir, [root])
    assert raw == "张三"


def test_extract_customer_raw_falls_back_to_parent_dir(tmp_path: Path):
    """Case dir not under any provided root → use parent dir name as fallback."""
    case_dir = tmp_path / "李四" / "case"
    raw = scanner.extract_customer_raw(case_dir, [tmp_path / "other-root"])
    assert raw == "李四"


# ----------------------------------------------------------------------
# discover_case_dirs — fixture-backed
# ----------------------------------------------------------------------


def test_discover_finds_leaf_dir_with_direct_images(tmp_path: Path):
    case_dir = tmp_path / "客户A" / "case-1"
    case_dir.mkdir(parents=True)
    (case_dir / "img1.jpg").write_bytes(b"x")
    (case_dir / "img2.png").write_bytes(b"x")

    cands = scanner.discover_case_dirs([tmp_path])
    assert len(cands) == 1
    assert cands[0].abs_path == case_dir
    assert cands[0].image_files == ["img1.jpg", "img2.png"]


def test_discover_skips_generated_artefact_dirs(tmp_path: Path):
    """A `.case-layout-output` subtree must not be listed as a candidate even
    if it contains image files.
    """
    artefact = tmp_path / ".case-layout-output" / "fumei"
    artefact.mkdir(parents=True)
    (artefact / "leaked.jpg").write_bytes(b"x")
    cands = scanner.discover_case_dirs([tmp_path])
    assert cands == []


def test_discover_skips_case_workbench_trash(tmp_path: Path):
    trash = tmp_path / "客户A" / "case-1" / ".case-workbench-trash" / "20260502"
    trash.mkdir(parents=True)
    (trash / "术后-废弃.jpg").write_bytes(b"x")
    cands = scanner.discover_case_dirs([tmp_path])
    assert cands == []


def test_discover_ignores_dir_without_direct_images(tmp_path: Path):
    """A parent dir whose only images live deeper isn't a leaf — only the
    leaf with direct images counts.
    """
    parent = tmp_path / "parent"
    leaf = parent / "leaf"
    leaf.mkdir(parents=True)
    (leaf / "img.jpg").write_bytes(b"x")
    cands = scanner.discover_case_dirs([tmp_path])
    assert len(cands) == 1
    assert cands[0].abs_path == leaf


def test_discover_groups_stage_subdirs_as_one_case(tmp_path: Path):
    case_root = tmp_path / "客户A" / "2025.8.1 下颌线"
    before = case_root / "术前"
    after = case_root / "术后"
    before.mkdir(parents=True)
    after.mkdir(parents=True)
    (before / "正面.jpg").write_bytes(b"x")
    (after / "正面.jpg").write_bytes(b"x")

    cands = scanner.discover_case_dirs([tmp_path])
    assert len(cands) == 1
    assert cands[0].abs_path == case_root
    assert cands[0].image_files == ["术前/正面.jpg", "术后/正面.jpg"]


def test_discover_skips_nonexistent_root_silently(tmp_path: Path):
    bogus = tmp_path / "missing"
    cands = scanner.discover_case_dirs([bogus])
    assert cands == []
