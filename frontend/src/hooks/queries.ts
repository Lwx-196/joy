/**
 * Centralized React Query hooks for case-workbench.
 *
 * Design:
 * - Query keys are tuples: ["cases"], ["cases", id], ["customers"], etc.
 *   This makes invalidation precise: invalidate ["cases"] invalidates list + detail.
 * - staleTime is tuned per data class:
 *     • cases / customers list / customer detail: 30s — semi-static, user-edited
 *     • case detail: 15s — more volatile (review state changes)
 *     • stats / scan latest: 5s — visible on dashboard, want fresh
 *     • issue dict: Infinity — pure code constant, only changes on backend redeploy
 * - Mutations call invalidateQueries on the relevant keys after success.
 * - All exports keep the imperative api.ts functions usable for one-off calls
 *   (e.g., Dict.tsx form needs resolveCandidates per keystroke without caching).
 */
import { useEffect, useRef } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from "@tanstack/react-query";
import {
  batchUpdateCases,
  cancelRenderJob,
  cancelUpgradeJob,
  createCustomer,
  createEvaluation,
  enqueueBatchRender,
  enqueueBatchUpgrade,
  enqueueRender,
  previewBatchRender,
  fetchCaseDetail,
  fetchCaseRenderJobs,
  fetchCaseRevisions,
  fetchRenderHistory,
  restoreRenderSnapshot,
  fetchCases,
  fetchCustomerDetail,
  fetchCustomers,
  fetchEvaluationsBySubject,
  fetchIssueDict,
  fetchLatestCaseRenderJob,
  fetchPendingCaseEvaluations,
  fetchPendingRenderEvaluations,
  fetchRecentCaseEvaluations,
  fetchRecentRenderEvaluations,
  fetchRenameSuggestion,
  fetchRenderBatch,
  fetchRenderJob,
  fetchScanLatest,
  fetchStats,
  fetchUpgradeBatch,
  fetchUpgradeJob,
  mergeCases,
  rescanCase,
  retryUpgradeJob,
  triggerScan,
  undoCase,
  undoCaseRender,
  undoEvaluation,
  undoUpgradeBatch,
  upgradeCase,
  updateCase,
  updateCustomer,
  VERDICT_LABEL,
  type CaseDetail,
  type CaseRevision,
  type CaseListParams,
  type CasesPage,
  type CaseSummary,
  type CaseUpdatePayload,
  type CreateEvaluationPayload,
  type CustomerDetail,
  type CustomerSummary,
  type EnqueueRenderPayload,
  type Evaluation,
  type EvaluationSubjectKind,
  type RenderJob,
  type RenderBatch,
  type UpgradeBatch,
  type UpgradeJob,
} from "../api";
import { useUndoStore } from "../lib/undo-toast";

// ---------- Query keys ----------

