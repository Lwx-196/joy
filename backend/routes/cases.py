"""Aggregated case routes."""
from __future__ import annotations

# ruff: noqa: F401,F403

from fastapi import APIRouter

from .cases_support import *
from .cases_catalog import router as catalog_router
from .cases_catalog import list_cases, stats, batch_update, case_detail, update_case
from .cases_lifecycle import router as lifecycle_router
from .cases_lifecycle import trash_cases, reveal_case_path, rename_suggestion, upgrade_case, rescan_case
from .cases_assets import router as assets_router
from .cases_assets import case_file, patch_image_override, review_case_image, trash_case_image, restore_case_image
from .cases_manual_render import router as manual_render_router
from .cases_manual_render import prepare_manual_render_sources, preview_manual_render, manual_render_preview_file
from .cases_source_group import router as source_group_router
from .cases_source_group import list_source_blockers, apply_source_blocker_action, source_binding_candidates, bind_source_directories, clear_source_directory_bindings, lock_source_group_slot, clear_source_group_slot_lock, accept_source_group_warning, case_source_group, classify_case_images
from .cases_simulation_jobs import router as simulation_jobs_router
from .cases_simulation_jobs import ps_image_model_options, simulation_job_file_by_id, review_simulation_job_by_id, simulate_case_after, list_case_simulation_jobs, simulation_job_file, review_simulation_job
from .cases_quality_governance import router as quality_governance_router
from .cases_quality_governance import list_simulation_quality_queue, get_simulation_review_policy, put_simulation_review_policy, preview_simulation_review_policy, quality_report, quality_report_publishable_items, get_simulation_legacy_publishable_risk, quarantine_legacy_simulation_publishable_risk

router = APIRouter()

for subrouter in (
    quality_governance_router,
    simulation_jobs_router,
    source_group_router,
    manual_render_router,
    assets_router,
    lifecycle_router,
    catalog_router,
):
    router.include_router(subrouter)
