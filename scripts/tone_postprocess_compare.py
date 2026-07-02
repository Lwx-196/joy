"""Compare old vs new tone postprocess parameters on real after-treatment images.

Usage: python scripts/tone_postprocess_compare.py
Output: /tmp/tone-compare/ with old/ new/ subdirs + delta stats printed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.services.ai_generation.adapter import _apply_comfyui_tone_detail_postprocess

OLD_SETTINGS = {
    "enabled": True,
    "strategy": "clinical_candidate_fidelity_guard_v11",
    "candidate_only": True,
    "mask_mode": "focus_mask_feathered",
    "midtone_lift": 1.01,
    "highlight_lift": 1.0,
    "local_contrast": 1.0,
    "detail_sharpness": 1.0,
    "shadow_lift": 1.06,
    "shadow_detail_contrast": 1.0,
    "shadow_threshold": 128,
    "global_luma_lift": 1.005,
    "global_max_delta": 3,
    "max_delta": 8,
    "preserve_chroma": True,
    "reference_chroma_match_strength": 1.0,
    "max_chroma_shift_delta": 4,
    "reference_luma_floor_delta": -4,
    "reference_luma_floor_max_lift": 32,
    "max_shadow_contrast_delta": 8,
    "shadow_floor_lift_max": 36,
    "max_highlight_p95_delta": 3,
    "max_highlight_p99_delta": 4,
    "highlight_guard_max_darken": 128,
    "specular_threshold": 228,
    "max_specular_ratio_delta": 0.006,
    "reference_blend_strength": 0.55,
    "face_tone_guard_enabled": True,
    "face_luma_target_delta": 5,
    "face_luma_max_lift": 14,
    "face_background_contrast_target_delta": 4,
    "face_contrast_max_lift": 6,
    "face_tone_highlight_protect_threshold": 190,
    "semantic_fidelity_guard_enabled": True,
    "background_preserve_blend_strength": 1.0,
    "feature_protect_blend_strength": 1.0,
    "feature_protect_min_delta": 4.0,
    "feature_protect_min_excess_delta": 0.5,
    "face_chroma_guard_enabled": True,
    "face_chroma_max_delta": 2.5,
    "face_chroma_blend_strength": 0.9,
}

NEW_SETTINGS = {
    "enabled": True,
    "strategy": "clinical_candidate_fidelity_guard_v11",
    "candidate_only": True,
    "mask_mode": "focus_mask_feathered",
    "midtone_lift": 1.005,
    "highlight_lift": 1.0,
    "local_contrast": 1.0,
    "detail_sharpness": 1.0,
    "shadow_lift": 1.03,
    "shadow_detail_contrast": 1.0,
    "shadow_threshold": 128,
    "global_luma_lift": 1.0,
    "global_max_delta": 3,
    "max_delta": 8,
    "preserve_chroma": True,
    "reference_chroma_match_strength": 1.0,
    "max_chroma_shift_delta": 4,
    "reference_luma_floor_delta": -4,
    "reference_luma_floor_max_lift": 32,
    "max_shadow_contrast_delta": 8,
    "shadow_floor_lift_max": 36,
    "max_highlight_p95_delta": 1,
    "max_highlight_p99_delta": 2,
    "highlight_guard_max_darken": 128,
    "specular_threshold": 228,
    "max_specular_ratio_delta": 0.006,
    "reference_blend_strength": 0.55,
    "face_tone_guard_enabled": True,
    "face_luma_target_delta": 3,
    "face_luma_max_lift": 8,
    "face_background_contrast_target_delta": 2,
    "face_contrast_max_lift": 6,
    "face_tone_highlight_protect_threshold": 190,
    "semantic_fidelity_guard_enabled": True,
    "background_preserve_blend_strength": 1.0,
    "feature_protect_blend_strength": 1.0,
    "feature_protect_min_delta": 4.0,
    "feature_protect_min_excess_delta": 0.5,
    "face_chroma_guard_enabled": True,
    "face_chroma_max_delta": 2.5,
    "face_chroma_blend_strength": 0.9,
}

TEST_IMAGES = [
    "/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/黄靖榕/2026.3.31弗缦1支注射泪沟，薇旖美1支注射眼下/术后1.jpg",
    "/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/赵建芬/2026.2.10玻尿酸注射法令纹/术后1.jpg",
    "/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/高雅静/2026.1.25玻尿酸卧蚕 唇填充/术后1.jpg",
    "/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/蔡春柳/25.6.11泪沟填充-妮凯丽/术后7.jpg",
    "/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/李建凤/2025.12.10反重力，面部除皱，玻尿酸丰唇/术后1.jpg",
]

BEFORE_IMAGES = [
    "/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/黄靖榕/2026.3.31弗缦1支注射泪沟，薇旖美1支注射眼下/术前1.jpg",
    "/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/赵建芬/2026.2.10玻尿酸注射法令纹/术前1.jpg",
    "/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/高雅静/2026.1.25玻尿酸卧蚕 唇填充/术前1.jpg",
    "/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/蔡春柳/25.6.11泪沟填充-妮凯丽/术后8.jpg",
    "/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/李建凤/2025.12.10反重力，面部除皱，玻尿酸丰唇/术前1.jpg",
]

OUTPUT_DIR = Path("/tmp/tone-compare")


def pixel_stats(path: Path) -> dict:
    from PIL import Image, ImageStat
    with Image.open(path) as img:
        img = img.convert("RGB")
        stat = ImageStat.Stat(img)
        return {
            "mean_r": round(stat.mean[0], 1),
            "mean_g": round(stat.mean[1], 1),
            "mean_b": round(stat.mean[2], 1),
            "mean_luma": round(0.299 * stat.mean[0] + 0.587 * stat.mean[1] + 0.114 * stat.mean[2], 1),
        }


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "old").mkdir(exist_ok=True)
    (OUTPUT_DIR / "new").mkdir(exist_ok=True)

    print(f"{'Image':<20} {'Metric':<12} {'Original':>10} {'Old params':>10} {'New params':>10} {'Old Δ':>8} {'New Δ':>8}")
    print("-" * 90)

    for i, (after_path_str, before_path_str) in enumerate(zip(TEST_IMAGES, BEFORE_IMAGES)):
        after_path = Path(after_path_str)
        before_path = Path(before_path_str)
        if not after_path.is_file():
            print(f"SKIP (not found): {after_path}")
            continue

        label = after_path.parent.name.split("/")[-1][:15]
        slug = f"case{i+1}"

        ref_path = before_path if before_path.is_file() else None

        old_out = OUTPUT_DIR / "old" / f"{slug}.jpg"
        new_out = OUTPUT_DIR / "new" / f"{slug}.jpg"

        report_old = _apply_comfyui_tone_detail_postprocess(
            after_path, old_out, mask_path=None, reference_path=ref_path, settings=OLD_SETTINGS,
        )
        report_new = _apply_comfyui_tone_detail_postprocess(
            after_path, new_out, mask_path=None, reference_path=ref_path, settings=NEW_SETTINGS,
        )

        orig_stats = pixel_stats(after_path)
        old_stats = pixel_stats(old_out)
        new_stats = pixel_stats(new_out)

        for metric in ["mean_luma", "mean_r", "mean_g", "mean_b"]:
            orig_v = orig_stats[metric]
            old_v = old_stats[metric]
            new_v = new_stats[metric]
            old_delta = round(old_v - orig_v, 1)
            new_delta = round(new_v - orig_v, 1)
            print(f"{slug:<20} {metric:<12} {orig_v:>10.1f} {old_v:>10.1f} {new_v:>10.1f} {old_delta:>+8.1f} {new_delta:>+8.1f}")

        # Print key report fields
        for key in ["masked_shadow_p10_delta", "masked_highlight_p95_delta", "masked_highlight_p99_delta"]:
            old_v = getattr(report_old, key, None) if hasattr(report_old, key) else report_old.get(key, "?")
            new_v = getattr(report_new, key, None) if hasattr(report_new, key) else report_new.get(key, "?")
            if isinstance(old_v, float):
                print(f"  {key}: old={old_v:+.1f}  new={new_v:+.1f}")

        print()

    print(f"\nOutputs in {OUTPUT_DIR}")
    print(f"  old/ = pre-commit parameters (more aggressive)")
    print(f"  new/ = post-commit parameters (conservative)")
    print(f"  Compare visually: open {OUTPUT_DIR}/old/ and {OUTPUT_DIR}/new/ side by side")


if __name__ == "__main__":
    main()