export const QK = {
  stats: ["stats"] as const,
  scanLatest: ["scan", "latest"] as const,
  cases: (params?: Parameters<typeof fetchCases>[0]) =>
    params && Object.keys(params).length > 0
      ? (["cases", params] as const)
      : (["cases"] as const),
  caseDetail: (id: number) => ["cases", id] as const,
  caseRename: (id: number) => ["cases", id, "rename"] as const,
  customers: (q?: string) =>
    q ? (["customers", { q }] as const) : (["customers"] as const),
  customerDetail: (id: number) => ["customers", id] as const,
  issueDict: ["issues", "dict"] as const,
  // Phase 3 render queue
  renderJobsForCase: (caseId: number) => ["render", "case", caseId, "jobs"] as const,
  renderLatestForCase: (caseId: number) => ["render", "case", caseId, "latest"] as const,
  renderJob: (jobId: number) => ["render", "job", jobId] as const,
  renderBatch: (batchId: string) => ["render", "batch", batchId] as const,
  // 阶段 11: render 历史归档抽屉 (per-brand+template)
  renderHistory: (caseId: number, brand: string, template: string) =>
    ["render", "case", caseId, "history", brand, template] as const,
  // Stage 2 upgrade queue
  upgradeJob: (jobId: number) => ["upgrade", "job", jobId] as const,
  upgradeBatch: (batchId: string) => ["upgrade", "batch", batchId] as const,
  // Stage 1 (post-Phase-3): per-case audit log for the "近期变更" drawer.
  caseRevisions: (caseId: number) => ["cases", caseId, "revisions"] as const,
  // 阶段 3: 评估台 (evaluations namespace).
  evaluationsPendingCase: ["evaluations", "pending", "case"] as const,
  evaluationsPendingRender: (brand?: string) =>
    brand
      ? (["evaluations", "pending", "render", brand] as const)
      : (["evaluations", "pending", "render"] as const),
  evaluationsRecentCase: ["evaluations", "recent", "case"] as const,
  evaluationsRecentRender: (brand?: string) =>
    brand
      ? (["evaluations", "recent", "render", brand] as const)
      : (["evaluations", "recent", "render"] as const),
  evaluationsBySubject: (kind: EvaluationSubjectKind, id: number) =>
    ["evaluations", "subject", kind, id] as const,
};

// ---------- Queries ----------

export function useStats() {
  return useQuery({
    queryKey: QK.stats,
    queryFn: fetchStats,
    staleTime: 5_000,
  });
}

export function useScanLatest() {
  return useQuery({
    queryKey: QK.scanLatest,
    queryFn: fetchScanLatest,
    staleTime: 5_000,
    refetchInterval: (query) => {
      // Poll every 2s while a scan is running (completed_at == null).
      const data = query.state.data;
      if (data?.scan && !data.scan.completed_at) return 2_000;
      return false;
    },
  });
}

/**
 * Legacy hook returning a flat array of cases. Internally uses the paginated
 * endpoint but unwraps `data.items` so existing callers (Dashboard, Dict,
 * etc.) keep working with array semantics. For UI pagination, use
 * `useCasesPage` instead.
 */
export function useCases(
  params: CaseListParams = {},
  options?: Pick<
    UseQueryOptions<CasesPage>,
    "enabled" | "refetchInterval"
  >
) {
  // Translate old `limit` calls to `page_size` on the fly
  const translated: CaseListParams = { ...params };
  if (translated.limit !== undefined) {
    translated.page_size = translated.limit;
    delete translated.limit;
  }
  const q = useQuery({
    queryKey: QK.cases(translated),
    queryFn: () => fetchCases(translated),
    staleTime: 30_000,
    ...options,
  });
  // Override .data to be the items array for backward compat.
  return { ...q, data: q.data?.items } as Omit<typeof q, "data"> & {
    data: CaseSummary[] | undefined;
  };
}

/**
 * Paginated cases hook: keeps the full {items, total, page, page_size}
 * envelope so the consumer can drive a Pagination UI.
 */
export function useCasesPage(params: CaseListParams) {
  return useQuery({
    queryKey: ["cases", "page", params],
    queryFn: () => fetchCases(params),
    placeholderData: (prev) => prev,
    staleTime: 5_000,
  });
}

export function useCaseDetail(id: number | null | undefined) {
  return useQuery({
    queryKey: id ? QK.caseDetail(id) : ["cases", "_disabled"],
    queryFn: () => fetchCaseDetail(id as number),
    enabled: !!id,
    staleTime: 15_000,
  });
}

export function useCaseRename(id: number | null | undefined) {
  return useQuery({
    queryKey: id ? QK.caseRename(id) : ["cases", "_rename_disabled"],
    queryFn: () => fetchRenameSuggestion(id as number),
    enabled: !!id,
    staleTime: 60_000,
  });
}

export function useCustomers(q?: string) {
  return useQuery({
    queryKey: QK.customers(q),
    queryFn: () => fetchCustomers(q),
    staleTime: 30_000,
  });
}

