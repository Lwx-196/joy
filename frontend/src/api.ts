import axios from "axios";

export const api = axios.create({ baseURL: "" });

export type Category =
  | "body"
  | "standard_face"
  | "non_labeled"
  | "fragment_only"
  | "unsupported";

export const CATEGORY_LABEL: Record<Category, string> = {
  body: "身体",
  standard_face: "标准面部",
  non_labeled: "未命名",
  fragment_only: "全帧图",
  unsupported: "不支持",
};

export const TIER_LABEL: Record<string, string> = {
  tri: "三联",
  bi: "双联",
  single: "单联",
  "body-dual-compare": "身体对比",
  unsupported: "不支持",
};

export type ReviewStatus = "pending" | "reviewed" | "needs_recheck";

export interface CaseSummary {
  id: number;
  abs_path: string;
  customer_raw: string | null;
  customer_id: number | null;
  customer_canonical: string | null;
  auto_category: Category;
  auto_template_tier: string | null;
  manual_category: Category | null;
  manual_template_tier: string | null;
  category: Category;
  template_tier: string | null;
  source_count: number | null;
  labeled_count: number | null;
  blocking_issue_count: number;
  notes: string | null;
  tags: string[];
  review_status: ReviewStatus | null;
  reviewed_at: string | null;
  // 三态之 "挂起"：held_until 非空 = 已挂起到该时刻
  held_until: string | null;
  hold_reason: string | null;
  latest_render_status: RenderStatus | null;
  latest_render_quality_status: RenderQualityStatus | null;
  latest_render_quality_score: number | null;
  last_modified: string;
  indexed_at: string;
}

export interface BlockingIssue {
  code: string;
  zh: string;
  next: string;
  // B2 schema v2: which source files are affected, and how severe.
  // Empty `files` means a case-level issue (e.g., missing a whole role of images).
  files?: string[];
  severity?: "block" | "warn";
}

export type SkillPhase = "before" | "after" | null;
export type SkillViewBucket = "front" | "oblique" | "side" | null;

export interface SkillImageMetadata {
  filename: string | null;
  relative_path: string | null;
  phase: SkillPhase;
  phase_source: string | null;
  angle: string | null;
  angle_source: string | null;
  angle_confidence: number | null;
  direction: string | null;
  view_bucket: SkillViewBucket;
  pose: { pitch: number; yaw: number; roll: number } | null;
  sharpness_score: number | null;
  sharpness_level: string | null;
  issues: string[];
  rejection_reason: string | null;
  // Stage B: 'manual' = user override applied; null/undefined = skill auto-detected
  phase_override_source?: "manual" | null;
  view_override_source?: "manual" | null;
  manual_transform?: ManualTransform | null;
  manual_transform_source?: "manual" | null;
  review_state?: ImageReviewState | null;
}

export interface ClassificationPreflightReviewItem {
  filename: string;
  phase: "before" | "after" | null;
  phase_source: string;
  view: "front" | "oblique" | "side" | null;
  view_source: string;
  manual: boolean;
  severity: "block" | "review" | "info";
  layer?: string;
  layer_label?: string;
  action?: string;
  reasons: string[];
  angle_confidence: number | null;
  issues: string[];
  rejection_reason: string | null;
  review_state?: ImageReviewState | null;
}

export interface ClassificationGap {
  kind: string;
  filename?: string;
  phase?: "before" | "after" | null;
  view?: "front" | "oblique" | "side" | null;
  missing?: string[];
  body_part?: string | null;
  treatment_area?: string | null;
}

export interface ClassificationReviewSlot {
  key: string;
  label: string;
  count: number;
  before?: string | null;
  after?: string | null;
  filenames: string[];
  pose_delta?: Record<string, unknown> | null;
}

export interface ClassificationReviewLayer {
  key: string;
  label: string;
  severity: "block" | "review" | "info" | string;
  count: number;
  action: string;
  filenames: string[];
  slots?: ClassificationReviewSlot[];
}

export interface ClassificationPreflightSlot {
  view: "front" | "oblique" | "side";
  label: string;
  before_count: number;
  after_count: number;
  manual_before_count: number;
  manual_after_count: number;
  ready: boolean;
}

export interface SupplementCandidate {
  case_id: number;
  filename: string;
  preview_url: string;
  case_url: string;
  case_title: string;
  customer_raw: string | null;
  phase: string;
  view: string;
  body_part: string;
  treatment_area: string | null;
  confidence: number;
  queue_state: string;
  manual: boolean;
  used_in_render: boolean;
  review_state: ImageReviewState | null;
  score: number;
  match_reasons: string[];
}

export interface SupplementGap {
  key: string;
  kind: "render_slot" | string;
  view: "front" | "oblique" | "side" | string;
  view_label: string;
  role: "before" | "after" | string;
  phase: "before" | "after" | string;
  role_label: string;
  body_part: string;
  treatment_area: string | null;
  current_count: number;
  required_count: number;
  candidates?: SupplementCandidate[];
  candidate_count?: number;
}

export interface ClassificationPreflight {
  classification: {
    source_count: number;
    source_profile?: CaseSourceProfile;
    metadata_count: number;
    classified_count: number;
    needs_manual_count: number;
    actionable_review_count?: number;
    expected_profile_noise_count?: number;
    reviewed_count?: number;
    deferred_review_count?: number;
    needs_repick_count?: number;
    render_excluded_count?: number;
    manual_override_count: number;
    low_confidence_count: number;
    review_count: number;
    gaps?: ClassificationGap[];
    review_layers?: ClassificationReviewLayer[];
    review_items: ClassificationPreflightReviewItem[];
  };
  render: {
    status: "ready" | "review" | "blocked" | string;
    ready: boolean;
    slots: ClassificationPreflightSlot[];
    blocking: { code: string; view: string; label: string; missing: string[] }[];
    gaps?: SupplementGap[];
    blocking_summary?: Record<string, number>;
    acceptable_review?: Record<string, number>;
    suggested_action: string;
  };
  latest_render: {
    job_id: number;
    job_status: string;
    quality_status: string | null;
    quality_score: number | null;
    can_publish: boolean | null;
    blocking_count: number;
    warning_count: number;
    warning_buckets: {
      candidate_noise?: number;
      face_detection: number;
      profile_expected?: number;
      profile_quality?: number;
      pose_delta: number;
      pose_candidates: number;
      other: number;
      noise_count?: number;
      actionable_count?: number;
    };
    warning_layers?: ClassificationReviewLayer[];
    acceptable_warning_count?: number;
    blocking_warning_count?: number;
    publish_blockers?: string[];
    review_verdict: string | null;
    ai_usage: {
      used_after_enhancement: boolean;
      used_ai_padfill: boolean;
      semantic_judge_requested?: unknown;
      semantic_judge_effective?: unknown;
    };
  } | null;
}

export interface CaseDetail extends CaseSummary {
  auto_blocking_issues: BlockingIssue[];
  manual_blocking_codes: string[];
  blocking_issues: BlockingIssue[];
  pose_delta_max: number | null;
  sharp_ratio_min: number | null;
  meta: {
    image_files?: string[];
    image_count_total?: number;
    // v3 upgrade marker (from case-layout-board skill)
    source?: string;
    skill_template?: string;
    skill_case_mode?: string;
    skill_status?: string;
    skill_warning_count?: number;
    skill_blocking_issue_count?: number;
    skill_upgraded_at?: string;
  };
  rename_suggestion: string | null;
  // Stage A: skill 透传(v3 升级后非空,数组按 manifest entries 顺序)
  skill_image_metadata: SkillImageMetadata[];
  skill_blocking_detail: string[];
  skill_warnings: string[];
  classification_preflight: ClassificationPreflight;
}

export interface CaseUpdatePayload {
  manual_category?: Category | null;
  manual_template_tier?: string | null;
  manual_blocking_codes?: string[];
  notes?: string;
  tags?: string[];
  review_status?: ReviewStatus;
  customer_id?: number;
  // 三态之 "挂起"：传 ISO 时间字符串挂起；通过 clear_fields 取消
  held_until?: string;
  hold_reason?: string;
  clear_fields?: string[];
}

export interface CustomerSummary {
  id: number;
  canonical_name: string;
  aliases: string[];
  notes: string | null;
  case_count: number;
}

export interface CustomerDetail extends CustomerSummary {
  cases: CaseSummary[];
}

export interface CandidateResult {
  raw: string;
  normalized: string;
  decision: "matched" | "candidates" | "new";
  suggestion: string;
  candidates: (CustomerSummary & { similarity?: number })[];
}

export interface ScanLatest {
  scan: {
    id: number;
    started_at: string;
    completed_at: string | null;
    case_count: number;
    mode: string;
    root_paths: string[];
  } | null;
}

export interface Stats {
  total: number;
  by_category: Partial<Record<Category, number>>;
  by_tier: Record<string, number>;
  by_review_status: Record<string, number>;
  manual_override_count: number;
}

export const fetchStats = () => api.get<Stats>("/api/cases/stats").then((r) => r.data);
export const fetchScanLatest = () =>
  api.get<ScanLatest>("/api/scan/latest").then((r) => r.data);
