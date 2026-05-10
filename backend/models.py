"""Pydantic v2 schemas for API responses."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


class CaseSummary(BaseModel):
    id: int
    abs_path: str
    customer_raw: str | None
    customer_id: int | None
    customer_canonical: str | None = None
    # category / tier 现在分两层：自动判读 + 手动覆盖
    auto_category: str
    auto_template_tier: str | None
    manual_category: str | None = None
    manual_template_tier: str | None = None
    category: str  # effective = manual ?? auto
    template_tier: str | None  # effective
    source_count: int | None
    labeled_count: int | None
    blocking_issue_count: int = 0
    notes: str | None = None
    tags: list[str] = []
    review_status: str | None = None
    reviewed_at: str | None = None
    # 三态 manual UX 之 "挂起"：held_until 为非 NULL 表示该 case 被搁置（工作队列隐藏）
    held_until: str | None = None
    hold_reason: str | None = None
    latest_render_status: str | None = None
    latest_render_quality_status: str | None = None
    latest_render_quality_score: float | None = None
    last_modified: str
    indexed_at: str


class CaseListResponse(BaseModel):
    items: list[CaseSummary]
    total: int
    page: int
    page_size: int


class CaseDetail(CaseSummary):
    auto_blocking_issues: list[dict[str, Any]] = []
    manual_blocking_codes: list[str] = []
    blocking_issues: list[dict[str, Any]] = []  # effective: auto ∪ manual
    pose_delta_max: float | None = None
    sharp_ratio_min: float | None = None
    meta: dict[str, Any] = {}
    rename_suggestion: str | None = None
    # Stage A: skill 透传(只在 v3 升级后非空)
    skill_image_metadata: list[dict[str, Any]] = []
    skill_blocking_detail: list[str] = []
    skill_warnings: list[str] = []
    classification_preflight: dict[str, Any] = {}


class ManualTransform(BaseModel):
    """Per-image manual layer adjustment for formal render.

    Offsets are normalized to the final render cell size: 0.03 means moving
    the layer by 3% of the cell width/height. Scale is relative, centered.
    """
    offset_x_pct: float = Field(default=0, ge=-0.25, le=0.25)
    offset_y_pct: float = Field(default=0, ge=-0.25, le=0.25)
    scale: float = Field(default=1, ge=0.85, le=1.15)


class ImageOverridePayload(BaseModel):
    """Stage B: PATCH /api/cases/{id}/images/{filename} 请求体。

    任一字段为 None 表示"不修改";空字符串 "" 表示清除该维度的覆盖回退到 skill 自动判读。
    """
    manual_phase: str | None = None
    manual_view: str | None = None
    manual_transform: ManualTransform | None = None


class ImageOverride(BaseModel):
    """Stage B: 单张图覆盖记录(响应)。"""
    case_id: int
    filename: str
    manual_phase: str | None = None
    manual_view: str | None = None
    manual_transform: dict[str, Any] | None = None
    updated_at: str


class ImageReviewPayload(BaseModel):
    """Source-image quality review decision.

    Decisions are kept in cases.meta_json so they can be audited together with
    the case row and used by render preflight without adding a schema migration.
    """
    verdict: Literal["usable", "deferred", "needs_repick", "excluded", "reopen"]
    reviewer: str | None = "operator"
    note: str | None = None
    layer: str | None = None


class ImageReviewResponse(BaseModel):
    case_id: int
    filename: str
    review_state: dict[str, Any] | None = None
    detail: CaseDetail


class ManualRenderImageInput(BaseModel):
    """One user-picked image for the manual before/after render flow.

    kind='existing': `filename` is a case-relative source image path.
    kind='upload': `data_url` is a browser FileReader data URL.
    """
    kind: Literal["existing", "upload"]
    filename: str | None = None
    upload_name: str | None = None
    data_url: str | None = None


class ManualRenderSourcesRequest(BaseModel):
    before: ManualRenderImageInput
    after: ManualRenderImageInput
    view: Literal["front", "oblique", "side"] = "front"
    before_transform: ManualTransform | None = None


class ManualRenderPreviewRequest(BaseModel):
    before: ManualRenderImageInput
    after: ManualRenderImageInput
    view: Literal["front", "oblique", "side"] = "front"
    brand: str = "fumei"
    before_transform: ManualTransform | None = None


class ManualRenderPreviewResponse(BaseModel):
    case_id: int
    preview_id: str
    view: str
    output_path: str
    manifest_path: str | None = None
    render_plan: dict[str, Any] = {}
    warnings: list[str] = []


class ManualRenderSourcesResponse(BaseModel):
    case_id: int
    view: str
    created_files: list[str]
    manual_overrides: list[ImageOverride]
    detail: CaseDetail


class ImageTrashRequest(BaseModel):
    filename: str


class ImageTrashResponse(BaseModel):
    case_id: int
    original_filename: str
    trash_path: str
    detail: CaseDetail


class ImageRestoreRequest(BaseModel):
    trash_path: str
    restore_to: str | None = None


class ImageRestoreResponse(BaseModel):
    case_id: int
    trash_path: str
    restored_filename: str
    detail: CaseDetail


class CaseRevealRequest(BaseModel):
    target: Literal["case_root", "render_output"]
    brand: str | None = "fumei"
    template: str | None = "tri-compare"


class CaseRevealResponse(BaseModel):
    opened: bool
    path: str


class FocusRegion(BaseModel):
    x: float
    y: float
    width: float
    height: float
    label: str | None = None


class SimulateAfterRequest(BaseModel):
    after_image_path: str | None = None
    after_image: ManualRenderImageInput | None = None
    before_image_path: str | None = None
    before_image: ManualRenderImageInput | None = None
    focus_targets: list[str] = Field(default_factory=list)
    focus_regions: list[FocusRegion] = Field(default_factory=list)
    ai_generation_authorized: bool = False
    provider: str = "ps_model_router"
    model_name: str | None = None
    note: str | None = None
    style_reference_paths: list[str] = Field(default_factory=list)


class SimulateAfterResponse(BaseModel):
    simulation_job_id: int
    case_id: int
    status: str
    focus_targets: list[str]
    focus_regions: list[dict[str, Any]] = []
    provider: str
    model_name: str | None = None
    input_refs: list[dict[str, Any]]
    output_refs: list[dict[str, Any]]
    audit: dict[str, Any]
    error_message: str | None = None


class PsImageModelOption(BaseModel):
    value: str
    label: str
    source: str
    description: str | None = None
    is_default: bool = False


class PsImageModelOptionsResponse(BaseModel):
    provider: str
    default_model: str | None = None
    fallback_model: str | None = None
    options: list[PsImageModelOption]


class SimulationJob(BaseModel):
    id: int
    group_id: int | None = None
    case_id: int | None = None
    status: str
    focus_targets: list[str]
    policy: dict[str, Any]
    model_plan: dict[str, Any]
    input_refs: list[dict[str, Any]]
    output_refs: list[dict[str, Any]]
    available_files: list[dict[str, Any]] = Field(default_factory=list)
    watermarked: bool
    audit: dict[str, Any]
    review_decision: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    review_status: str | None = None
    reviewer: str | None = None
    review_note: str | None = None
    reviewed_at: str | None = None
    can_publish: bool = False
    created_at: str
    updated_at: str


class SimulationJobReviewRequest(BaseModel):
    verdict: Literal["approved", "needs_recheck", "rejected"]
    reviewer: str
    note: str | None = None


class SourceBlockerActionRequest(BaseModel):
    action: Literal["mark_not_source", "clear_not_source"]
    reviewer: str | None = None
    note: str | None = None


class SourceDirectoryBindRequest(BaseModel):
    source_case_ids: list[int] = Field(default_factory=list)
    reviewer: str | None = None
    note: str | None = None


class CaseUpdate(BaseModel):
    manual_category: str | None = None
    manual_template_tier: str | None = None
    manual_blocking_codes: list[str] | None = None
    notes: str | None = None
    tags: list[str] | None = None
    review_status: str | None = None
    customer_id: int | None = None
    # 三态 manual UX 之 "挂起"
    held_until: str | None = None
    hold_reason: str | None = None
    # clear_fields 内放字段名（如 "manual_category" / "held_until"）即把对应列置 NULL
    clear_fields: list[str] = []


class CaseBatchUpdate(BaseModel):
    case_ids: list[int]
    update: CaseUpdate


class CaseTrashRequest(BaseModel):
    case_ids: list[int]
    reason: str | None = None


class CaseTrashSkipped(BaseModel):
    case_id: int
    reason: str


class CaseTrashResponse(BaseModel):
    trashed: int
    case_ids: list[int]
    skipped: list[CaseTrashSkipped] = []


class CustomerSummary(BaseModel):
    id: int
    canonical_name: str
    aliases: list[str] = []
    notes: str | None = None
    case_count: int = 0


class CustomerDetail(CustomerSummary):
    cases: list[CaseSummary] = []


class CustomerCreate(BaseModel):
    canonical_name: str
    aliases: list[str] = []
    notes: str | None = None


class CustomerUpdate(BaseModel):
    canonical_name: str | None = None
    aliases: list[str] | None = None
    notes: str | None = None


class CustomerMerge(BaseModel):
    case_ids: list[int]


class ScanRequest(BaseModel):
    mode: str = "incremental"  # full | incremental


class ScanResult(BaseModel):
    scan_id: int
    case_count: int
    new_count: int
    updated_count: int
    skipped_count: int
    duration_ms: int
    mode: str


class IssueDictEntry(BaseModel):
    code: str
    zh: str
    next: str