export function useCustomerDetail(id: number | null | undefined) {
  return useQuery({
    queryKey: id ? QK.customerDetail(id) : ["customers", "_disabled"],
    queryFn: () => fetchCustomerDetail(id as number),
    enabled: !!id,
    staleTime: 30_000,
  });
}

export function useIssueDict() {
  return useQuery({
    queryKey: QK.issueDict,
    queryFn: fetchIssueDict,
    staleTime: Infinity,
  });
}

// ---------- Mutations ----------

/**
 * Invalidate everything that *could* depend on a case row mutation.
 * - All ["cases", ...] entries (list + detail + rename suggestions)
 * - Stats (counts change)
 * - Customers list (case_count derives from cases)
 * - The specific customer detail if customer_id is bound (caller passes via meta)
 */
function invalidateCaseRelated(
  qc: ReturnType<typeof useQueryClient>,
  customerId?: number | null
) {
  qc.invalidateQueries({ queryKey: ["cases"] });
  qc.invalidateQueries({ queryKey: QK.stats });
  qc.invalidateQueries({ queryKey: ["customers"] });
  if (customerId) {
    qc.invalidateQueries({ queryKey: QK.customerDetail(customerId) });
  }
}

/** Build a short Chinese label describing what the mutation did, for the toast. */
function describeUpdate(payload: CaseUpdatePayload): string {
  const cleared = payload.clear_fields ?? [];
  if (cleared.includes("manual_category") && cleared.includes("manual_template_tier")) {
    return "已清除手动覆盖";
  }
  if (cleared.includes("held_until")) return "已取消挂起";
  if (payload.held_until) return "已挂起";
  if (payload.review_status === "reviewed") return "已标记为已审核";
  if (payload.review_status === "needs_recheck") return "已标记为需复检";
  if (payload.review_status === "pending") return "已标记为待审核";
  if (payload.manual_category) return `已覆盖类别为 ${payload.manual_category}`;
  if (payload.manual_template_tier) return `已覆盖模板为 ${payload.manual_template_tier}`;
  if (payload.notes !== undefined) return "已更新备注";
  if (payload.tags !== undefined) return "已更新标签";
  if (payload.manual_blocking_codes !== undefined) return "已更新阻塞码";
  return "已更新案例";
}

export function useUpdateCase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { id: number; payload: CaseUpdatePayload }) =>
      updateCase(vars.id, vars.payload),
    onSuccess: (data: CaseDetail, vars) => {
      // Optimistic-ish: write the fresh detail back into the cache so the page
      // doesn't flicker through a refetch.
      qc.setQueryData(QK.caseDetail(data.id), data);
      invalidateCaseRelated(qc, data.customer_id);
      // Open the 30-second undo window.
      useUndoStore.getState().push({
        caseIds: [data.id],
        label: describeUpdate(vars.payload),
      });
    },
  });
}

export function useBatchUpdateCases() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { caseIds: number[]; update: CaseUpdatePayload }) =>
      batchUpdateCases(vars.caseIds, vars.update),
    onSuccess: (_data, vars) => {
      // Batch can touch many customers at once — invalidate broadly.
      invalidateCaseRelated(qc);
      useUndoStore.getState().push({
        caseIds: vars.caseIds,
        label: `${describeUpdate(vars.update)} · ${vars.caseIds.length} 条`,
      });
    },
  });
}

/**
 * On-demand v3 upgrade — runs case-layout-board's full MediaPipe analysis on
 * one case. Slow (5-30s) but produces precise category/tier/blocking_issues.
 */
export function useUpgradeCase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { caseId: number; brand?: string }) =>
      upgradeCase(vars.caseId, vars.brand || "fumei"),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: QK.caseDetail(vars.caseId) });
      qc.invalidateQueries({ queryKey: ["cases"] });
      qc.invalidateQueries({ queryKey: QK.stats });
      // v3 upgrade is undoable — open the 30s window.
      useUndoStore.getState().push({
        caseIds: [vars.caseId],
        label: "已升级到 v3 判读",
      });
    },
  });
}