export const triggerScan = (mode: "full" | "incremental" = "incremental") =>
  api.post(`/api/scan?mode=${mode}`).then((r) => r.data);

export type CaseListParams = {
  page?: number;
  page_size?: number;
  /**
   * Legacy alias — Dashboard/Dict still pass `limit`. The useCases hook
   * translates this to `page_size` before calling the API.
   */
  limit?: number;
  category?: string;
  tier?: string;
  customer_id?: number;
  review_status?: string;
  q?: string;
  tag?: string;
  since?: string;
  blocking?: string;
  include_held?: number;
};

export type CasesPage = {
  items: CaseSummary[];
  total: number;
  page: number;
  page_size: number;
};

export const fetchCases = (params: CaseListParams = {}) =>
  api.get<CasesPage>("/api/cases", { params }).then((r) => r.data);

export const fetchCaseDetail = (id: number) =>
  api.get<CaseDetail>(`/api/cases/${id}`).then((r) => r.data);

export const updateCase = (id: number, payload: CaseUpdatePayload) =>
  api.patch<CaseDetail>(`/api/cases/${id}`, payload).then((r) => r.data);

export const batchUpdateCases = (case_ids: number[], update: CaseUpdatePayload) =>
  api
    .post<{ updated: number; case_ids: number[] }>(`/api/cases/batch`, { case_ids, update })
    .then((r) => r.data);

export interface CaseTrashResponse {
  trashed: number;
  case_ids: number[];
  skipped: { case_id: number; reason: string }[];
}

export const trashCases = (case_ids: number[], reason?: string | null) =>
  api
    .post<CaseTrashResponse>(`/api/cases/trash`, { case_ids, reason: reason ?? null })
    .then((r) => r.data);

export const caseFileUrl = (id: number, name: string) =>
  `/api/cases/${id}/files?name=${encodeURIComponent(name)}`;

const withCacheBuster = (url: string, cacheBuster?: number | null) =>
  cacheBuster != null ? `${url}${url.includes("?") ? "&" : "?"}v=${Math.floor(cacheBuster)}` : url;

export type CaseRevealTarget = "case_root" | "render_output";

export interface CaseRevealPayload {
  target: CaseRevealTarget;
  brand?: string | null;
  template?: string | null;
}

export interface CaseRevealResponse {
  opened: boolean;
  path: string;
}

export const revealCasePath = (id: number, payload: CaseRevealPayload) =>
  api.post<CaseRevealResponse>(`/api/cases/${id}/reveal`, payload).then((r) => r.data);

// Stage B: 单张图 phase / view 手动覆盖
export type ImageOverridePhase = "before" | "after" | null;
export type ImageOverrideView = "front" | "oblique" | "side" | null;

export interface ImageOverridePayload {
  manual_phase?: string | null; // null = unchanged; "" = clear; allowed value = set
  manual_view?: string | null;
  manual_transform?: ManualTransform | null;
}

export interface ImageOverride {
  case_id: number;
  filename: string;
  manual_phase: ImageOverridePhase;
  manual_view: ImageOverrideView;
  manual_transform: ManualTransform | null;
  updated_at: string;
}

export type ImageReviewVerdict = "usable" | "deferred" | "needs_repick" | "excluded" | "reopen";

export interface ImageReviewState {
  verdict?: Exclude<ImageReviewVerdict, "reopen">;
  label?: string;
  reviewer?: string | null;
  note?: string | null;
  layer?: string | null;
  render_excluded?: boolean;
  reviewed_at?: string | null;
  copied_requires_review?: boolean;
  copied_from_case_id?: number;
  copied_from_filename?: string;
  inherited_verdict?: string | null;
  inherited_label?: string | null;
}

export interface ImageReviewPayload {
  verdict: ImageReviewVerdict;
  reviewer?: string | null;
  note?: string | null;
  layer?: string | null;
}

export interface ImageReviewResponse {
  case_id: number;
  filename: string;
  review_state: ImageReviewState | null;
  detail: CaseDetail;
}

export type ImageWorkbenchState =
  | "needs_manual"
  | "identified"
  | "manual"
  | "low_confidence"
  | "used_in_render"
  | "usable"
  | "deferred"
  | "needs_repick"
  | "render_excluded"
  | "copied_review";

export interface ImageWorkbenchItem {
  case_id: number;
  group_id: number;
  observation_id: number;
  filename: string;
  image_path: string;
  preview_url: string;
  case_url: string;
  case_title: string;
  case_abs_path: string;
  customer_raw: string | null;
  customer_id?: number | null;
  phase: string;
  phase_source: string;
  view: string;
  view_source: string;
  body_part: string;
  treatment_area: string | null;
  confidence: number;
  source: string;
  reasons: string[];
  queue_state: ImageWorkbenchState | string;
  manual: boolean;
  used_in_render: boolean;
  low_confidence: boolean;
  needs_manual: boolean;
  render_excluded: boolean;
  review_state: ImageReviewState | null;
  quality: Record<string, unknown>;
  classification_suggestion?: {
    suggested_labels: {
      phase: string | null;
      view: string | null;
      body_part: string | null;
      treatment_area: string | null;
    };
	    label_confidence: number | null;
	    confidence_band?: "high" | "review" | "low" | string;
	    task_groups: string[];
	    blocker_level: "ok" | "review" | "block" | string;
	    render_gate?: {
	      blocks_render: boolean;
	      reason: string;
	      level: "ok" | "block" | string;
	      message: string;
	    };
	    recommended_actions: { code: string; label: string; primary?: boolean }[];
	    classification_layers: {
	      deterministic?: Record<string, unknown>;
	      local_visual?: Record<string, unknown>;
	      visual?: Record<string, unknown>;
	      manual?: Record<string, unknown>;
	    };
  };
  task_groups?: string[];
  blocker_level?: "ok" | "review" | "block" | string;
  recommended_actions?: { code: string; label: string; primary?: boolean }[];
  safe_confirm?: {
    eligible: boolean;
    reason: string;
    threshold: number;
    phase?: string;
    view?: string;
    would_mark_usable?: boolean;
  };
  case_preflight?: {
    source_kind?: string;
    reason?: string | null;
    reason_label?: string | null;
    recommended_action?: string | null;
    source_count?: number;
    before_count?: number;
    after_count?: number;
    source_phase_hint?: string | null;
    source_phase_hint_label?: string | null;
    missing_source_count?: number;
  };
  updated_at: string;
}

export interface ImageWorkbenchCaseGroup {
  case_id: number;
  case_title: string;
  case_url: string;
  customer_raw: string | null;
  total_count: number;
  filtered_count: number;
  needs_manual_count: number;
  missing_phase_count: number;
  missing_view_count: number;
  missing_usability_count: number;
  low_confidence_count: number;
  used_in_render_count: number;
  render_excluded_count: number;
  safe_confirm_count: number;
  readiness_score: number;
  preflight_status: "ready" | "blocked" | string;
  processing_mode?: "classification" | "classify_or_bind" | "source_fix" | string;
  processing_mode_label?: string;
  priority_score: number;
  next_action: string;
  queue_url: string;
  classification_url?: string;
  source_fix_url?: string;
  source_context: {
    source_kind?: string;
    reason?: string | null;
    reason_label?: string | null;
    recommended_action?: string | null;
    source_count?: number;
    before_count?: number;
    after_count?: number;
    source_phase_hint?: string | null;
    source_phase_hint_label?: string | null;
    missing_source_count?: number;
  };
  missing_slots: { view: string; label: string; missing: string[] }[];
  hard_blockers: {
    code: string;
    label?: string | null;
    recommended_action?: string | null;
    count?: number;
    slots?: { view: string; label: string; missing: string[] }[];
  }[];
}

export interface ImageWorkbenchBatchGroup {
  id: string;
  case_id: number;
  case_title: string;
  case_url: string;
  processing_mode: "classification" | "classify_or_bind" | "source_fix" | string;
  processing_mode_label: string;
  source_reason: string | null;
  source_reason_label: string | null;
  source_phase_hint: string | null;
  source_phase_hint_label: string | null;
  recommended_action: string;
  task_groups: string[];
  filename_bucket: string;
  phase_key: string;
  view_key: string;
  body_part: string;
  item_count: number;
  filenames: string[];
  sample_images: { case_id: number; filename: string; preview_url: string }[];
  missing_phase_count: number;
  missing_view_count: number;
  missing_usability_count: number;
  low_confidence_count: number;
  safe_confirm_count: number;
  used_in_render_count: number;
  phase_counts: Record<string, number>;
  view_counts: Record<string, number>;
  body_part_counts: Record<string, number>;
  confidence_avg: number;
  confidence_min: number;
  suggested_phase: string | null;
  suggested_view: string | null;
  recommended_patch: Partial<ImageWorkbenchBatchPayload> | null;
  can_bulk_apply_suggestion: boolean;
  classification_url: string;
  source_fix_url: string;
}

