import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  type AiReviewPolicy,
  type AiReviewPolicyPreview,
  type ImageWorkbenchCaseGroup,
  type QualityReport,
  renderJobOutputUrl,
  simulationJobDirectFileUrl,
  type RenderJob,
  type RenderQualityQueueItem,
  type RenderQualityQueueStatus,
  type SimulationJob,
  type SimulationQualityQueueItem,
  type SimulationQualityQueueStatus,
} from "../api";
import {
  useRenderQualityQueue,
  useAiReviewPolicy,
  useImageWorkbenchQueue,
  usePreviewAiReviewPolicy,
  useQualityReport,
  useReviewRenderQuality,
  useReviewSimulationJobById,
  useSimulationQualityQueue,
  useUpdateAiReviewPolicy,
} from "../hooks/queries";
import { Ico } from "../components/atoms";

type QueueKind = "render" | "simulation";
type ReviewVerdict = "approved" | "needs_recheck" | "rejected";
type SimulationRecommendationFilter = "approved" | "needs_recheck" | "rejected" | "manual_override" | null;
type TFunc = (key: string, options?: Record<string, unknown>) => string;

// react-i18next's typed TFunction is too strict for our dynamic key usage
// (e.g. `statuses.${dynamicKey}`). We wrap the hook output once per component
// and let the JSON acts as the runtime safety net (verified by i18n completeness scan).
function useT(): TFunc {
  const { t } = useTranslation("qualityReview");
  return t as unknown as TFunc;
}

const RENDER_FILTER_KEYS: { key: RenderQualityQueueStatus; labelKey: string }[] = [
  { key: "review_required", labelKey: "renderFilters.review_required" },
  { key: "done_with_issues", labelKey: "renderFilters.done_with_issues" },
  { key: "blocked", labelKey: "renderFilters.blocked" },
  { key: "failed", labelKey: "renderFilters.failed" },
  { key: "reviewed", labelKey: "renderFilters.reviewed" },
  { key: "all", labelKey: "renderFilters.all" },
];

const SIM_FILTER_KEYS: { key: SimulationQualityQueueStatus; labelKey: string }[] = [
  { key: "review_required", labelKey: "simFilters.review_required" },
  { key: "done_with_issues", labelKey: "simFilters.done_with_issues" },
  { key: "failed", labelKey: "simFilters.failed" },
  { key: "approved", labelKey: "simFilters.approved" },
  { key: "reviewed", labelKey: "simFilters.reviewed" },
  { key: "all", labelKey: "simFilters.all" },
];

function statusLabel(t: TFunc, status: string | null | undefined): string {
  if (!status) return "";
  // i18next returns key when missing — used as fallback to raw status string
  const translated = t(`statuses.${status}` as never);
  return translated === `statuses.${status}` ? status : translated;
}

function recommendationLabel(t: TFunc, key: string | null | undefined): string {
  if (!key) return "";
  const translated = t(`recommendations.${key}` as never);
  return translated === `recommendations.${key}` ? key : translated;
}

function policyThresholdLabel(t: TFunc, key: string): string {
  const translated = t(`policyThresholds.${key}` as never);
  return translated === `policyThresholds.${key}` ? key : translated;
}

function caseTitle(t: TFunc, absPath: string | null | undefined): string {
  if (!absPath) return t("case.unbound");
  const parts = absPath.split("/").filter(Boolean);
  if (parts.length <= 1) return absPath;
  return parts.slice(-2).join(" / ");
}

function statusTone(status: string | null | undefined): { bg: string; ink: string; border: string } {
  if (status === "done" || status === "approved") return { bg: "var(--ok-50)", ink: "var(--ok)", border: "var(--ok-100)" };
  if (status === "done_with_issues" || status === "needs_recheck") return { bg: "var(--amber-50)", ink: "var(--amber-ink)", border: "var(--amber-200)" };
  return { bg: "var(--err-50)", ink: "var(--err)", border: "var(--err-100)" };
}

function renderTone(job: RenderJob) {
  return statusTone(job.quality?.quality_status ?? job.status);
}

function countFor(counts: Record<string, number> | undefined, key: string): number | null {
  if (!counts) return null;
  if (key === "review_required" || key === "all") return null;
  if (key === "reviewed") return counts.reviewed ?? 0;
  return counts[key] ?? 0;
}

function errorText(t: TFunc, err: unknown): string {
  if (err && typeof err === "object" && "response" in err) {
    const resp = (err as { response?: { data?: { detail?: string } } }).response;
    if (resp?.data?.detail) return resp.data.detail;
  }
  if (err instanceof Error) return err.message;
  return t("errors.operationFailed");
}

function shortText(text: string, max = 220): string {
  const compact = text.replace(/\s+/g, " ").trim();
  if (compact.length <= max) return compact;
  return compact.slice(0, max - 1) + "…";
}

function renderIssueTarget(text: string): { slot: "front" | "oblique" | "side"; code: string; contains: string } | null {
  const value = String(text || "");
  if (!value) return null;
  const slot = value.includes("45") || value.includes("45°") ? "oblique" : value.includes("侧面") || value.includes("侧脸") || value.includes("侧向") ? "side" : value.includes("正面") ? "front" : null;
  if (!slot) return null;
  if (value.includes("方向不一致")) return { slot, code: "direction_mismatch", contains: "方向不一致" };
  if (value.includes("姿态差")) return { slot, code: "pose_delta_large", contains: "姿态差" };
  if (value.includes("清晰度")) return { slot, code: "sharpness_delta", contains: "清晰度" };
  if (value.includes("侧面人脸检测失败")) return { slot, code: "side_face_alignment_fallback", contains: "侧面人脸检测失败" };
  if (value.includes("构图") || value.includes("兜底")) return { slot, code: "side_face_alignment_fallback", contains: "兜底" };
  if (value.includes("面部检测") || value.includes("正脸检测")) return { slot, code: "face_detection_review", contains: "面部检测" };
  return null;
}

function percent(t: TFunc, value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return t("report.metricNa");
  return `${Math.round(value * 1000) / 10}%`;
}

function rootCauseUnit(t: TFunc, unit: string) {
  if (unit === "image") return t("rootCause.units.image");
  if (unit === "case") return t("rootCause.units.case");
  return t("rootCause.units.default");
}

function roleLabel(t: TFunc, role: string) {
  if (role === "before") return t("roles.before");
  if (role === "after") return t("roles.after");
  return role;
}

function compactTitle(t: TFunc, text: string | null | undefined, max = 34) {
  const value = String(text || t("blockerQueue.untitled")).replace(/\s+/g, " ").trim();
  if (value.length <= max) return value;
  return `${value.slice(0, max - 1)}…`;
}

function rootCauseRenderStatus(code: string): RenderQualityQueueStatus | null {
  if (code === "renderer_failed") return "failed";
  if (code === "source_directory" || code === "missing_render_slots" || code === "classification_open") return "blocked";
  if (code === "output_invisible" || code === "pair_quality" || code === "face_quality" || code === "composition_review") return "done_with_issues";
  return null;
}

function modelName(t: TFunc, job: SimulationJob): string {
  const plan = job.model_plan || {};
  const raw = plan.model_name || plan.model || plan.provider;
  return typeof raw === "string" && raw.trim() ? raw : t("modelDefault");
}

