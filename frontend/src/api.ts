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

export const caseFileUrl = (id: number, name: string) =>
  `/api/cases/${id}/files?name=${encodeURIComponent(name)}`;

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

// ---------- Phase 3: render queue ----------

export type RenderStatus = "queued" | "running" | "done" | "failed" | "cancelled" | "undone";

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
  };
  /** Unix timestamp (seconds) of final-board.jpg mtime. Only populated by
   * GET /api/cases/{id}/render/latest when status==='done'. Used as a
   * cache-buster so restore_render forces the <img> to refetch. */
  output_mtime?: number | null;
  /** Stage A: passthrough of manifest.final.json blocking_issues/warnings
   * string lists (read on-demand by /api/render/jobs/:id and /render/latest).
   * Empty when manifest is missing or job is queued/running. */
  blocking_issues?: string[];
  warnings?: string[];
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
    .post<{ batch_id: string; job_ids: number[]; skipped_count: number }>(
      "/api/cases/render/batch",
      { case_ids, ...payload }
    )
    .then((r) => r.data);

export type BatchPreviewInvalidReason = "case_not_found" | "duplicate_in_batch";

export interface RenderBatchPreview {
  valid_count: number;
  invalid_count: number;
  valid_case_ids: number[];
  invalid: { case_id: number; reason: BatchPreviewInvalidReason }[];
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
  const base = `/api/cases/${caseId}/files?name=${encodeURIComponent(
    `.case-layout-output/${brand}/${template}/render/final-board.jpg`
  )}`;
  return cacheBuster != null ? `${base}&v=${Math.floor(cacheBuster)}` : base;
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