export interface ImageWorkbenchAngleSortGroup {
  id: string;
  case_id: number;
  case_title: string | null;
  orientation: string;
  orientation_label: string;
  item_count: number;
  filenames: string[];
  sample_images: { case_id: number; filename: string; preview_url: string }[];
  images?: { case_id: number; filename: string; preview_url: string }[];
  sequence_range: string;
  composition_summary: string;
  reason_labels: string[];
  metrics: {
    similarity_score: number;
    distance_avg: number;
    aspect_avg: number;
    brightness_avg: number;
    edge_density_avg: number;
    local_angle_confidence_avg?: number | null;
    local_angle_agreement?: number | null;
  };
  local_angle_votes?: Record<string, number>;
  suggested_view?: "front" | "oblique" | "side" | string | null;
  suggested_view_label?: string | null;
  suggested_view_confidence?: number | null;
  suggested_view_agreement?: number | null;
  suggested_phase?: "before" | "after" | string | null;
  suggested_phase_label?: string | null;
  missing_phase_count?: number;
  can_quick_confirm_angle?: boolean;
  recommended_patch?: Partial<ImageWorkbenchBatchPayload> | null;
  angle_evidence_labels?: string[];
  recommended_action: string;
}

export interface ImageWorkbenchQueueResponse {
  items: ImageWorkbenchItem[];
  total: number;
  limit: number;
  offset: number;
  status: string;
  counts: Record<string, number>;
	  summary: {
    needs_manual: number;
    low_confidence: number;
    manual: number;
    identified: number;
    used_in_render: number;
    render_excluded: number;
    needs_repick: number;
    copied_review?: number;
    missing_phase?: number;
    missing_view?: number;
    missing_usability?: number;
	    blocked_case?: number;
	  };
	  task_queues?: Record<string, {
	    key: string;
	    label: string;
	    count: number;
	    item_count: number;
	    blocks_render: boolean;
	    recommended_action: string;
	    queue_url: string;
	  }>;
	  production_summary?: {
	    review_needed_total: number;
	    blocking_image_count: number;
	    low_confidence_count: number;
	    bulk_group_count: number;
	    angle_sort_group_count: number;
	    ready_for_render_candidate_count: number;
	    policy: {
	      name: string;
	      high_confidence_threshold: number;
	      low_confidence_blocks_render: boolean;
	    };
	  };
	  case_groups?: ImageWorkbenchCaseGroup[];
  batch_groups?: ImageWorkbenchBatchGroup[];
  angle_sort_groups?: ImageWorkbenchAngleSortGroup[];
}

export interface SupplementCandidatesResponse {
  target_case_id: number;
  gaps: SupplementGap[];
  summary: {
    gap_count: number;
    candidate_count: number;
  };
}

export interface ImageWorkbenchBatchPayload {
  items: { case_id: number; filename: string }[];
  manual_phase?: "before" | "after" | "clear" | null;
  manual_view?: "front" | "oblique" | "side" | "clear" | null;
  body_part?: "face" | "body" | "unknown" | "clear" | null;
  treatment_area?: string | null;
  verdict?: ImageReviewVerdict | null;
  reviewer?: string | null;
  note?: string | null;
}

export interface ImageWorkbenchBatchResponse {
  updated: number;
  items: { case_id: number; filename: string }[];
  skipped: { case_id: number; filename: string; reason: string }[];
}

export interface ImageWorkbenchConfirmSuggestionsPayload {
  items: { case_id: number; filename: string }[];
  min_confidence?: number;
  reviewer?: string | null;
  note?: string | null;
  mark_usable?: boolean;
}

export interface ImageWorkbenchConfirmSuggestionsResponse {
  updated: number;
  min_confidence: number;
  items: { case_id: number; filename: string; manual_phase: string; manual_view: string }[];
  skipped: { case_id: number; filename: string; reason: string }[];
}

export interface ImageWorkbenchTransferPayload {
  items: { case_id: number; filename: string }[];
  target_case_id: number;
  mode?: "copy";
  inherit_manual?: boolean;
  inherit_review?: boolean;
  require_target_review?: boolean;
  reviewer?: string | null;
  note?: string | null;
}

export interface ImageWorkbenchTransferResponse {
  mode: "copy";
  target_case_id: number;
  copied: number;
  items: {
    source_case_id: number;
    source_filename: string;
    target_case_id: number;
    target_filename: string;
  }[];
  skipped: { case_id: number; filename: string; reason: string }[];
}

export type ManualRenderView = "front" | "oblique" | "side";

export interface ManualTransform {
  offset_x_pct: number;
  offset_y_pct: number;
  scale: number;
}

export type ManualRenderImageInput =
  | { kind: "existing"; filename: string }
  | { kind: "upload"; upload_name: string; data_url: string };

export interface FocusRegion {
  x: number;
  y: number;
  width: number;
  height: number;
  label?: string | null;
}

export interface ManualRenderSourcesPayload {
  before: ManualRenderImageInput;
  after: ManualRenderImageInput;
  view: ManualRenderView;
  before_transform?: ManualTransform | null;
}

export interface ManualRenderPreviewPayload {
  before: ManualRenderImageInput;
  after: ManualRenderImageInput;
  view: ManualRenderView;
  brand?: string;
  before_transform?: ManualTransform | null;
}

export interface ManualRenderPreviewResponse {
  case_id: number;
  preview_id: string;
  view: ManualRenderView;
  output_path: string;
  manifest_path: string | null;
  render_plan: Record<string, unknown>;
  warnings: string[];
}

export interface ManualRenderSourcesResponse {
  case_id: number;
  view: ManualRenderView;
  created_files: string[];
  manual_overrides: ImageOverride[];
  detail: CaseDetail;
}

export interface ImageTrashResponse {
  case_id: number;
  original_filename: string;
  trash_path: string;
  detail: CaseDetail;
}

export interface ImageRestoreResponse {
  case_id: number;
  trash_path: string;
  restored_filename: string;
  detail: CaseDetail;
}

export interface SimulateAfterPayload {
  after_image_path?: string | null;
  after_image?: ManualRenderImageInput | null;
  before_image_path?: string | null;
  before_image?: ManualRenderImageInput | null;
  focus_targets: string[];
  focus_regions: FocusRegion[];
  ai_generation_authorized: boolean;
  provider?: "ps_model_router";
  model_name?: string | null;
  note?: string | null;
}

export interface SimulateAfterResponse {
  simulation_job_id: number;
  case_id: number;
  status: "done" | "done_with_issues" | "failed" | "blocked" | string;
  focus_targets: string[];
  focus_regions: FocusRegion[];
  provider: string;
  model_name: string | null;
  input_refs: { role: string; path: string; case_relative_path?: string }[];
  output_refs: { kind: string; path: string; watermarked?: boolean }[];
  audit: Record<string, unknown>;
  error_message: string | null;
}

export interface PsImageModelOption {
  value: string;
  label: string;
  source: "primary" | "fallback" | "tuzi_builtin" | string;
  description: string | null;
  is_default: boolean;
}

export interface PsImageModelOptionsResponse {
  provider: "ps_model_router" | string;
  default_model: string | null;
  fallback_model: string | null;
  options: PsImageModelOption[];
}

export interface SimulationJob {
  id: number;
  group_id: number | null;
  case_id: number | null;
  status: string;
  focus_targets: string[];
  policy: Record<string, unknown>;
  model_plan: Record<string, unknown>;
  input_refs: { role: string; path: string; case_relative_path?: string }[];
  output_refs: { kind: string; path: string; watermarked?: boolean }[];
  available_files: {
    kind: string;
    label: string;
    filename: string;
    path?: string;
    watermarked?: boolean;
  }[];
  watermarked: boolean;
  audit: Record<string, unknown>;
  review_decision: {
    recommended_verdict?: "approved" | "needs_recheck" | "rejected" | string;
    label?: string;
    severity?: "ok" | "review" | "block" | string;
    policy_version?: number;
    policy_name?: string;
    can_approve?: boolean;
    blocking_reasons?: string[];
    warning_reasons?: string[];
    passing_reasons?: string[];
    metrics?: Record<string, number>;
    thresholds?: Record<string, number>;
  };
  error_message: string | null;
  review_status: "approved" | "needs_recheck" | "rejected" | null;
  reviewer: string | null;
  review_note: string | null;
  reviewed_at: string | null;
  can_publish: boolean;
  created_at: string;
  updated_at: string;
}

export type SimulationQualityQueueStatus =
  | "review_required"
  | "all"
  | "done"
  | "done_with_issues"
  | "failed"
  | "reviewed"
  | "approved"
  | "needs_recheck"
  | "rejected"
  | "publishable"
  | "not_publishable";

export interface SimulationQualityQueueItem {
  job: SimulationJob;
  case: {
    id: number;
    abs_path: string;
    customer_raw: string | null;
    customer_canonical: string | null;
  } | null;
  reviewable: boolean;
  issue_summary: string[];
  warning_summary: string[];
}

export interface SimulationQualityQueueResponse {
  items: SimulationQualityQueueItem[];
  total: number;
  counts: Record<string, number>;
  status: SimulationQualityQueueStatus | string;
  recommendation?: string | null;
  limit: number;
}

