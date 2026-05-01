"""Pydantic v2 schemas for API responses."""
from __future__ import annotations

from typing import Any
from pydantic import BaseModel


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
