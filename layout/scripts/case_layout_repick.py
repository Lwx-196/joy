#!/usr/bin/env python3
"""case_layout_repick.py

对 `案例挑图` 产出的单案例 `pick-manifest.json` 做候选预览与原地改选。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
CASE_LAYOUT_PATH = SCRIPT_DIR / "case_layout_board.py"
CASE_LAYOUT_PICK_PATH = SCRIPT_DIR / "case_layout_pick.py"

SLOT_ALIASES = {
    "front": "front",
    "正面": "front",
    "oblique": "oblique",
    "45侧": "oblique",
    "45°侧": "oblique",
    "45度侧": "oblique",
    "45度": "oblique",
    "side": "side",
    "侧面": "side",
    "back": "back",
    "背面": "back",
}
PHASE_ALIASES = {
    "before": "before",
    "术前": "before",
    "after": "after",
    "术后": "after",
}


def load_module(module_name: str, module_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CASE_LAYOUT = load_module("case_layout_board", CASE_LAYOUT_PATH)
PICK = load_module("case_layout_pick", CASE_LAYOUT_PICK_PATH)


def discover_picked_case(input_path: Path) -> tuple[Path, Path]:
    target = input_path.resolve()
    if target.is_file():
        if target.name != "pick-manifest.json":
            raise ValueError(f"案例改选只接受 picked 单案例目录或 pick-manifest.json: {input_path}")
        return target.parent, target
    if target.is_dir():
        manifest_path = target / "pick-manifest.json"
        if manifest_path.exists():
            return target, manifest_path
    raise ValueError(f"案例改选只接受 picked 单案例目录或 pick-manifest.json: {input_path}")


def normalize_slot_token(raw: str) -> str:
    token = str(raw or "").strip()
    if "::" in token:
        group_name, slot_token = token.rsplit("::", 1)
        normalized_slot = SLOT_ALIASES.get(slot_token.strip(), slot_token.strip())
        return f"{group_name}::{normalized_slot}"
    return SLOT_ALIASES.get(token, token)


def normalize_phase_token(raw: str) -> str:
    token = str(raw or "").strip()
    normalized = PHASE_ALIASES.get(token)
    if not normalized:
        raise ValueError(f"不支持的 phase: {raw}")
    return normalized


def resolve_slot_key(manifest: dict, raw_slot: str) -> str:
    selected_slots = manifest.get("selected_slots") or {}
    normalized = normalize_slot_token(raw_slot)
    if normalized in selected_slots:
        return normalized

    if "::" in normalized:
        raise ValueError(f"未命中可改槽位: {raw_slot}")

    matches = [
        slot_key
        for slot_key in selected_slots
        if slot_key == normalized or slot_key.endswith(f"::{normalized}")
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"未命中可改槽位: {raw_slot}")
    raise ValueError(f"槽位 `{raw_slot}` 命中多个分组，请改成显式 key: {', '.join(matches)}")


def serialize_candidate(index: int, candidate: dict) -> dict:
    source_path = candidate.get("source_path") or candidate.get("file_path")
    return {
        "index": index,
        "name": candidate.get("name") or (Path(source_path).name if source_path else ""),
        "source_path": source_path,
        "score": round(float(candidate.get("score") or 0.0), 2),
        "direction": candidate.get("direction") or candidate.get("direction_guess"),
        "is_current": bool(candidate.get("is_current")),
    }


def build_preview_payload(case_dir: Path, manifest_path: Path, manifest: dict) -> dict:
    slots = []
    slot_order = manifest.get("render_views") or list((manifest.get("selected_slots") or {}).keys())
    for slot_key in slot_order:
        selection = (manifest.get("selected_slots") or {}).get(slot_key)
        if not selection:
            continue
        slot_payload = {
            "slot_key": slot_key,
            "slot_label": selection.get("slot_label") or slot_key,
            "direction": selection.get("direction"),
        }
        for phase_key in ("before", "after"):
            phase = selection.get(phase_key) or {}
            alternatives = (selection.get("alternatives") or {}).get(phase_key) or []
            slot_payload[phase_key] = {
                "current_name": Path(phase.get("source_path") or "").name if phase.get("source_path") else "",
                "current_source_path": phase.get("source_path"),
                "picked_path": phase.get("picked_path"),
                "alternatives": [
                    serialize_candidate(index, item)
                    for index, item in enumerate(alternatives, start=1)
                ],
            }
        slots.append(slot_payload)

    return {
        "mode": "preview",
        "customer": manifest.get("customer"),
        "case_name": manifest.get("case_name"),
        "picked_case_dir": str(case_dir.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "slot_count": len(slots),
        "slots": slots,
    }


def apply_repick(case_dir: Path, manifest_path: Path, manifest: dict, slot_key: str, phase_key: str, pick_index: int) -> dict:
    selected_slots = manifest.get("selected_slots") or {}
    selection = selected_slots.get(slot_key)
    if not selection:
        raise ValueError(f"未命中可改槽位: {slot_key}")

    alternatives = (selection.get("alternatives") or {}).get(phase_key) or []
    if not alternatives:
        raise ValueError(f"{slot_key} / {phase_key} 没有候选图可供改选")
    if pick_index < 1 or pick_index > len(alternatives):
        raise ValueError(f"{slot_key} / {phase_key} 候选编号越界: {pick_index}，可选 1-{len(alternatives)}")

    chosen = alternatives[pick_index - 1]
    chosen_source_path = chosen.get("source_path") or chosen.get("file_path")
    if not chosen_source_path:
        raise ValueError(f"{slot_key} / {phase_key} 候选 {pick_index} 缺少 source_path")

    picked_path = (selection.get(phase_key) or {}).get("picked_path")
    if not picked_path:
        raise ValueError(f"{slot_key} / {phase_key} 缺少 picked_path，无法原地改选")

    source_path = Path(chosen_source_path).resolve()
    target_path = Path(picked_path).resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    previous_source_path = (selection.get(phase_key) or {}).get("source_path")
    backup_path = target_path.with_name(f".{target_path.name}.repick-backup")
    if target_path.exists():
        shutil.copy2(target_path, backup_path)

    shutil.copy2(source_path, target_path)
    try:
        validated_manifest = CASE_LAYOUT.build_manifest(
            case_dir.resolve(),
            CASE_LAYOUT.resolve_brand("fumei"),
            "tri-compare",
            semantic_judge_mode="off",
        )
        group_name, slot_name = PICK.split_slot_key(slot_key, manifest.get("groups") or [])
        if group_name:
            validated_group = next(
                (group for group in (validated_manifest.get("groups") or []) if group.get("name") == group_name),
                None,
            )
            slot_kept = bool(validated_group and slot_name in (validated_group.get("selected_slots") or {}))
        else:
            slot_kept = any(
                slot_name in (group.get("selected_slots") or {})
                for group in (validated_manifest.get("groups") or [])
            )
        if validated_manifest.get("status") != "ok" or not slot_kept:
            reason = "; ".join((validated_manifest.get("blocking_issues") or [])[:2]) or "改选后 inspect 未通过"
            raise ValueError(f"{slot_key} / {phase_key} 候选 {pick_index} 无法通过正式预检: {reason}")
    except Exception:
        if backup_path.exists():
            shutil.copy2(backup_path, target_path)
        raise
    finally:
        if backup_path.exists():
            backup_path.unlink()

    selection.setdefault(phase_key, {})
    selection[phase_key]["source_path"] = str(source_path)
    selection[phase_key]["picked_path"] = str(target_path)
    if chosen.get("score") is not None:
        selection[phase_key]["score"] = round(float(chosen.get("score") or 0.0), 2)

    manifest.setdefault("selection_history", []).append({
        "changed_at": CASE_LAYOUT.now_iso(),
        "slot_key": slot_key,
        "phase": phase_key,
        "pick_index": pick_index,
        "from_source_path": previous_source_path,
        "to_source_path": str(source_path),
        "picked_path": str(target_path),
    })
    PICK.ensure_manifest_alternatives(manifest)
    PICK.write_json(manifest_path, manifest)

    return {
        "mode": "updated",
        "customer": manifest.get("customer"),
        "case_name": manifest.get("case_name"),
        "picked_case_dir": str(case_dir.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "slot_key": slot_key,
        "phase": phase_key,
        "pick_index": pick_index,
        "previous_source_path": previous_source_path,
        "current_source_path": str(source_path),
        "picked_path": str(target_path),
        "selection_history_count": len(manifest.get("selection_history") or []),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="对 pick-manifest 执行案例改选")
    parser.add_argument("input_path", help="picked 单案例目录或 pick-manifest.json")
    parser.add_argument("--slot", help="槽位，例如 front / oblique / side / back")
    parser.add_argument("--phase", help="阶段，例如 before / after 或 术前 / 术后")
    parser.add_argument("--pick", type=int, help="候选编号，从 1 开始")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    case_dir, manifest_path = discover_picked_case(Path(args.input_path))
    manifest = PICK.read_json(manifest_path)
    PICK.ensure_manifest_alternatives(manifest)
    PICK.write_json(manifest_path, manifest)

    if manifest.get("status") != "ready":
        raise ValueError(f"当前 pick-manifest 状态不是 ready，已拒绝改选: {manifest.get('status')}")

    wants_apply = any(value is not None for value in (args.slot, args.phase, args.pick))
    if wants_apply:
        if not (args.slot and args.phase and args.pick):
            raise ValueError("执行案例改选时必须同时传入 --slot、--phase、--pick")
        slot_key = resolve_slot_key(manifest, args.slot)
        phase_key = normalize_phase_token(args.phase)
        payload = apply_repick(case_dir, manifest_path, manifest, slot_key, phase_key, int(args.pick))
    else:
        payload = build_preview_payload(case_dir, manifest_path, manifest)

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