export interface AiReviewPolicy {
  version: number;
  name: string;
  description: string;
  thresholds: Record<string, number>;
  updated_at: string | null;
}

export interface QualityReport {
  generated_at: string;
  limit: number;
  code_version?: {
    repo: string;
    commit: string;
    dirty: boolean | null;
    dirty_file_count: number | null;
  };
	  policy: AiReviewPolicy;
	  delivery_baseline?: {
	    scope: string;
	    sample_size: number;
	    current_latest_case_count: number;
	    renderer: {
	      terminal_count: number;
	      generated_count: number;
	      failed_count: number;
	      blocked_guardrail_count: number;
	      blocked_is_guardrail: boolean;
	      failed_rate_excluding_blocked: number | null;
	      success_rate_excluding_blocked: number | null;
	    };
	    publishability: {
	      publishable_count: number;
	      publishable_rate: number | null;
	      final_board_visible_rate: number | null;
	      final_board_missing_count: number | null;
	    };
	    quality: {
	      done_count: number;
	      done_with_issues_count: number;
	      done_with_issues_rate: number | null;
	      actionable_warning_count: number;
	    };
	    classification: {
	      source_image_count: number;
	      classified_count: number;
	      needs_manual_count: number;
	      low_confidence_count: number;
	      completion_rate: number | null;
	    };
	    root_causes?: {
	      scope?: string;
	      by_category?: Record<string, number>;
	      top_causes?: NonNullable<QualityReport["root_causes"]>["top_causes"];
	    };
	  };
	  totals: {
    artifacts: number;
    reviewed: number;
    publishable: number;
    not_publishable: number;
    classification_completion_rate?: number;
    final_board_visible_rate?: number | null;
  };
  classification?: {
    case_count: number;
    source_image_count: number;
    classified_count: number;
    needs_manual_count: number;
    low_confidence_count: number;
    manual_override_count: number;
    reviewed_count: number;
    render_excluded_count: number;
    needs_repick_count: number;
    completion_rate: number;
  };
  root_causes?: {
    scope: string;
    by_category: Record<string, number>;
    top_causes: {
      code: string;
      label: string;
      category: string;
      severity: "block" | "review" | string;
      action: string;
      href: string;
      count: number;
      unit: string;
      job_impact_count?: number;
      case_ids: number[];
      job_ids: number[];
      examples: string[];
    }[];
  };
  render: {
    total: number;
    by_status: Record<string, number>;
    by_quality_status: Record<string, number>;
    by_review_verdict: Record<string, number>;
    publishable: number;
    not_publishable: number;
    reviewed: number;
    avg_quality_score: number | null;
    artifact_visibility?: {
      output_artifact_count: number;
      final_board_visible_count: number;
      final_board_missing_count: number;
      final_board_visible_rate: number | null;
      manifest_visible_count: number;
    };
    current_version_baseline?: {
      scope: string;
      sample_size: number;
      current_latest_case_count?: number;
      historical_archived_count?: number;
      by_status: Record<string, number>;
      by_quality_status?: Record<string, number>;
      blocked_as_guardrail: number;
      generated_total: number;
      renderer_failure_count: number;
      review_required_count?: number;
      actionable_warning_count?: number;
      renderer_success_rate_excluding_blocked: number | null;
      clean_done_rate: number | null;
      done_with_issues_rate?: number | null;
      publishable_rate: number | null;
      artifact_visibility: Record<string, unknown>;
    };
    recent: Record<string, unknown>[];
  };
  simulation: {
    total: number;
    by_status: Record<string, number>;
    by_review_status: Record<string, number>;
    by_system_recommendation: Record<string, number>;
    reviewed: number;
    pending: number;
    aligned_with_system: number;
    manual_override: number;
    publishable: number;
    not_publishable: number;
    avg_full_frame_change: number | null;
    avg_non_target_change: number | null;
    risk_reasons: Record<string, number>;
    recent: Record<string, unknown>[];
  };
}

export interface AiReviewPolicyPreviewDecision {
  recommended_verdict: string;
  label?: string | null;
  severity?: string | null;
  metrics?: Record<string, number>;
  blocking_reasons?: string[];
  warning_reasons?: string[];
  passing_reasons?: string[];
}

export interface AiReviewPolicyPreviewItem {
  id: number;
  case_id: number | null;
  customer_raw: string | null;
  status: string;
  review_status: string | null;
  can_publish: boolean;
  changed: boolean;
  current: AiReviewPolicyPreviewDecision;
  preview: AiReviewPolicyPreviewDecision;
}

export interface AiReviewPolicyPreview {
  generated_at: string;
  limit: number;
  current_policy: AiReviewPolicy;
  preview_policy: AiReviewPolicy;
  summary: {
    total: number;
    changed_count: number;
    review_conflict_count: number;
    manual_override_count: number;
    by_current: Record<string, number>;
    by_preview: Record<string, number>;
    changed_transitions: Record<string, number>;
  };
  items: AiReviewPolicyPreviewItem[];
}

export const updateImageOverride = (
  caseId: number,
  filename: string,
  payload: ImageOverridePayload,
) =>
  api
    .patch<ImageOverride>(
      `/api/cases/${caseId}/images/${encodeURIComponent(filename)}`,
      payload,
    )
    .then((r) => r.data);

export const reviewCaseImage = (
  caseId: number,
  filename: string,
  payload: ImageReviewPayload,
) =>
  api
    .post<ImageReviewResponse>(
      `/api/cases/${caseId}/image-review/${encodeURIComponent(filename)}`,
      payload,
    )
    .then((r) => r.data);

export const fetchImageWorkbenchQueue = (
  params: {
    status?: string;
    phase?: string;
    view?: string;
    body_part?: string;
    q?: string;
    case_id?: number;
    source_group_case_id?: number;
    limit?: number;
    offset?: number;
  } = {},
) => api.get<ImageWorkbenchQueueResponse>("/api/image-workbench/queue", { params }).then((r) => r.data);

export const fetchSupplementCandidates = (
  targetCaseId: number,
  params: { limit_per_gap?: number } = {},
) =>
  api
    .get<SupplementCandidatesResponse>("/api/image-workbench/supplement-candidates", {
      params: { target_case_id: targetCaseId, ...params },
    })
    .then((r) => r.data);

export const batchUpdateImageWorkbench = (payload: ImageWorkbenchBatchPayload) =>
  api.post<ImageWorkbenchBatchResponse>("/api/image-workbench/batch", payload).then((r) => r.data);

export const confirmImageWorkbenchSuggestions = (payload: ImageWorkbenchConfirmSuggestionsPayload) =>
  api.post<ImageWorkbenchConfirmSuggestionsResponse>("/api/image-workbench/confirm-suggestions", payload).then((r) => r.data);

export const transferImageWorkbenchImages = (payload: ImageWorkbenchTransferPayload) =>
  api.post<ImageWorkbenchTransferResponse>("/api/image-workbench/transfer", payload).then((r) => r.data);

export const prepareManualRenderSources = (
  caseId: number,
  payload: ManualRenderSourcesPayload,
) =>
  api
    .post<ManualRenderSourcesResponse>(
      `/api/cases/${caseId}/manual-render-sources`,
      payload,
    )
    .then((r) => r.data);

export const previewManualRender = (
  caseId: number,
  payload: ManualRenderPreviewPayload,
) =>
  api
    .post<ManualRenderPreviewResponse>(
      `/api/cases/${caseId}/manual-render-preview`,
      payload,
    )
    .then((r) => r.data);

export const manualRenderPreviewFileUrl = (caseId: number, previewId: string) =>
  `/api/cases/${caseId}/manual-render-preview/${encodeURIComponent(previewId)}/file`;

export const trashCaseImage = (caseId: number, filename: string) =>
  api
    .post<ImageTrashResponse>(`/api/cases/${caseId}/images/trash`, { filename })
    .then((r) => r.data);

export const restoreCaseImage = (
  caseId: number,
  payload: { trash_path: string; restore_to?: string | null },
) =>
  api
    .post<ImageRestoreResponse>(`/api/cases/${caseId}/images/restore`, payload)
    .then((r) => r.data);

export const simulateCaseAfter = (caseId: number, payload: SimulateAfterPayload) =>
  api
    .post<SimulateAfterResponse>(`/api/cases/${caseId}/simulate-after`, payload)
    .then((r) => r.data);

export const fetchPsImageModelOptions = () =>
  api
    .get<PsImageModelOptionsResponse>("/api/cases/ps-image-model-options")
    .then((r) => r.data);

export const fetchCaseSimulationJobs = (caseId: number, limit = 10) =>
  api
    .get<SimulationJob[]>(`/api/cases/${caseId}/simulation-jobs`, { params: { limit } })
    .then((r) => r.data);

export const reviewSimulationJob = (
  caseId: number,
  jobId: number,
  payload: { verdict: "approved" | "needs_recheck" | "rejected"; reviewer: string; note?: string | null },
) =>
  api
    .post<SimulationJob>(`/api/cases/${caseId}/simulation-jobs/${jobId}/review`, payload)
    .then((r) => r.data);

