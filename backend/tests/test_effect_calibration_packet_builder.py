"""Effect-calibration harness wiring tests (anchored-sim Phase 3.3).

Validates the 0-quota dry-run pipeline end to end WITHOUT spending AI quota:
  - _resolve_effect_pairs: real parse_procedures + 反臆造 fail-closed
  - build_item / build_packet: stub raw-copy candidate, packet shape, drops
  - run_calibration: report aggregation over a stub judge (gate pass/floor)

Images are real (small PIL PNGs), parse_procedures is real (no mock). Only the
VLM judge is stubbed — that is wiring validation, not a calibration verdict.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from backend.scripts import effect_calibration_packet_builder as builder
from backend.scripts import effect_calibration_report as report
from backend.scripts.focal_p4_packet_builder import CaseSpec
from backend.services.effect_delivery_qa import EffectDeliveryQA
from backend.tests.test_effect_delivery_qa import StubEffectJudge

# A real brand-tagged folder name that parse_procedures resolves to evidence-
# anchored pairs (衡力→botulinum_toxin, 海魅→HA_filler), verified live.
_PROC_DIR = "2025.10.29衡力20抬头、川字、海魅1.0ml注射唇、下巴"
# A folder name with anatomical regions but NO registered brand (反臆造 → empty).
_NO_BRAND_DIR = "25.8.13泪沟填充"


def _png(path: Path, color: tuple[int, int, int] = (130, 100, 90)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (96, 96), color).save(path, "PNG")
    return path


def _spec(tmp_path: Path, proc_dir: str, *, patient: str = "康巧佳") -> CaseSpec:
    case_dir = tmp_path / "cases" / patient / proc_dir
    before = _png(case_dir / "before.jpg", (120, 90, 80))
    after = _png(case_dir / "after.jpg", (140, 110, 100))
    return CaseSpec(
        case_dir=case_dir, before_path=before, after_path=after, focus_targets=["下巴"],
    )


def test_resolve_effect_pairs_evidence_anchored(tmp_path: Path) -> None:
    spec = _spec(tmp_path, _PROC_DIR)
    pairs, parsed = builder._resolve_effect_pairs(spec.case_dir, spec.focus_targets)
    assert pairs, "brand-tagged folder must resolve evidence-anchored pairs"
    # Every returned pair must have a registered evidence row (反臆造).
    from backend.services import procedure_region_mappings as prm
    for project, region in pairs:
        assert prm.effect_row(project, region) is not None
    assert any(proj == "HA_filler" for proj, _ in pairs)


def test_resolve_effect_pairs_fail_closed_no_brand(tmp_path: Path) -> None:
    spec = _spec(tmp_path, _NO_BRAND_DIR)
    pairs, parsed = builder._resolve_effect_pairs(spec.case_dir, spec.focus_targets)
    assert pairs == [], "no registered brand → refuse to invent effect_pairs (反臆造)"


def test_build_item_stub_raw_copy(tmp_path: Path) -> None:
    spec = _spec(tmp_path, _PROC_DIR)
    item = builder.build_item(
        spec, scratch_root=tmp_path / "scratch", stub=True, job_id=-1,
    )
    assert item["judge_profile"] == "effect_projection"
    assert item["effect_pairs"], "stub item still carries resolved effect_pairs"
    # Stub candidate is a byte-identical raw copy of the baseline.
    base = Path(item["baseline"]["full_res_path"]).read_bytes()
    cand = Path(item["candidate"]["full_res_path"]).read_bytes()
    assert base == cand
    # Judge-facing bounded images exist.
    assert Path(item["baseline"]["source_path"]).is_file()
    assert Path(item["candidate"]["source_path"]).is_file()


def test_build_item_drops_no_effect_pairs(tmp_path: Path) -> None:
    spec = _spec(tmp_path, _NO_BRAND_DIR)
    with pytest.raises(RuntimeError, match="no evidence-anchored effect_pairs"):
        builder.build_item(spec, scratch_root=tmp_path / "scratch", stub=True, job_id=-1)


def test_build_packet_drops_reported(tmp_path: Path) -> None:
    good = _spec(tmp_path, _PROC_DIR, patient="康巧佳")
    bad = _spec(tmp_path, _NO_BRAND_DIR, patient="无品牌")
    packet = builder.build_packet([good, bad], scratch_root=tmp_path / "scratch", stub=True)
    assert packet["judge_item_count"] == 1
    assert packet["dropped_count"] == 1
    assert packet["stub"] is True
    assert packet["judge_items"][0]["judge_profile"] == "effect_projection"


def _stub_packet(tmp_path: Path) -> dict:
    spec = _spec(tmp_path, _PROC_DIR)
    return builder.build_packet([spec], scratch_root=tmp_path / "scratch", stub=True)


def test_report_pipeline_candidate_pass(tmp_path: Path) -> None:
    packet = _stub_packet(tmp_path)
    qa = EffectDeliveryQA(StubEffectJudge(winner_role="candidate"), conn=None)
    rep = report.run_calibration(packet, qa)
    assert rep["judge_item_count"] == 1
    assert rep["gate_pass"] == 1
    assert rep["winner_distribution"].get("candidate") == 1
    assert rep["verdict_distribution"].get("pass") == 1


def test_report_baseline_is_held(tmp_path: Path) -> None:
    packet = _stub_packet(tmp_path)
    qa = EffectDeliveryQA(StubEffectJudge(winner_role="baseline"), conn=None)
    rep = report.run_calibration(packet, qa)
    # baseline win = honest loss → NOT a pass (gate held).
    assert rep["gate_pass"] == 0
    assert rep["winner_distribution"].get("baseline") == 1


def test_report_judge_down_fail_closed(tmp_path: Path) -> None:
    packet = _stub_packet(tmp_path)
    qa = EffectDeliveryQA(StubEffectJudge(down=True), conn=None)
    rep = report.run_calibration(packet, qa)
    assert rep["gate_pass"] == 0
    assert rep["verdict_distribution"].get("unavailable") == 1


def test_render_markdown_smoke(tmp_path: Path) -> None:
    packet = _stub_packet(tmp_path)
    qa = EffectDeliveryQA(StubEffectJudge(winner_role="candidate"), conn=None)
    rep = report.run_calibration(packet, qa)
    md = report.render_markdown(rep)
    assert "Effect-projection calibration report" in md
    assert "gate pass" in md
