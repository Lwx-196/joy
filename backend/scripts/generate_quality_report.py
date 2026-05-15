#!/usr/bin/env python3
"""Generate the formal delivery quality report.

Usage:
    python -m backend.scripts.generate_quality_report [--output delivery/quality_report.md]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from backend.services.delivery_gate import (
    P0_THRESHOLD,
    P1_THRESHOLD,
    _case_display_name as _case_name,
    _customer_name,
    classify_tier as _tier,
)

ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = ROOT / "case-workbench.db"
DEFAULT_OUTPUT = ROOT / "delivery" / "quality_report.md"
DEFAULT_DRIFT_THRESHOLD = 3


@dataclass(frozen=True)
class BaselineSnapshot:
    """Frozen snapshot of expected delivery-pool counts at a known point in time.

    Used by `render_report` to detect drift between the requirement-doc state
    and live DB state. The default (`DEFAULT_BASELINE`) tracks the original
    formal-delivery-v1 requirement doc (2026-05-15 03:50); override at the CLI
    with `--baseline-snapshot path.json` when the snapshot itself moves.
    """

    taken_at: str
    can_publish_total: int
    p0_count: int
    p1_count: int
    p2_count: int
    unrendered_total: int


DEFAULT_BASELINE = BaselineSnapshot(
    taken_at="2026-05-15 03:50",
    can_publish_total=29,
    p0_count=17,
    p1_count=5,
    p2_count=7,
    unrendered_total=74,
)


def _load_baseline(path: Path | None) -> BaselineSnapshot:
    if path is None:
        return DEFAULT_BASELINE
    data = json.loads(path.read_text(encoding="utf-8"))
    return BaselineSnapshot(**data)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def collect_deliverables(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT c.id AS case_id, c.abs_path, c.category,
               COALESCE(c.template_tier, 'auto') AS template_tier,
               j.id AS job_id, j.output_path,
               q.quality_score, q.quality_status, q.artifact_mode,
               q.blocking_count, q.warning_count
        FROM cases c
        JOIN render_jobs j ON j.case_id = c.id
        JOIN render_quality q ON q.render_job_id = j.id
        WHERE c.trashed_at IS NULL
          AND q.can_publish = 1
        ORDER BY c.id, q.quality_score DESC
        """
    ).fetchall()

    seen: set[int] = set()
    items: list[dict] = []
    for row in rows:
        cid = row["case_id"]
        if cid in seen:
            continue
        seen.add(cid)
        items.append(
            {
                "case_id": cid,
                "customer": _customer_name(row["abs_path"]),
                "case_name": _case_name(row["abs_path"]),
                "category": row["category"],
                "template_tier": row["template_tier"],
                "quality_score": float(row["quality_score"]),
                "quality_status": row["quality_status"],
                "artifact_mode": row["artifact_mode"],
                "blocking_count": int(row["blocking_count"] or 0),
                "warning_count": int(row["warning_count"] or 0),
                "tier": _tier(float(row["quality_score"])),
            }
        )
    items.sort(key=lambda d: (-d["quality_score"], d["case_id"]))
    return items


def _bucket(items: Iterable[dict], tier: str) -> list[dict]:
    return [it for it in items if it["tier"] == tier]