export const fetchSimulationQualityQueue = (
  params: { status?: SimulationQualityQueueStatus; recommendation?: string | null; limit?: number } = {},
) =>
  api
    .get<SimulationQualityQueueResponse>("/api/cases/simulation-jobs/quality-queue", { params })
    .then((r) => r.data);

export const fetchAiReviewPolicy = () =>
  api
    .get<AiReviewPolicy>("/api/cases/simulation-jobs/review-policy")
    .then((r) => r.data);

export const updateAiReviewPolicy = (payload: Partial<AiReviewPolicy>) =>
  api
    .put<AiReviewPolicy>("/api/cases/simulation-jobs/review-policy", payload)
    .then((r) => r.data);

export const previewAiReviewPolicy = (
  payload: Partial<AiReviewPolicy>,
  params: { limit?: number } = {},
) =>
  api
    .post<AiReviewPolicyPreview>("/api/cases/simulation-jobs/review-policy/preview", payload, { params })
    .then((r) => r.data);

export const fetchQualityReport = (params: { limit?: number } = {}) =>
  api
    .get<QualityReport>("/api/cases/quality-report", { params })
    .then((r) => r.data);

export const reviewSimulationJobById = (
  jobId: number,
  payload: { verdict: "approved" | "needs_recheck" | "rejected"; reviewer: string; note?: string | null },
) =>
  api
    .post<SimulationJob>(`/api/cases/simulation-jobs/${jobId}/review`, payload)
    .then((r) => r.data);

export const simulationJobDirectFileUrl = (
  jobId: number,
  kind: string = "ai_after_simulation",
) =>
  `/api/cases/simulation-jobs/${jobId}/file?kind=${encodeURIComponent(kind)}`;

export const simulationJobFileUrl = (
  caseId: number,
  jobId: number,
  kind: string = "ai_after_simulation",
) =>
  `/api/cases/${caseId}/simulation-jobs/${jobId}/file?kind=${encodeURIComponent(kind)}`;

export interface RenameSuggestion {
  command: string | null;
  note: string;
  dry_run: boolean;
  affected_count: number;
  affected_files: string[];
}

export const fetchRenameSuggestion = (id: number) =>
  api.get<RenameSuggestion>(`/api/cases/${id}/rename-suggestion`).then((r) => r.data);

export const rescanCase = (id: number) =>
  api.post<unknown>(`/api/cases/${id}/rescan`).then((r) => r.data);

export const upgradeCase = (id: number, brand: string = "fumei") =>
  api.post<unknown>(`/api/cases/${id}/upgrade`, null, { params: { brand } }).then((r) => r.data);

// ---------- Case grouping / diagnosis ----------

export type CaseGroupStatus = "auto" | "needs_review" | "confirmed";

export interface CaseGroupSummary {
  id: number;
  group_key: string;
  primary_case_id: number | null;
  customer_raw: string | null;
  title: string;
  root_path: string;
  case_ids: number[];
  status: CaseGroupStatus | string;
  diagnosis: {
    image_count?: number;
    low_confidence_count?: number;
    blocking_pair_count?: number;
    suggested_template?: string;
    needs_review?: boolean;
    phase_counts?: Record<string, number>;
    view_counts?: Record<string, number>;
    model_policy?: Record<string, string>;
  };
  category: string | null;
  template_tier: string | null;
  created_at: string;
  updated_at: string;
}

export interface ImageObservation {
  id: number;
  case_id: number | null;
  image_path: string;
  phase: string;
  body_part: string;
  view: string;
  quality: Record<string, unknown>;
  confidence: number;
  source: string;
  reasons: string[];
  updated_at: string;
}

export interface PairCandidate {
  id: number;
  slot: string;
  before_image_path: string | null;
  after_image_path: string | null;
  score: number;
  metrics: Record<string, unknown>;
  status: string;
  template_hint: string | null;
  updated_at: string;
}

export interface CaseGroupDiagnosis {
  group: CaseGroupSummary;
  cases: { id: number; abs_path: string; category: string; template_tier: string | null }[];
  image_observations: ImageObservation[];
  pair_candidates: PairCandidate[];
}

export const rescanCaseGroups = () =>
  api.post<{
    group_count: number;
    image_observation_count: number;
    pair_candidate_count: number;
    low_confidence_group_count: number;
  }>("/api/cases/rescan-groups").then((r) => r.data);

export const fetchCaseGroups = (params: { status?: string; limit?: number } = {}) =>
  api.get<{ items: CaseGroupSummary[]; total: number }>("/api/case-groups", { params }).then((r) => r.data);

export const fetchCaseGroupDiagnosis = (id: number) =>
  api.get<CaseGroupDiagnosis>(`/api/case-groups/${id}/diagnosis`).then((r) => r.data);

export const confirmCaseGroupClassification = (
  id: number,
  payload: { status?: string; category?: string | null; template_tier?: string | null; note?: string | null },
) =>
  api.post<CaseGroupDiagnosis>(`/api/case-groups/${id}/confirm-classification`, payload).then((r) => r.data);

export const renderCaseGroup = (id: number, payload: EnqueueRenderPayload = {}) =>
  api.post<{ job_id: number; case_id: number; group_id: number }>(`/api/case-groups/${id}/render`, payload).then((r) => r.data);

export const simulateCaseGroupAfter = (
  id: number,
  payload: {
    focus_targets: string[];
    ai_generation_authorized: boolean;
    provider?: string | null;
    model_name?: string | null;
    note?: string | null;
  },
) =>
  api.post<{
    simulation_job_id: number;
    group_id: number;
    case_id: number | null;
    status: string;
    focus_targets: string[];
    policy: Record<string, unknown>;
    error_message: string | null;
  }>(`/api/case-groups/${id}/simulate-after`, payload).then((r) => r.data);

// ---------- Phase 3: render queue ----------

export type RenderStatus = "queued" | "running" | "done" | "done_with_issues" | "blocked" | "failed" | "cancelled" | "undone";
export type RenderQualityStatus = "done" | "done_with_issues" | "blocked";

export type Brand = "fumei" | "shimei";

export const BRAND_LABEL: Record<Brand, string> = {
  fumei: "芙美和颜",
  shimei: "莳美",
};

export interface RenderJob {
  id: number;
  case_id: number;
  brand: string;
  template: string;
  status: RenderStatus;
  batch_id: string | null;
  enqueued_at: string;
  started_at: string | null;
  finished_at: string | null;
  output_path: string | null;
  manifest_path: string | null;
  error_message: string | null;
  semantic_judge: string;
	  meta: {
    status?: string;
    blocking_issue_count?: number;
    warning_count?: number;
    case_mode?: string;
    effective_templates?: string[];
    ai_usage?: Record<string, unknown>;
	    composition_alerts?: CompositionAlert[];
	    run_id?: string | null;
	    code_version?: Record<string, unknown>;
	    source_manifest_hash?: string | null;
	    quality_summary?: Record<string, unknown>;
	    render_selection_audit?: Record<string, unknown>;
	    render_selection_dropped_slots?: unknown[];
	    render_selection_source_provenance?: unknown[];
	  };
  quality: {
    id: number;
    render_job_id: number;
    quality_status: RenderQualityStatus | string;
    quality_score: number;
    can_publish: boolean;
    artifact_mode: string;
    manifest_status: string | null;
    blocking_count: number;
    warning_count: number;
    metrics: Record<string, unknown>;
    review_verdict: string | null;
    reviewer: string | null;
    review_note: string | null;
    reviewed_at: string | null;
    created_at: string;
    updated_at: string;
  } | null;
  /** Unix timestamp (seconds) of final-board.jpg mtime. Only populated by
   * GET /api/cases/{id}/render/latest when status==='done'. Used as a
   * cache-buster so restore_render forces the <img> to refetch. */
  output_mtime?: number | null;
  /** Stage A: passthrough of manifest.final.json blocking_issues/warnings
   * string lists (read on-demand by /api/render/jobs/:id and /render/latest).
   * Empty when manifest is missing or job is queued/running. */
	  blocking_issues?: string[];
	  warnings?: string[];
	  delivery_audit?: {
	    run_id?: string | null;
	    code_version?: Record<string, unknown>;
	    source_manifest_hash?: string | null;
	    selected_slots: string[];
	    dropped_slots: unknown[];
	    source_provenance: unknown[];
	    quality_summary: {
	      quality_status?: string | null;
	      quality_score?: number | null;
	      can_publish?: boolean;
	      actionable_warning_count?: number | null;
	    };
	  };
	}

export type RenderQualityQueueStatus =
  | "review_required"
  | "all"
  | "done"
  | "done_with_issues"
  | "blocked"
  | "failed"
  | "reviewed"
  | "publishable"
  | "not_publishable";

export interface RenderQualityQueueItem {
  job: RenderJob;
  case: {
    id: number;
    abs_path: string;
    customer_raw: string | null;
    customer_canonical: string | null;
  };
  reviewable: boolean;
  issue_summary: string[];
  warning_summary: string[];
  action_summary?: { code: string; label: string; source?: string }[];
}

