"""FastAPI entrypoint for case-workbench Phase 1."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import db, render_quality
from .render_queue import RENDER_QUEUE
from .routes import audit, case_groups, cases, customers, evaluations, image_workbench, issues, jobs, render, scan, stress, upgrade
from .upgrade_queue import UPGRADE_QUEUE

app = FastAPI(title="case-workbench", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5175", "http://127.0.0.1:5175"],
    allow_methods=["*"],
    allow_headers=["*"],
)

db.init_schema()
with db.connect() as _conn:
    render_quality.backfill_existing_render_quality(_conn)
RENDER_QUEUE.recover()
UPGRADE_QUEUE.recover()

app.include_router(scan.router)
app.include_router(stress.router)
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


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
