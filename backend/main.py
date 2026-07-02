"""FastAPI entrypoint for case-workbench Phase 1."""
from __future__ import annotations

import logging
import threading

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import db, render_quality
from .render_queue import RENDER_QUEUE
from .routes import audit, best_pair, case_groups, cases, classification, customers, evaluations, image_workbench, issues, jobs, render, review_tickets, scan, stress, upgrade
from .upgrade_queue import UPGRADE_QUEUE

app = FastAPI(title="case-workbench", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5175", "http://127.0.0.1:5175"],
    allow_methods=["*"],
    allow_headers=["*"],
)

db.init_schema()


def _backfill_render_quality_async() -> None:
    # 版本门重评在 bump 后是全库 CV 扫描（数百板），同步跑会阻塞服务启动几十分钟；
    # backfill 内部逐行 commit，后台线程不长持写锁。
    try:
        with db.connect() as conn:
            count = render_quality.backfill_existing_render_quality(conn)
        if count:
            logging.getLogger(__name__).info("render_quality backfill 完成：%s 行", count)
    except Exception:
        logging.getLogger(__name__).exception("render_quality backfill 后台线程失败")


threading.Thread(
    target=_backfill_render_quality_async, name="render-quality-backfill", daemon=True
).start()
RENDER_QUEUE.recover()
UPGRADE_QUEUE.recover()

app.include_router(scan.router)
app.include_router(stress.router)
app.include_router(best_pair.router)
app.include_router(case_groups.router)
app.include_router(cases.router)
app.include_router(image_workbench.router)
app.include_router(audit.router)
app.include_router(render.router)
app.include_router(upgrade.router)
app.include_router(jobs.router)
app.include_router(customers.router)
app.include_router(issues.router)
app.include_router(evaluations.router)
app.include_router(classification.router)
app.include_router(review_tickets.router)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