export interface RenderQualityQueueResponse {
  items: RenderQualityQueueItem[];
  total: number;
  counts: Record<string, number>;
  archive?: {
    scope: string;
    hidden_by_current_latest: number;
    by_status: Record<string, number>;
    by_quality_status: Record<string, number>;
  };
  status: RenderQualityQueueStatus | string;
  limit: number;
}

export interface CompositionAlert {
  slot?: string;
  slot_label?: string;
  code?: string;
  severity?: string;
  message?: string;
  recommended_action?: string;
  metrics?: Record<string, unknown>;
}

export interface RenderBatch {
  batch_id: string;
  total: number;
  counts: Partial<Record<RenderStatus, number>>;
  jobs: RenderJob[];
}

export interface EnqueueRenderPayload {
  brand?: string;
  template?: string;
  semantic_judge?: "off" | "auto";
}

export const enqueueRender = (id: number, payload: EnqueueRenderPayload = {}) =>
  api
    .post<{ job_id: number; batch_id: string | null }>(`/api/cases/${id}/render`, payload)
    .then((r) => r.data);

export const enqueueBatchRender = (
  case_ids: number[],
  payload: EnqueueRenderPayload = {}
) =>
  api
    .post<{ batch_id: string; job_ids: number[]; skipped_count: number; invalid?: RenderBatchPreviewInvalid[] }>(
      "/api/cases/render/batch",
      { case_ids, ...payload }
    )
    .then((r) => r.data);

export type BatchPreviewInvalidReason =
  | "case_not_found"
  | "duplicate_in_batch"
  | "missing_source_files"
  | "no_real_source_photos"
  | "insufficient_source_photos"
  | "missing_before_after_pair";

export interface CaseSourceProfile {
  source_kind: string;
  raw_image_count: number;
  source_count: number;
  generated_artifact_count: number;
  before_count: number;
  after_count: number;
  unlabeled_source_count: number;
  manual_not_source?: boolean;
  raw_meta_image_count?: number;
  missing_source_count?: number;
  missing_source_samples?: string[];
  file_integrity_status?: string;
  source_samples?: string[];
  generated_artifact_samples?: string[];
}

export type SourceBlockerReason =
  | "missing_source_files"
  | "no_real_source_photos"
  | "insufficient_source_photos"
  | "missing_before_after_pair";

export interface SourceBlockerItem {
  case_id: number;
  case_title: string;
  abs_path: string;
  customer_raw: string | null;
  customer_id: number | null;
  reason: SourceBlockerReason;
  reason_label: string;
  recommended_action: string;
  source_profile: CaseSourceProfile;
  effective_source_profile?: CaseSourceProfile;
  bound_case_ids?: number[];
  marked_not_source: boolean;
  tags: string[];
  notes: string | null;
  latest_render_status: RenderStatus | null;
  latest_render_quality_status: RenderQualityStatus | null;
  case_url: string;
}

export interface SourceBlockersResponse {
  items: SourceBlockerItem[];
  total: number;
  counts: Record<string, number>;
  reason: "all" | SourceBlockerReason;
  limit: number;
}

export interface SourceBlockerActionPayload {
  action: "mark_not_source" | "clear_not_source";
  reviewer?: string | null;
  note?: string | null;
}

export const fetchSourceBlockers = (params: { reason?: "all" | SourceBlockerReason; limit?: number } = {}) =>
  api.get<SourceBlockersResponse>("/api/cases/source-blockers", { params }).then((r) => r.data);

export const applySourceBlockerAction = (caseId: number, payload: SourceBlockerActionPayload) =>
  api
    .post<{ case_id: number; action: string; marked_not_source: boolean; tags: string[]; manual_blocking_codes: string[] }>(
      `/api/cases/source-blockers/${caseId}/action`,
      payload
    )
    .then((r) => r.data);

export interface SourceBindingCandidate {
  case_id: number;
  case_title: string;
  abs_path: string;
  customer_raw: string | null;
  score: number;
  match_reasons: string[];
  source_profile: CaseSourceProfile;
  merged_source_profile: CaseSourceProfile;
  can_complete_pair: boolean;
  already_bound: boolean;
  case_url: string;
  projected_preflight?: SourceBindingPreflightPreview;
}

export interface SourceBindingPreflightPreview {
  status: "ready" | "review" | "blocked" | string;
  readiness_score?: number;
  needs_manual_count?: number;
  missing_source_count?: number;
  selected_count?: number;
  missing_slots: { view: string; label: string; missing: string[] }[];
  slots: { view: string; label: string; before_count: number; after_count: number; ready: boolean }[];
  hard_blockers: { code: string; severity?: string; message?: string; recommended_action?: string }[];
}

export interface SourceBindingCandidatesResponse {
  case_id: number;
  source_profile: CaseSourceProfile;
  effective_source_profile: CaseSourceProfile;
  bound_case_ids: number[];
  candidates: SourceBindingCandidate[];
}

export const fetchSourceBindingCandidates = (caseId: number, params: { limit?: number } = {}) =>
  api
    .get<SourceBindingCandidatesResponse>(`/api/cases/${caseId}/source-binding-candidates`, { params })
    .then((r) => r.data);

export interface SourceGroupImage {
  case_id: number;
  filename: string;
  preview_url: string;
  phase: "before" | "after" | null;
  phase_source: string;
  view: "front" | "oblique" | "side" | null;
  view_source: string;
  manual: boolean;
  needs_manual: boolean;
  review_state: ImageReviewState | null;
  review_verdict: string | null;
  render_excluded: boolean;
  copied_requires_review: boolean;
  body_part: string;
  treatment_area: string | null;
  angle_confidence: number | null;
  rejection_reason: string | null;
  issues: string[];
}

export interface SourceGroupCandidate {
  case_id: number;
  case_title: string;
  source_role: "primary" | "bound" | string;
  filename: string;
  preview_url: string;
  phase: "before" | "after";
  phase_source: string | null;
  view: "front" | "oblique" | "side";
  view_source: string | null;
  manual: boolean;
  review_verdict: string | null;
  review_label: string | null;
  angle_confidence: number | null;
  rejection_reason: string | null;
  selection_score: number;
  selection_reasons: string[];
  quality_warnings: { code: string; severity: "ok" | "info" | "review" | "block" | string; message: string }[];
  risk_level: "ok" | "review" | "block" | string;
  source_group_lock?: SourceGroupSelectionLock | null;
}

export interface SourceGroupSelectionLock {
  locked: boolean;
  slot?: "front" | "oblique" | "side" | string;
  role?: "before" | "after" | string;
  reviewer?: string | null;
  reason?: string | null;
  updated_at?: string | null;
}

export interface SourceGroupPairQuality {
  score: number;
  label: "strong" | "review" | "risky" | string;
  severity: "ok" | "review" | "block" | string;
  reasons: string[];
  warnings: { code: string; severity: "ok" | "info" | "review" | "block" | string; message: string }[];
  metrics: Record<string, unknown>;
}

export interface SourceGroupSource {
  case_id: number;
  case_title: string;
  abs_path: string;
  customer_raw: string | null;
  customer_id: number | null;
  role: "primary" | "bound" | string;
  source_profile: CaseSourceProfile;
  raw_meta_image_count: number;
  missing_image_count: number;
  missing_image_samples: string[];
  image_count: number;
  images: SourceGroupImage[];
}

export interface SourceGroupSlot {
  view: "front" | "oblique" | "side";
  label: string;
  before_count: number;
  after_count: number;
  ready: boolean;
  source_case_ids: number[];
  before_candidates: SourceGroupCandidate[];
  after_candidates: SourceGroupCandidate[];
  selected_before: SourceGroupCandidate | null;
  selected_after: SourceGroupCandidate | null;
  pair_quality: SourceGroupPairQuality | null;
  selection_lock?: SourceGroupSelectionLock | null;
}

export interface SourceGroupSelectionControls {
  locked_slots: Record<string, {
    before: { case_id: number; filename: string };
    after: { case_id: number; filename: string };
    reviewer?: string | null;
    reason?: string | null;
    updated_at?: string | null;
  }>;
  accepted_warnings: {
    job_id?: number | null;
    slot: string;
    code: string;
    message_contains?: string | null;
    reviewer?: string | null;
    note?: string | null;
    accepted_at?: string | null;
  }[];
}

