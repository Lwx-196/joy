"""Objective fidelity probes — pre-screen enhancement arms before the VLM judge.

Three zero-quota numpy measures over (raw, enhanced, focus-mask). A 磨皮
(over-smoothing) or 重绘 (whole-image regeneration) arm fails here and is culled
*before* spending any judge token (L-140: pick保真 over重绘 at selection time):

  1. ``hf_energy_ratio`` — Laplacian variance inside the mask, enhanced / raw.
     Fidelity sharpening RAISES focal high-frequency energy (ratio >= 1.0);
     smoothing collapses it (ratio < 1.0) → FAIL.
  2. ``luma_signed_shift`` / ``chroma_abs_shift`` — mean ΔY / mean |ΔCb,ΔCr|
     inside the mask. Fidelity preserves tone (~0); darkening (negative luma) or
     de-saturation / colour-cast (large chroma) → FAIL.
  3. ``out_mask_mean_abs_delta`` — mean |ΔY| OUTSIDE the mask. A focal
     enhancement leaves the background pristine (~0); whole-image regeneration
     bleeds change everywhere → FAIL.

``prescreen_verdict`` turns the probes into ``{passed, reasons}``. Thresholds are
calibrated so the pure-classical arm (focal unsharp, pristine background) passes
by construction and a global de-smoothing regenerator fails; Phase 4 may
re-calibrate against the real arm distribution.

Pure numpy + PIL (no cv2/scipy) so it runs anywhere Pillow does.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# Pass/fail thresholds (8-bit 0-255 scale). Tunable; documented rationale above.
HF_RATIO_MIN = 0.98          # enhanced focal HF must not collapse below raw
LUMA_DARKEN_MIN = -4.0       # mean focal luma must not drop (압暗 = de-fidelity)
LUMA_SHIFT_ABS_MAX = 8.0     # |mean luma shift| ceiling (tone preserved)
CHROMA_ABS_MAX = 8.0         # mean |chroma shift| ceiling (colour preserved)
OUT_MASK_DELTA_MAX = 2.5     # background must stay ~pristine (focal locality)


def _laplacian_var(gray, mask_bool) -> float:
    """Variance of a discrete Laplacian over the masked interior.

    A 4-neighbour Laplacian (numpy roll) measures local high-frequency energy;
    its variance inside the focus mask is a sharpness proxy (the same family as
    OpenCV's classic ``Laplacian().var()`` blur metric, implemented without cv2).
    """
    import numpy as np

    lap = (
        -4.0 * gray
        + np.roll(gray, 1, 0)
        + np.roll(gray, -1, 0)
        + np.roll(gray, 1, 1)
        + np.roll(gray, -1, 1)
    )
    vals = lap[mask_bool]
    return float(vals.var()) if vals.size else 0.0


def compute_fidelity_probes(
    raw_path: Path, enhanced_path: Path, mask_path: Path
) -> dict[str, Any]:
    """Compute the three fidelity probes for one (raw, enhanced, mask) triple."""
    import numpy as np
    from PIL import Image

    with Image.open(raw_path) as _r:
        raw = _r.convert("RGB")
    with Image.open(enhanced_path) as _e:
        enh = _e.convert("RGB")
    if enh.size != raw.size:
        enh = enh.resize(raw.size, Image.LANCZOS)
    with Image.open(mask_path) as _m:
        mask = _m.convert("L")
    if mask.size != raw.size:
        mask = mask.resize(raw.size, Image.NEAREST)

    raw_ycc = np.asarray(raw.convert("YCbCr"), dtype=np.float64)
    enh_ycc = np.asarray(enh.convert("YCbCr"), dtype=np.float64)
    m = np.asarray(mask) > 127
    out = ~m

    raw_y, enh_y = raw_ycc[..., 0], enh_ycc[..., 0]

    # 1. Focal high-frequency energy ratio.
    raw_hf = _laplacian_var(raw_y, m)
    enh_hf = _laplacian_var(enh_y, m)
    hf_ratio = float(enh_hf / raw_hf) if raw_hf > 1e-9 else (1.0 if enh_hf <= 1e-9 else 99.0)

    # 2. Tone / colour shift inside the mask.
    in_n = int(m.sum())
    if in_n:
        luma_signed = float((enh_y[m] - raw_y[m]).mean())
        d_cb = float(np.abs(enh_ycc[..., 1][m] - raw_ycc[..., 1][m]).mean())
        d_cr = float(np.abs(enh_ycc[..., 2][m] - raw_ycc[..., 2][m]).mean())
        chroma_abs = (d_cb + d_cr) / 2.0
    else:
        luma_signed = 0.0
        chroma_abs = 0.0

    # 3. Background locality (mean |ΔY| outside the mask).
    out_n = int(out.sum())
    out_mask_delta = (
        float(np.abs(enh_y[out] - raw_y[out]).mean()) if out_n else 0.0
    )
    in_mask_delta = (
        float(np.abs(enh_y[m] - raw_y[m]).mean()) if in_n else 0.0
    )

    return {
        "hf_energy_ratio": round(hf_ratio, 4),
        "raw_hf_var": round(raw_hf, 3),
        "enh_hf_var": round(enh_hf, 3),
        "luma_signed_shift": round(luma_signed, 3),
        "chroma_abs_shift": round(chroma_abs, 3),
        "in_mask_mean_abs_delta": round(in_mask_delta, 3),
        "out_mask_mean_abs_delta": round(out_mask_delta, 3),
        "mask_px_in": in_n,
        "mask_px_out": out_n,
    }


def prescreen_verdict(probes: dict[str, Any]) -> dict[str, Any]:
    """Turn probes into ``{passed: bool, reasons: [str]}``.

    A FAIL means the arm looks like 磨皮/重绘 on objective evidence and should be
    culled before the VLM judge (saves quota, encodes the保真 philosophy).
    """
    reasons: list[str] = []
    if probes["hf_energy_ratio"] < HF_RATIO_MIN:
        reasons.append(
            f"focal high-frequency energy collapsed (ratio "
            f"{probes['hf_energy_ratio']} < {HF_RATIO_MIN}) — looks like 磨皮/smoothing"
        )
    if probes["luma_signed_shift"] < LUMA_DARKEN_MIN:
        reasons.append(
            f"focal darkened (luma shift {probes['luma_signed_shift']} "
            f"< {LUMA_DARKEN_MIN}) — tone not preserved"
        )
    if abs(probes["luma_signed_shift"]) > LUMA_SHIFT_ABS_MAX:
        reasons.append(
            f"focal luma shifted (|{probes['luma_signed_shift']}| "
            f"> {LUMA_SHIFT_ABS_MAX}) — tone not preserved"
        )
    if probes["chroma_abs_shift"] > CHROMA_ABS_MAX:
        reasons.append(
            f"focal colour shifted (chroma {probes['chroma_abs_shift']} "
            f"> {CHROMA_ABS_MAX}) — de-saturation / colour-cast"
        )
    if probes["out_mask_mean_abs_delta"] > OUT_MASK_DELTA_MAX:
        reasons.append(
            f"background changed (out-mask Δ {probes['out_mask_mean_abs_delta']} "
            f"> {OUT_MASK_DELTA_MAX}) — not local, looks like whole-image 重绘"
        )
    return {"passed": not reasons, "reasons": reasons}
