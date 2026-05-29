"""P4 gate report — aggregate the VLM judge verdict + board perceptibility.

Consumes a focal_p4 packet (board paths) and the comfyui_vlm_judge_runner output
(t52 results), and produces the gate decision for the crisp-focal-crop Phase 4
formal gate: candidate (FOCAL) win-rate ≥ 60% vs baseline (existing shipped board).

Per case it also computes a **board pixel-diff** (mean Δ, % pixels changed > 5) —
the perceptibility metric behind the P4 N=1 finding that focal's localized change
can fall below the VLM judge's threshold once downscaled into the layout board.
A tie with a near-zero board diff means "focal change invisible at board scale";
a tie with a large board diff means "judge saw it but didn't prefer it".

Verdicts: decisive judgments carry ``winner_role`` (baseline|candidate); ties /
provider failures land in ``manual_review_judgments`` and count as non-wins
(conservative — a tie is not a candidate win for a ≥60% gate).

Usage::

    # results already produced by comfyui_vlm_judge_runner
    python -m backend.scripts.focal_p4_gate_report \
        --packet-json /tmp/focal-p4-big/packet.json \
        --results-json /tmp/focal-p4-big/results.json \
        --output /tmp/focal-p4-big/gate-report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

GATE_THRESHOLD = 0.60


def parse_winner(judgment: dict[str, Any]) -> str:
    """Return 'baseline' | 'candidate' | 'tie' for a single judgment.

    Prefer the explicit ``winner_role``; otherwise fall back to summing the
    per-criterion baseline/candidate scores (higher total wins, equal = tie).
    """
    role = (judgment.get("winner_role") or "").strip().lower()
    if role in {"baseline", "candidate"}:
        return role
    if role in {"tie", "draw", "equal"}:
        return "tie"
    scores = judgment.get("criterion_scores") or {}
    b = sum((v or {}).get("baseline", 0) for v in scores.values())
    c = sum((v or {}).get("candidate", 0) for v in scores.values())
    if c > b:
        return "candidate"
    if b > c:
        return "baseline"
    return "tie"


def aggregate(results: dict[str, Any]) -> dict[str, Any]:
    """Tally winners across decisive judgments + manual-review (tie/failed)."""
    decisive = results.get("judgments") or []
    manual = results.get("manual_review_judgments") or []
    per_case: dict[str, str] = {}
    for j in decisive:
        per_case[j.get("ab_unit_id", "?")] = parse_winner(j)
    for j in manual:
        # Manual-review = no decisive winner. Use score fallback but treat an
        # equal/missing result as a tie (never silently a candidate win).
        per_case.setdefault(j.get("ab_unit_id", "?"), parse_winner(j))

    candidate_wins = sum(1 for v in per_case.values() if v == "candidate")
    baseline_wins = sum(1 for v in per_case.values() if v == "baseline")
    ties = sum(1 for v in per_case.values() if v == "tie")
    total = len(per_case)
    decisive_n = candidate_wins + baseline_wins
    return {
        "per_case_winner": per_case,
        "total": total,
        "candidate_wins": candidate_wins,
        "baseline_wins": baseline_wins,
        "ties": ties,
        # Conservative gate metric: ties count against the candidate.
        "win_rate": (candidate_wins / total) if total else 0.0,
        # Decisive-only rate (excludes ties), for context.
        "decisive_win_rate": (candidate_wins / decisive_n) if decisive_n else 0.0,
    }


def board_diff(baseline_path: str, candidate_path: str) -> dict[str, Any]:
    """Per-case board perceptibility: mean pixel Δ + % pixels changed > 5."""
    try:
        from PIL import Image, ImageChops
        import numpy as np
    except ImportError as exc:  # pragma: no cover - PIL/numpy are deps
        return {"error": f"pixel-diff unavailable: {exc}"}
    try:
        a = Image.open(baseline_path).convert("RGB")
        b = Image.open(candidate_path).convert("RGB")
    except (FileNotFoundError, OSError) as exc:
        return {"error": f"open failed: {exc}"}
    if a.size != b.size:
        b = b.resize(a.size)
    d = np.asarray(ImageChops.difference(a, b), dtype=float)
    return {
        "size": list(a.size),
        "mean_delta": round(float(d.mean()), 3),
        "max_delta": int(d.max()),
        "pct_pixels_gt5": round(float((d.max(axis=2) > 5).mean()) * 100, 2),
    }


def build_report(
    packet: dict[str, Any],
    results: dict[str, Any],
    *,
    with_diff: bool = True,
    threshold: float = GATE_THRESHOLD,
) -> dict[str, Any]:
    agg = aggregate(results)
    boards = {
        it.get("ab_unit_id"): (it.get("baseline", {}).get("source_path"),
                               it.get("candidate", {}).get("source_path"))
        for it in packet.get("judge_items", [])
    }
    cases = []
    for unit_id, winner in agg["per_case_winner"].items():
        row: dict[str, Any] = {"ab_unit_id": unit_id, "winner": winner}
        if with_diff and unit_id in boards and all(boards[unit_id]):
            row["board_diff"] = board_diff(*boards[unit_id])
        cases.append(row)
    gate_pass = agg["win_rate"] >= threshold
    return {
        "scope": "focal_p4_gate_report_v1",
        "real_vlm_judge": results.get("real_vlm_judge"),
        "judge_model": results.get("model"),
        "gate_threshold": threshold,
        "gate_pass": gate_pass,
        "summary": {k: agg[k] for k in
                    ("total", "candidate_wins", "baseline_wins", "ties",
                     "win_rate", "decisive_win_rate")},
        "cases": cases,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--packet-json", type=Path, required=True)
    p.add_argument("--results-json", type=Path, required=True)
    p.add_argument("--output", type=Path, default=None)
    p.add_argument("--threshold", type=float, default=GATE_THRESHOLD)
    p.add_argument("--no-diff", action="store_true", help="skip board pixel-diff")
    args = p.parse_args(argv)

    packet = json.loads(args.packet_json.read_text(encoding="utf-8"))
    results = json.loads(args.results_json.read_text(encoding="utf-8"))
    report = build_report(packet, results, with_diff=not args.no_diff, threshold=args.threshold)

    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    s = report["summary"]
    verdict = "PASS ✅" if report["gate_pass"] else "FAIL ❌"
    print(f"=== P4 gate: {verdict} (threshold {report['gate_threshold']:.0%}) ===", file=sys.stderr)
    print(f"candidate {s['candidate_wins']}/{s['total']} wins "
          f"(win_rate {s['win_rate']:.0%}, decisive {s['decisive_win_rate']:.0%}); "
          f"baseline {s['baseline_wins']}, ties {s['ties']}", file=sys.stderr)
    for c in report["cases"]:
        bd = c.get("board_diff", {})
        diff = (f" boardΔ={bd.get('mean_delta')} >{bd.get('pct_pixels_gt5')}%"
                if bd and "error" not in bd else "")
        print(f"  {c['winner']:9} {c['ab_unit_id']}{diff}", file=sys.stderr)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
