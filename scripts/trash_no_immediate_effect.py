#!/usr/bin/env python3
"""批量剔除无即刻术后视觉效果的案例（肉毒/生物刺激剂）。

默认 dry-run：只输出报告（纯无即刻效果 / 混合案例 / 保留），不动数据。
加 --execute 才真正执行 trash。

用法：
    python -m scripts.trash_no_immediate_effect                  # dry-run
    python -m scripts.trash_no_immediate_effect --execute         # 真执行
    python -m scripts.trash_no_immediate_effect --db path/to/db   # 指定数据库
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.procedure_region_mappings import (
    IMMEDIATE_EFFECT_PROJECTS,
    has_immediate_visible_effect,
    parse_procedures,
)


def _default_db_path() -> Path:
    env_path = None
    import os
    env_path = os.environ.get("CASE_WORKBENCH_DB_PATH")
    if env_path:
        return Path(env_path)
    return PROJECT_ROOT / "case-workbench.db"


def main() -> None:
    parser = argparse.ArgumentParser(description="剔除无即刻效果案例")
    parser.add_argument("--db", type=Path, default=None, help="数据库路径")
    parser.add_argument("--execute", action="store_true", help="真正执行 trash（默认 dry-run）")
    args = parser.parse_args()

    db_path = args.db or _default_db_path()
    if not db_path.exists():
        print(f"数据库不存在: {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, abs_path FROM cases WHERE trashed_at IS NULL ORDER BY abs_path"
    ).fetchall()

    pure_no_effect: list[dict] = []
    mixed: list[dict] = []
    kept: int = 0

    for row in rows:
        case_id = int(row["id"])
        abs_path = row["abs_path"]
        case_name = Path(abs_path).name

        has_effect, reason = has_immediate_visible_effect(case_name)
        if has_effect:
            kept += 1
            continue

        parsed = parse_procedures(case_name)
        projects = {p["project"] for p in parsed["procedures"]}
        has_immediate = bool(projects & IMMEDIATE_EFFECT_PROJECTS)

        entry = {
            "case_id": case_id,
            "case_name": case_name,
            "reason": reason,
            "brands": [p["brand"] for p in parsed["procedures"]],
        }
        if has_immediate:
            mixed.append(entry)
        else:
            pure_no_effect.append(entry)

    print("=" * 60)
    print(f"扫描完成: 共 {len(rows)} 个活跃案例")
    print(f"  保留: {kept}")
    print(f"  纯无即刻效果（建议剔除）: {len(pure_no_effect)}")
    print(f"  混合案例（需人工决定）: {len(mixed)}")
    print("=" * 60)

    if pure_no_effect:
        print("\n--- 纯无即刻效果（建议剔除）---")
        for e in pure_no_effect:
            print(f"  [{e['case_id']}] {e['case_name']}")
            print(f"        原因: {e['reason']}")

    if mixed:
        print("\n--- 混合案例（含填充+肉毒/生物刺激剂，需人工决定）---")
        for e in mixed:
            print(f"  [{e['case_id']}] {e['case_name']}")
            print(f"        品牌: {', '.join(e['brands'])}")

    if args.execute and pure_no_effect:
        print(f"\n执行 trash: 将剔除 {len(pure_no_effect)} 个案例...")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        trashed_at = datetime.now(timezone.utc).isoformat()
        count = 0
        for e in pure_no_effect:
            reason_text = f"no_immediate_visible_effect: {e['reason']}"
            conn.execute(
                """UPDATE cases
                   SET trashed_at = ?, trash_reason = ?
                   WHERE id = ? AND trashed_at IS NULL""",
                (trashed_at, reason_text, e["case_id"]),
            )
            count += 1
        conn.commit()
        print(f"已标记 {count} 个案例为 trashed（目录未移动，仅 DB 标记）。")
    elif args.execute and not pure_no_effect:
        print("\n无需剔除的案例。")
    else:
        print("\n（dry-run 模式，加 --execute 执行）")

    conn.close()


if __name__ == "__main__":
    main()