/**
 * Single-case rescan — re-runs the lite scanner on one directory.
 * Useful after the user manually renamed files on disk.
 */
export function useRescanCase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (caseId: number) => rescanCase(caseId),
    onSuccess: (_data, caseId) => {
      qc.invalidateQueries({ queryKey: QK.caseDetail(caseId) });
      qc.invalidateQueries({ queryKey: ["cases"] });
      qc.invalidateQueries({ queryKey: QK.stats });
      // Rescan IS the kind of thing you might want to undo (if scanner now
      // judges differently than what user manually edited). Open undo window.
      useUndoStore.getState().push({
        caseIds: [caseId],
        label: "已重新判读",
      });
    },
  });
}

export function useTriggerScan() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (mode: "full" | "incremental" = "incremental") =>
      triggerScan(mode),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.scanLatest });
      qc.invalidateQueries({ queryKey: ["cases"] });
      qc.invalidateQueries({ queryKey: QK.stats });
    },
  });
}

export function useCreateCustomer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: createCustomer,
    onSuccess: (data: CustomerSummary) => {
      qc.invalidateQueries({ queryKey: ["customers"] });
      // Touch detail cache so the new id is materialized.
      qc.setQueryData(QK.customerDetail(data.id), {
        ...data,
        cases: [],
      } satisfies CustomerDetail);
    },
  });
}

export function useUpdateCustomer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: {
      id: number;
      payload: { canonical_name?: string; aliases?: string[]; notes?: string };
    }) => updateCustomer(vars.id, vars.payload),
    onSuccess: (data: CustomerSummary) => {
      qc.invalidateQueries({ queryKey: ["customers"] });
      qc.invalidateQueries({ queryKey: QK.customerDetail(data.id) });
    },
  });
}

// ---------- Phase 3: render queue ----------

/** Enqueue a render for a single case. Opens an undo window on success. */
export function useRenderCase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { caseId: number; payload?: EnqueueRenderPayload }) =>
      enqueueRender(vars.caseId, vars.payload || {}),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: QK.renderJobsForCase(vars.caseId) });
      qc.invalidateQueries({ queryKey: QK.renderLatestForCase(vars.caseId) });
      // Note: undo toast is opened on `done` via SSE, not on enqueue, because
      // enqueue is fast (<1s) and `undo` only makes sense after a real artifact
      // has been written.
    },
  });
}

/** Enqueue a batch render. Returns batch_id and job_ids. */
export function useBatchRenderCases() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { caseIds: number[]; payload?: EnqueueRenderPayload }) =>
      enqueueBatchRender(vars.caseIds, vars.payload || {}),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: QK.renderBatch(data.batch_id) });
      // Per-case lists/latest entries also need refresh on the next read.
      qc.invalidateQueries({ queryKey: ["render", "case"] });
    },
  });
}

/** Dry-run validate a CSV-imported batch before committing. Returns valid/invalid breakdown. */
export function usePreviewBatchRender() {
  return useMutation({
    mutationFn: (vars: { caseIds: number[]; payload?: EnqueueRenderPayload }) =>
      previewBatchRender(vars.caseIds, vars.payload || {}),
  });
}

/** Cancel a queued render job. */
export function useCancelRenderJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (jobId: number) => cancelRenderJob(jobId),
    onSuccess: (_data, jobId) => {
      qc.invalidateQueries({ queryKey: QK.renderJob(jobId) });
      qc.invalidateQueries({ queryKey: ["render", "case"] });
      qc.invalidateQueries({ queryKey: ["render", "batch"] });
    },
  });
}