export interface SourceGroupResponse {
  case_id: number;
  source_profile: CaseSourceProfile;
  effective_source_profile: CaseSourceProfile;
  bound_case_ids: number[];
  missing_bound_case_ids: number[];
  binding: { case_ids?: number[]; reviewer?: string | null; note?: string | null; updated_at?: string | null } | null;
  source_count: number;
  image_count: number;
  missing_image_count: number;
  sources: SourceGroupSource[];
  preflight: {
    status: "ready" | "review" | "blocked" | string;
    readiness_score?: number;
    hard_blockers?: {
      code: string;
      severity: "block" | "review" | "info" | string;
      message: string;
      recommended_action: string;
      samples?: unknown[];
      slots?: unknown[];
    }[];
	    formal_candidate_manifest?: {
	      version: number;
	      policy: string;
	      required_slots: string[];
	      readiness_score?: number;
	      selected_count: number;
	      renderable_slot_count?: number;
	      effective_template_hint?: string | null;
	      blocking_reasons?: {
	        code?: string | null;
	        view?: string | null;
	        message?: string | null;
	        recommended_action?: string | null;
	      }[];
	      slots: Record<string, {
	        label?: string;
	        ready?: boolean;
	        before?: unknown;
	        after?: unknown;
	        pair_quality?: Record<string, unknown> | null;
	        quality_prediction?: {
	          slot: string;
	          decision: "render" | "drop" | "block" | string;
	          blocks_render: boolean;
	          pair_score?: number | null;
	          pair_label?: string | null;
	          pose_delta?: Record<string, unknown> | null;
	          angle_confidence?: { before?: number | null; after?: number | null };
	          warning_codes?: string[];
	          drop_reason?: unknown;
	          recommended_action?: string;
	        };
	        selection_lock?: unknown;
	        candidate_counts?: { before?: number; after?: number };
	      }>;
	      source_provenance: unknown[];
	    };
    selection_controls?: SourceGroupSelectionControls;
    accepted_warnings?: SourceGroupSelectionControls["accepted_warnings"];
    slots: SourceGroupSlot[];
    missing_slots: { view: string; label: string; missing: string[] }[];
    needs_manual_count: number;
    needs_manual_samples: { case_id: number; filename: string; missing: string[] }[];
    render_excluded_count: number;
    render_excluded_samples: { case_id: number; filename: string }[];
    missing_source_count: number;
    missing_source_samples: string[];
  };
  audit: {
    bound_source_case_ids: number[];
    binding_reviewer: string | null;
    binding_updated_at: string | null;
    binding_note: string | null;
    source_group_selection?: SourceGroupSelectionControls;
  };
}

export const fetchCaseSourceGroup = (caseId: number) =>
  api.get<SourceGroupResponse>(`/api/cases/${caseId}/source-group`).then((r) => r.data);

export const bindSourceDirectories = (
  caseId: number,
  payload: { source_case_ids: number[]; reviewer?: string | null; note?: string | null },
) =>
  api
    .post<{ case_id: number; bound_case_ids: number[]; effective_source_profile: CaseSourceProfile }>(
      `/api/cases/${caseId}/source-bindings`,
      payload,
    )
    .then((r) => r.data);

export const clearSourceDirectoryBindings = (caseId: number) =>
  api.delete<{ case_id: number; bound_case_ids: number[] }>(`/api/cases/${caseId}/source-bindings`).then((r) => r.data);

export const lockSourceGroupSlot = (
  caseId: number,
  payload: {
    view: "front" | "oblique" | "side" | string;
    before: { case_id: number; filename: string };
    after: { case_id: number; filename: string };
    reviewer?: string | null;
    reason?: string | null;
  },
) => api.post<SourceGroupResponse>(`/api/cases/${caseId}/source-group/slot-locks`, payload).then((r) => r.data);

export const clearSourceGroupSlotLock = (caseId: number, view: "front" | "oblique" | "side" | string) =>
  api.delete<SourceGroupResponse>(`/api/cases/${caseId}/source-group/slot-locks/${view}`).then((r) => r.data);

export const acceptSourceGroupWarning = (
  caseId: number,
  payload: {
    slot: "front" | "oblique" | "side" | string;
    code: string;
    job_id?: number | null;
    message_contains?: string | null;
    reviewer?: string | null;
    note?: string | null;
  },
) => api.post<SourceGroupResponse>(`/api/cases/${caseId}/source-group/accepted-warnings`, payload).then((r) => r.data);

export interface RenderBatchPreviewInvalid {
  case_id: number;
  reason: BatchPreviewInvalidReason;
  source_profile?: CaseSourceProfile;
}

export interface RenderBatchPreview {
  valid_count: number;
  invalid_count: number;
  valid_case_ids: number[];
  invalid: RenderBatchPreviewInvalid[];
  brand: string;
  template: string;
  semantic_judge: string;
}

export const previewBatchRender = (
  case_ids: number[],
  payload: EnqueueRenderPayload = {}
) =>
  api
    .post<RenderBatchPreview>(
      "/api/cases/render/batch/preview",
      { case_ids, ...payload }
    )
    .then((r) => r.data);

export const fetchCaseRenderJobs = (id: number, limit = 20) =>
  api
    .get<RenderJob[]>(`/api/cases/${id}/render/jobs`, { params: { limit } })
    .then((r) => r.data);

export const fetchLatestCaseRenderJob = (id: number) =>
  api
    .get<{ job: RenderJob | null }>(`/api/cases/${id}/render/latest`)
    .then((r) => r.data.job);

export interface RenderHistorySnapshot {
  filename: string;
  archived_at: string;
  size_bytes: number;
}

export interface RenderHistoryResponse {
  case_id: number;
  brand: string;
  template: string;
  snapshots: RenderHistorySnapshot[];
}

export const fetchRenderHistory = (
  id: number,
  brand: string = "fumei",
  template: string = "tri-compare"
) =>
  api
    .get<RenderHistoryResponse>(`/api/cases/${id}/render/history`, {
      params: { brand, template },
    })
    .then((r) => r.data);

/**
 * Build the URL for one archived render snapshot under .history/.
 * Mirrors renderOutputUrl but targets the .history/<filename> path.
 */
export const renderHistorySnapshotUrl = (
  caseId: number,
  brand: string,
  template: string,
  filename: string
) =>
  `/api/cases/${caseId}/files?name=${encodeURIComponent(
    `.case-layout-output/${brand}/${template}/render/.history/${filename}`
  )}`;

export interface RenderRestoreResponse {
  case_id: number;
  brand: string;
  template: string;
  restored_from: string;
  /** Timestamp of the just-archived previous final-board.jpg, or null if there
   * was no current final to archive (e.g. case never rendered before restore). */
  previous_archived_at: string | null;
  revision_id: number;
  output_path: string;
}

export const restoreRenderSnapshot = (
  caseId: number,
  brand: string,
  template: string,
  archivedAt: string
) =>
  api
    .post<RenderRestoreResponse>(`/api/cases/${caseId}/render/restore`, {
      brand,
      template,
      archived_at: archivedAt,
    })
    .then((r) => r.data);

export const fetchRenderJob = (jobId: number) =>
  api.get<RenderJob>(`/api/render/jobs/${jobId}`).then((r) => r.data);

export const fetchRenderQualityQueue = (
  params: { status?: RenderQualityQueueStatus; limit?: number } = {},
) =>
  api
    .get<RenderQualityQueueResponse>("/api/render/quality-queue", { params })
    .then((r) => r.data);

export const fetchRenderBatch = (batchId: string) =>
  api.get<RenderBatch>(`/api/render/batches/${batchId}`).then((r) => r.data);

export const cancelRenderJob = (jobId: number) =>
  api.post<{ cancelled: boolean }>(`/api/render/jobs/${jobId}/cancel`).then((r) => r.data);

export const undoCaseRender = (id: number) =>
  api
    .post<{ undone: boolean; output_path: string | null; revision_id: number; removed_file: boolean }>(
      `/api/cases/${id}/render/undo`
    )
    .then((r) => r.data);

export const reviewRenderQuality = (
  jobId: number,
  payload: { verdict: "approved" | "needs_recheck" | "rejected"; reviewer: string; note?: string | null; can_publish?: boolean },
) =>
  api.post<NonNullable<RenderJob["quality"]>>(`/api/render-jobs/${jobId}/quality-review`, payload).then((r) => r.data);

export const undoCase = (id: number) =>
  api
    .post<{ undone: boolean; case_id: number; restored: Record<string, unknown> }>(
      `/api/cases/${id}/undo`
    )
    .then((r) => r.data);

/** A single audit-log row. `before`/`after` are tracked-column snapshots,
 * except for op='render' / 'undo_render' where they only carry artifact path
 * metadata (these go through render-undo, not the generic apply_undo path). */
export interface CaseRevision {
  id: number;
  case_id: number;
  changed_at: string;
  actor: string;
  op: "patch" | "batch" | "rescan" | "merge_customer" | "rename" | "upgrade" | "render" | "undo" | "undo_render" | "evaluate" | "undo_evaluate" | "restore_render";
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  source_route: string | null;
  undone_at: string | null;
}

export const fetchCaseRevisions = (id: number, limit = 20) =>
  api
    .get<{ revisions: CaseRevision[] }>(`/api/cases/${id}/revisions`, { params: { limit } })
    .then((r) => r.data.revisions);

/**
 * Build the URL for a rendered final-board.jpg via the existing /files endpoint.
 * The file lives under <case_dir>/.case-layout-output/<brand>/<template>/render/final-board.jpg
 * and case_file enforces the path stays within the case dir, so this works.
 */