function differenceMetrics(job: SimulationJob): { full: number; target: number | null; nonTarget: number; p95: number | null; ratio: number | null } | null {
  const raw = job.audit?.difference_analysis;
  if (!raw || typeof raw !== "object") return null;
  const item = raw as Record<string, unknown>;
  const full = Number(item.full_frame_change_score);
  const target = item.target_region_change_score == null ? null : Number(item.target_region_change_score);
  const nonTarget = Number(item.non_target_change_score);
  const p95Value = item.p95_change_score == null ? null : Number(item.p95_change_score);
  const ratioValue = item.changed_pixel_ratio_8pct == null ? null : Number(item.changed_pixel_ratio_8pct);
  if (!Number.isFinite(full) || !Number.isFinite(nonTarget)) return null;
  return {
    full,
    target: Number.isFinite(target) ? target : null,
    nonTarget,
    p95: Number.isFinite(p95Value) ? p95Value : null,
    ratio: Number.isFinite(ratioValue) ? ratioValue : null,
  };
}

function simulationFile(job: SimulationJob, kind: string) {
  const canonical = kind === "comparison" ? "controlled_policy_comparison" : kind;
  return job.available_files?.find((file) => file.kind === canonical) ?? null;
}

function decisionTone(decision: SimulationJob["review_decision"] | undefined) {
  if (decision?.severity === "ok" || decision?.recommended_verdict === "approved") {
    return { bg: "var(--ok-50)", ink: "var(--ok)", border: "var(--ok-100)" };
  }
  if (decision?.severity === "block" || decision?.recommended_verdict === "rejected") {
    return { bg: "var(--err-50)", ink: "var(--err)", border: "var(--err-100)" };
  }
  return { bg: "var(--amber-50)", ink: "var(--amber-ink)", border: "var(--amber-200)" };
}

export default function QualityReview() {
  const t = useT();
  const [kind, setKind] = useState<QueueKind>("render");
  const [renderStatus, setRenderStatus] = useState<RenderQualityQueueStatus>("review_required");
  const [simStatus, setSimStatus] = useState<SimulationQualityQueueStatus>("review_required");
  const [simRecommendation, setSimRecommendation] = useState<SimulationRecommendationFilter>(null);
  const renderQ = useRenderQualityQueue({ status: renderStatus, limit: 120 });
  const simQ = useSimulationQualityQueue({ status: simStatus, recommendation: simRecommendation, limit: 120 });
  const blockerQueueQ = useImageWorkbenchQueue({ status: "review_needed", limit: 20 });
  const policyQ = useAiReviewPolicy();
  const reportQ = useQualityReport({ limit: 500 });
  const renderReviewMut = useReviewRenderQuality();
  const simReviewMut = useReviewSimulationJobById();
  const policyMut = useUpdateAiReviewPolicy();
  const policyPreviewMut = usePreviewAiReviewPolicy();
  const [message, setMessage] = useState<string | null>(null);
  const [policyOverride, setPolicyOverride] = useState<AiReviewPolicy | null>(null);
  const policyDraft = policyOverride ?? policyQ.data ?? null;

  const renderFilters = useMemo(
    () => RENDER_FILTER_KEYS.map((f) => ({ key: f.key, label: t(f.labelKey as never) })),
    [t],
  );
  const simFilterDefs = useMemo(
    () => SIM_FILTER_KEYS.map((f) => ({ key: f.key, label: t(f.labelKey as never) })),
    [t],
  );

  const activeTotal = kind === "render" ? renderQ.data?.total ?? 0 : simQ.data?.total ?? 0;
  const renderStats = useMemo(() => {
    const counts = renderQ.data?.counts ?? {};
    const archiveHidden = renderQ.data?.archive?.hidden_by_current_latest ?? 0;
    return {
      failed: counts.failed ?? 0,
      blocked: counts.blocked ?? 0,
      review: counts.done_with_issues ?? 0,
      reviewed: counts.reviewed ?? 0,
      archived: archiveHidden,
    };
  }, [renderQ.data]);
  const simStats = useMemo(() => {
    const counts = simQ.data?.counts ?? {};
    return {
      failed: counts.failed ?? 0,
      review: counts.done_with_issues ?? 0,
      approved: counts.approved ?? 0,
      reviewed: counts.reviewed ?? 0,
    };
  }, [simQ.data]);

  const reviewRenderJob = async (item: RenderQualityQueueItem, verdict: ReviewVerdict) => {
    const note = window.prompt(
      verdict === "approved"
        ? t("prompts.approveNote")
        : verdict === "needs_recheck"
          ? t("prompts.needsRecheckNote")
          : t("prompts.rejectNote"),
      "",
    );
    if (note === null) return;
    try {
      await renderReviewMut.mutateAsync({
        jobId: item.job.id,
        payload: {
          verdict,
          reviewer: "quality-page",
          note: note.trim() || null,
          can_publish: verdict === "approved" && item.job.status !== "blocked",
        },
      });
      setMessage(t("messages.renderRecorded", { id: item.job.id, status: statusLabel(t, verdict) }));
    } catch (err) {
      setMessage(errorText(t, err));
    }
  };

  const reviewSimulationJob = async (item: SimulationQualityQueueItem, verdict: ReviewVerdict) => {
    const note = window.prompt(
      verdict === "approved"
        ? t("prompts.approveSimNote")
        : verdict === "needs_recheck"
          ? t("prompts.needsRecheckNote")
          : t("prompts.rejectNote"),
      "",
    );
    if (note === null) return;
    try {
      await simReviewMut.mutateAsync({
        jobId: item.job.id,
        payload: {
          verdict,
          reviewer: "quality-page",
          note: note.trim() || null,
        },
      });
      setMessage(t("messages.simRecorded", { id: item.job.id, status: statusLabel(t, verdict) }));
    } catch (err) {
      setMessage(errorText(t, err));
    }
  };

  const savePolicy = async () => {
    if (!policyDraft) return;
    try {
      const saved = await policyMut.mutateAsync(policyDraft);
      setPolicyOverride(saved);
      await Promise.all([simQ.refetch(), reportQ.refetch()]);
      setMessage(t("messages.policySaved", { name: saved.name, version: saved.version }));
    } catch (err) {
      setMessage(errorText(t, err));
    }
  };

  const previewPolicy = async () => {
    if (!policyDraft) return;
    try {
      const preview = await policyPreviewMut.mutateAsync(policyDraft);
      setMessage(t("messages.policyPreviewDone", { count: preview.summary.changed_count }));
    } catch (err) {
      setMessage(errorText(t, err));
    }
  };

  const drillRender = (status: RenderQualityQueueStatus) => {
    setKind("render");
    setRenderStatus(status);
    setMessage(t("messages.drillRender", { label: statusLabel(t, status) || status }));
  };

  const drillSimulation = (recommendation: SimulationRecommendationFilter) => {
    setKind("simulation");
    setSimStatus("all");
    setSimRecommendation(recommendation);
    setMessage(
      t("messages.drillSim", {
        label: recommendation ? recommendationLabel(t, recommendation) : t("recommendations.all"),
      }),
    );
  };

  const drillRootCause = (code: string) => {
    if (code === "ai_review") {
      setKind("simulation");
      setSimStatus("review_required");
      setSimRecommendation(null);
      setMessage(t("messages.drillSimReviewQueue"));
      return;
    }
    const status = rootCauseRenderStatus(code);
    if (status) drillRender(status);
  };

  const messageIsError = useMemo(() => {
    if (!message) return false;
    const failedKeyword = t("statuses.failed");
    return message.includes(failedKeyword) || message.includes("not");
  }, [message, t]);

  return (
    <div style={{ height: "100%", display: "grid", gridTemplateRows: "auto 1fr", overflow: "hidden" }}>
      <div className="page-header">
        <div>
          <h1 className="page-title">
            {t("header.title")}
            <span style={{ fontFamily: "var(--mono)", color: "var(--ink-3)", fontSize: 14, fontWeight: 500, marginLeft: 6 }}>
              {activeTotal}
            </span>
          </h1>
          <div className="page-sub">
            {t("header.subtitle")}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", justifyContent: "flex-end" }}>
          {kind === "render" ? (
            <>
              <span className="badge" style={{ background: "var(--amber-50)", color: "var(--amber-ink)", borderColor: "var(--amber-200)" }}>{t("summaryBadges.needsReview", { count: renderStats.review })}</span>
              <span className="badge" style={{ background: "var(--err-50)", color: "var(--err)", borderColor: "var(--err-100)" }}>{t("summaryBadges.blocked", { count: renderStats.blocked })}</span>
              <span className="badge">{t("summaryBadges.failed", { count: renderStats.failed })}</span>
              <span className="badge">{t("summaryBadges.reviewed", { count: renderStats.reviewed })}</span>
              <span className="badge">{t("summaryBadges.archived", { count: renderStats.archived })}</span>
            </>
          ) : (
            <>
              <span className="badge" style={{ background: "var(--amber-50)", color: "var(--amber-ink)", borderColor: "var(--amber-200)" }}>{t("summaryBadges.needsReview", { count: simStats.review })}</span>
              <span className="badge">{t("summaryBadges.failed", { count: simStats.failed })}</span>
              <span className="badge" style={{ background: "var(--ok-50)", color: "var(--ok)", borderColor: "var(--ok-100)" }}>{t("summaryBadges.approved", { count: simStats.approved })}</span>
              <span className="badge">{t("summaryBadges.reviewed", { count: simStats.reviewed })}</span>
            </>
          )}
          <button
            className="btn sm"
            onClick={() => (kind === "render" ? renderQ.refetch() : simQ.refetch())}
            disabled={kind === "render" ? renderQ.isFetching : simQ.isFetching}
          >
            <Ico name="refresh" size={12} />
            {(kind === "render" ? renderQ.isFetching : simQ.isFetching) ? t("header.refreshing") : t("header.refresh")}
          </button>
        </div>
      </div>

      <main style={{ minHeight: 0, overflow: "auto", padding: 18, display: "grid", gap: 12, alignContent: "start" }}>
        <section className="card">
          <div className="card-b" style={{ display: "grid", gap: 12 }}>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              <button type="button" className={`btn sm${kind === "render" ? " primary" : ""}`} onClick={() => setKind("render")}>
                <Ico name="image" size={12} />
                {t("kindTabs.render")}
              </button>
              <button type="button" className={`btn sm${kind === "simulation" ? " primary" : ""}`} onClick={() => setKind("simulation")}>
                <Ico name="edit" size={12} />
                {t("kindTabs.simulation")}
              </button>
            </div>
            {kind === "render" ? (
              <FilterRow
                filters={renderFilters}
                status={renderStatus}
                counts={renderQ.data?.counts}
                onSelect={(v) => setRenderStatus(v as RenderQualityQueueStatus)}
                summary={t("summary.renderActiveIssues", {
                  total: renderQ.data?.total ?? 0,
                  archived: renderQ.data?.archive?.hidden_by_current_latest ?? 0,
                })}
              />
            ) : (
              <FilterRow
                filters={simFilterDefs}
                status={simStatus}
                counts={simQ.data?.counts}
                onSelect={(v) => {
                  setSimStatus(v as SimulationQualityQueueStatus);
                  setSimRecommendation(null);
                }}
                summary={
                  simRecommendation
                    ? t("summary.simByRecommendation", {
                        total: simQ.data?.total ?? 0,
                        label: recommendationLabel(t, simRecommendation),
                      })
                    : t("summary.simDefault", { total: simQ.data?.total ?? 0 })
                }
              />
            )}
            {kind === "simulation" && simRecommendation && (
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 12, color: "var(--ink-3)" }}>
                <span>{t("recommendationFilter.label", { label: recommendationLabel(t, simRecommendation) })}</span>
                <button className="btn sm" type="button" onClick={() => setSimRecommendation(null)}>
                  {t("recommendationFilter.clear")}
                </button>
              </div>
            )}
          </div>
        </section>

        {message && (
          <div
            style={{
              border: "1px solid var(--line)",
              background: "var(--panel)",
              borderRadius: 6,
              padding: "9px 12px",
              fontSize: 12,
              color: messageIsError ? "var(--err)" : "var(--ink-2)",
            }}
          >
            {message}
          </div>
        )}

        <QualityReportPanel
          loading={reportQ.isLoading}
          error={reportQ.isError}
          data={reportQ.data}
          blockerGroups={blockerQueueQ.data?.case_groups ?? []}
          blockerLoading={blockerQueueQ.isLoading}
          onRefresh={() => reportQ.refetch()}
          refreshing={reportQ.isFetching}
          onDrillRender={drillRender}
          onDrillSimulation={drillSimulation}
          onDrillRootCause={drillRootCause}
        />

        <AiPolicyPanel
          loading={policyQ.isLoading}
          error={policyQ.isError}
          draft={policyDraft}
          saving={policyMut.isPending}
          previewing={policyPreviewMut.isPending}
          preview={policyPreviewMut.data ?? null}
          onChange={setPolicyOverride}
          onPreview={previewPolicy}
          onSave={savePolicy}
        />

        {kind === "render" ? (
          <RenderQueue
            loading={renderQ.isLoading}
            error={renderQ.isError}
            items={renderQ.data?.items ?? []}
            reviewing={renderReviewMut.isPending}
            onReview={reviewRenderJob}
          />
        ) : (
          <SimulationQueue
            loading={simQ.isLoading}
            error={simQ.isError}
            items={simQ.data?.items ?? []}
            reviewing={simReviewMut.isPending}
            onReview={reviewSimulationJob}
          />
        )}
      </main>
    </div>
  );
}

