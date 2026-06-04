#!/usr/bin/env python3
"""case_layout_pick.py

从 `案例分类` 结果中为 ready_* 案例挑出推荐图，并导出标准命名目录。
输出：
- pick-summary.json
- pick-report.md
- picked/<customer>/<case>/*
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path


CASE_LAYOUT_PATH = Path(__file__).resolve().parent / "case_layout_board.py"


def load_module(module_name: str, module_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CASE_LAYOUT = load_module("case_layout_board", CASE_LAYOUT_PATH)

READY_CATEGORIES = {"ready_tri_compare", "ready_bi_compare", "ready_single_compare", "ready_body_dual_compare"}
FACE_RENDER_VALIDATE_CATEGORIES = {"ready_tri_compare", "ready_bi_compare", "ready_single_compare"}
PICK_REUSE_DIRNAME = ".case-layout-pick"
PICK_REUSE_INSPECT_MANIFEST_NAME = "render-inspect-manifest.json"
PICK_REUSE_VERSION = 1
VIEW_EXPORT_LABELS = {
    "front": "正面",
    "oblique": "45侧",
    "side": "侧面",
    "back": "背面",
}
DIR_EXPORT_LABELS = {
    "left": "左",
    "right": "右",
}
VIEW_PRIORITY = ["front", "oblique", "side", "back"]
QUALITY_SCORE = {"good": 40, "fair": 20, "poor": 0}
PHASE_SCORE = {"术前": 12, "术后": 12, "不确定": 0}
ALT_LIMIT = 5


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_picked_image_files(picked_case_dir: Path):
    for file_path in picked_case_dir.rglob("*"):
        try:
            relative_parts = file_path.relative_to(picked_case_dir).parts
        except ValueError:
            continue
        if any(part.startswith(".") for part in relative_parts):
            continue
        if CASE_LAYOUT.is_image_file(file_path):
            yield file_path


def build_picked_image_signatures(picked_case_dir: Path) -> list[dict]:
    signatures = []
    for file_path in sorted(iter_picked_image_files(picked_case_dir), key=lambda item: str(item.relative_to(picked_case_dir))):
        stat = file_path.stat()
        signatures.append({
            "relative_path": file_path.relative_to(picked_case_dir).as_posix(),
            "size": stat.st_size,
            "mtime_ns": str(stat.st_mtime_ns),
            "sha256": sha256_file(file_path),
        })
    return signatures


def write_reusable_inspect_manifest(picked_case_dir: Path, manifest: dict, brand: dict, template: str = "tri-compare") -> Path:
    reusable_manifest = json.loads(json.dumps(manifest, ensure_ascii=False))
    reusable_manifest["pick_reuse"] = {
        "version": PICK_REUSE_VERSION,
        "created_at": CASE_LAYOUT.now_iso(),
        "source": "case_layout_pick",
        "picked_case_dir": str(picked_case_dir.resolve()),
        "brand_id": brand["id"],
        "template": template,
        "semantic_judge_mode": reusable_manifest.get("semantic_judge_mode"),
        "image_signatures": build_picked_image_signatures(picked_case_dir),
    }
    reusable_manifest["outputs"] = {}
    cache_path = picked_case_dir / PICK_REUSE_DIRNAME / PICK_REUSE_INSPECT_MANIFEST_NAME
    write_json(cache_path, reusable_manifest)
    return cache_path


def discover_run_dir(input_path: Path) -> tuple[Path, tuple[str, str] | None]:
    target = input_path.resolve()
    current = target
    while True:
        if (current / "classify-summary.json").exists() and (current / "classify-images.json").exists():
            run_dir = current
            break
        if current.parent == current:
            raise ValueError(f"未发现可用的案例分类结果目录: {input_path}")
        current = current.parent

    case_filter = None
    classified_root = run_dir / "classified"
    try:
        rel = target.relative_to(classified_root)
        if len(rel.parts) >= 3:
            case_filter = (rel.parts[1], rel.parts[2])
    except ValueError:
        pass
    return run_dir, case_filter


def build_out_dir(run_dir: Path, out_dir: str | None) -> Path:
    run_id = CASE_LAYOUT.now_iso().replace(":", "").replace("+", "_").replace("-", "").replace(".", "")
    if out_dir:
        return Path(out_dir).resolve() / run_id
    return run_dir / "pick" / run_id


def image_score(image_record: dict) -> float:
    score = 0.0
    score += QUALITY_SCORE.get(image_record.get("quality"), 0)
    score += 50 if image_record.get("usable") else 0
    score += PHASE_SCORE.get(image_record.get("phase_guess"), 0)
    if image_record.get("direction_guess") not in {"unknown", "center", None}:
        score += 6
    if image_record.get("route_source") == "inspect":
        score += 5
    sharpness_score = float(image_record.get("sharpness_score") or 0.0)
    score += min(sharpness_score, 120.0) / 10.0
    return score


def normalize_phase_key(phase_guess: str) -> str | None:
    if phase_guess == "术前":
        return "before"
    if phase_guess == "术后":
        return "after"
    return None


def phase_label(phase_key: str) -> str:
    return CASE_LAYOUT.PHASE_LABELS["before" if phase_key == "before" else "after"]


def same_path(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return Path(left).resolve() == Path(right).resolve()
    except Exception:
        return str(left) == str(right)


def normalize_candidate_entry(candidate: dict, selected_source_path: str | None = None) -> dict:
    source_path = (
        candidate.get("source_path")
        or candidate.get("file_path")
        or candidate.get("path")
    )
    direction = (
        candidate.get("direction")
        or candidate.get("direction_guess")
        or "unknown"
    )
    payload = {
        "source_path": source_path,
        "file_path": source_path,
        "name": candidate.get("name") or (Path(source_path).name if source_path else ""),
        "score": round(float(candidate.get("score") or 0.0), 2),
        "direction": direction,
        "direction_guess": direction,
        "angle_source": candidate.get("angle_source"),
        "sharpness_score": candidate.get("sharpness_score"),
        "sharpness_level": candidate.get("sharpness_level"),
        "quality": candidate.get("quality"),
        "usable": candidate.get("usable", True),
        "is_current": same_path(source_path, selected_source_path),
    }
    return payload


def prioritize_current_candidates(candidates: list[dict], selected_source_path: str | None, limit: int = ALT_LIMIT) -> list[dict]:
    normalized = [normalize_candidate_entry(item, selected_source_path) for item in candidates]
    current = [item for item in normalized if item.get("is_current")]
    others = [item for item in normalized if not item.get("is_current")]
    if selected_source_path and not current:
        current.insert(0, normalize_candidate_entry({
            "source_path": selected_source_path,
            "name": Path(selected_source_path).name,
            "score": 0.0,
            "direction": "unknown",
            "usable": True,
        }, selected_source_path))
    return (current[:1] + others)[:limit]


def build_inspect_alternatives(
    entries: list[dict],
    slot: str,
    phase_key: str,
    selected_source_path: str | None,
    preferred_direction: str | None,
    paired_source_path: str | None = None,
    group_name: str | None = None,
    angle_priority_profile: dict | None = None,
) -> list[dict]:
    candidate_matrix = CASE_LAYOUT.build_phase_slot_candidates(
        entries,
        angle_priority_profile=angle_priority_profile,
    )
    candidates = list((candidate_matrix.get(phase_key) or {}).get(slot) or [])
    directional_preference = preferred_direction if slot != "front" else None
    if directional_preference:
        directional = [
            item
            for item in candidates
            if item.get("direction") == directional_preference
            or item.get("direction") in {"center", "unspecified", "unknown"}
        ]
        if directional:
            candidates = directional

    ranked = sorted(
        candidates,
        key=lambda item: (
            CASE_LAYOUT.candidate_score(item, slot, directional_preference),
            item.get("name"),
        ),
        reverse=True,
    )
    paired_entry = None
    if paired_source_path:
        for entry in entries:
            if same_path(entry.get("path"), paired_source_path):
                paired_entry = entry
                break

    payloads = []
    for item in ranked:
        if paired_entry:
            before_candidates = [item] if phase_key == "before" else [paired_entry]
            after_candidates = [paired_entry] if phase_key == "before" else [item]
            validated, _blocking, _warnings, _rejections = CASE_LAYOUT.resolve_slot_pair(
                group_name or slot,
                slot,
                before_candidates,
                after_candidates,
                focus_targets=None,
                semantic_context=None,
            )
            if not validated:
                continue
        payloads.append({
            "source_path": item.get("path"),
            "name": item.get("name"),
            "score": CASE_LAYOUT.candidate_score(item, slot, directional_preference),
            "direction": item.get("direction"),
            "angle_source": item.get("angle_source"),
            "sharpness_score": item.get("sharpness_score"),
            "sharpness_level": item.get("sharpness_level"),
            "usable": True,
        })
    return prioritize_current_candidates(payloads, selected_source_path)


def build_body_alternatives(
    entries: list[dict],
    slot: str,
    phase_key: str,
    selected_source_path: str | None,
) -> list[dict]:
    candidates = [
        item
        for item in entries
        if item.get("phase") == phase_key
        and item.get("section") == slot
        and not item.get("rejection_reason")
    ]
    ranked = sorted(
        candidates,
        key=lambda item: (CASE_LAYOUT.body_candidate_score(item), item.get("name")),
        reverse=True,
    )
    payloads = [
        {
            "source_path": item.get("path"),
            "name": item.get("name"),
            "score": CASE_LAYOUT.body_candidate_score(item),
            "direction": item.get("direction"),
            "sharpness_score": item.get("sharpness_score"),
            "sharpness_level": item.get("sharpness_level"),
            "usable": True,
        }
        for item in ranked
    ]
    return prioritize_current_candidates(payloads, selected_source_path)


def split_slot_key(slot_key: str, groups: list[dict] | None = None) -> tuple[str | None, str]:
    if "::" in slot_key:
        group_name, slot = slot_key.split("::", 1)
        return group_name, slot
    group_items = groups or []
    if len(group_items) == 1:
        return group_items[0].get("name"), slot_key
    return None, slot_key


def selection_has_alternatives(selection: dict) -> bool:
    alternatives = selection.get("alternatives") or {}
    return bool(alternatives.get("before")) and bool(alternatives.get("after"))


def normalize_selection_alternatives(selection: dict) -> dict:
    alternatives = selection.get("alternatives") or {}
    selection["alternatives"] = {
        "before": prioritize_current_candidates(
            alternatives.get("before") or [],
            ((selection.get("before") or {}).get("source_path")),
        ),
        "after": prioritize_current_candidates(
            alternatives.get("after") or [],
            ((selection.get("after") or {}).get("source_path")),
        ),
    }
    return selection


def ensure_manifest_alternatives(manifest: dict) -> dict:
    groups = manifest.get("groups") or []
    for selection in (manifest.get("selected_slots") or {}).values():
        normalize_selection_alternatives(selection)

    needs_backfill = any(
        not selection_has_alternatives(selection)
        for selection in (manifest.get("selected_slots") or {}).values()
    )
    if not needs_backfill:
        return manifest

    case_dir = manifest.get("case_dir")
    if not case_dir or not groups:
        return manifest

    rebuilt = CASE_LAYOUT.build_manifest(
        Path(case_dir).resolve(),
        CASE_LAYOUT.resolve_brand("fumei"),
        "tri-compare",
        semantic_judge_mode="off",
    )
    rebuilt_groups = {group["name"]: group for group in rebuilt.get("groups") or []}
    default_group_name = None
    if len(groups) == 1:
        default_group_name = groups[0].get("name")

    for slot_key, selection in (manifest.get("selected_slots") or {}).items():
        group_name, slot = split_slot_key(slot_key, groups)
        if not group_name:
            group_name = default_group_name
        group = rebuilt_groups.get(group_name)
        if not group:
            continue
        preferred_direction = selection.get("direction")
        selection["alternatives"] = {
            "before": build_inspect_alternatives(
                group.get("entries") or [],
                slot,
                "before",
                ((selection.get("before") or {}).get("source_path")),
                preferred_direction,
                paired_source_path=((selection.get("after") or {}).get("source_path")),
                group_name=group_name,
                angle_priority_profile=rebuilt.get("angle_priority_profile") or {},
            ),
            "after": build_inspect_alternatives(
                group.get("entries") or [],
                slot,
                "after",
                ((selection.get("after") or {}).get("source_path")),
                preferred_direction,
                paired_source_path=((selection.get("before") or {}).get("source_path")),
                group_name=group_name,
                angle_priority_profile=rebuilt.get("angle_priority_profile") or {},
            ),
        }
        normalize_selection_alternatives(selection)
    return manifest


def list_render_views(primary_category: str, grouped: dict[str, dict[str, list[dict]]]) -> list[str]:
    if primary_category == "ready_body_dual_compare":
        for slot in ["back", "oblique", "side"]:
            if grouped.get(slot, {}).get("before") and grouped.get(slot, {}).get("after"):
                return ["front", slot]
        return ["front"]
    if primary_category == "ready_single_compare":
        return ["front"]
    if primary_category == "ready_tri_compare":
        return ["front", "oblique", "side"]
    if primary_category == "ready_bi_compare":
        for slot in ["oblique", "side", "back"]:
            if grouped.get(slot, {}).get("before") and grouped.get(slot, {}).get("after"):
                return ["front", slot]
        return ["front"]
    return []


def filter_candidates_for_direction(candidates: list[dict], direction: str | None) -> list[dict]:
    if not direction:
        return candidates
    filtered = [
        item for item in candidates
        if item.get("direction_guess") == direction
        or item.get("direction_guess") in {"unknown", "center", None}
    ]
    return filtered or candidates


def pick_best_pair(slot: str, grouped: dict[str, dict[str, list[dict]]]) -> dict | None:
    before_candidates = grouped.get(slot, {}).get("before") or []
    after_candidates = grouped.get(slot, {}).get("after") or []
    if not before_candidates or not after_candidates:
        return None

    def sorted_candidates(items: list[dict]) -> list[dict]:
        return sorted(
            items,
            key=lambda item: (image_score(item), Path(item["file_path"]).name),
            reverse=True,
        )

    before_ranked = sorted_candidates(before_candidates)
    after_ranked = sorted_candidates(after_candidates)

    if slot == "front":
        return {
            "slot": slot,
            "direction": "center",
            "before": before_ranked[0],
            "after": after_ranked[0],
            "alternatives": {
                "before": before_ranked[:3],
                "after": after_ranked[:3],
            },
        }

    before_dirs = {
        item.get("direction_guess")
        for item in before_ranked
        if item.get("direction_guess") not in {"unknown", "center", None}
    }
    after_dirs = {
        item.get("direction_guess")
        for item in after_ranked
        if item.get("direction_guess") not in {"unknown", "center", None}
    }
    preferred_directions = sorted(before_dirs & after_dirs)
    if not preferred_directions:
        preferred_directions = [None]

    best = None
    best_score = -1e9
    for direction in preferred_directions:
        before_filtered = sorted_candidates(filter_candidates_for_direction(before_ranked, direction))
        after_filtered = sorted_candidates(filter_candidates_for_direction(after_ranked, direction))
        if not before_filtered or not after_filtered:
            continue
        score = image_score(before_filtered[0]) + image_score(after_filtered[0])
        if direction:
            score += 10
        if score > best_score:
            best_score = score
            best = {
                "slot": slot,
                "direction": direction or "unknown",
                "before": before_filtered[0],
                "after": after_filtered[0],
                "alternatives": {
                    "before": before_filtered[:3],
                    "after": after_filtered[:3],
                },
            }
    return best


def export_selected_pair(case_root: Path, pair: dict) -> dict:
    exported = {}
    for phase_key in ("before", "after"):
        source = Path(pair[phase_key]["file_path"])
        suffix = source.suffix.lower()
        direction_prefix = ""
        if pair["slot"] in {"oblique", "side"} and pair.get("direction") in DIR_EXPORT_LABELS:
            direction_prefix = DIR_EXPORT_LABELS[pair["direction"]]
        output_name = f"{phase_label(phase_key)}-{direction_prefix}{VIEW_EXPORT_LABELS[pair['slot']]}{suffix}"
        output_path = case_root / output_name
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, output_path)
        exported[phase_key] = str(output_path.resolve())
    return exported


def clear_picked_paths(selected_slots: dict) -> None:
    for selection in selected_slots.values():
        for phase_key in ("before", "after"):
            phase_payload = selection.get(phase_key)
            if isinstance(phase_payload, dict):
                phase_payload.pop("picked_path", None)


def validate_face_pick_against_render_inspect(picked_case_dir: Path, brand: dict) -> dict:
    try:
        manifest = CASE_LAYOUT.build_manifest(
            picked_case_dir,
            brand,
            "tri-compare",
            semantic_judge_mode="off",
        )
    except Exception as exc:
        return {
            "status": "error",
            "blocking_issues": [f"{picked_case_dir.name}：picked 目录 render inspect 校验失败：{exc}"],
            "manifest": None,
        }

    if manifest.get("status") == "ok":
        return {
            "status": "ok",
            "blocking_issues": [],
            "manifest": manifest,
        }

    blocking_issues = list(manifest.get("blocking_issues") or [])
    if not blocking_issues:
        blocking_issues.append(f"{picked_case_dir.name}：picked 目录未通过 render inspect 姿态一致性校验")
    return {
        "status": "error",
        "blocking_issues": blocking_issues,
        "manifest": manifest,
    }


def summarize_case_from_inspect(case_record: dict, out_dir: Path, brand: dict) -> dict:
    case_dir = Path(case_record["case_dir"]).resolve()
    manifest = CASE_LAYOUT.build_manifest(case_dir, brand, "tri-compare", semantic_judge_mode="off")
    case_mode = manifest.get("case_mode") or "face"
    picked_case_dir = out_dir / "picked" / case_record["customer"] / case_record["case_name"]
    if manifest.get("status") != "ok":
        return {
            "customer": case_record["customer"],
            "case_name": case_record["case_name"],
            "case_dir": case_record["case_dir"],
            "primary_category": case_record["primary_category"],
            "status": "blocked",
            "reason": case_record.get("reason") or case_record["primary_category"],
            "render_views": [],
            "blocking_issues": list(manifest.get("blocking_issues") or []),
            "selected_slots": {},
            "picked_case_dir": None,
            "groups": [
                {
                    "name": group.get("name"),
                    "picked_group_dir": None,
                    "render_views": [],
                    "selected_slots": {},
                    "blocking_issues": list(group.get("blocking_issues") or []),
                }
                for group in manifest.get("groups") or []
            ],
        }

    groups_out = []
    flat_selected_slots = {}
    multi_group = len(manifest.get("groups") or []) > 1
    for group in manifest.get("groups") or []:
        if not group.get("selected_slots"):
            continue
        group_root = picked_case_dir
        if multi_group and group.get("relative_path") not in {".", ""}:
            group_root = picked_case_dir / group["name"]
        group_selected = {}
        for slot in group.get("render_slots") or []:
            selected = (group.get("selected_slots") or {}).get(slot)
            if not selected:
                continue
            flat_key = slot if not multi_group else f"{group['name']}::{slot}"
            pair = {
                "slot": slot,
                "direction": selected.get("direction"),
                "before": {"file_path": selected["before"]["path"]},
                "after": {"file_path": selected["after"]["path"]},
            }
            exported = export_selected_pair(group_root, pair)
            if case_mode == "body":
                alternatives = {
                    "before": build_body_alternatives(
                        group.get("entries") or [],
                        slot,
                        "before",
                        selected["before"]["path"],
                    ),
                    "after": build_body_alternatives(
                        group.get("entries") or [],
                        slot,
                        "after",
                        selected["after"]["path"],
                    ),
                }
            else:
                alternatives = {
                    "before": build_inspect_alternatives(
                        group.get("entries") or [],
                        slot,
                        "before",
                        selected["before"]["path"],
                        selected.get("direction"),
                        paired_source_path=selected["after"]["path"],
                        group_name=group["name"],
                        angle_priority_profile=manifest.get("angle_priority_profile") or {},
                    ),
                    "after": build_inspect_alternatives(
                        group.get("entries") or [],
                        slot,
                        "after",
                        selected["after"]["path"],
                        selected.get("direction"),
                        paired_source_path=selected["before"]["path"],
                        group_name=group["name"],
                        angle_priority_profile=manifest.get("angle_priority_profile") or {},
                    ),
                }
            group_selected[slot] = {
                "slot": slot,
                "slot_key": flat_key,
                "group_name": group["name"],
                "slot_label": VIEW_EXPORT_LABELS.get(slot, slot),
                "direction": selected.get("direction"),
                "before": {
                    "source_path": selected["before"]["path"],
                    "picked_path": exported["before"],
                },
                "after": {
                    "source_path": selected["after"]["path"],
                    "picked_path": exported["after"],
                },
                "alternatives": alternatives,
            }
            flat_selected_slots[flat_key] = group_selected[slot]
        groups_out.append({
            "name": group["name"],
            "picked_group_dir": str(group_root.resolve()),
            "render_views": list(group_selected.keys()),
            "selected_slots": group_selected,
            "blocking_issues": list(group.get("blocking_issues") or []),
        })

    validation_manifest = None
    validation_blocking = []
    if (
        manifest.get("status") == "ok"
        and flat_selected_slots
        and case_mode != "body"
        and case_record.get("primary_category") in FACE_RENDER_VALIDATE_CATEGORIES
    ):
        validation = validate_face_pick_against_render_inspect(picked_case_dir, brand)
        if validation["status"] == "ok":
            validation_manifest = validation["manifest"]
        else:
            validation_blocking = list(validation["blocking_issues"])
            clear_picked_paths(flat_selected_slots)
            shutil.rmtree(picked_case_dir, ignore_errors=True)

    status = "ready" if manifest.get("status") == "ok" and flat_selected_slots and not validation_blocking else "blocked"
    result = {
        "customer": case_record["customer"],
        "case_name": case_record["case_name"],
        "case_dir": case_record["case_dir"],
        "primary_category": case_record["primary_category"],
        "status": status,
        "reason": case_record.get("reason") or case_record["primary_category"],
        "render_views": list(flat_selected_slots.keys()) if status == "ready" else [],
        "blocking_issues": list(manifest.get("blocking_issues") or []) + validation_blocking,
        "selected_slots": flat_selected_slots,
        "picked_case_dir": str(picked_case_dir.resolve()) if status == "ready" and flat_selected_slots else None,
        "groups": groups_out,
    }
    if status == "ready" and flat_selected_slots:
        ensure_manifest_alternatives(result)
        if validation_manifest:
            write_reusable_inspect_manifest(picked_case_dir, validation_manifest, brand)
        write_json(picked_case_dir / "pick-manifest.json", result)
    return result


def summarize_case(case_record: dict, image_records: list[dict], out_dir: Path, brand: dict) -> dict:
    primary_category = case_record["primary_category"]
    if primary_category not in READY_CATEGORIES:
        return {
            "customer": case_record["customer"],
            "case_name": case_record["case_name"],
            "case_dir": case_record["case_dir"],
            "primary_category": primary_category,
            "status": "skipped",
            "reason": case_record.get("reason") or primary_category,
            "picked_case_dir": None,
            "selected_slots": {},
        }

    if case_record.get("route_source") == "inspect":
        return summarize_case_from_inspect(case_record, out_dir, brand)

    grouped = defaultdict(lambda: {"before": [], "after": []})
    for item in image_records:
        phase_key = normalize_phase_key(item.get("phase_guess"))
        if not phase_key:
            continue
        grouped[item.get("view_guess") or "other"][phase_key].append(item)

    render_views = list_render_views(primary_category, grouped)
    picked_case_dir = out_dir / "picked" / case_record["customer"] / case_record["case_name"]
    selected_slots = {}
    blocking = []
    for slot in render_views:
        pair = pick_best_pair(slot, grouped)
        if not pair:
            blocking.append(f"缺少可用 {slot} 配对")
            continue
        exported = export_selected_pair(picked_case_dir, pair)
        selected_slots[slot] = {
            "slot": slot,
            "slot_key": slot,
            "group_name": None,
            "slot_label": VIEW_EXPORT_LABELS.get(slot, slot),
            "direction": pair["direction"],
            "before": {
                "source_path": pair["before"]["file_path"],
                "picked_path": exported["before"],
                "score": round(image_score(pair["before"]), 2),
            },
            "after": {
                "source_path": pair["after"]["file_path"],
                "picked_path": exported["after"],
                "score": round(image_score(pair["after"]), 2),
            },
            "alternatives": {
                phase: prioritize_current_candidates([
                    {
                        "source_path": item["file_path"],
                        "name": Path(item["file_path"]).name,
                        "score": image_score(item),
                        "direction": item.get("direction_guess"),
                        "quality": item.get("quality"),
                        "usable": item.get("usable"),
                        "sharpness_score": item.get("sharpness_score"),
                    }
                    for item in pair["alternatives"][phase]
                ], pair[phase]["file_path"])
                for phase in ("before", "after")
            },
        }

    validation_manifest = None
    if not blocking and selected_slots and primary_category in FACE_RENDER_VALIDATE_CATEGORIES:
        validation = validate_face_pick_against_render_inspect(picked_case_dir, brand)
        if validation["status"] == "ok":
            validation_manifest = validation["manifest"]
        else:
            blocking.extend(validation["blocking_issues"])
            clear_picked_paths(selected_slots)
            shutil.rmtree(picked_case_dir, ignore_errors=True)

    status = "ready" if not blocking and selected_slots else "blocked"
    manifest = {
        "customer": case_record["customer"],
        "case_name": case_record["case_name"],
        "case_dir": case_record["case_dir"],
        "primary_category": primary_category,
        "status": status,
        "reason": case_record.get("reason") or primary_category,
        "render_views": render_views if status == "ready" else [],
        "blocking_issues": blocking,
        "selected_slots": selected_slots,
        "picked_case_dir": str(picked_case_dir.resolve()) if status == "ready" and selected_slots else None,
    }
    if status == "ready" and selected_slots:
        ensure_manifest_alternatives(manifest)
        if validation_manifest:
            write_reusable_inspect_manifest(picked_case_dir, validation_manifest, brand)
        write_json(picked_case_dir / "pick-manifest.json", manifest)
    return manifest


def build_report(summary: dict) -> str:
    lines = [
        "# case-layout-board 挑图报告",
        "",
        f"- 时间: `{summary['created_at']}`",
        f"- 来源目录: `{summary['source_dir']}`",
        f"- 处理案例数: `{summary['processed_case_count']}`",
        f"- 已挑图案例数: `{summary['picked_case_count']}`",
        f"- 跳过案例数: `{summary['skipped_case_count']}`",
        "",
        "## 案例明细",
        "",
    ]

    for case in summary["cases"]:
        lines.append(
            f"- `{case['customer']} / {case['case_name']}`"
            f" | category=`{case['primary_category']}`"
            f" | status=`{case['status']}`"
            + (f" | {case['reason']}" if case.get("reason") else "")
        )
        if case.get("picked_case_dir"):
            lines.append(f"  picked: `{case['picked_case_dir']}`")
        for slot, selection in (case.get("selected_slots") or {}).items():
            lines.append(
                f"  - {selection['slot_label']}: 术前 `{Path(selection['before']['source_path']).name}` / "
                f"术后 `{Path(selection['after']['source_path']).name}`"
            )
        for issue in case.get("blocking_issues") or []:
            lines.append(f"  - blocking: {issue}")

    return "\n".join(lines).strip() + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从案例分类结果中自动挑图")
    parser.add_argument("input_path", help="案例分类输出目录或 classified 下的单案例目录")
    parser.add_argument("--out", help="输出目录；默认写到 <classify-run>/pick/<timestamp>")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir, case_filter = discover_run_dir(Path(args.input_path))
    out_dir = build_out_dir(run_dir, args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    classify_summary = read_json(run_dir / "classify-summary.json")
    classify_images = read_json(run_dir / "classify-images.json")
    image_index = defaultdict(list)
    for item in classify_images:
        image_index[(item["customer"], item["case_name"])].append(item)

    cases = []
    brand = CASE_LAYOUT.resolve_brand(classify_summary.get("brand") or "fumei")
    for case_record in classify_summary.get("records") or []:
        key = (case_record["customer"], case_record["case_name"])
        if case_filter and key != case_filter:
            continue
        cases.append(summarize_case(case_record, image_index.get(key) or [], out_dir, brand))

    if not cases:
        raise ValueError(f"在 {args.input_path} 未找到可处理案例")

    processed_case_count = len(cases)
    picked_case_count = sum(1 for item in cases if item["status"] == "ready")
    skipped_case_count = processed_case_count - picked_case_count
    category_counts = Counter(item["primary_category"] for item in cases)
    summary = {
        "created_at": CASE_LAYOUT.now_iso(),
        "source_dir": str(run_dir.resolve()),
        "processed_case_count": processed_case_count,
        "picked_case_count": picked_case_count,
        "skipped_case_count": skipped_case_count,
        "category_counts": dict(category_counts),
        "cases": cases,
    }

    summary_path = out_dir / "pick-summary.json"
    report_path = out_dir / "pick-report.md"
    write_json(summary_path, summary)
    report_path.write_text(build_report(summary), encoding="utf-8")

    print(json.dumps({
        "source_dir": str(run_dir.resolve()),
        "out_dir": str(out_dir.resolve()),
        "processed_case_count": processed_case_count,
        "picked_case_count": picked_case_count,
        "skipped_case_count": skipped_case_count,
        "summary_path": str(summary_path.resolve()),
        "report_path": str(report_path.resolve()),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
