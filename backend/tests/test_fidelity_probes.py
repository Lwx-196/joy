"""Tests for backend/services/fidelity_probes.py.

Synthetic raw/enhanced/mask triples assert the three objective probes catch the
failure modes L-140 cares about: smoothing (HF collapse), darkening/desaturation
(tone/colour shift), and whole-image regeneration (lost locality). A genuine
focal sharpen must PASS.
"""
from __future__ import annotations

import pytest
from PIL import Image, ImageFilter

from backend.services import fidelity_probes as fp

np = pytest.importorskip("numpy")


def _structured_img(path, size=(256, 256), seed=0):
    """Low-frequency structure + mild noise, then slightly blurred → leaves
    head-room for a sharpen to raise high-frequency energy."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0 : size[1], 0 : size[0]]
    base = (
        128
        + 40 * np.sin(xx / 18.0)
        + 30 * np.cos(yy / 23.0)
        + rng.integers(-12, 12, size=(size[1], size[0]))
    )
    base = np.clip(base, 0, 255).astype(np.uint8)
    rgb = np.stack([base, np.clip(base * 0.8 + 30, 0, 255).astype(np.uint8), np.clip(base * 0.6 + 50, 0, 255).astype(np.uint8)], axis=-1)
    img = Image.fromarray(rgb, "RGB").filter(ImageFilter.GaussianBlur(1.2))
    img.save(path)
    return img


def _mask(path, size=(256, 256)):
    m = np.zeros((size[1], size[0]), dtype=np.uint8)
    m[64:192, 64:192] = 255
    Image.fromarray(m, "L").save(path)
    return np.asarray(Image.open(path))


def _focal_apply(raw_img, mask_arr, transform):
    """Apply ``transform`` only inside the mask; pristine outside (mirrors a real
    feather-composite focal enhancement)."""
    arr_raw = np.asarray(raw_img).copy()
    arr_t = np.asarray(transform(raw_img))
    m = mask_arr > 127
    arr_raw[m] = arr_t[m]
    return Image.fromarray(arr_raw, "RGB")


def test_focal_sharpen_passes(tmp_path):
    raw_p = tmp_path / "raw.png"
    raw = _structured_img(raw_p)
    mask_p = tmp_path / "m.png"
    mask = _mask(mask_p)
    enh = _focal_apply(raw, mask, lambda im: im.filter(ImageFilter.UnsharpMask(2, 150, 2)))
    enh_p = tmp_path / "enh.png"
    enh.save(enh_p)

    probes = fp.compute_fidelity_probes(raw_p, enh_p, mask_p)
    assert probes["hf_energy_ratio"] >= 1.0, probes
    assert probes["out_mask_mean_abs_delta"] < fp.OUT_MASK_DELTA_MAX, probes
    verdict = fp.prescreen_verdict(probes)
    assert verdict["passed"] is True, verdict


def test_smoothing_fails_hf(tmp_path):
    raw_p = tmp_path / "raw.png"
    raw = _structured_img(raw_p)
    mask_p = tmp_path / "m.png"
    mask = _mask(mask_p)
    enh = _focal_apply(raw, mask, lambda im: im.filter(ImageFilter.GaussianBlur(3)))
    enh_p = tmp_path / "enh.png"
    enh.save(enh_p)

    probes = fp.compute_fidelity_probes(raw_p, enh_p, mask_p)
    assert probes["hf_energy_ratio"] < fp.HF_RATIO_MIN, probes
    verdict = fp.prescreen_verdict(probes)
    assert verdict["passed"] is False
    assert any("smoothing" in r or "high-frequency" in r for r in verdict["reasons"])


def test_darkening_fails_luma(tmp_path):
    raw_p = tmp_path / "raw.png"
    raw = _structured_img(raw_p)
    mask_p = tmp_path / "m.png"
    mask = _mask(mask_p)

    def darken(im):
        a = np.asarray(im).astype(np.int16) - 30
        return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB")

    enh = _focal_apply(raw, mask, darken)
    enh_p = tmp_path / "enh.png"
    enh.save(enh_p)

    probes = fp.compute_fidelity_probes(raw_p, enh_p, mask_p)
    assert probes["luma_signed_shift"] < fp.LUMA_DARKEN_MIN, probes
    assert fp.prescreen_verdict(probes)["passed"] is False


def _saturated_img(path, size=(256, 256), seed=4):
    """Vivid, strongly-saturated skin-like image so de-saturation collapses
    chroma well past the prescreen ceiling (real magazine-smoothing does this)."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0 : size[1], 0 : size[0]]
    tex = rng.integers(-10, 10, size=(size[1], size[0]))
    r = np.clip(195 + 0.1 * xx + tex, 0, 255)
    g = np.clip(110 + 0.05 * yy + tex, 0, 255)
    b = np.clip(95 + tex, 0, 255)
    rgb = np.stack([r, g, b], axis=-1).astype(np.uint8)
    img = Image.fromarray(rgb, "RGB").filter(ImageFilter.GaussianBlur(1.0))
    img.save(path)
    return img


def test_desaturation_fails_chroma(tmp_path):
    raw_p = tmp_path / "raw.png"
    raw = _saturated_img(raw_p)
    mask_p = tmp_path / "m.png"
    mask = _mask(mask_p)

    def desat(im):
        gray = np.asarray(im.convert("L"))
        return Image.fromarray(np.stack([gray] * 3, axis=-1), "RGB")

    enh = _focal_apply(raw, mask, desat)
    enh_p = tmp_path / "enh.png"
    enh.save(enh_p)

    probes = fp.compute_fidelity_probes(raw_p, enh_p, mask_p)
    assert probes["chroma_abs_shift"] > fp.CHROMA_ABS_MAX, probes
    assert fp.prescreen_verdict(probes)["passed"] is False


def test_global_change_fails_locality(tmp_path):
    raw_p = tmp_path / "raw.png"
    raw = _structured_img(raw_p)
    mask_p = tmp_path / "m.png"
    _mask(mask_p)
    # Change the WHOLE image (regeneration) → background no longer pristine.
    a = np.asarray(raw).astype(np.int16) + 12
    enh = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB")
    enh_p = tmp_path / "enh.png"
    enh.save(enh_p)

    probes = fp.compute_fidelity_probes(raw_p, enh_p, mask_p)
    assert probes["out_mask_mean_abs_delta"] > fp.OUT_MASK_DELTA_MAX, probes
    verdict = fp.prescreen_verdict(probes)
    assert verdict["passed"] is False
    assert any("background" in r or "重绘" in r for r in verdict["reasons"])