function FilterRow({
  filters,
  status,
  counts,
  onSelect,
  summary,
}: {
  filters: { key: string; label: string }[];
  status: string;
  counts: Record<string, number> | undefined;
  onSelect: (value: string) => void;
  summary: string;
}) {
  return (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", justifyContent: "space-between" }}>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {filters.map((f) => {
          const active = status === f.key;
          const count = countFor(counts, f.key);
          return (
            <button
              key={f.key}
              type="button"
              className={`btn sm${active ? " primary" : ""}`}
              onClick={() => onSelect(f.key)}
            >
              {f.label}
              {count !== null && <span style={{ fontFamily: "var(--mono)", opacity: 0.75 }}>{count}</span>}
            </button>
          );
        })}
      </div>
      <div style={{ fontSize: 12, color: "var(--ink-3)" }}>{summary}</div>
    </div>
  );
}

function QualityReportPanel({
  loading,
  error,
  data,
  blockerGroups,
  blockerLoading,
  onRefresh,
  refreshing,
  onDrillRender,
  onDrillSimulation,
  onDrillRootCause,
}: {
  loading: boolean;
  error: boolean;
  data: QualityReport | undefined;
  blockerGroups: ImageWorkbenchCaseGroup[];
  blockerLoading: boolean;
  onRefresh: () => void;
  refreshing: boolean;
  onDrillRender: (status: RenderQualityQueueStatus) => void;
  onDrillSimulation: (recommendation: SimulationRecommendationFilter) => void;
  onDrillRootCause: (code: string) => void;
}) {
  const t = useT();
  if (loading) return <div className="empty">{t("report.loading")}</div>;
  if (error || !data) return <div className="empty">{t("report.loadError")}</div>;
  const simRec = data.simulation.by_system_recommendation;
  const renderQuality = data.render.by_quality_status;
  const baseline = data.render.current_version_baseline;
  const delivery = data.delivery_baseline;
  const visibility = data.render.artifact_visibility;
  const classification = delivery?.classification ?? data.classification;
  const overrideRate =
    data.simulation.reviewed > 0
      ? Math.round((data.simulation.manual_override / data.simulation.reviewed) * 1000) / 10
      : 0;
  const sep = " · ";
  const commit = data.code_version?.commit ?? t("report.metaCommitUnknown");
  const dirtyPart = data.code_version?.dirty
    ? `${sep}${t("report.metaDirty", { count: data.code_version.dirty_file_count })}`
    : "";
  const policyPart = `${sep}${t("report.metaPolicy", { name: data.policy.name, version: data.policy.version })}`;
  const archivedPart =
    baseline?.historical_archived_count != null
      ? `${sep}${t("report.metaArchived", { count: baseline.historical_archived_count })}`
      : "";
  const scopePart = delivery?.scope ? `${sep}${delivery.scope}` : "";
  const naLabel = t("report.metricNa");

  return (
    <section className="card">
      <div className="card-b" style={{ display: "grid", gap: 12 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, flexWrap: "wrap" }}>
          <div>
            <div style={{ fontSize: 14, fontWeight: 700 }}>{t("report.title")}</div>
            <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
              {t("report.metaPrefix")}{sep}{commit}{dirtyPart}{policyPart}{archivedPart}{scopePart}
            </div>
          </div>
          <button className="btn sm" type="button" onClick={onRefresh} disabled={refreshing}>
            <Ico name="refresh" size={12} />
            {refreshing ? t("report.refreshing") : t("report.refresh")}
          </button>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 8 }}>
          <MetricTile label={t("report.metrics.deliverySample")} value={delivery?.sample_size ?? baseline?.sample_size ?? naLabel} />
          <MetricTile label={t("report.metrics.currentLatest")} value={delivery?.current_latest_case_count ?? baseline?.current_latest_case_count ?? baseline?.sample_size ?? naLabel} />
          <MetricTile label={t("report.metrics.rendererSuccess")} value={percent(t, delivery?.renderer.success_rate_excluding_blocked ?? baseline?.renderer_success_rate_excluding_blocked)} tone="ok" />
          <MetricTile label={t("report.metrics.rendererFailed")} value={percent(t, delivery?.renderer.failed_rate_excluding_blocked)} tone={(delivery?.renderer.failed_count ?? 0) > 0 ? "warn" : "ok"} />
          <MetricTile label={t("report.metrics.currentCleanDone")} value={percent(t, baseline?.clean_done_rate)} />
          <MetricTile label={t("report.metrics.currentDoneWithIssues")} value={percent(t, delivery?.quality.done_with_issues_rate ?? baseline?.done_with_issues_rate)} />
          <MetricTile label={t("report.metrics.currentPublishRate")} value={percent(t, delivery?.publishability.publishable_rate ?? baseline?.publishable_rate)} />
          <MetricTile label={t("report.metrics.finalBoardVisible")} value={percent(t, delivery?.publishability.final_board_visible_rate ?? visibility?.final_board_visible_rate ?? data.totals.final_board_visible_rate)} tone={(delivery?.publishability.final_board_missing_count ?? visibility?.final_board_missing_count ?? 0) > 0 ? "warn" : "ok"} />
          <MetricTile label={t("report.metrics.classificationRate")} value={percent(t, classification?.completion_rate ?? data.totals.classification_completion_rate)} />
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 8 }}>
          <MetricTile label={t("report.metrics.blockedGuardrail")} value={delivery?.renderer.blocked_guardrail_count ?? baseline?.blocked_as_guardrail ?? naLabel} />
          <MetricTile label={t("report.metrics.rendererFailedCount")} value={delivery?.renderer.failed_count ?? baseline?.renderer_failure_count ?? naLabel} tone={(delivery?.renderer.failed_count ?? baseline?.renderer_failure_count ?? 0) > 0 ? "warn" : "ok"} />
          <MetricTile label={t("report.metrics.currentReviewRequired")} value={baseline?.review_required_count ?? naLabel} tone={(baseline?.review_required_count ?? 0) > 0 ? "warn" : undefined} />
          <MetricTile label={t("report.metrics.currentActionable")} value={delivery?.quality.actionable_warning_count ?? baseline?.actionable_warning_count ?? naLabel} tone={(delivery?.quality.actionable_warning_count ?? baseline?.actionable_warning_count ?? 0) > 0 ? "warn" : "ok"} />
          <MetricTile label={t("report.metrics.totalArtifacts")} value={data.totals.artifacts} />
          <MetricTile label={t("report.metrics.reviewed")} value={data.totals.reviewed} />
          <MetricTile label={t("report.metrics.publishable")} value={data.totals.publishable} tone="ok" />
          <MetricTile label={t("report.metrics.notPublishable")} value={data.totals.not_publishable} tone="warn" />
          <MetricTile label={t("report.metrics.renderAvgScore")} value={data.render.avg_quality_score ?? naLabel} />
          <MetricTile label={t("report.metrics.simAvgNonTarget")} value={data.simulation.avg_non_target_change ?? naLabel} />
        </div>
        {classification && (
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", fontSize: 12 }}>
            <span className="badge">{t("report.classificationBadges.sourceImages", { count: classification.source_image_count })}</span>
            <span className="badge">{t("report.classificationBadges.classified", { count: classification.classified_count })}</span>
            <span className="badge">{t("report.classificationBadges.needsManual", { count: classification.needs_manual_count })}</span>
            <span className="badge">{t("report.classificationBadges.lowConfidence", { count: classification.low_confidence_count })}</span>
          </div>
        )}
        {data.root_causes?.top_causes?.length ? (
          <div style={{ display: "grid", gap: 8 }}>
            <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 8 }}>
              <div style={{ fontSize: 12, fontWeight: 700 }}>{t("report.rootCausesTitle")}</div>
              <div style={{ fontSize: 11, color: "var(--ink-3)" }}>{data.root_causes.scope}</div>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 8 }}>
              {data.root_causes.top_causes.slice(0, 6).map((cause) => (
                <RootCauseActionCard key={cause.code} cause={cause} onDrillRootCause={onDrillRootCause} />
              ))}
            </div>
          </div>
        ) : null}
        <ClassificationBlockerQueuePanel groups={blockerGroups} loading={blockerLoading} />
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 10 }}>
          <div style={{ display: "grid", gap: 6 }}>
            <div style={{ fontSize: 12, fontWeight: 700 }}>{t("report.renderSection")}</div>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              <DrillChip label={t("report.drillRender.done")} value={renderQuality.done ?? 0} onClick={() => onDrillRender("done")} />
              <DrillChip label={t("report.drillRender.done_with_issues")} value={renderQuality.done_with_issues ?? 0} onClick={() => onDrillRender("done_with_issues")} />
              <DrillChip label={t("report.drillRender.blocked")} value={renderQuality.blocked ?? 0} onClick={() => onDrillRender("blocked")} />
              <DrillChip label={t("report.drillRender.failed")} value={data.render.by_status.failed ?? 0} onClick={() => onDrillRender("failed")} />
            </div>
          </div>
          <div style={{ display: "grid", gap: 6 }}>
            <div style={{ fontSize: 12, fontWeight: 700 }}>{t("report.simSection")}</div>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              <DrillChip label={t("report.drillSim.approved")} value={simRec.approved ?? 0} onClick={() => onDrillSimulation("approved")} />
              <DrillChip label={t("report.drillSim.needs_recheck")} value={simRec.needs_recheck ?? 0} onClick={() => onDrillSimulation("needs_recheck")} />
              <DrillChip label={t("report.drillSim.rejected")} value={simRec.rejected ?? 0} onClick={() => onDrillSimulation("rejected")} />
              <DrillChip label={t("report.drillSim.manual_override")} value={`${data.simulation.manual_override} (${overrideRate}%)`} onClick={() => onDrillSimulation("manual_override")} />
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function RootCauseActionCard({
  cause,
  onDrillRootCause,
}: {
  cause: NonNullable<QualityReport["root_causes"]>["top_causes"][number];
  onDrillRootCause: (code: string) => void;
}) {
  const content = <RootCauseCardBody cause={cause} />;
  const baseStyle = {
    border: "1px solid var(--line)",
    borderRadius: 6,
    padding: 10,
    display: "grid",
    gap: 6,
    background: cause.severity === "block" ? "rgba(239,68,68,.035)" : "var(--panel)",
    color: "inherit",
  };
  if (cause.href.startsWith("/images")) {
    return (
      <Link to={cause.href} style={{ ...baseStyle, textDecoration: "none" }}>
        {content}
      </Link>
    );
  }
  return (
    <button
      type="button"
      onClick={() => onDrillRootCause(cause.code)}
      style={{ ...baseStyle, textAlign: "left", cursor: "pointer" }}
    >
      {content}
    </button>
  );
}