export const renderOutputUrl = (
  caseId: number,
  brand: string,
  template: string = "tri-compare",
  cacheBuster?: number | null
) => {
  const base = caseFileUrl(caseId, `.case-layout-output/${brand}/${template}/render/final-board.jpg`);
  return withCacheBuster(base, cacheBuster);
};

const normalizeLocalPath = (value: string) => value.replace(/\\/g, "/").replace(/\/+$/, "");

const outputPathRelativeToCase = (outputPath?: string | null, caseAbsPath?: string | null) => {
  if (!outputPath || !caseAbsPath) return null;
  const output = normalizeLocalPath(outputPath);
  const base = normalizeLocalPath(caseAbsPath);
  const prefix = `${base}/`;
  if (!output.startsWith(prefix)) return null;
  const relative = output.slice(prefix.length);
  return relative && !relative.startsWith("../") ? relative : null;
};

export const renderJobOutputUrl = (
  caseId: number,
  job: Pick<RenderJob, "id" | "brand" | "template" | "output_path" | "output_mtime">,
  caseAbsPath?: string | null,
) => {
  const outputRelativePath = outputPathRelativeToCase(job.output_path, caseAbsPath);
  if (outputRelativePath) {
    return withCacheBuster(caseFileUrl(caseId, outputRelativePath), job.output_mtime);
  }
  if (job.output_path) {
    return withCacheBuster(`/api/render/jobs/${job.id}/file?kind=output`, job.output_mtime);
  }
  return renderOutputUrl(caseId, job.brand, job.template, job.output_mtime);
};

export const fetchCustomers = (q?: string) =>
  api.get<CustomerSummary[]>("/api/customers", { params: q ? { q } : {} }).then((r) => r.data);

export const fetchCustomerDetail = (id: number) =>
  api.get<CustomerDetail>(`/api/customers/${id}`).then((r) => r.data);

export const resolveCandidates = (raw: string) =>
  api
    .get<CandidateResult>("/api/customers/candidates", { params: { raw } })
    .then((r) => r.data);

export const createCustomer = (payload: {
  canonical_name: string;
  aliases?: string[];
  notes?: string;
}) => api.post<CustomerSummary>("/api/customers", payload).then((r) => r.data);

export const updateCustomer = (
  id: number,
  payload: { canonical_name?: string; aliases?: string[]; notes?: string }
) => api.patch<CustomerSummary>(`/api/customers/${id}`, payload).then((r) => r.data);

export const mergeCases = (customerId: number, caseIds: number[]) =>
  api
    .post<{ customer_id: number; moved: number }>(
      `/api/customers/${customerId}/merge`,
      { case_ids: caseIds }
    )
    .then((r) => r.data);

export const fetchIssueDict = () =>
  api.get<BlockingIssue[]>("/api/issues/dict").then((r) => r.data);

// ---------- Stage 2: v3 upgrade queue ----------

export type UpgradeStatus = "queued" | "running" | "done" | "failed" | "cancelled" | "undone";

export interface UpgradeJob {
  id: number;
  case_id: number;
  brand: string;
  status: UpgradeStatus;
  batch_id: string | null;
  enqueued_at: string;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
  meta: {
    category?: string;
    template_tier?: string | null;
    blocking_count?: number;
    warning_count?: number;
    skill_status?: string;
    case_mode?: string;
    skill_template?: string;
  };
}

export interface UpgradeBatch {
  batch_id: string;
  total: number;
  counts: Partial<Record<UpgradeStatus, number>>;
  jobs: UpgradeJob[];
}

export interface UpgradeBatchUndoResult {
  batch_id: string;
  undone: number[];
  skipped: { case_id: number; reason: string }[];
  errors: { case_id: number; message: string }[];
}

export const enqueueBatchUpgrade = (case_ids: number[], brand: string = "fumei") =>
  api
    .post<{ batch_id: string; job_ids: number[]; skipped_count: number }>(
      "/api/cases/upgrade/batch",
      { case_ids, brand }
    )
    .then((r) => r.data);

export const fetchUpgradeBatch = (batchId: string) =>
  api.get<UpgradeBatch>(`/api/jobs/upgrade/batches/${batchId}`).then((r) => r.data);

export const fetchUpgradeJob = (jobId: number) =>
  api.get<UpgradeJob>(`/api/jobs/upgrade/${jobId}`).then((r) => r.data);

export const cancelUpgradeJob = (jobId: number) =>
  api
    .post<{ cancelled: boolean }>(`/api/jobs/upgrade/${jobId}/cancel`)
    .then((r) => r.data);

export const retryUpgradeJob = (jobId: number) =>
  api
    .post<{ retried: boolean; old_job_id: number; new_job_id: number }>(
      `/api/jobs/upgrade/${jobId}/retry`
    )
    .then((r) => r.data);

export const undoUpgradeBatch = (batchId: string) =>
  api
    .post<UpgradeBatchUndoResult>(`/api/jobs/upgrade/batches/${batchId}/undo`)
    .then((r) => r.data);

// ---------- 阶段 3: 评估台 ----------

export type EvaluationVerdict = "approved" | "needs_recheck" | "rejected";
export type EvaluationSubjectKind = "case" | "render";

export const VERDICT_LABEL: Record<EvaluationVerdict, string> = {
  approved: "通过",
  needs_recheck: "需重审",
  rejected: "打回",
};

export interface Evaluation {
  id: number;
  subject_kind: EvaluationSubjectKind;
  subject_id: number;
  verdict: EvaluationVerdict;
  reviewer: string;
  note: string | null;
  source_route: string | null;
  created_at: string;
  undone_at: string | null;
}

/** Pending list item — has subject metadata to render rows without extra fetches. */
export interface PendingCaseEvaluationItem {
  subject_kind: "case";
  subject_id: number;
  case_id: number;
  abs_path: string;
  customer_raw: string | null;
  customer_name: string | null;
  category: Category;
  template_tier: string | null;
  blocking_issues_json: string | null;
  review_status: ReviewStatus | null;
  indexed_at: string;
}

export interface PendingRenderEvaluationItem {
  subject_kind: "render";
  subject_id: number;
  case_id: number;
  brand: string;
  template: string;
  output_path: string | null;
  manifest_path: string | null;
  finished_at: string | null;
  meta_json: string | null;
  abs_path: string;
  customer_raw: string | null;
  customer_name: string | null;
}

export type PendingEvaluationItem =
  | PendingCaseEvaluationItem
  | PendingRenderEvaluationItem;

export interface PendingEvaluationsResponse<T extends PendingEvaluationItem = PendingEvaluationItem> {
  subject_kind: EvaluationSubjectKind;
  total: number;
  items: T[];
}

export interface RecentCaseEvaluation extends Evaluation {
  case_id: number;
  abs_path: string;
  customer_raw: string | null;
  customer_name: string | null;
  category: Category;
  template_tier: string | null;
}

export interface RecentRenderEvaluation extends Evaluation {
  case_id: number;
  brand: string;
  template: string;
  output_path: string | null;
  finished_at: string | null;
  abs_path: string;
  customer_raw: string | null;
  customer_name: string | null;
}

export interface RecentEvaluationsResponse<T extends Evaluation = Evaluation> {
  subject_kind: EvaluationSubjectKind;
  items: T[];
}

export interface CreateEvaluationPayload {
  subject_kind: EvaluationSubjectKind;
  subject_id: number;
  verdict: EvaluationVerdict;
  reviewer: string;
  note?: string | null;
}

export const createEvaluation = (payload: CreateEvaluationPayload) =>
  api.post<Evaluation>("/api/evaluations", payload).then((r) => r.data);

export const fetchEvaluationsBySubject = (
  subject_kind: EvaluationSubjectKind,
  subject_id: number,
  limit = 50
) =>
  api
    .get<Evaluation[]>("/api/evaluations", {
      params: { subject_kind, subject_id, limit },
    })
    .then((r) => r.data);

export const fetchPendingCaseEvaluations = (limit = 50) =>
  api
    .get<PendingEvaluationsResponse<PendingCaseEvaluationItem>>(
      "/api/evaluations/pending",
      { params: { subject_kind: "case", limit } }
    )
    .then((r) => r.data);

export const fetchPendingRenderEvaluations = (
  brand?: string,
  limit = 50
) =>
  api
    .get<PendingEvaluationsResponse<PendingRenderEvaluationItem>>(
      "/api/evaluations/pending",
      { params: { subject_kind: "render", brand, limit } }
    )
    .then((r) => r.data);

export const fetchRecentCaseEvaluations = (limit = 20) =>
  api
    .get<RecentEvaluationsResponse<RecentCaseEvaluation>>(
      "/api/evaluations/recent",
      { params: { subject_kind: "case", limit } }
    )
    .then((r) => r.data);

export const fetchRecentRenderEvaluations = (brand?: string, limit = 20) =>
  api
    .get<RecentEvaluationsResponse<RecentRenderEvaluation>>(
      "/api/evaluations/recent",
      { params: { subject_kind: "render", brand, limit } }
    )
    .then((r) => r.data);

export const undoEvaluation = (id: number) =>
  api.post<Evaluation>(`/api/evaluations/${id}/undo`).then((r) => r.data);
