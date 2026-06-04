"""AI after-image simulation job endpoints."""
from __future__ import annotations

# ruff: noqa: F403,F405

from .cases_support import *

router = APIRouter(prefix="/api/cases", tags=["cases"])


@router.get("/ps-image-model-options", response_model=PsImageModelOptionsResponse)
def ps_image_model_options() -> PsImageModelOptionsResponse:
    return PsImageModelOptionsResponse(**ai_generation_adapter.get_ps_image_model_options())


@router.get("/simulation-jobs/{job_id}/file")
def simulation_job_file_by_id(
    job_id: int,
    kind: str = Query("ai_after_simulation"),
) -> FileResponse:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM simulation_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "simulation job not found")
    return FileResponse(_simulation_output_file(row, kind))


@router.post("/simulation-jobs/{job_id}/review", response_model=SimulationJob)
def review_simulation_job_by_id(
    job_id: int,
    payload: SimulationJobReviewRequest,
) -> SimulationJob:
    return _review_simulation_job_by_id(job_id, payload)


# ---------------------------------------------------------------------------
# Async simulation: POST creates job + submits to shared pool, returns immediately.
# ---------------------------------------------------------------------------


def _run_simulation_background(
    *,
    job_id: int,
    ai_run_id: int,
    provider: str,
    after_path: Path,
    before_path: Path | None,
    focus_targets: list[str],
    focus_regions: list[dict[str, Any]],
    model_name: str | None,
    note: str | None,
    style_reference_paths: list[Path],
    brand: str,
    case_id: int,
    input_refs: list[dict[str, Any]],
) -> None:
    """Worker function executed in _job_pool thread. Owns its own DB connections."""
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT status FROM simulation_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row or row["status"] != "queued":
                return
            conn.execute(
                "UPDATE simulation_jobs SET status = 'running', updated_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), job_id),
            )
            conn.execute(
                "UPDATE ai_runs SET status = 'running' WHERE id = ?",
                (ai_run_id,),
            )

        result = ai_generation_adapter.run_after_simulation(
            provider=provider,
            job_id=job_id,
            after_image_path=after_path,
            before_image_path=before_path,
            focus_targets=focus_targets,
            focus_regions=focus_regions,
            model_name=model_name,
            note=note,
            style_reference_image_paths=style_reference_paths,
            brand=brand,
            case_id=case_id,
        )
        status = str(result["status"])
        output_refs = result["output_refs"]
        audit_payload = {
            **result["audit"],
            "input_refs": input_refs,
            "output_refs": output_refs,
            "failure": None,
        }
        audit_payload = stress.tag_payload(audit_payload)
        error_message = result.get("error_message") if status != "done" else None
        with db.connect() as conn:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                UPDATE simulation_jobs
                SET status = ?,
                    output_refs_json = ?,
                    watermarked = ?,
                    audit_json = ?,
                    error_message = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(output_refs, ensure_ascii=False),
                    1 if result.get("watermarked") else 0,
                    json.dumps(audit_payload, ensure_ascii=False),
                    error_message,
                    now,
                    job_id,
                ),
            )
            conn.execute(
                """
                UPDATE ai_runs
                SET output_json = ?,
                    status = ?,
                    error_message = ?,
                    finished_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps({"output_refs": output_refs, "audit": audit_payload}, ensure_ascii=False),
                    status,
                    error_message,
                    now,
                    ai_run_id,
                ),
            )
    except Exception as exc:  # noqa: BLE001
        _tb = traceback.format_exc()
        if len(_tb) > 16384:
            _tb = _tb[:16384] + f"\n... [truncated {len(_tb) - 16384} chars]"
        failure_block = {
            "failure_stage": "provider_call",
            "error_class": type(exc).__name__,
            "error_message": str(exc)[:4000],
            "provider_attempts": [
                {
                    "provider": provider,
                    "model_name": model_name,
                    "attempt": 1,
                    "error_class": type(exc).__name__,
                }
            ],
            "workflow_name": model_name,
            "retry_trace": [],
            "traceback": _tb,
        }
        audit_payload = {
            "provider": provider,
            "model_name": model_name,
            "focus_targets": focus_targets,
            "focus_regions": focus_regions,
            "input_refs": input_refs,
            "output_refs": [],
            "policy": _simulation_policy(focus_regions, case_id=case_id),
            "note": note,
            "failure": failure_block,
        }
        audit_payload = stress.tag_payload(audit_payload)
        error_message = str(exc)[:4000]
        with db.connect() as conn:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                UPDATE simulation_jobs
                SET status = 'failed',
                    output_refs_json = '[]',
                    watermarked = 0,
                    audit_json = ?,
                    error_message = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(audit_payload, ensure_ascii=False), error_message, now, job_id),
            )
            conn.execute(
                """
                UPDATE ai_runs
                SET output_json = ?,
                    status = 'failed',
                    error_message = ?,
                    finished_at = ?
                WHERE id = ?
                """,
                (json.dumps({"error": error_message}, ensure_ascii=False), error_message, now, ai_run_id),
            )


@router.post("/{case_id}/simulate-after", response_model=SimulateAfterResponse)
def simulate_case_after(case_id: int, payload: SimulateAfterRequest) -> SimulateAfterResponse:
    focus_targets = [x.strip() for x in payload.focus_targets if x.strip()]
    focus_regions = _normalize_focus_regions(payload.focus_regions)
    if not payload.ai_generation_authorized:
        raise HTTPException(400, "ai_generation_authorized must be true")
    provider = (payload.provider or _SIMULATION_PROVIDER).strip()
    _validate_simulation_provider(provider, payload.model_name)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    with db.connect() as conn:
        case_dir = _case_dir_for_update(conn, case_id)
        after_path = _resolve_simulation_image_input(
            case_dir,
            path=payload.after_image_path,
            image=payload.after_image,
            role="after",
            stamp=stamp,
            required=True,
        )
        assert after_path is not None
        before_path = _resolve_simulation_image_input(
            case_dir,
            path=payload.before_image_path,
            image=payload.before_image,
            role="before",
            stamp=stamp,
            required=False,
        )
        style_reference_paths = [
            _resolve_style_reference_path(p) for p in payload.style_reference_paths
        ]
        input_refs = [
            _simulation_input_ref(case_dir, "after_source", after_path),
            *(
                [_simulation_input_ref(case_dir, "before_pose_reference", before_path)]
                if before_path
                else []
            ),
            *[
                _simulation_input_ref(case_dir, "style_reference", sp)
                for sp in style_reference_paths
            ],
        ]
        job_id = _insert_simulation_job(
            conn,
            case_id=case_id,
            focus_targets=focus_targets,
            focus_regions=focus_regions,
            input_refs=input_refs,
            provider=provider,
            model_name=payload.model_name,
            note=payload.note,
            status="queued",
        )
        ai_run_id = _insert_ai_run(
            conn,
            job_id=job_id,
            provider=provider,
            model_name=payload.model_name,
            focus_targets=focus_targets,
            focus_regions=focus_regions,
            input_refs=input_refs,
            status="queued",
        )

    _job_pool.submit(
        _run_simulation_background,
        job_id=job_id,
        ai_run_id=ai_run_id,
        provider=provider,
        after_path=after_path,
        before_path=before_path,
        focus_targets=focus_targets,
        focus_regions=focus_regions,
        model_name=payload.model_name,
        note=payload.note,
        style_reference_paths=style_reference_paths,
        brand=payload.brand,
        case_id=case_id,
        input_refs=input_refs,
    )

    return SimulateAfterResponse(
        simulation_job_id=job_id,
        case_id=case_id,
        status="queued",
        focus_targets=focus_targets,
        focus_regions=focus_regions,
        provider=provider,
        model_name=payload.model_name,
        input_refs=input_refs,
    )


@router.get("/{case_id}/simulation-jobs", response_model=list[SimulationJob])
def list_case_simulation_jobs(
    case_id: int,
    limit: int = Query(10, ge=1, le=100),
) -> list[SimulationJob]:
    with db.connect() as conn:
        exists = conn.execute("SELECT id FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "case not found")
        rows = conn.execute(
            """
            SELECT * FROM simulation_jobs
            WHERE case_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (case_id, limit),
        ).fetchall()
    return [_simulation_row_to_model(row) for row in rows]


@router.get("/{case_id}/simulation-jobs/{job_id}/file")
def simulation_job_file(
    case_id: int,
    job_id: int,
    kind: str = Query("ai_after_simulation"),
) -> FileResponse:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM simulation_jobs WHERE id = ? AND case_id = ?",
            (job_id, case_id),
        ).fetchone()
    if not row:
        raise HTTPException(404, "simulation job not found")
    return FileResponse(_simulation_output_file(row, kind))


@router.post("/{case_id}/simulation-jobs/{job_id}/review", response_model=SimulationJob)
def review_simulation_job(
    case_id: int,
    job_id: int,
    payload: SimulationJobReviewRequest,
) -> SimulationJob:
    return _review_simulation_job_by_id(job_id, payload, case_id=case_id)