export function useCaseRenderJobs(caseId: number | null | undefined, limit = 20) {
  return useQuery({
    queryKey: caseId ? QK.renderJobsForCase(caseId) : ["render", "_disabled"],
    queryFn: () => fetchCaseRenderJobs(caseId as number, limit),
    enabled: !!caseId,
    staleTime: 10_000,
  });
}

export function useLatestCaseRenderJob(caseId: number | null | undefined) {
  return useQuery({
    queryKey: caseId ? QK.renderLatestForCase(caseId) : ["render", "_latest_disabled"],
    queryFn: () => fetchLatestCaseRenderJob(caseId as number),
    enabled: !!caseId,
    staleTime: 5_000,
  });
}

/** Fetch the .history/ snapshot list for a case+brand+template combo. Only
 * runs while the drawer is open — closes the loop on the LRU-archived
 * final-board.jpg backups produced by render_executor before each render. */
export function useRenderHistory(
  caseId: number | null | undefined,
  brand: string,
  template: string,
  enabled: boolean,
) {
  return useQuery({
    queryKey: caseId
      ? QK.renderHistory(caseId, brand, template)
      : ["render", "_history_disabled"],
    queryFn: () => fetchRenderHistory(caseId as number, brand, template),
    enabled: enabled && !!caseId,
    staleTime: 10_000,
  });
}

/** 阶段 12: 把一份 .history/ 快照恢复为当前 final-board.jpg。
 *
 * Backend 会先把当前 final-board.jpg 自归档到 .history/（previous_archived_at
 * 反映这次自归档的 ts），再 copy 选中的快照覆盖。所以连续 restore 不会丢历史。
 * 成功后 invalidate：
 *  - renderHistory：列表会多一条（自归档的旧 final）
 *  - caseDetail：详情卡的 final-board URL 不变但底层文件 mtime 已变（前端如有
 *    cache-buster 会再走一次 GET）
 *  - caseRevisions：新增一条 op="restore_render" 的 audit row
 *  - renderLatestForCase：与 final-board 对应的「最近一次 render」展示
 */
export function useRestoreSnapshot() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: {
      caseId: number;
      brand: string;
      template: string;
      archivedAt: string;
    }) =>
      restoreRenderSnapshot(vars.caseId, vars.brand, vars.template, vars.archivedAt),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({
        queryKey: QK.renderHistory(vars.caseId, vars.brand, vars.template),
      });
      qc.invalidateQueries({ queryKey: QK.caseDetail(vars.caseId) });
      qc.invalidateQueries({ queryKey: QK.caseRevisions(vars.caseId) });
      qc.invalidateQueries({ queryKey: QK.renderLatestForCase(vars.caseId) });
    },
  });
}

export function useRenderJob(jobId: number | null | undefined) {
  return useQuery({
    queryKey: jobId ? QK.renderJob(jobId) : ["render", "_job_disabled"],
    queryFn: () => fetchRenderJob(jobId as number),
    enabled: !!jobId,
    // While the job is in-flight, refetch frequently — the SSE feed handles real-time
    // updates but polling is the fallback when SSE drops.
    refetchInterval: (query) => {
      const job = query.state.data as RenderJob | undefined;
      if (!job) return 2_000;
      if (job.status === "queued" || job.status === "running") return 2_000;
      return false;
    },
    staleTime: 2_000,
  });
}

export function useRenderBatch(batchId: string | null | undefined) {
  return useQuery({
    queryKey: batchId ? QK.renderBatch(batchId) : ["render", "_batch_disabled"],
    queryFn: () => fetchRenderBatch(batchId as string),
    enabled: !!batchId,
    refetchInterval: (query) => {
      const data = query.state.data as RenderBatch | undefined;
      if (!data) return 2_000;
      const remaining =
        (data.counts.queued ?? 0) + (data.counts.running ?? 0);
      if (remaining > 0) return 2_000;
      return false;
    },
    staleTime: 2_000,
  });
}