function RootCauseCardBody({ cause }: { cause: NonNullable<QualityReport["root_causes"]>["top_causes"][number] }) {
  const t = useT();
  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "center" }}>
        <b style={{ fontSize: 12 }}>{cause.label}</b>
        <span style={{ fontFamily: "var(--mono)", color: cause.severity === "block" ? "var(--err)" : "var(--amber-ink)", fontWeight: 800 }}>
          {cause.count}{rootCauseUnit(t, cause.unit)}
        </span>
      </div>
      <div style={{ fontSize: 11.5, color: "var(--ink-3)", lineHeight: 1.35 }}>{cause.action}</div>
      {cause.job_impact_count ? (
        <div style={{ fontSize: 11, color: "var(--ink-3)" }}>{t("rootCause.jobImpact", { count: cause.job_impact_count })}</div>
      ) : null}
      {(cause.case_ids.length > 0 || cause.job_ids.length > 0) && (
        <div style={{ display: "flex", gap: 5, flexWrap: "wrap" }}>
          {cause.case_ids.slice(0, 3).map((id) => <span className="badge" key={`c-${id}`}>{t("rootCause.caseTag", { id })}</span>)}
          {cause.job_ids.slice(0, 2).map((id) => <span className="badge" key={`j-${id}`}>{t("rootCause.jobTag", { id })}</span>)}
        </div>
      )}
    </>
  );
}

