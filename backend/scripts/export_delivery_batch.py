#!/usr/bin/env python3
"""Export all deliverable cases to a unified delivery directory.

A fail-closed board QA gate (D6) screens every candidate's rendered board JPG
through single-board VLM assessment BEFORE export: `blocker` / `unavailable`
boards are held out of the shipped set and written to a human-review queue
(`delivery_held_review.json`) instead of silently shipping. Enabled by default;
`--no-qa` (or `CASE_WORKBENCH_DELIVERY_QA=0`) disables it for emergencies.

Usage:
    python -m backend.scripts.export_delivery_batch [--output-dir ./delivery] [--dry-run] [--no-qa]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from backend.services.board_delivery_qa import BoardDeliveryQA
from backend.services.delivery_gate import (
    DeliverableItem,
    DeliveryGate,
    HeldBoard,
    P0_THRESHOLD,
    P1_THRESHOLD,
)
from backend.services.single_image_delivery import (
    EnhancedAfter,
    SingleImageBuildError,
    build_enhanced_after,
    closeup_filename,
)
from backend.services.single_image_delivery_qa import SingleImageDeliveryQA, SingleImageQAVerdict
from backend.services.simulation_delivery_gate import SimulationDeliveryGate
from backend.services.vlm_provider import VLMProvider

if TYPE_CHECKING:
    from backend.services.effect_delivery_qa import EffectDeliveryQA, EffectQAVerdict

DB_PATH = Path(__file__).resolve().parent.parent.parent / "case-workbench.db"
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent.parent / "delivery"


def _load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _build_board_qa(conn: sqlite3.Connection, repo_root: Path) -> BoardDeliveryQA:
    """Construct the fail-closed VLM QA gate, loading t54 ADC config if present."""
    env = dict(os.environ)
    env.update(_load_env_file(repo_root / "tasks" / "t54_vertex_adc.local.env"))
    return BoardDeliveryQA(VLMProvider(env=env), conn)


def _build_single_image_qa(conn: sqlite3.Connection, repo_root: Path) -> SingleImageDeliveryQA:
    env = dict(os.environ)
    env.update(_load_env_file(repo_root / "tasks" / "t54_vertex_adc.local.env"))
    return SingleImageDeliveryQA(conn, env=env)


# ── Effect-projection delivery lane (anchored-sim Phase 4) ──────────────────
# 第三条 opt-in 交付 lane（与 D6 板 / #51 closeups 并列）。从案例库 discover 术前图
# → raw-first gpt-image-2 投影 → EffectDeliveryQA advisory 判官 → 全件 held 人工审核。
# 输入源 = 文件系统案例库（不是 DB 板）：术前 before 图只在案例库，DeliverableItem 没有
# （focal_p4 注释：DB abs_path 指向另一拷贝、tags_json 空，DB 路由=0 case）。
EFFECT_RECIPE_VERSION = "raw_first-gpt-image-2-v1"  # 配方版本 → 生成缓存键的一部分
EFFECT_OUTPUT_SUBDIR = "effect-projection"
_EFFECT_JOB_BASE = -990_000  # 合成 job_id（避开真 case_id；与 calibration 的 -920000 不撞）


def _build_effect_qa(conn: sqlite3.Connection, repo_root: Path) -> "EffectDeliveryQA":
    from backend.services.effect_delivery_qa import EffectDeliveryQA

    env = dict(os.environ)
    env.update(_load_env_file(repo_root / "tasks" / "t54_vertex_adc.local.env"))
    return EffectDeliveryQA(VLMProvider(env=env), conn, purpose="judge")


def _effect_cache_dir() -> Path:
    """跨 export 持久化的生成缓存目录（按 before-hash+pairs+recipe 复用，不重烧 quota）。"""
    from backend import ai_generation_adapter as aga

    return aga.SIMULATION_ROOT.parent / "effect_projection_cache"


def _effect_generate_candidate(
    before_path: Path,
    effect_pairs: list[tuple[str, str]],
    *,
    job_id: int,
    cache_dir: Path,
    allow_generate: bool = True,
) -> tuple[Path | None, bool]:
    """raw-first effect 投影 + 生成缓存。返回 (candidate_path | None, cache_hit)。

    缓存键 = sha256(术前图字节 + 排序后的 effect_pairs + recipe 版本)。命中则复用、不烧
    gpt-image-2 quota（owner 成本决策）。未命中走生产 node router(raw_first) → 取
    output_refs 的 generated_raw（raw-first 无 effect_anchored）。``allow_generate=False``
    （dry-run）且未命中缓存 → 返回 ``(None, False)``，**不生成、不烧 quota**（预览用）。
    """
    import hashlib

    from backend import ai_generation_adapter as aga

    key_src = (
        before_path.read_bytes()
        + repr(sorted(effect_pairs)).encode("utf-8")
        + EFFECT_RECIPE_VERSION.encode("utf-8")
    )
    key = hashlib.sha256(key_src).hexdigest()[:40]
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{key}.png"
    if cached.is_file():
        return cached, True
    if not allow_generate:
        return None, False  # dry-run + 未缓存：跳过生成（0 quota），调用方记 would-project

    result = aga.run_ps_model_router_after_simulation(
        job_id=job_id,
        after_image_path=before_path,
        before_image_path=None,
        focus_targets=[region for _, region in effect_pairs],
        brand="fumei",
        mode=aga.EFFECT_PROJECTION_MODE,
        effect_pairs=list(effect_pairs),
        anchor_mode=aga.ANCHOR_MODE_RAW,
    )
    refs = {
        r["kind"]: r["path"]
        for r in result.get("output_refs", [])
        if isinstance(r, dict) and r.get("kind") and r.get("path")
    }
    candidate = refs.get("generated_raw")  # raw-first：交付源 = raw AI 全脸精修
    if not candidate:
        raise RuntimeError(f"effect projection produced no candidate: refs={sorted(refs)}")
    shutil.copyfile(candidate, cached)
    return cached, False


def _effect_held_row(
    *,
    customer: str,
    case_name: str,
    effect_pairs: list[tuple[str, str]],
    candidate_path: str | None,
    baseline_path: str,
    verdict: "EffectQAVerdict | None" = None,
    reason: str,
    cache_hit: bool = False,
) -> dict:
    """effect held 队列一行。judge verdict 仅作 advisory 字段（不决定发货）。"""
    row: dict = {
        "customer": customer,
        "case_name": case_name,
        "effect_pairs": [list(p) for p in effect_pairs],
        "baseline_path": baseline_path,
        "candidate_path": candidate_path,
        "cache_hit": cache_hit,
        "reason": reason,
    }
    if verdict is not None:
        row["advisory_judge"] = {
            "verdict": verdict.verdict,
            "winner_role": verdict.winner_role,
            "confidence": verdict.confidence,
            "hard_veto_reason": verdict.hard_veto_reason,
            "content_hash": verdict.content_hash,
        }
    return row


def _write_effect_held_report(
    output_dir: Path, held: list[dict], dry_run: bool, *, discovered: int, eligible: int
) -> Path | None:
    if dry_run or not held:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "effect_held_review.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "discovered_cases": discovered,
                "eligible_cases": eligible,
                "held_count": len(held),
                "note": (
                    "Effect-projection deliverables — ALL held for mandatory human "
                    "review at launch (judge is advisory only, never auto-ships). "
                    "no_visible_change is a deterministic hard veto. Boards/closeups "
                    "are unaffected. Clear via EffectDeliveryQA.clear_effect."
                ),
                "images": held,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"  🧪 Effect-projection held-review queue: {path}")
    return path


def _run_effect_delivery(
    output_dir: Path,
    *,
    dry_run: bool,
    qa: "EffectDeliveryQA",
    cases_root: Path,
) -> tuple[list[dict], list[dict]]:
    """Discover 案例库 → scope-gate eligible → raw-first 投影(缓存) → advisory 判官 →
    全件 held。返回 (passed, held)；**passed 恒为空**（launch posture = 判官 advisory，
    lane 层硬覆盖、不读 verdict.deliverable、不调 screen_effect_deliverables）。
    """
    from backend import ai_generation_adapter as aga
    from backend import source_images
    from backend.scripts import focal_p4_packet_builder as fp4
    from backend.services import effect_delivery_selector as sel
    from backend.services.effect_delivery_qa import REVIEW_CLEARED

    specs = fp4.discover_cases(cases_root, aga.MD_ANATOMICAL_KEYWORDS, source_images._phase_from_filename)
    cache_dir = _effect_cache_dir()
    passed_rows: list[dict] = []
    held_rows: list[dict] = []
    eligible = 0

    for idx, spec in enumerate(specs):
        pairs, _parsed = sel.resolve_effect_pairs(spec.case_dir.name)
        in_scope, _skips = sel.scope_gate(pairs)
        if not in_scope:
            continue  # 不在上线 scope（profile/expression/out-of-scope）或无 evidence pair
        eligible += 1
        customer = spec.case_dir.parent.name or "case"
        baseline_path = str(spec.before_path)
        # 源图质量门：effect 投影要术前单张干净照。discover 可能把「术前｜术后」双拼板/
        # 多人源当 baseline（编辑成品板=garbage-in）→ ≥2 张脸标 suspect、入 held、跳过生成
        # （省 gpt-image-2）。fail-open：人脸数不可测则放行（held 队列兜底）。
        suspect = sel.source_quality_suspect(spec.before_path)
        if suspect:
            held_rows.append(
                _effect_held_row(
                    customer=customer, case_name=spec.case_dir.name, effect_pairs=in_scope,
                    candidate_path=None, baseline_path=baseline_path, reason=suspect,
                )
            )
            continue
        try:
            candidate, cache_hit = _effect_generate_candidate(
                spec.before_path, in_scope, job_id=_EFFECT_JOB_BASE - idx,
                cache_dir=cache_dir, allow_generate=not dry_run,
            )
        except Exception as exc:  # noqa: BLE001 — fail-closed: 投影失败该 case 入 held 报错，不阻塞
            held_rows.append(
                _effect_held_row(
                    customer=customer, case_name=spec.case_dir.name, effect_pairs=in_scope,
                    candidate_path=None, baseline_path=baseline_path,
                    reason=f"projection_error: {type(exc).__name__}: {str(exc)[:160]}",
                )
            )
            continue

        if candidate is None:
            # dry-run + 未缓存：预览，不生成不评判（0 quota）。
            held_rows.append(
                _effect_held_row(
                    customer=customer, case_name=spec.case_dir.name, effect_pairs=in_scope,
                    candidate_path=None, baseline_path=baseline_path,
                    reason="would_project (dry-run, not generated — real run burns gpt-image-2 quota)",
                )
            )
            continue

        verdict = qa.assess(
            baseline=spec.before_path,
            candidate=candidate,
            effect_pairs=in_scope,
            job_id=_EFFECT_JOB_BASE - idx,
            ab_unit_id=spec.case_dir.name,
        )
        # launch posture (BLOCKER-C): judge is ADVISORY — ship ONLY when an operator
        # has explicitly released the hash (review_status == CLEARED). A judge "pass"
        # alone is NOT enough (held); REJECTED + pending all stay held. We deliberately
        # do NOT read verdict.deliverable (it returns True on an unreviewed judge pass).
        if verdict.review_status == REVIEW_CLEARED:
            dest_path = str(candidate)
            if not dry_run:
                dest = output_dir / customer / EFFECT_OUTPUT_SUBDIR / f"{spec.slug}__effect.png"
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate, dest)
                dest_path = str(dest)
            passed_rows.append(
                {
                    "customer": customer,
                    "case_name": spec.case_dir.name,
                    "effect_pairs": [list(p) for p in in_scope],
                    "dest_path": dest_path,
                    "cleared_by": verdict.review_note or "operator",
                    "advisory_judge": {"verdict": verdict.verdict, "winner_role": verdict.winner_role},
                }
            )
        else:
            held_rows.append(
                _effect_held_row(
                    customer=customer, case_name=spec.case_dir.name, effect_pairs=in_scope,
                    candidate_path=str(candidate), baseline_path=baseline_path,
                    verdict=verdict, reason=verdict.reason, cache_hit=cache_hit,
                )
            )

    _write_effect_held_report(output_dir, held_rows, dry_run, discovered=len(specs), eligible=eligible)
    return passed_rows, held_rows


def _held_row(held: HeldBoard) -> dict:
    return {
        "case_id": held.case_id,
        "customer": held.customer,
        "case_name": held.case_name,
        "job_id": held.job_id,
        "verdict": held.verdict,
        "primary_defect": held.primary_defect,
        "families": list(held.families),
        "confidence": held.confidence,
        "content_hash": held.content_hash,
        "reason": held.reason,
        "source_path": held.source_path,
    }


def _write_held_report(output_dir: Path, held: list[HeldBoard], dry_run: bool) -> Path | None:
    """Persist the human-review queue (held boards) next to the manifest."""
    if dry_run or not held:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "delivery_held_review.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "held_count": len(held),
                "note": (
                    "Fail-closed board QA (D6) held these boards. Review each, then "
                    "clear (ships) or re-render (new bytes → re-assessed)."
                ),
                "boards": [_held_row(hb) for hb in held],
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"  🛡  Held-review queue: {path}")
    return path


def _single_image_dest_path(output_dir: Path, item: DeliverableItem) -> Path:
    return output_dir / item.customer / "closeups" / closeup_filename(item)


def _single_image_manifest_row(
    item: DeliverableItem,
    enhanced: EnhancedAfter,
    verdict: SingleImageQAVerdict,
    dest_path: Path,
) -> dict:
    return {
        "case_id": item.case_id,
        "customer": item.customer,
        "case_name": item.case_name,
        "focus_targets": list(enhanced.focus_targets),
        "dest_path": str(dest_path),
        "source_after": str(enhanced.source_after_path),
        "enhanced_path": str(enhanced.enhanced_path),
        "verdict": verdict.verdict,
        "winner_role": verdict.winner_role,
        "confidence": verdict.confidence,
        "content_hash": verdict.content_hash,
    }


def _single_image_held_row(
    item: DeliverableItem,
    *,
    enhanced: EnhancedAfter | None = None,
    verdict: SingleImageQAVerdict | None = None,
    reason: str,
) -> dict:
    return {
        "case_id": item.case_id,
        "customer": item.customer,
        "case_name": item.case_name,
        "quality_score": item.quality_score,
        "source_after": str(enhanced.source_after_path) if enhanced else "",
        "enhanced_path": str(enhanced.enhanced_path) if enhanced else "",
        "focus_targets": list(enhanced.focus_targets) if enhanced else [],
        "verdict": verdict.verdict if verdict else "build_failed",
        "winner_role": verdict.winner_role if verdict else "",
        "hard_veto_reason": verdict.hard_veto_reason if verdict else "",
        "confidence": verdict.confidence if verdict else None,
        "prescreen_reasons": list(verdict.prescreen_reasons) if verdict else [],
        "content_hash": verdict.content_hash if verdict else "",
        "reason": reason,
    }


def _write_single_image_manifest(output_dir: Path, rows: list[dict], dry_run: bool) -> Path | None:
    if dry_run or not rows:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "single_image_manifest.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "total_images": len(rows),
                "images": rows,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"  🖼  Single-image manifest: {path}")
    return path


def _write_single_image_held_report(output_dir: Path, held: list[dict], dry_run: bool) -> Path | None:
    if dry_run or not held:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "single_image_held_review.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "held_count": len(held),
                "note": (
                    "Single-image closeups held by source lookup, enhancement, "
                    "prescreen, or fidelity judge. Boards are unaffected."
                ),
                "images": held,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"  🖼  Single-image held-review queue: {path}")
    return path


def _run_single_image_delivery(
    items: list[DeliverableItem],
    output_dir: Path,
    *,
    dry_run: bool,
    conn: sqlite3.Connection,
    qa: SingleImageDeliveryQA,
) -> tuple[list[dict], list[dict]]:
    scratch_root = output_dir / "_single_image_scratch"
    manifest_rows: list[dict] = []
    held_rows: list[dict] = []

    for item in items:
        try:
            enhanced = build_enhanced_after(item, scratch_root, conn)
        except SingleImageBuildError as exc:
            held_rows.append(_single_image_held_row(item, reason=str(exc)))
            continue

        verdict = qa.assess(
            enhanced.raw_path,
            enhanced.enhanced_path,
            enhanced.mask_path,
            case_id=item.case_id,
            customer=item.customer,
            focus_targets=enhanced.focus_targets,
        )
        if verdict.deliverable:
            dest_path = _single_image_dest_path(output_dir, item)
            if not dry_run:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(enhanced.enhanced_path, dest_path)
            manifest_rows.append(_single_image_manifest_row(item, enhanced, verdict, dest_path))
        else:
            held_rows.append(
                _single_image_held_row(
                    item,
                    enhanced=enhanced,
                    verdict=verdict,
                    reason=verdict.reason,
                )
            )

    _write_single_image_manifest(output_dir, manifest_rows, dry_run)
    _write_single_image_held_report(output_dir, held_rows, dry_run)
    return manifest_rows, held_rows

MANIFEST_FIELDS = [
    "case_id", "customer", "case_name", "category",
    "template_tier", "quality_score", "quality_status",
    "artifact_mode", "tier", "dest_path", "source_path",
]


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _manifest_row(item: DeliverableItem, dest_path: Path) -> dict:
    return {
        "case_id": item.case_id,
        "customer": item.customer,
        "case_name": item.case_name,
        "category": item.category,
        "template_tier": item.template_tier,
        "quality_score": item.quality_score,
        "quality_status": item.quality_status,
        "artifact_mode": item.artifact_mode,
        "tier": item.tier,
        "dest_path": str(dest_path),
        "source_path": item.source_path,
    }


def export(
    output_dir: Path,
    dry_run: bool = False,
    db_path: Path = DB_PATH,
    qa_enabled: bool = True,
    single_image_enabled: bool = False,
    effect_projection_enabled: bool = False,
    effect_cases_root: Path | None = None,
) -> list[dict]:
    conn = _connect(db_path)
    held: list[HeldBoard] = []
    single_image_rows: list[dict] = []
    single_image_held: list[dict] = []
    effect_passed: list[dict] = []
    effect_held: list[dict] = []
    try:
        board_qa = _build_board_qa(conn, db_path.parent) if qa_enabled else None
        gate = DeliveryGate(conn, board_qa=board_qa)
        sim_gate = SimulationDeliveryGate(conn)
        screen = gate.screen_deliverables(simulation_gate=sim_gate)
        items = screen.passed
        held = screen.held

        if board_qa is not None:
            print(
                f"\n  🛡  Board QA gate: {len(items)} ship / {len(held)} held for review"
            )
            for hb in held:
                print(
                    f"     ⛔ Case #{hb.case_id:>3d} [{hb.verdict}] "
                    f"{hb.customer}/{hb.case_name[:36]} — {hb.primary_defect[:48]}"
                )

        if not items:
            if held:
                print(
                    f"\n❌ No shippable cases — all {len(held)} candidate(s) held by QA. "
                    "Review delivery_held_review.json, then clear or re-render."
                )
                _write_held_report(output_dir, held, dry_run)
            else:
                print("❌ No deliverable cases found.")
            return []

        print(f"\n{'=' * 60}")
        print(f"  📦 Delivery Export: {len(items)} cases")
        print(f"  📁 Output: {output_dir}")
        print(f"  🕐 Timestamp: {datetime.now(timezone.utc).isoformat()}")
        print(f"{'=' * 60}\n")

        p0_count = sum(1 for it in items if it.tier == "P0")
        p1_count = sum(1 for it in items if it.tier == "P1")
        print(f"  🟢 P0 精品级 (≥{P0_THRESHOLD:.0f}): {p0_count}")
        print(f"  🟡 P1 可交付级 ({P1_THRESHOLD:.0f}-{P0_THRESHOLD - 0.1:.1f}): {p1_count}")
        print()

        if not dry_run:
            output_dir.mkdir(parents=True, exist_ok=True)

        manifest_rows: list[dict] = []
        for item in items:
            dest_path = gate.export(item, output_dir, dry_run=dry_run)
            print(
                f"  {item.tier} Case #{item.case_id:>3d} | [{item.category}] "
                f"{item.customer}/{item.case_name[:40]} | score={item.quality_score}"
            )
            manifest_rows.append(_manifest_row(item, dest_path))

        if single_image_enabled:
            print("\n  🖼  Single-image closeups: enabled")
            single_image_qa = _build_single_image_qa(conn, db_path.parent)
            try:
                single_image_rows, single_image_held = _run_single_image_delivery(
                    items,
                    output_dir,
                    dry_run=dry_run,
                    conn=conn,
                    qa=single_image_qa,
                )
            except Exception as exc:  # noqa: BLE001 - companion artifacts never block boards
                print(f"  ⚠️  Single-image delivery skipped: {type(exc).__name__}: {str(exc)[:160]}")

        if effect_projection_enabled:
            print("\n  🧪 Effect-projection lane: enabled (ALL held for human review)")
            try:
                effect_qa = _build_effect_qa(conn, db_path.parent)
                cases_root = effect_cases_root or _effect_cases_root_default()
                effect_passed, effect_held = _run_effect_delivery(
                    output_dir, dry_run=dry_run, qa=effect_qa, cases_root=cases_root
                )
            except Exception as exc:  # noqa: BLE001 - effect lane never blocks boards/closeups
                print(f"  ⚠️  Effect-projection lane skipped: {type(exc).__name__}: {str(exc)[:160]}")
    finally:
        conn.close()

    csv_path = output_dir / "delivery_manifest.csv"
    json_path = output_dir / "delivery_manifest.json"
    if not dry_run:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
            writer.writeheader()
            writer.writerows(manifest_rows)

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "total_cases": len(manifest_rows),
                    "p0_count": p0_count,
                    "p1_count": p1_count,
                    "min_score": min(d["quality_score"] for d in manifest_rows),
                    "cases": manifest_rows,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

    _write_held_report(output_dir, held, dry_run)

    print(f"\n{'=' * 60}")
    print(f"  ✅ Export complete: {len(manifest_rows)} cases")
    if held:
        print(f"  🛡  Held for review: {len(held)} board(s) (not shipped)")
    if single_image_enabled:
        print(
            f"  🖼  Single-image closeups: {len(single_image_rows)} ship / "
            f"{len(single_image_held)} held"
        )
    if effect_projection_enabled:
        print(
            f"  🧪 Effect-projection: {len(effect_passed)} shipped (operator-cleared) / "
            f"{len(effect_held)} held (judge advisory + mandatory human review)"
        )
    if not dry_run:
        print(f"  📋 CSV: {csv_path}")
        print(f"  📋 JSON: {json_path}")
    else:
        print("  ℹ️  Dry run — no files copied")
    print(f"{'=' * 60}")
    return manifest_rows


def _qa_enabled_default() -> bool:
    return str(os.environ.get("CASE_WORKBENCH_DELIVERY_QA", "1")).strip().lower() not in {
        "0", "false", "no", "off",
    }


def _single_image_enabled_default() -> bool:
    return str(os.environ.get("CASE_WORKBENCH_SINGLE_IMAGE_DELIVERY", "0")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _effect_projection_enabled_default() -> bool:
    return str(os.environ.get("CASE_WORKBENCH_EFFECT_DELIVERY", "0")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _effect_cases_root_default() -> Path:
    env = os.environ.get("CASE_WORKBENCH_EFFECT_CASES_ROOT")
    if env:
        return Path(env)
    from backend.scripts.effect_calibration_packet_builder import DEFAULT_CASES_ROOT

    return DEFAULT_CASES_ROOT


def main() -> None:
    parser = argparse.ArgumentParser(description="Export deliverable cases")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument(
        "--no-qa",
        action="store_true",
        help="Disable the fail-closed board QA gate (emergency escape hatch).",
    )
    parser.add_argument(
        "--single-image",
        dest="single_image",
        action="store_true",
        default=None,
        help="Add screened standalone clarity closeups for shipped cases.",
    )
    parser.add_argument(
        "--no-single-image",
        dest="single_image",
        action="store_false",
        help="Disable standalone closeup delivery even if the env flag is set.",
    )
    parser.add_argument(
        "--effect-projection",
        dest="effect_projection",
        action="store_true",
        default=None,
        help="Add raw-first AI effect-projection deliverables (ALL held for review, "
             "judge advisory). Discovers from the case library; burns gpt-image-2 quota "
             "(generation-cached). Opt-in, default OFF.",
    )
    parser.add_argument(
        "--no-effect-projection",
        dest="effect_projection",
        action="store_false",
        help="Disable effect-projection lane even if the env flag is set.",
    )
    args = parser.parse_args()
    qa_enabled = _qa_enabled_default() and not args.no_qa
    single_image_enabled = (
        _single_image_enabled_default() if args.single_image is None else args.single_image
    )
    effect_projection_enabled = (
        _effect_projection_enabled_default()
        if args.effect_projection is None
        else args.effect_projection
    )
    if not qa_enabled:
        print("⚠️  Board QA gate DISABLED — boards ship without VLM screening.")
    export(
        args.output_dir,
        args.dry_run,
        db_path=args.db,
        qa_enabled=qa_enabled,
        single_image_enabled=single_image_enabled,
        effect_projection_enabled=effect_projection_enabled,
    )


if __name__ == "__main__":
    main()