/**
 * Subscribe to /api/jobs/stream (unified render + upgrade feed) and call
 * `onEvent` for each parsed event.
 *
 * `jobType` filters which events trigger `onEvent` — invalidation runs for
 * every event regardless, so any open list/detail will refresh without manual
 * wiring whether the change came from a render or upgrade job.
 */
export function useJobStream(opts?: {
  jobType?: "render" | "upgrade";
  onEvent?: (event: JobStreamEvent) => void;
}) {
  const qc = useQueryClient();
  const onEventRef = useRef(opts?.onEvent);
  const filterRef = useRef(opts?.jobType);
  onEventRef.current = opts?.onEvent;
  filterRef.current = opts?.jobType;

  useEffect(() => {
    const es = new EventSource("/api/jobs/stream");
    es.onmessage = (msg) => {
      let parsed: JobStreamEvent | null = null;
      try {
        parsed = JSON.parse(msg.data) as JobStreamEvent;
      } catch {
        return;
      }
      if (!parsed) return;
      const t: "render" | "upgrade" = parsed.job_type ?? "render";
      if (t === "render") {
        if (parsed.job_id != null) {
          qc.invalidateQueries({ queryKey: QK.renderJob(parsed.job_id) });
        }
        if (parsed.case_id != null) {
          qc.invalidateQueries({ queryKey: QK.renderLatestForCase(parsed.case_id) });
          qc.invalidateQueries({ queryKey: QK.renderJobsForCase(parsed.case_id) });
          if (parsed.status === "done" || parsed.status === "undone") {
            qc.invalidateQueries({ queryKey: ["cases"] });
          }
        }
        if (parsed.batch_id) {
          qc.invalidateQueries({ queryKey: QK.renderBatch(parsed.batch_id) });
        }
      } else {
        if (parsed.job_id != null) {
          qc.invalidateQueries({ queryKey: QK.upgradeJob(parsed.job_id) });
        }
        if (parsed.case_id != null) {
          qc.invalidateQueries({ queryKey: QK.caseDetail(parsed.case_id) });
          qc.invalidateQueries({ queryKey: QK.caseRevisions(parsed.case_id) });
          if (parsed.status === "done" || parsed.status === "undone") {
            qc.invalidateQueries({ queryKey: ["cases"] });
            qc.invalidateQueries({ queryKey: QK.stats });
          }
        }
        if (parsed.batch_id) {
          qc.invalidateQueries({ queryKey: QK.upgradeBatch(parsed.batch_id) });
        }
      }
      if (filterRef.current && filterRef.current !== t) return;
      onEventRef.current?.(parsed);
    };
    es.onerror = () => {
      // EventSource auto-retries.
    };
    return () => {
      es.close();
    };
  }, [qc]);
}

export interface JobStreamEvent {
  type: string;
  job_type?: "render" | "upgrade";
  job_id?: number;
  case_id?: number;
  batch_id?: string | null;
  status?: string;
  output_path?: string | null;
  manifest_path?: string | null;
  error_message?: string;
  summary?: Record<string, unknown>;
  brand?: string;
  template?: string;
}

// ---------- end Phase 3 ----------

// ---------- Stage 2: v3 upgrade queue ----------

export function useBatchUpgradeCases() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { caseIds: number[]; brand?: string }) =>
      enqueueBatchUpgrade(vars.caseIds, vars.brand || "fumei"),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: QK.upgradeBatch(data.batch_id) });
    },
  });
}

export function useUpgradeBatch(batchId: string | null | undefined) {
  return useQuery({
    queryKey: batchId ? QK.upgradeBatch(batchId) : ["upgrade", "_batch_disabled"],
    queryFn: () => fetchUpgradeBatch(batchId as string),
    enabled: !!batchId,
    refetchInterval: (query) => {
      const data = query.state.data as UpgradeBatch | undefined;
      if (!data) return 2_000;
      const remaining = (data.counts.queued ?? 0) + (data.counts.running ?? 0);
      if (remaining > 0) return 2_000;
      return false;
    },
    staleTime: 2_000,
  });
}