def _count_unrendered(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT c.category, COUNT(*) AS n
        FROM cases c
        LEFT JOIN render_jobs j ON j.case_id = c.id
        WHERE c.trashed_at IS NULL AND j.id IS NULL
        GROUP BY c.category
        """
    ).fetchall()
    return {row["category"]: int(row["n"]) for row in rows}


def _format_table(items: list[dict]) -> str:
    if not items:
        return "_（空）_\n"
    lines = [
        "| Case | 客户 | 案例名 | 分数 | 模板 | 状态 | warnings |",
        "|------|------|--------|------|------|------|----------|",
    ]
    for it in items:
        safe_name = it["case_name"].replace("|", "/")[:50]
        lines.append(
            f"| #{it['case_id']} | {it['customer']} | {safe_name} | "
            f"{it['quality_score']:.1f} | {it['template_tier']} | {it['quality_status']} | "
            f"{it['warning_count']} |"
        )
    return "\n".join(lines) + "\n"


def _drift_lines(
    actuals: dict[str, int],
    baseline: BaselineSnapshot,
    threshold: int,
) -> list[str]:
    """Return the WARNING block lines when any dimension drifts beyond threshold.

    Returns `[]` when every dimension is within +/- threshold.
    """
    drifts: list[tuple[str, int, int]] = []
    pairs = [
        ("can_publish=1 总数", actuals["total"], baseline.can_publish_total),
        ("P0", actuals["p0"], baseline.p0_count),
        ("P1", actuals["p1"], baseline.p1_count),
        ("P2", actuals["p2"], baseline.p2_count),
        ("无 render", actuals["unrendered"], baseline.unrendered_total),
    ]
    for label, actual, expected in pairs:
        diff = actual - expected
        if abs(diff) > threshold:
            drifts.append((label, actual, diff))
    if not drifts:
        return []
    lines = [
        "## ⚠️ Baseline Drift\n",
        f"基线快照（{baseline.taken_at}）已超过 ±{threshold} 案的容差，建议刷新 snapshot：\n\n",
        "| 维度 | 真实值 | 偏差 |\n|---|---|---|\n",
    ]
    for label, actual, diff in drifts:
        lines.append(f"| {label} | **{actual}** | {diff:+d} |\n")
    lines.append("\n")
    return lines


def render_report(
    conn: sqlite3.Connection,
    baseline: BaselineSnapshot = DEFAULT_BASELINE,
    drift_threshold: int = DEFAULT_DRIFT_THRESHOLD,
) -> tuple[str, dict[str, int]]:
    """Render the markdown report. Returns (report, actuals).

    `actuals` exposes the same dimensions the caller can use to set an exit
    code (e.g. CI flagging drift); `render_report` itself never raises on
    drift — it surfaces it in-band via a `## ⚠️ Baseline Drift` section.
    """
    items = collect_deliverables(conn)
    p0 = _bucket(items, "P0")
    p1 = _bucket(items, "P1")
    p2 = _bucket(items, "P2")
    unrendered = _count_unrendered(conn)
    unrendered_total = sum(unrendered.values())
    actuals = {
        "total": len(items),
        "p0": len(p0),
        "p1": len(p1),
        "p2": len(p2),
        "unrendered": unrendered_total,
    }

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    parts: list[str] = []
    parts.append(f"# Case Workbench 交付质量报告\n")
    parts.append(f"_生成于 {now}_\n")

    parts.append("## 现状对账\n")
    parts.append(
        f"| 维度 | 需求快照 ({baseline.taken_at}) | 真实 DB 状态 | 偏差 |\n"
        "|---|---|---|---|\n"
        f"| `can_publish=1` 总数 | {baseline.can_publish_total} | **{actuals['total']}** | "
        f"{actuals['total'] - baseline.can_publish_total:+d} |\n"
        f"| P0 (≥{P0_THRESHOLD:.0f}) | {baseline.p0_count} | **{actuals['p0']}** | "
        f"{actuals['p0'] - baseline.p0_count:+d} |\n"
        f"| P1 ({P1_THRESHOLD:.0f}-{P0_THRESHOLD - 0.1:.1f}) | {baseline.p1_count} | "
        f"**{actuals['p1']}** | {actuals['p1'] - baseline.p1_count:+d} |\n"
        f"| P2 误锁 (<{P1_THRESHOLD:.0f}) | {baseline.p2_count} | **{actuals['p2']}** | "
        f"{actuals['p2'] - baseline.p2_count:+d} |\n"
        f"| 无 render 案例 | {baseline.unrendered_total} | **{actuals['unrendered']}** | "
        f"{actuals['unrendered'] - baseline.unrendered_total:+d} |\n"
    )
    parts.append("\n")

    parts.extend(_drift_lines(actuals, baseline, drift_threshold))

    parts.append(f"## 🟢 P0 精品级 (score ≥ {P0_THRESHOLD:.0f}) — {len(p0)} 案\n")
    parts.append(_format_table(p0))
    parts.append("\n")

    parts.append(
        f"## 🟡 P1 可交付级 (score {P1_THRESHOLD:.0f}-{P0_THRESHOLD - 0.1:.1f}) — {len(p1)} 案\n"
    )
    parts.append("> 需运营在 QualityReview 页面逐案确认。\n\n")
    parts.append(_format_table(p1))
    parts.append("\n")

    parts.append(f"## 🔴 P2 需复核级 (score < {P1_THRESHOLD:.0f}) — {len(p2)} 案\n")
    if p2:
        parts.append("> ⚠️ 这些案例 `can_publish=1` 但分数过低，本应在 batch_unlock_quota.py 修复后回锁。\n\n")
        parts.append(_format_table(p2))
    else:
        parts.append("✅ 当前 P2 = 0，低分误锁已全部修复。\n")
    parts.append("\n")

    parts.append("## 未渲染池（待 Phase 3 处理）\n")
    if unrendered:
        parts.append("| Category | 数量 |\n|---|---|\n")
        for cat, n in sorted(unrendered.items()):
            parts.append(f"| {cat} | {n} |\n")
    parts.append(f"\n**合计**：{unrendered_total} 案（依赖 API 额度恢复 + 批量渲染脚本）\n\n")

    parts.append("## Open Questions 跟进\n")
    parts.append(
        "| # | 问题 | 当前状态 |\n"
        "|---|------|----------|\n"
        "| 1 | `delivery/` 目录结构（客户/日期/项目类型） | 当前 by-customer（18 客户子目录） |\n"
        f"| 2 | P2 案例处置（修复 vs 跳过） | 当前 P2={len(p2)}，无需立即处理 |\n"
        f"| 3 | 74 案批量渲染是否纳入本批次 | 当前未渲染 {unrendered_total} 案，推后 v3 |\n"
        "| 4 | body 类模板稳定性 | 16 case 暂用 body-dual-compare，待回归 |\n"
        "| 5 | API 额度恢复状态 | 待用户确认 |\n"
    )
    return "".join(parts), actuals


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate delivery quality report")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument(
        "--baseline-snapshot",
        type=Path,
        default=None,
        help=(
            "Path to a JSON file describing the baseline counts to compare DB "
            "against (fields: taken_at, can_publish_total, p0_count, p1_count, "
            "p2_count, unrendered_total). Defaults to the embedded "
            f"DEFAULT_BASELINE ({DEFAULT_BASELINE.taken_at})."
        ),
    )
    parser.add_argument(
        "--drift-threshold",
        type=int,
        default=DEFAULT_DRIFT_THRESHOLD,
        help=f"Per-dimension absolute drift tolerance (default {DEFAULT_DRIFT_THRESHOLD}).",
    )
    args = parser.parse_args()

    baseline = _load_baseline(args.baseline_snapshot)

    conn = _connect(args.db)
    try:
        report, actuals = render_report(conn, baseline, args.drift_threshold)
    finally:
        conn.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"✅ Report written: {args.output} ({len(report)} chars)")

    drifted = _drift_lines(actuals, baseline, args.drift_threshold)
    if drifted:
        # Surface drift on stderr so CI / cron jobs can grep without parsing markdown.
        print(
            f"⚠️ Baseline drift detected (threshold ±{args.drift_threshold}); "
            f"baseline taken_at={baseline.taken_at}; actuals={json.dumps(actuals)}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