function ClassificationBlockerQueuePanel({ groups, loading }: { groups: ImageWorkbenchCaseGroup[]; loading: boolean }) {
  const t = useT();
  const topGroups = groups.slice(0, 5);
  if (loading) {
    return <div className="empty" style={{ padding: 10 }}>{t("blockerQueue.loading")}</div>;
  }
  if (!topGroups.length) return null;
  return (
    <div style={{ display: "grid", gap: 8 }}>
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 8 }}>
        <div style={{ fontSize: 12, fontWeight: 700 }}>{t("blockerQueue.title")}</div>
        <Link to="/images?status=review_needed" style={{ fontSize: 11, color: "var(--accent)" }}>{t("blockerQueue.viewAll")}</Link>
      </div>
      <div style={{ display: "grid", gap: 8 }}>
        {topGroups.map((group) => {
          const missingSlots = group.missing_slots
            .slice(0, 3)
            .map((slot) =>
              t("blockerQueue.missingSlot", {
                label: slot.label,
                roles: slot.missing.map((r) => roleLabel(t, r)).join(t("blockerQueue.rolesJoin")),
              }),
            )
            .join(t("blockerQueue.missingJoin"));
          return (
            <div
              key={group.case_id}
              style={{
                border: "1px solid var(--line)",
                borderRadius: 6,
                padding: 10,
                display: "grid",
                gap: 8,
                background: group.preflight_status === "blocked" ? "rgba(239,68,68,.03)" : "var(--panel)",
              }}
            >
              <div style={{ display: "flex", gap: 8, justifyContent: "space-between", alignItems: "start", flexWrap: "wrap" }}>
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontWeight: 700, fontSize: 12 }}>{t("blockerQueue.caseTitle", { id: group.case_id, title: compactTitle(t, group.case_title) })}</div>
                  <div style={{ fontSize: 11, color: "var(--ink-3)", marginTop: 2 }}>{group.next_action}</div>
                </div>
                <span className="badge">{t("blockerQueue.ready", { score: group.readiness_score })}</span>
              </div>
              <div style={{ display: "flex", gap: 5, flexWrap: "wrap", fontSize: 11 }}>
                <span className="badge">{t("blockerQueue.needsManual", { count: group.needs_manual_count })}</span>
                <span className="badge">{t("blockerQueue.missingPhase", { count: group.missing_phase_count })}</span>
                <span className="badge">{t("blockerQueue.missingView", { count: group.missing_view_count })}</span>
                <span className="badge">{t("blockerQueue.lowConfidence", { count: group.low_confidence_count })}</span>
                <span className="badge">{t("blockerQueue.safeConfirm", { count: group.safe_confirm_count })}</span>
                {group.processing_mode_label && <span className="badge">{group.processing_mode_label}</span>}
              </div>
              {missingSlots ? <div style={{ fontSize: 11.5, color: "var(--ink-3)" }}>{missingSlots}</div> : null}
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                {group.hard_blockers.slice(0, 2).map((blocker) => (
                  <span className="badge warn" key={`${group.case_id}-${blocker.code}`}>{blocker.label || blocker.code}</span>
                ))}
              </div>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                <Link className="btn sm primary" to={group.queue_url}>{group.processing_mode === "source_fix" ? t("blockerQueue.actionFixSource") : t("blockerQueue.actionFixBlocker")}</Link>
                <Link className="btn sm" to={`/cases/${group.case_id}#source-group-preflight`}>{t("blockerQueue.viewPreflight")}</Link>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function DrillChip({ label, value, onClick }: { label: string; value: number | string; onClick: () => void }) {
  return (
    <button
      className="btn sm"
      type="button"
      onClick={onClick}
      style={{ minHeight: 26, fontSize: 12 }}
    >
      {label} <span style={{ fontFamily: "var(--mono)" }}>{value}</span>
    </button>
  );
}

function MetricTile({ label, value, tone }: { label: string; value: number | string; tone?: "ok" | "warn" }) {
  const color = tone === "ok" ? "var(--ok)" : tone === "warn" ? "var(--amber-ink)" : "var(--ink-1)";
  return (
    <div style={{ border: "1px solid var(--line)", borderRadius: 6, padding: 10, background: "var(--panel)" }}>
      <div style={{ fontSize: 11, color: "var(--ink-3)" }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 750, color, fontFamily: "var(--mono)" }}>{value}</div>
    </div>
  );
}

function AiPolicyPanel({
  loading,
  error,
  draft,
  saving,
  previewing,
  preview,
  onChange,
  onPreview,
  onSave,
}: {
  loading: boolean;
  error: boolean;
  draft: AiReviewPolicy | null;
  saving: boolean;
  previewing: boolean;
  preview: AiReviewPolicyPreview | null;
  onChange: (draft: AiReviewPolicy) => void;
  onPreview: () => void;
  onSave: () => void;
}) {
  const t = useT();
  if (loading) return <div className="empty">{t("policyPanel.loading")}</div>;
  if (error || !draft) return <div className="empty">{t("policyPanel.loadError")}</div>;
  const updateThreshold = (key: string, value: string) => {
    const number = Number(value);
    if (!Number.isFinite(number) || number < 0) return;
    onChange({ ...draft, thresholds: { ...draft.thresholds, [key]: number } });
  };
  const updatedAtLabel = draft.updated_at
    ? new Date(draft.updated_at).toLocaleString("zh-CN", { hour12: false })
    : t("policyPanel.defaultUpdated");
  return (
    <section className="card">
      <div className="card-b" style={{ display: "grid", gap: 12 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, flexWrap: "wrap" }}>
          <div>
            <div style={{ fontSize: 14, fontWeight: 700 }}>{t("policyPanel.title")}</div>
            <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
              {draft.name} v{draft.version}{t("policyPanel.metaSeparator")}{updatedAtLabel}
            </div>
          </div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            <button className="btn sm" type="button" onClick={onPreview} disabled={previewing || saving}>
              <Ico name="eye" size={12} />
              {previewing ? t("policyPanel.previewing") : t("policyPanel.preview")}
            </button>
            <button className="btn sm primary" type="button" onClick={onSave} disabled={saving}>
              <Ico name="check" size={12} />
              {saving ? t("policyPanel.saving") : t("policyPanel.save")}
            </button>
          </div>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(210px, 1fr))", gap: 8 }}>
          {Object.entries(draft.thresholds).map(([key, value]) => (
            <label key={key} style={{ display: "grid", gap: 4, fontSize: 12, color: "var(--ink-2)" }}>
              <span>{policyThresholdLabel(t, key)}</span>
              <input
                value={String(value)}
                inputMode="decimal"
                onChange={(e) => updateThreshold(key, e.target.value)}
                style={{ height: 30, border: "1px solid var(--line)", borderRadius: 6, padding: "0 8px" }}
              />
            </label>
          ))}
        </div>
        {preview && <PolicyPreviewPanel preview={preview} />}
      </div>
    </section>
  );
}