export function useUpgradeJob(jobId: number | null | undefined) {
  return useQuery({
    queryKey: jobId ? QK.upgradeJob(jobId) : ["upgrade", "_job_disabled"],
    queryFn: () => fetchUpgradeJob(jobId as number),
    enabled: !!jobId,
    refetchInterval: (query) => {
      const job = query.state.data as UpgradeJob | undefined;
      if (!job) return 2_000;
      if (job.status === "queued" || job.status === "running") return 2_000;
      return false;
    },
    staleTime: 2_000,
  });
}

export function useCancelUpgradeJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (jobId: number) => cancelUpgradeJob(jobId),
    onSuccess: (_data, jobId) => {
      qc.invalidateQueries({ queryKey: QK.upgradeJob(jobId) });
      qc.invalidateQueries({ queryKey: ["upgrade", "batch"] });
    },
  });
}

export function useRetryUpgradeJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (jobId: number) => retryUpgradeJob(jobId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["upgrade"] });
    },
  });
}

export function useUndoUpgradeBatch() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (batchId: string) => undoUpgradeBatch(batchId),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: QK.upgradeBatch(data.batch_id) });
      // Undone cases revert to pre-upgrade state — invalidate broadly.
      qc.invalidateQueries({ queryKey: ["cases"] });
      qc.invalidateQueries({ queryKey: QK.stats });
      for (const cid of data.undone) {
        qc.invalidateQueries({ queryKey: QK.caseDetail(cid) });
        qc.invalidateQueries({ queryKey: QK.caseRevisions(cid) });
      }
    },
  });
}

// ---------- end Stage 2 ----------

export function useMergeCases() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { customerId: number; caseIds: number[] }) =>
      mergeCases(vars.customerId, vars.caseIds),
    onSuccess: (_data, vars) => {
      // Cases moved → customer counts change → invalidate broadly.
      invalidateCaseRelated(qc, vars.customerId);
      useUndoStore.getState().push({
        caseIds: vars.caseIds,
        label: `已绑定 ${vars.caseIds.length} 条案例到客户`,
      });
    },
  });
}

// ---------- Stage 1 (post-Phase-3): Revisions drawer ----------

/** Fetch the last N audit-log entries for a case (newest-first). Used by the
 * 「近期变更」drawer in CaseDetail to recover from a missed 30-second toast. */
export function useCaseRevisions(caseId: number | null | undefined, limit = 20) {
  return useQuery({
    queryKey: caseId ? QK.caseRevisions(caseId) : ["cases", "_revisions_disabled"],
    queryFn: () => fetchCaseRevisions(caseId as number, limit),
    enabled: !!caseId,
    // 5s — drawer is interactive, user wants quick refresh after they hit 撤销.
    staleTime: 5_000,
  });
}

/**
 * Undo the latest active revision for a case.
 *
 * The drawer routes by op:
 * - op='render' → /api/cases/{id}/render/undo (deletes the artifact file)
 * - everything else → /api/cases/{id}/undo (apply_undo on tracked columns)
 *
 * Backend's `latest_active_revision` already skips render/undo_render, so /undo
 * is safe to call even if a render exists between the target and now (it'll
 * undo the next non-render revision down the stack).
 */
export function useUndoCaseFromDrawer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (vars: { caseId: number; op: CaseRevision["op"] }) => {
      if (vars.op === "render") {
        await undoCaseRender(vars.caseId);
      } else {
        await undoCase(vars.caseId);
      }
      return { caseId: vars.caseId, op: vars.op };
    },
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: QK.caseRevisions(vars.caseId) });
      qc.invalidateQueries({ queryKey: QK.caseDetail(vars.caseId) });
      qc.invalidateQueries({ queryKey: ["cases"] });
      qc.invalidateQueries({ queryKey: QK.stats });
      if (vars.op === "render") {
        qc.invalidateQueries({ queryKey: QK.renderLatestForCase(vars.caseId) });
        qc.invalidateQueries({ queryKey: QK.renderJobsForCase(vars.caseId) });
        // Render undo cascades to evaluations on the backend; refetch.
        qc.invalidateQueries({ queryKey: ["evaluations"] });
      }
    },
  });
}

