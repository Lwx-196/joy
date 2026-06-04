import os

import pytest

from scripts.case_layout_board import FRONT_RE, OBLIQUE_RE, SIDE_RE


VIEW_PATTERNS = (OBLIQUE_RE, SIDE_RE, FRONT_RE)


@pytest.mark.parametrize(
    "filename",
    [
        "2025.4.5.jpg",
        "客户34号-面诊.jpg",
        "case-id-345-no-view.jpg",
        "2026.0405.jpg",
        "test-file.jpg",
    ],
)
def test_non_view_basenames_do_not_match_any_view(filename):
    basename = os.path.basename(filename)

    for pattern in VIEW_PATTERNS:
        assert pattern.search(basename) is None


@pytest.mark.parametrize(
    ("filename", "pattern"),
    [
        ("侧面照.jpg", SIDE_RE),
        ("45度角.jpg", OBLIQUE_RE),
        ("正面照-术后.jpg", FRONT_RE),
        ("半侧脸.jpg", OBLIQUE_RE),
        ("正脸-素颜.jpg", FRONT_RE),
    ],
)
def test_view_basenames_match_expected_pattern(filename, pattern):
    basename = os.path.basename(filename)

    assert pattern.search(basename) is not None
