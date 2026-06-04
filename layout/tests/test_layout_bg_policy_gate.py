import numpy as np

from scripts.case_layout_board import apply_conservative_background_policy


def _dummy_image():
    return np.ones((100, 100, 3), dtype=np.uint8) * 200


def test_gate_off_by_default(monkeypatch):
    monkeypatch.delenv("CASE_LAYOUT_BACKGROUND_MODE", raising=False)
    image = _dummy_image()

    output, mask, cleanup = apply_conservative_background_policy(image, "front")

    assert cleanup["status"] == "noop"
    assert cleanup["reason"] == "env gate disabled"
    assert np.array_equal(output, image)
    assert mask.shape == image.shape[:2]
    assert mask.dtype == np.uint8
    assert not mask.any()


def test_gate_on_with_clean_white(monkeypatch):
    monkeypatch.setenv("CASE_LAYOUT_BACKGROUND_MODE", "clean-white")
    image = _dummy_image()

    output, mask, cleanup = apply_conservative_background_policy(image, "front")

    assert cleanup["status"] != "noop"
    assert output.shape == image.shape
    assert mask.shape == image.shape[:2]


def test_gate_off_with_other_value(monkeypatch):
    monkeypatch.setenv("CASE_LAYOUT_BACKGROUND_MODE", "anything")
    image = _dummy_image()

    output, mask, cleanup = apply_conservative_background_policy(image, "front")

    assert cleanup["status"] == "noop"
    assert cleanup["reason"] == "env gate disabled"
    assert np.array_equal(output, image)
    assert mask.shape == image.shape[:2]
