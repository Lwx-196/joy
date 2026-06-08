"""Effect-projection calibration report (anchored-simulation Phase 3.3).

Runs the production effect gate (``EffectDeliveryQA``, judge_profile=
effect_projection) over an ``effect_calibration_packet`` and aggregates the
verdict distribution into a calibration report — the Phase 3 Exit artifact.

Judge credentials (``tasks/t52_vlm_judge.local.env``) are SEPARATE
from gpt-image-2 image quota: the judge runs as soon as a packet exists. Over a
``--stub`` packet (candidate = raw copy) the judge SHOULD reject every item
(no-change effect = failure, never a candidate win) — that is the calibration
of the gate's discrimination floor. Over a real-projection packet it produces
the actual effect calibration.

Usage:
    source case-workbench/tasks/t52_vlm_judge.local.env
    python -m backend.scripts.effect_calibration_report \
        --packet-json /tmp/effect-cal/packet.json \
        --report-output /tmp/effect-cal/report.md \
        --json-output /tmp/effect-cal/report.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Protocol

from backend.services.effect_delivery_qa import EffectDeliveryQA


class _Judge(Protocol):
    def call_vision(self, prompt: str, images: list[Path], **kwargs: Any) -> Any: ...


def run_calibration(packet: dict[str, Any], qa: EffectDeliveryQA) -> dict[str, Any]:
    """Assess every packet item through the effect gate; aggregate the verdicts.

    Uses each item's judge-facing ``source_path`` (the bounded image the judge
    actually perceives) so the report reflects the real judging scale.
    """
    rows: list[dict[str, Any]] = []
    for item in packet.get("judge_items", []):
        verdict = qa.assess(
            baseline=item["baseline"]["source_path"],
            candidate=item["candidate"]["source_path"],
            effect_pairs=item.get("effect_pairs", []),
            do_not_touch=item.get("do_not_touch", []),
            criteria=item.get("criteria", []),
            ab_unit_id=item.get("ab_unit_id", ""),
        )
        rows.append({
            "ab_unit_id": item.get("ab_unit_id", ""),
            "effect_pairs": item.get("effect_pairs", []),
            "verdict": verdict.verdict,
            "winner_role": verdict.winner_role,
            "hard_veto_reason": verdict.hard_veto_reason,
            "confidence": verdict.confidence,
            "rationale": verdict.rationale[:240],
            "error": verdict.error,
        })

    winners = Counter(r["winner_role"] or "(none)" for r in rows)
    verdicts = Counter(r["verdict"] for r in rows)
    n = len(rows)
    n_pass = verdicts.get("pass", 0)
    return {
        "scope": packet.get("scope"),
        "stub_packet": bool(packet.get("stub")),
        "judge_item_count": n,
        "verdict_distribution": dict(verdicts),
        "winner_distribution": dict(winners),
        "gate_pass": n_pass,
        "gate_pass_rate": (n_pass / n) if n else 0.0,
        "rows": rows,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Effect-projection calibration report (anchored-sim Phase 3.3)")
    lines.append("")
    stub = report["stub_packet"]
    lines.append(
        f"- packet scope: `{report['scope']}` "
        + ("(**STUB** candidate=raw copy — gate discrimination floor)" if stub
           else "(real effect projection)")
    )
    lines.append(f"- judge items: {report['judge_item_count']}")
    lines.append(
        f"- gate pass: {report['gate_pass']}/{report['judge_item_count']} "
        f"({report['gate_pass_rate'] * 100:.1f}%)"
    )
    lines.append(f"- verdict distribution: `{report['verdict_distribution']}`")
    lines.append(f"- winner distribution: `{report['winner_distribution']}`")
    if stub:
        lines.append("")
        lines.append(
            "> STUB expectation: a correct gate yields **0 pass** (candidate==baseline → "
            "no-change projection = failure). Any `pass` on a stub packet is a gate bug."
        )
    lines.append("")
    lines.append("## Per-case")
    lines.append("")
    lines.append("| case | effect_pairs | verdict | winner | confidence | note |")
    lines.append("|---|---|---|---|---|---|")
    for r in report["rows"]:
        pairs = ", ".join("/".join(p) for p in r["effect_pairs"])
        note = r["error"] or r["hard_veto_reason"] or (r["rationale"] or "")[:80]
        conf = f"{r['confidence']:.2f}" if r["confidence"] is not None else "—"
        lines.append(
            f"| {r['ab_unit_id']} | {pairs} | {r['verdict']} | {r['winner_role'] or '—'} "
            f"| {conf} | {note} |"
        )
    lines.append("")
    return "\n".join(lines)


def _build_provider(env: dict[str, str]) -> _Judge:
    from backend.services.vlm_provider import VLMProvider

    return VLMProvider(env=env)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet-json", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, default=None)
    parser.add_argument("--json-output", type=Path, default=None)
    parser.add_argument(
        "--purpose", default="judge",
        help="VLMProvider purpose route (env-based provider selection).",
    )
    parser.add_argument(
        "--env-file", type=Path, action="append", default=[],
        help="judge 凭证 env 文件(如 tasks/t52_vlm_judge.local.env)。t52 是无 export "
             "前缀的 VAR=val 格式，用 --env-file 读取免去 `set -a; source`(对齐 judge runner)。",
    )
    args = parser.parse_args(argv)

    from backend.scripts.comfyui_vlm_judge_runner import load_env_files

    env = load_env_files(dict(os.environ), [p.resolve() for p in args.env_file])
    packet = json.loads(args.packet_json.read_text(encoding="utf-8"))
    qa = EffectDeliveryQA(_build_provider(env), conn=None, purpose=args.purpose)
    report = run_calibration(packet, qa)

    md = render_markdown(report)
    if args.report_output:
        args.report_output.parent.mkdir(parents=True, exist_ok=True)
        args.report_output.write_text(md, encoding="utf-8")
        print(f"wrote report → {args.report_output}", file=sys.stderr)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"wrote json → {args.json_output}", file=sys.stderr)
    if not args.report_output and not args.json_output:
        print(md)
    print(
        f"gate pass {report['gate_pass']}/{report['judge_item_count']} "
        f"verdicts={report['verdict_distribution']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
