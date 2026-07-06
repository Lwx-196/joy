"""Work-queue summary — 服务端等价 frontend deriveLanes 的聚合接口。

Dashboard 原先拉全量 case 列表(limit=2000,每行带 render_jobs latest 子查询
join)只为派生 5 车道计数 + 两个批量车道的 case id。本接口一次轻量扫描
(9 列、无 join)在服务端完成等价聚合,payload 从全量行降到一个计数对象。

谓词与 frontend/src/lib/work-queue.ts 的车道定义逐条对应。blocking 计数
必须复用 issue_translator.merge_codes(条目 normalize 后 code 为空会被丢弃,
SQL json_array_length 不严格等价),所以在 Python 侧逐行算而非 SQL 聚合。

case_ids 只在有批量操作按钮的车道(todayNew / pendingReview)返回,按
id DESC 与列表接口同序,保证前端 slice(0, MAX_BATCH) 行为不变。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

from fastapi import APIRouter

from .. import db, issue_translator

router = APIRouter(prefix="/api/work-queue", tags=["work-queue"])


def _is_today_local(iso: str | None, today: date) -> bool:
    """等价 frontend isToday:按本地时区取年月日与今天比较。

    JS `new Date(iso)` 对带时区的 ISO 转本地、对裸时间戳按本地墙钟解释;
    fromisoformat 的 aware/naive 分支正好一一对应。
    """
    if not iso:
        return False
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return False
    if t.tzinfo is not None:
        t = t.astimezone()
    return t.date() == today


@router.get("/summary")
def work_queue_summary() -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()
    today = datetime.now().astimezone().date()
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, last_modified, category, manual_category,
                   customer_id, customer_raw, review_status,
                   blocking_issues_json, manual_blocking_issues_json
            FROM cases
            WHERE trashed_at IS NULL
              AND (held_until IS NULL OR held_until < ?)
            ORDER BY id DESC
            """,
            (now_iso,),
        ).fetchall()

    today_ids: list[int] = []
    pending_ids: list[int] = []
    missing_label = 0
    blocking_open = 0
    unbound_customer = 0
    for row in rows:
        if _is_today_local(row["last_modified"], today):
            today_ids.append(row["id"])
        if row["category"] == "non_labeled" and row["manual_category"] is None:
            missing_label += 1
        if row["review_status"] != "reviewed":
            auto_raw = json.loads(row["blocking_issues_json"] or "[]")
            manual_raw = json.loads(row["manual_blocking_issues_json"] or "[]")
            if issue_translator.merge_codes([*auto_raw, *manual_raw]):
                blocking_open += 1
        if row["customer_id"] is None and row["customer_raw"]:
            unbound_customer += 1
        if row["review_status"] == "pending":
            pending_ids.append(row["id"])

    return {
        "total": len(rows),
        "lanes": {
            "todayNew": {"count": len(today_ids), "case_ids": today_ids},
            "missingLabel": {"count": missing_label},
            "blockingOpen": {"count": blocking_open},
            "unboundCustomer": {"count": unbound_customer},
            "pendingReview": {"count": len(pending_ids), "case_ids": pending_ids},
        },
    }