// ---------- 阶段 3: 评估台 ----------

export function usePendingCaseEvaluations(limit = 50) {
  return useQuery({
    queryKey: QK.evaluationsPendingCase,
    queryFn: () => fetchPendingCaseEvaluations(limit),
    staleTime: 10_000,
  });
}

export function usePendingRenderEvaluations(brand?: string, limit = 50) {
  return useQuery({
    queryKey: QK.evaluationsPendingRender(brand),
    queryFn: () => fetchPendingRenderEvaluations(brand, limit),
    staleTime: 10_000,
  });
}

export function useRecentCaseEvaluations(limit = 20) {
  return useQuery({
    queryKey: QK.evaluationsRecentCase,
    queryFn: () => fetchRecentCaseEvaluations(limit),
    staleTime: 5_000,
  });
}

export function useRecentRenderEvaluations(brand?: string, limit = 20) {
  return useQuery({
    queryKey: QK.evaluationsRecentRender(brand),
    queryFn: () => fetchRecentRenderEvaluations(brand, limit),
    staleTime: 5_000,
  });
}

export function useEvaluationsBySubject(
  kind: EvaluationSubjectKind | null | undefined,
  subjectId: number | null | undefined,
  limit = 50
) {
  return useQuery({
    queryKey: kind && subjectId ? QK.evaluationsBySubject(kind, subjectId) : ["evaluations", "_disabled"],
    queryFn: () => fetchEvaluationsBySubject(kind as EvaluationSubjectKind, subjectId as number, limit),
    enabled: !!kind && !!subjectId,
    staleTime: 5_000,
  });
}

/** Create an evaluation. Pushes a 30s undo toast on success.
 *
 * Caller can pass `caseId` (the case this evaluation belongs to, even when the
 * subject is a render job) so the toast invalidates per-case caches and the
 * drawer refreshes immediately.
 */
export function useCreateEvaluation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { payload: CreateEvaluationPayload; caseId?: number }) =>
      createEvaluation(vars.payload).then((evaluation) => ({ evaluation, caseId: vars.caseId })),
    onSuccess: ({ evaluation, caseId }) => {
      qc.invalidateQueries({ queryKey: ["evaluations"] });
      if (caseId) {
        qc.invalidateQueries({ queryKey: QK.caseDetail(caseId) });
        qc.invalidateQueries({ queryKey: QK.caseRevisions(caseId) });
      }
      const subjectLabel =
        evaluation.subject_kind === "case" ? "案例" : "出图";
      const verdictLabel = VERDICT_LABEL[evaluation.verdict];
      useUndoStore.getState().push({
        kind: "evaluation",
        caseIds: caseId ? [caseId] : [],
        evaluationId: evaluation.id,
        label: `已评估 ${subjectLabel} · ${verdictLabel}`,
      });
    },
  });
}

/** Undo an evaluation directly (used by the evaluations page row buttons). */
export function useUndoEvaluation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { evaluationId: number; caseId?: number }) =>
      undoEvaluation(vars.evaluationId).then((evaluation) => ({ evaluation, caseId: vars.caseId })),
    onSuccess: ({ caseId }) => {
      qc.invalidateQueries({ queryKey: ["evaluations"] });
      if (caseId) {
        qc.invalidateQueries({ queryKey: QK.caseDetail(caseId) });
        qc.invalidateQueries({ queryKey: QK.caseRevisions(caseId) });
      }
    },
  });
}