function PolicyPreviewPanel({ preview }: { preview: AiReviewPolicyPreview }) {
  const t = useT();
  const byPreview = preview.summary.by_preview;
  const transitions = Object.entries(preview.summary.changed_transitions);
  return (
    <div style={{ border: "1px solid var(--line)", borderRadius: 6, padding: 10, display: "grid", gap: 10, background: "var(--bg-1)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
        <div>
          <div style={{ fontSize: 13, fontWeight: 700 }}>{t("policyPreview.title")}</div>
          <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
            {t("policyPreview.subtitle", { total: preview.summary.total })}
          </div>
        </div>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          <span className="badge">{t("policyPreview.changed", { count: preview.summary.changed_count })}</span>
          <span className="badge">{t("policyPreview.manualOverride", { count: preview.summary.manual_override_count })}</span>
          <span className="badge">{t("policyPreview.newConflict", { count: preview.summary.review_conflict_count })}</span>
        </div>
      </div>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", fontSize: 12 }}>
        <span className="badge">{t("policyPreview.approved", { count: byPreview.approved ?? 0 })}</span>
        <span className="badge">{t("policyPreview.needsRecheck", { count: byPreview.needs_recheck ?? 0 })}</span>
        <span className="badge">{t("policyPreview.rejected", { count: byPreview.rejected ?? 0 })}</span>
      </div>
      {transitions.length > 0 && (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", fontSize: 12, color: "var(--ink-3)" }}>
          {transitions.map(([key, value]) => {
            const [from, to] = key.split("->");
            return (
              <span key={key} className="badge">
                {t("policyPreview.transitionValue", {
                  from: recommendationLabel(t, from) || from,
                  to: recommendationLabel(t, to) || to,
                  count: value,
                })}
              </span>
            );
          })}
        </div>
      )}
      {preview.items.length > 0 && (
        <div style={{ display: "grid", gap: 6 }}>
          {preview.items.slice(0, 6).map((item) => (
            <div
              key={item.id}
              style={{
                display: "grid",
                gridTemplateColumns: "minmax(0, 1fr) auto",
                gap: 8,
                alignItems: "center",
                fontSize: 12,
                borderTop: "1px solid var(--line)",
                paddingTop: 6,
              }}
            >
              <div style={{ minWidth: 0 }}>
                <span style={{ fontWeight: 700 }}>{t("policyPreview.itemTitle", { id: item.id })}</span>
                <span style={{ color: "var(--ink-3)" }}>
                  {t("policyPreview.itemMeta", {
                    customer: item.customer_raw ?? t("policyPreview.unboundCustomer"),
                    reviewStatus: item.review_status ? statusLabel(t, item.review_status) || item.review_status : t("policyPreview.unreviewed"),
                  })}
                </span>
              </div>
              <span className="badge" style={{ justifySelf: "end" }}>
                {t("policyPreview.verdictTransition", {
                  from: recommendationLabel(t, item.current.recommended_verdict) || item.current.recommended_verdict,
                  to: recommendationLabel(t, item.preview.recommended_verdict) || item.preview.recommended_verdict,
                })}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function RenderQueue({
  loading,
  error,
  items,
  reviewing,
  onReview,
}: {
  loading: boolean;
  error: boolean;
  items: RenderQualityQueueItem[];
  reviewing: boolean;
  onReview: (item: RenderQualityQueueItem, verdict: ReviewVerdict) => void;
}) {
  const t = useT();
  if (loading) return <div className="empty">{t("renderQueue.loading")}</div>;
  if (error) return <div className="empty">{t("renderQueue.loadError")}</div>;
  if (items.length === 0) return <div className="empty">{t("renderQueue.empty")}</div>;
  return (
    <div style={{ display: "grid", gap: 12 }}>
      {items.map((item) => (
        <RenderQualityItem
          key={item.job.id}
          item={item}
          reviewing={reviewing}
          onReview={onReview}
        />
      ))}
    </div>
  );
}

function SimulationQueue({
  loading,
  error,
  items,
  reviewing,
  onReview,
}: {
  loading: boolean;
  error: boolean;
  items: SimulationQualityQueueItem[];
  reviewing: boolean;
  onReview: (item: SimulationQualityQueueItem, verdict: ReviewVerdict) => void;
}) {
  const t = useT();
  if (loading) return <div className="empty">{t("simQueue.loading")}</div>;
  if (error) return <div className="empty">{t("simQueue.loadError")}</div>;
  if (items.length === 0) return <div className="empty">{t("simQueue.empty")}</div>;
  return (
    <div style={{ display: "grid", gap: 12 }}>
      {items.map((item) => (
        <SimulationQualityItem
          key={item.job.id}
          item={item}
          reviewing={reviewing}
          onReview={onReview}
        />
      ))}
    </div>
  );
}

function RenderQualityItem({
  item,
  reviewing,
  onReview,
}: {
  item: RenderQualityQueueItem;
  reviewing: boolean;
  onReview: (item: RenderQualityQueueItem, verdict: ReviewVerdict) => void;
}) {
  const t = useT();
  const job = item.job;
  const tone = renderTone(job);
  const quality = job.quality;
  const previewable = !!job.output_path && job.status !== "failed";
  const publishable = !!quality?.can_publish;
  const canApprove = item.reviewable && previewable;
  const firstIssues = item.issue_summary.length > 0 ? item.issue_summary : job.blocking_issues ?? [];
  const firstWarnings = item.warning_summary;
  const actions = item.action_summary ?? [];
  const audit = job.delivery_audit;
  const previewUrl = previewable ? renderJobOutputUrl(item.case.id, job, item.case.abs_path) : undefined;
  const issueTargets = firstWarnings
    .map((warning) => ({ warning, target: renderIssueTarget(warning) }))
    .filter((it): it is { warning: string; target: NonNullable<ReturnType<typeof renderIssueTarget>> } => it.target != null)
    .slice(0, 3);

  const customerLabel = item.case.customer_canonical ?? item.case.customer_raw ?? t("renderItem.customerUnbound");
  const finishedAt = job.finished_at
    ? new Date(job.finished_at).toLocaleString("zh-CN", { hour12: false })
    : t("renderItem.noFinishedAt");

  return (
    <section className="card">
      <div className="card-b" style={{ display: "grid", gridTemplateColumns: "156px minmax(0, 1fr) auto", gap: 14, alignItems: "start" }}>
        <PreviewBox
          href={previewUrl}
          src={previewUrl}
          alt={`render job ${job.id}`}
        />
        <div style={{ minWidth: 0, display: "grid", gap: 8 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <Link to={`/cases/${item.case.id}`} style={{ fontSize: 14, fontWeight: 600, color: "var(--ink-1)" }}>
              {caseTitle(t, item.case.abs_path)}
            </Link>
            <span className="badge" style={{ background: tone.bg, color: tone.ink, borderColor: tone.border }}>
              {statusLabel(t, quality?.quality_status ?? job.status) || quality?.quality_status || job.status}
              {quality?.quality_score != null && <span style={{ fontFamily: "var(--mono)" }}>{Math.round(quality.quality_score)}</span>}
            </span>
            <span className="badge">{t("renderItem.brandTemplate", { brand: job.brand, template: job.template })}</span>
            <span className="badge">{publishable ? t("renderItem.publishable") : t("renderItem.notPublishable")}</span>
            {quality?.review_verdict && (
              <span className="badge">{t("renderItem.reviewBadge", { status: statusLabel(t, quality.review_verdict) || quality.review_verdict })}</span>
            )}
          </div>
          <MetaLine
            parts={[
              t("renderItem.customer", { name: customerLabel }),
              t("renderItem.renderJob", { id: job.id }),
              finishedAt,
            ]}
          />
          <PathLine text={item.case.abs_path} />
          {audit && (
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", fontSize: 12 }}>
              {audit.selected_slots.length > 0 && <span className="badge">{t("renderItem.selected", { slots: audit.selected_slots.join(t("renderItem.selectedJoin")) })}</span>}
              {audit.dropped_slots.length > 0 && <span className="badge">{t("renderItem.dropped", { count: audit.dropped_slots.length })}</span>}
              {audit.source_manifest_hash && <span className="badge" title={audit.source_manifest_hash}>{t("renderItem.manifestAudit")}</span>}
            </div>
          )}
          <IssueLines issues={firstIssues} warnings={firstWarnings} />
          {actions.length > 0 && (
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", fontSize: 12 }}>
              {actions.slice(0, 4).map((action) => (
                <span className="badge" key={`${job.id}-${action.code}`}>{t("renderItem.actionTag", { label: action.label })}</span>
              ))}
            </div>
          )}
          {issueTargets.length > 0 && (
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", fontSize: 12 }}>
              {issueTargets.map(({ warning, target }) => {
                const slotLabel =
                  target.slot === "oblique"
                    ? t("renderItem.slotLabels.oblique")
                    : target.slot === "side"
                      ? t("renderItem.slotLabels.side")
                      : t("renderItem.slotLabels.front");
                return (
                  <Link
                    className="btn sm"
                    key={`${job.id}-${target.slot}-${target.code}-${warning}`}
                    to={`/cases/${item.case.id}?source_group_focus=${target.slot}&issue_code=${target.code}&issue_text=${encodeURIComponent(shortText(warning, 80))}#source-group-preflight`}
                  >
                    {t("renderItem.fixSlot", { slot: slotLabel })}
                  </Link>
                );
              })}
            </div>
          )}
        </div>
        <ActionColumn
          reviewable={item.reviewable}
          canApprove={canApprove}
          reviewing={reviewing}
          approveTitle={!canApprove ? t("renderItem.approveTitleNoOutput") : undefined}
          onReview={(verdict) => onReview(item, verdict)}
          caseId={item.case.id}
        />
      </div>
    </section>
  );
}

function SimulationQualityItem({
  item,
  reviewing,
  onReview,
}: {
  item: SimulationQualityQueueItem;
  reviewing: boolean;
  onReview: (item: SimulationQualityQueueItem, verdict: ReviewVerdict) => void;
}) {
  const t = useT();
  const job = item.job;
  const reviewState = job.review_status ?? job.status;
  const tone = statusTone(reviewState);
  const decision = job.review_decision ?? {};
  const decisionUi = decisionTone(decision);
  const imageFile = simulationFile(job, "ai_after_simulation");
  const diff = differenceMetrics(job);
  const previewable = !!imageFile && job.status !== "failed";
  const canApprove = item.reviewable && previewable && job.watermarked && decision.can_approve !== false;
  const firstIssues = item.issue_summary;
  const firstWarnings = item.warning_summary;
  const decisionReasons = [
    ...(decision.blocking_reasons ?? []),
    ...(decision.warning_reasons ?? []),
    ...(decision.passing_reasons ?? []),
  ];
  const focus = job.focus_targets.join(t("simItem.focusJoin")) || t("simItem.focusFallback");
  const customerLabel = item.case?.customer_canonical ?? item.case?.customer_raw ?? t("simItem.customerUnbound");
  const naLabel = t("simItem.diffNa");

  const diffParts: string[] = [];
  if (diff) {
    const targetStr = diff.target == null ? naLabel : diff.target.toFixed(1);
    const p95Str = diff.p95 == null ? "" : t("simItem.diffP95", { p95: diff.p95.toFixed(1) });
    const ratioStr = diff.ratio == null ? "" : t("simItem.diffRatio", { ratio: (diff.ratio * 100).toFixed(1) });
    diffParts.push(
      t("simItem.diffLine", {
        target: targetStr,
        full: diff.full.toFixed(1),
        nonTarget: diff.nonTarget.toFixed(1),
        p95: p95Str,
        ratio: ratioStr,
      }),
    );
  }

  return (
    <section className="card">
      <div className="card-b" style={{ display: "grid", gridTemplateColumns: "156px minmax(0, 1fr) auto", gap: 14, alignItems: "start" }}>
        <PreviewBox
          href={previewable ? simulationJobDirectFileUrl(job.id, "ai_after_simulation") : undefined}
          src={previewable ? simulationJobDirectFileUrl(job.id, "ai_after_simulation") : undefined}
          alt={`simulation job ${job.id}`}
          emptyLabel={t("preview.noAiOutput")}
        />
        <div style={{ minWidth: 0, display: "grid", gap: 8 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            {item.case ? (
              <Link to={`/cases/${item.case.id}`} style={{ fontSize: 14, fontWeight: 600, color: "var(--ink-1)" }}>
                {caseTitle(t, item.case.abs_path)}
              </Link>
            ) : (
              <span style={{ fontSize: 14, fontWeight: 600, color: "var(--ink-1)" }}>{t("case.unbound")}</span>
            )}
            <span className="badge" style={{ background: tone.bg, color: tone.ink, borderColor: tone.border }}>
              {statusLabel(t, reviewState) || reviewState}
            </span>
            <span className="badge">{t("simItem.kindBadge")}</span>
            <span className="badge">{modelName(t, job)}</span>
            <span className="badge">{job.watermarked ? t("simItem.watermarked") : t("simItem.noWatermark")}</span>
            <span className="badge">{job.can_publish ? t("simItem.publishable") : t("simItem.notPublishable")}</span>
            <span className="badge" style={{ background: decisionUi.bg, color: decisionUi.ink, borderColor: decisionUi.border }}>
              {decision.label ?? t("simItem.decisionPlaceholder")}
            </span>
            {diff && <span className="badge">{t("simItem.diff", { full: diff.full.toFixed(1), nonTarget: diff.nonTarget.toFixed(1) })}</span>}
          </div>
          <MetaLine
            parts={[
              t("simItem.customer", { name: customerLabel }),
              t("simItem.simJob", { id: job.id }),
              t("simItem.focus", { focus }),
              ...diffParts,
              new Date(job.updated_at).toLocaleString("zh-CN", { hour12: false }),
            ]}
          />
          <PathLine text={item.case?.abs_path ?? t("simItem.standalone")} />
          <SimulationFileStrip job={job} />
          {decisionReasons.length > 0 && (
            <div style={{ display: "grid", gap: 4 }}>
              {decisionReasons.slice(0, 4).map((reason, idx) => (
                <div key={`${job.id}-decision-${idx}`} style={{ fontSize: 12, color: idx < (decision.blocking_reasons?.length ?? 0) ? "var(--err)" : "var(--ink-3)" }}>
                  {reason}
                </div>
              ))}
            </div>
          )}
          <IssueLines issues={firstIssues} warnings={firstWarnings} />
        </div>
        <ActionColumn
          reviewable={item.reviewable}
          canApprove={canApprove}
          reviewing={reviewing}
          approveTitle={!canApprove ? t("simItem.approveTitleNoApprove") : undefined}
          onReview={(verdict) => onReview(item, verdict)}
          caseId={item.case?.id ?? null}
        />
      </div>
    </section>
  );
}

function SimulationFileStrip({ job }: { job: SimulationJob }) {
  const t = useT();
  const specs = [
    { kind: "original_after", label: t("simulationFiles.labels.original_after") },
    { kind: "ai_after_simulation", label: t("simulationFiles.labels.ai_after_simulation") },
    { kind: "difference_heatmap", label: t("simulationFiles.labels.difference_heatmap") },
    { kind: "controlled_policy_comparison", label: t("simulationFiles.labels.controlled_policy_comparison") },
    { kind: "before_reference", label: t("simulationFiles.labels.before_reference") },
  ];
  const files = specs.filter((spec) => simulationFile(job, spec.kind));
  if (files.length === 0) {
    return (
      <div style={{ fontSize: 12, color: "var(--err)" }}>
        {t("simulationFiles.noFiles")}
      </div>
    );
  }
  return (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
      {files.map((file) => (
        <SimulationMiniPreview
          key={file.kind}
          label={file.label}
          href={simulationJobDirectFileUrl(job.id, file.kind)}
          src={simulationJobDirectFileUrl(job.id, file.kind)}
        />
      ))}
    </div>
  );
}

function SimulationMiniPreview({ label, href, src }: { label: string; href: string; src: string }) {
  const t = useT();
  const [failed, setFailed] = useState(false);
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      style={{
        width: 92,
        display: "grid",
        gap: 4,
        color: "var(--ink-2)",
        textDecoration: "none",
      }}
    >
      <span
        style={{
          width: 92,
          height: 68,
          border: "1px solid var(--line)",
          borderRadius: 6,
          overflow: "hidden",
          display: "grid",
          placeItems: "center",
          background: "var(--bg-2)",
          color: failed ? "var(--err)" : "var(--ink-4)",
        }}
      >
        {failed ? (
          <span style={{ fontSize: 10, textAlign: "center", padding: 6 }}>{t("preview.loadFailed")}</span>
        ) : (
          <img
            src={src}
            alt={label}
            loading="lazy"
            onError={() => setFailed(true)}
            style={{ width: "100%", height: "100%", objectFit: "contain", display: "block" }}
          />
        )}
      </span>
      <span style={{ fontSize: 11, textAlign: "center", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
        {label}
      </span>
    </a>
  );
}

function PreviewBox({ href, src, alt, emptyLabel }: { href?: string; src?: string; alt: string; emptyLabel?: string }) {
  const t = useT();
  const [failed, setFailed] = useState(false);
  const fallback = emptyLabel ?? t("preview.noOutput");
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      style={{
        width: 156,
        minHeight: 104,
        border: "1px solid var(--line)",
        borderRadius: 7,
        overflow: "hidden",
        display: "grid",
        placeItems: "center",
        background: "var(--bg-2)",
        color: "var(--ink-4)",
        textDecoration: "none",
      }}
    >
      {src && !failed ? (
        <img
          src={src}
          alt={alt}
          onError={() => setFailed(true)}
          style={{ width: "100%", height: 104, objectFit: "contain", display: "block" }}
        />
      ) : (
        <span style={{ display: "grid", gap: 4, justifyItems: "center", fontSize: 11 }}>
          <Ico name="image" size={18} />
          {failed ? t("preview.imageLoadFailed") : fallback}
        </span>
      )}
    </a>
  );
}

function MetaLine({ parts }: { parts: string[] }) {
  return (
    <div style={{ fontSize: 12, color: "var(--ink-3)", display: "flex", gap: 10, flexWrap: "wrap" }}>
      {parts.map((part) => <span key={part}>{part}</span>)}
    </div>
  );
}

function PathLine({ text }: { text: string }) {
  return (
    <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--ink-4)", wordBreak: "break-all" }}>
      {text}
    </div>
  );
}

function IssueLines({ issues, warnings }: { issues: string[]; warnings: string[] }) {
  if (issues.length === 0 && warnings.length === 0) return null;
  return (
    <div style={{ display: "grid", gap: 5 }}>
      {issues.slice(0, 3).map((issue, idx) => (
        <div key={`issue-${idx}`} title={issue} style={{ color: "var(--err)", fontSize: 12 }}>
          <Ico name="alert" size={11} /> {shortText(issue)}
        </div>
      ))}
      {warnings.slice(0, 3).map((warning, idx) => (
        <div key={`warning-${idx}`} title={warning} style={{ color: "var(--amber-ink)", fontSize: 12 }}>
          <Ico name="alert" size={11} /> {shortText(warning)}
        </div>
      ))}
    </div>
  );
}

function ActionColumn({
  reviewable,
  canApprove,
  reviewing,
  approveTitle,
  onReview,
  caseId,
}: {
  reviewable: boolean;
  canApprove: boolean;
  reviewing: boolean;
  approveTitle?: string;
  onReview: (verdict: ReviewVerdict) => void;
  caseId: number | null;
}) {
  const t = useT();
  return (
    <div style={{ display: "grid", gap: 6, justifyItems: "stretch", minWidth: 104 }}>
      {caseId ? (
        <Link className="btn sm" to={`/cases/${caseId}`}>
          <Ico name="link" size={11} />
          {t("actions.openCase")}
        </Link>
      ) : (
        <span className="btn sm" style={{ opacity: 0.5, pointerEvents: "none" }}>
          <Ico name="link" size={11} />
          {t("actions.noCase")}
        </span>
      )}
      <button
        className="btn sm primary"
        disabled={!canApprove || reviewing}
        onClick={() => onReview("approved")}
        title={approveTitle}
      >
        <Ico name="check" size={11} />
        {t("actions.approve")}
      </button>
      <button
        className="btn sm"
        disabled={!reviewable || reviewing}
        onClick={() => onReview("needs_recheck")}
      >
        <Ico name="recheck" size={11} />
        {t("actions.recheck")}
      </button>
      <button
        className="btn sm danger"
        disabled={!reviewable || reviewing}
        onClick={() => onReview("rejected")}
      >
        <Ico name="x" size={11} />
        {t("actions.reject")}
      </button>
    </div>
  );
}
