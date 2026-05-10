/**
 * Single-case render status card.
 *
 * Mounted on CaseDetail just below the action toolbar. Shows the latest render
 * job for the current case and reacts in real time to SSE events.
 *
 * Lifecycle states it has to render:
 *   - no job yet                   → null (caller hides the card)
 *   - queued                       → spinner + "排队中"
 *   - running                      → spinner + "渲染中" + elapsed seconds
 *   - done                         → thumbnail + brand/template + finished_at
 *   - failed                       → error message + 重试 button
 *   - cancelled                    → "已取消" + 重新开始 button
 *
 * On `done` events for the watched case, push an undo toast so the user gets
 * the same 30s ⌘Z window we have for v1.5 mutations.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { renderJobOutputUrl, type CompositionAlert, type RenderJob } from "../api";
import {
  useCancelRenderJob,
  useJobStream,
  useLatestCaseRenderJob,
  useRevealCasePath,
  useRenderCase,
} from "../hooks/queries";
import { useBrand } from "../lib/brand-context";
import { useUndoStore } from "../lib/undo-toast";
import { Ico } from "./atoms";

interface Props {
  caseId: number;
  /** Override the global brand (e.g., when the user opens the card from a per-case dropdown later). */
  brand?: string;
  /** Absolute case directory, used for local reveal/copy helpers. */
  caseAbsPath?: string;
}

const STATUS_BG: Record<string, { bg: string; fg: string }> = {
  queued: { bg: "var(--bg-2)", fg: "var(--ink-2)" },
  running: { bg: "var(--bg-2)", fg: "var(--ink-2)" },
  done: { bg: "var(--ok-50, #DCFCE7)", fg: "var(--ok)" },
  done_with_issues: { bg: "var(--amber-50)", fg: "var(--amber-ink)" },
  blocked: { bg: "var(--err-50)", fg: "var(--err)" },
  failed: { bg: "var(--err-50)", fg: "var(--err)" },
  cancelled: { bg: "var(--bg-2)", fg: "var(--ink-3)" },
  undone: { bg: "var(--bg-2)", fg: "var(--ink-3)" },
};

const EMPTY_MESSAGES: string[] = [];

type WarningGroup = {
  key: string;
  count: number;
  samples: string[];
  raw: string[];
};

function warningSampleName(prefix: string): string {
  const parts = prefix.split(/[：:]/);
  return (parts[parts.length - 1] || prefix).trim();
}

function groupWarnings(warnings: string[]): WarningGroup[] {
  const groups = new Map<string, WarningGroup>();
  for (const warning of warnings) {
    const match = warning.match(/^(.*?)\s+-\s+(.+)$/);
    const prefix = match?.[1]?.trim() ?? "";
    const detail = match?.[2]?.trim() ?? warning;
    const hasImagePrefix = /\.(jpe?g|png|webp)$/i.test(prefix);
    const key = hasImagePrefix ? detail : warning;
    const sample = hasImagePrefix ? warningSampleName(prefix) : "";
    const existing = groups.get(key) ?? { key, count: 0, samples: [], raw: [] };
    existing.count += 1;
    if (sample && existing.samples.length < 6) existing.samples.push(sample);
    existing.raw.push(warning);
    groups.set(key, existing);
  }
  return Array.from(groups.values()).sort((a, b) => b.count - a.count);
}

function formatDuration(startIso: string | null, endIso: string | null): string {
  if (!startIso) return "—";
  const start = new Date(startIso).getTime();
  const end = endIso ? new Date(endIso).getTime() : Date.now();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return "—";
  const sec = Math.round((end - start) / 1000);
  if (sec < 60) return `${sec}s`;
  return `${Math.floor(sec / 60)}m${sec % 60}s`;
}

export function RenderStatusCard({ caseId, brand: brandOverride, caseAbsPath }: Props) {
  const { t } = useTranslation("render");
  const globalBrand = useBrand();
  const brand = brandOverride || globalBrand;
  const { data: job, refetch } = useLatestCaseRenderJob(caseId);
  const cancelMut = useCancelRenderJob();
  const renderMut = useRenderCase();
  const revealMut = useRevealCasePath();
  const pushUndo = useUndoStore((s) => s.push);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [failedPreviewUrl, setFailedPreviewUrl] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);

  const statusLabelMap: Record<string, string> = {
    queued: t("status.queued"),
    running: t("status.running"),
    done: t("status.done"),
    done_with_issues: t("status.doneWithIssues"),
    blocked: t("status.blocked"),
    failed: t("status.failed"),
    cancelled: t("status.cancelled"),
    undone: t("status.undone"),
  };

  // Re-render every 1s while running so the elapsed counter advances.
  const [, setTick] = useState(0);
  useEffect(() => {
    if (job?.status !== "running" && job?.status !== "queued") return;
    const t = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [job?.status]);

  // SSE: when a `done` event arrives for THIS case, open undo toast.
  // We track which job ids we've already toasted to avoid double-toasting on
  // initial fetch + SSE replay.
  const toastedRef = useRef<Set<number>>(new Set());
  useJobStream({
    jobType: "render",
    onEvent: (ev) => {
      if (ev.case_id !== caseId) return;
      if ((ev.status === "done" || ev.status === "done_with_issues") && ev.job_id != null && !toastedRef.current.has(ev.job_id)) {
        toastedRef.current.add(ev.job_id);
        pushUndo({
          caseIds: [caseId],
          label: t("messages.undoLabel"),
          kind: "render",
        });
      }
      // Refresh latest-job query as a defensive fallback (the hook also invalidates).
      refetch();
    },
  });

  const previewUrl = useMemo(() => {
    if (!job || (job.status !== "done" && job.status !== "done_with_issues")) return null;
    return renderJobOutputUrl(caseId, job, caseAbsPath);
  }, [job, caseId, caseAbsPath]);

  const imageLoadFailed = previewUrl != null && failedPreviewUrl === previewUrl;

  if (!job) {
    return (
      <div
        style={cardStyle}
        data-testid="render-status-card-empty"
      >
        <span style={{ color: "var(--ink-4)" }}>
          {t("empty", { brandName: brand === "shimei" ? t("brand.shimei") : t("brand.fumei") })}
        </span>
      </div>
    );
  }

  const status = job.status;
  const statusLabel = statusLabelMap[status] ?? status;
  const isPending = status === "queued" || status === "running";
  const outputDir = job.output_path ? job.output_path.replace(/\/[^/]+$/, "") : null;
  const copyText = async (text: string, message: string) => {
    await navigator.clipboard.writeText(text);
    setActionMessage(message);
    window.setTimeout(() => setActionMessage(null), 1800);
  };
  const revealPath = async (target: "case_root" | "render_output") => {
    const fallbackPath = target === "case_root" ? caseAbsPath : outputDir;
    try {
      const result = await revealMut.mutateAsync({
        caseId,
        payload: { target, brand: job.brand, template: job.template },
      });
      setActionMessage(t("messages.openedPath", { path: result.path }));
    } catch {
      if (fallbackPath) {
        await copyText(fallbackPath, t("messages.openFallbackCopied"));
        return;
      }
      setActionMessage(t("messages.openFailed"));
    }
    window.setTimeout(() => setActionMessage(null), 1800);
  };

  return (
    <div style={cardStyle} data-testid="render-status-card">
      <header style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6, flexWrap: "wrap" }}>
        <Ico name="image" size={14} />
        <strong style={{ fontSize: 13 }}>{t("title")}</strong>
        <span
          style={{
            fontFamily: "var(--mono)",
            fontSize: 11,
            color: "var(--ink-3)",
            background: "var(--bg-2)",
            border: "1px solid var(--line)",
            borderRadius: 4,
            padding: "1px 6px",
          }}
        >
          {job.brand} · {job.template}
        </span>
        <span
          className="badge"
          style={{
            background: STATUS_BG[status]?.bg ?? "var(--bg-2)",
            color: STATUS_BG[status]?.fg ?? "var(--ink-2)",
            fontFamily: "var(--mono)",
            fontSize: 11,
          }}
        >
          {statusLabel}
        </span>
        {isPending && (
          <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--ink-3)" }}>
            {formatDuration(job.started_at, null)}
          </span>
        )}
        {(status === "done" || status === "done_with_issues") && (
          <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--ink-3)" }}>
            {t("elapsed", { duration: formatDuration(job.started_at, job.finished_at) })}
          </span>
        )}
      </header>

      {/* Pending: progress bar */}
      {isPending && (
        <div style={{ marginBottom: 8 }}>
          <div
            style={{
              height: 4,
              borderRadius: 4,
              background: "var(--bg-2)",
              overflow: "hidden",
              position: "relative",
            }}
          >
            <div
              style={{
                position: "absolute",
                top: 0,
                left: 0,
                bottom: 0,
                width: status === "queued" ? "20%" : "60%",
                background: "var(--cyan, #0EA5E9)",
                animation: "indeterminate 1.4s ease-in-out infinite",
              }}
            />
          </div>
          {status === "queued" && (
            <button
              type="button"
              className="btn sm ghost"
              style={{ marginTop: 6 }}
              onClick={() => cancelMut.mutate(job.id)}
              disabled={cancelMut.isPending}
            >
              <Ico name="x" size={11} />
              {t("actions.cancel")}
            </button>
          )}
        </div>
      )}

      {/* Done: thumbnail + path */}
      {(status === "done" || status === "done_with_issues") && previewUrl && (
        <>
          <div className="render-final-layout">
            <button
              type="button"
              onClick={() => setPreviewOpen(true)}
              title={t("thumbnailTitle")}
              className="render-final-preview"
            >
              {!imageLoadFailed ? (
                <img
                  src={previewUrl}
                  alt="final-board"
                  onError={() => setFailedPreviewUrl(previewUrl)}
                />
              ) : (
                <span className="render-final-preview-empty">
                  <Ico name="image" size={20} />
                  {t("messages.previewLoadFailed")}
                </span>
              )}
            </button>
            <div style={{ flex: 1, minWidth: 0, fontSize: 12 }}>
              <RenderSummary job={job} />
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 7 }}>
                <button type="button" className="btn sm" onClick={() => setPreviewOpen(true)}>
                  <Ico name="eye" size={11} />
                  {t("actions.openPreview")}
                </button>
                <button
                  type="button"
                  className="btn sm"
                  onClick={() => job.output_path && copyText(job.output_path, t("messages.outputPathCopied"))}
                  disabled={!job.output_path}
                >
                  <Ico name="copy" size={11} />
                  {t("actions.copyOutputPath")}
                </button>
                <button
                  type="button"
                  className="btn sm"
                  onClick={() => caseAbsPath && copyText(caseAbsPath, t("messages.caseRootCopied"))}
                  disabled={!caseAbsPath}
                >
                  <Ico name="copy" size={11} />
                  {t("actions.copyCaseRoot")}
                </button>
                <button
                  type="button"
                  className="btn sm"
                  onClick={() => revealPath("case_root")}
                  disabled={!caseAbsPath || revealMut.isPending}
                >
                  <Ico name="folder" size={11} />
                  {t("actions.openCaseRoot")}
                </button>
                <button
                  type="button"
                  className="btn sm"
                  onClick={() => revealPath("render_output")}
                  disabled={!outputDir || revealMut.isPending}
                >
                  <Ico name="folder" size={11} />
                  {t("actions.openOutputDir")}
                </button>
                <a className="btn sm ghost" href={previewUrl} target="_blank" rel="noreferrer">
                  <Ico name="link" size={11} />
                  {t("actions.openInNewTab")}
                </a>
              </div>
              {actionMessage && (
                <div style={{ marginTop: 5, color: "var(--ink-3)", fontSize: 11 }} data-testid="render-action-message">
                  {actionMessage}
                </div>
              )}
            </div>
          </div>
          <RenderBlockingDetail job={job} />
          {previewOpen && (
            <div
              role="dialog"
              aria-modal="true"
              aria-label={t("previewModal.title")}
              style={{
                position: "fixed",
                inset: 0,
                zIndex: 80,
                background: "rgba(28,25,23,.74)",
                display: "grid",
                gridTemplateRows: "auto 1fr",
                padding: 18,
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 12,
                  color: "#fff",
                  marginBottom: 10,
                }}
              >
                <div style={{ fontWeight: 700 }}>{t("previewModal.title")}</div>
                <button type="button" className="btn sm" onClick={() => setPreviewOpen(false)}>
                  <Ico name="x" size={11} />
                  {t("previewModal.close")}
                </button>
              </div>
              <div
                style={{
                  minHeight: 0,
                  borderRadius: 8,
                  background: "#fff",
                  overflow: "auto",
                  display: "grid",
                  placeItems: "center",
                  padding: 14,
                }}
              >
                <img
                  src={previewUrl}
                  alt="final-board-large"
                  style={{ maxWidth: "100%", maxHeight: "calc(100vh - 96px)", objectFit: "contain" }}
                />
              </div>
            </div>
          )}
        </>
      )}

      {/* Failed: error + retry */}
      {status === "failed" && (
        <div style={{ fontSize: 12, color: "var(--err)" }}>
          <div
            style={{
              fontFamily: "var(--mono)",
              fontSize: 11,
              padding: 8,
              background: "var(--err-50)",
              borderRadius: 4,
              maxHeight: 80,
              overflow: "auto",
              wordBreak: "break-all",
            }}
          >
            {job.error_message || t("messages.unknownError")}
          </div>
          <div style={{ marginTop: 6 }}>
            <button
              type="button"
              className="btn sm"
              onClick={() =>
                renderMut.mutate({
                  caseId,
                  payload: {
                    brand: job.brand,
                    template: job.template,
                    semantic_judge: (job.semantic_judge === "auto" ? "auto" : "off"),
                  },
                })
              }
              disabled={renderMut.isPending}
            >
              <Ico name="refresh" size={11} />
              {t("actions.retry")}
            </button>
          </div>
          <RenderBlockingDetail job={job} />
        </div>
      )}

      {status === "blocked" && (
        <div style={{ fontSize: 12, color: "var(--err)" }}>
          <RenderSummary job={job} />
          {job.error_message && (
            <div
              style={{
                fontFamily: "var(--mono)",
                fontSize: 11,
                padding: 8,
                background: "var(--err-50)",
                borderRadius: 4,
                maxHeight: 80,
                overflow: "auto",
                wordBreak: "break-all",
                marginTop: 6,
              }}
            >
              {job.error_message}
            </div>
          )}
          <div style={{ marginTop: 6 }}>{t("messages.blockedHint")}</div>
          <button
            type="button"
            className="btn sm"
            style={{ marginTop: 6 }}
            onClick={() =>
              renderMut.mutate({
                caseId,
                payload: {
                  brand: job.brand,
                  template: job.template,
                  semantic_judge: "auto",
                },
              })
            }
            disabled={renderMut.isPending}
            title={t("actions.visionRetryTitle")}
          >
            <Ico name="scan" size={11} />
            {t("actions.visionRetry")}
          </button>
          <RenderBlockingDetail job={job} />
        </div>
      )}

      {/* Cancelled: rerun */}
      {status === "cancelled" && (
        <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
          {t("messages.cancelledHint")}
          <button
            type="button"
            className="btn sm"
            style={{ marginLeft: 8 }}
            onClick={() =>
              renderMut.mutate({
                caseId,
                payload: { brand: job.brand, template: job.template },
              })
            }
            disabled={renderMut.isPending}
          >
            {t("messages.rerunButton")}
          </button>
        </div>
      )}

      {/* Undone: previous render was rolled back */}
      {status === "undone" && (
        <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
          {t("messages.undoneHint")}
          <button
            type="button"
            className="btn sm"
            style={{ marginLeft: 8 }}
            onClick={() =>
              renderMut.mutate({
                caseId,
                payload: {
                  brand: job.brand,
                  template: job.template,
                  semantic_judge: job.semantic_judge === "auto" ? "auto" : "off",
                },
              })
            }
            disabled={renderMut.isPending}
          >
            <Ico name="image" size={11} />
            {t("actions.rerender")}
          </button>
        </div>
      )}
    </div>
  );
}

function RenderSummary({ job }: { job: RenderJob }) {
  const { t } = useTranslation("render");
  const meta = job.meta || {};
  const skillStatus = meta.status || "—";
  const blocking = meta.blocking_issue_count ?? 0;
  const warnings = meta.warning_count ?? 0;
  const tplList = (meta.effective_templates || []).join(" / ") || job.template;
  const compositionAlerts = getCompositionAlerts(job);
  return (
    <div style={{ display: "grid", gap: 6, minWidth: 0 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
        <span className="badge">
          {t("skill.label")}
          <span
            style={{
              fontFamily: "var(--mono)",
              color: skillStatus === "ok" ? "var(--ok)" : "var(--amber, #B45309)",
            }}
          >
            {skillStatus}
          </span>
        </span>
        <span className="badge" style={{ fontFamily: "var(--mono)" }}>{tplList}</span>
        <span className="badge">
          {t("skill.block")}
          <b style={{ color: blocking > 0 ? "var(--err)" : "var(--ink-2)" }}>{blocking}</b>
        </span>
        <span className="badge">
          {t("skill.warn")}
          <b style={{ color: warnings > 0 ? "var(--amber, #B45309)" : "var(--ink-2)" }}>{warnings}</b>
        </span>
        {job.quality && (
          <span className="badge">
            {t("quality.label")}
            <b style={{ color: job.quality.quality_status === "done" ? "var(--ok)" : "var(--amber-ink)" }}>
              {job.quality.quality_status}
            </b>
            <span style={{ fontFamily: "var(--mono)" }}>{Math.round(job.quality.quality_score)}</span>
            <span>{job.quality.can_publish ? t("quality.publishable") : t("quality.reviewRequired")}</span>
          </span>
        )}
      </div>
      {job.output_path && (
        <div
          style={{
            color: "var(--ink-4)",
            fontFamily: "var(--mono)",
            fontSize: 10.5,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
          title={job.output_path}
        >
          {job.output_path}
        </div>
      )}
      {compositionAlerts.length > 0 && (
        <div
          style={{
            padding: "6px 8px",
            border: "1px solid var(--amber-200, #FCD34D)",
            background: "var(--amber-50)",
            borderRadius: 6,
            color: "var(--amber-ink)",
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: 3 }}>{t("composition.title")}</div>
          <ul style={{ paddingLeft: 16, margin: 0 }}>
            {compositionAlerts.slice(0, 3).map((alert, index) => (
              <li key={`${alert.slot || "slot"}-${alert.code || index}`} style={{ marginBottom: 2 }}>
                {alert.message || t("composition.generic")}
              </li>
            ))}
          </ul>
          <div style={{ color: "var(--ink-3)", marginTop: 4 }}>
            {t("composition.action")}
          </div>
        </div>
      )}
    </div>
  );
}

function isCompositionAlert(value: unknown): value is CompositionAlert {
  return Boolean(value && typeof value === "object" && ("message" in value || "code" in value));
}

function getCompositionAlerts(job: RenderJob): CompositionAlert[] {
  const fromQuality = job.quality?.metrics?.composition_alerts;
  if (Array.isArray(fromQuality)) {
    return fromQuality.filter(isCompositionAlert);
  }
  const fromMeta = job.meta?.composition_alerts;
  if (Array.isArray(fromMeta)) {
    return fromMeta.filter(isCompositionAlert);
  }
  return [];
}

/**
 * Stage A: 渲染 manifest.final.json 的 blocking_issues / warnings 字符串列表。
 *
 * - 仅当 job.blocking_issues / warnings 任一非空时显示(failed 状态下后端可能仍能写
 *   manifest;done + block>0 时本来就有完整列表)
 * - 默认折叠,点「展开详情」展开列表 + 折叠 warnings(warnings 总是单独折叠层,因为
 *   通常是逐图警告,17 条够多)
 * - 不调用 issueDict 翻译(后端原始字符串已是中文+具体数字,直接展示更精确)
 */
function RenderBlockingDetail({ job }: { job: RenderJob }) {
  const { t } = useTranslation("render");
  const blocks = job.blocking_issues ?? EMPTY_MESSAGES;
  const warns = job.warnings ?? EMPTY_MESSAGES;
  const warningGroups = useMemo(() => groupWarnings(warns), [warns]);
  const [expanded, setExpanded] = useState(false);
  const [warnExpanded, setWarnExpanded] = useState(false);
  if (blocks.length === 0 && warns.length === 0) return null;
  const toggleLabel = expanded ? t("actions.collapseDetail") : t("actions.expandDetail");
  return (
    <div
      data-testid="render-detail"
      style={{
        marginTop: 8,
        borderTop: "1px solid var(--line-2)",
        paddingTop: 8,
        fontSize: 11.5,
      }}
    >
      <button
        type="button"
        className="btn sm ghost"
        onClick={() => setExpanded((v) => !v)}
        data-testid="render-detail-toggle"
        style={{ marginBottom: expanded ? 6 : 0 }}
      >
        <Ico name={expanded ? "down" : "arrow-r"} size={11} />
        {toggleLabel} · {t("detail.blockingTitle", { count: blocks.length })} · {t("detail.warningsTitle", { count: warns.length })}
      </button>
      {expanded && (
        <div style={{ display: "grid", gap: 6 }}>
          {blocks.length > 0 && (
            <div data-testid="render-detail-blocks">
              <div style={{ fontWeight: 600, color: "var(--err)", marginBottom: 3 }}>
                {t("detail.blockingTitle", { count: blocks.length })}
              </div>
              <ul style={{ paddingLeft: 16, margin: 0, color: "var(--ink-2)" }}>
                {blocks.map((s, i) => (
                  <li key={i} style={{ marginBottom: 2, wordBreak: "break-all" }}>{s}</li>
                ))}
              </ul>
            </div>
          )}
          {warns.length > 0 && (
            <div data-testid="render-detail-warnings">
              <button
                type="button"
                className="btn sm ghost"
                onClick={() => setWarnExpanded((v) => !v)}
                data-testid="render-detail-warnings-toggle"
                style={{ marginBottom: 4 }}
              >
                <Ico name={warnExpanded ? "down" : "arrow-r"} size={11} />
                <span style={{ color: "var(--amber, #B45309)" }}>
                  {t("detail.groupedWarningsTitle", { groups: warningGroups.length, count: warns.length })}
                </span>
              </button>
              {warnExpanded && (
                <ul style={{ paddingLeft: 16, margin: 0, color: "var(--ink-3)", maxHeight: 260, overflowY: "auto" }}>
                  {warningGroups.map((group) => (
                    <li key={group.key} style={{ marginBottom: 6, wordBreak: "break-all" }}>
                      <div>
                        {group.key}
                        {group.count > 1 && (
                          <span style={{ fontFamily: "var(--mono)", color: "var(--amber-ink)", marginLeft: 6 }}>
                            ×{group.count}
                          </span>
                        )}
                      </div>
                      {group.samples.length > 0 && (
                        <div style={{ fontSize: 10.5, color: "var(--ink-4)", marginTop: 2 }}>
                          {t("detail.warningSamples")} {group.samples.join("、")}
                        </div>
                      )}
                      {group.count > 1 && (
                        <details style={{ marginTop: 2 }}>
                          <summary style={{ cursor: "pointer", color: "var(--ink-4)" }}>
                            {t("detail.rawWarningsTitle", { count: group.raw.length })}
                          </summary>
                          <ul style={{ paddingLeft: 14, margin: "4px 0 0" }}>
                            {group.raw.map((s, i) => (
                              <li key={i} style={{ marginBottom: 2 }}>{s}</li>
                            ))}
                          </ul>
                        </details>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const cardStyle: React.CSSProperties = {
  border: "1px solid var(--line)",
  borderRadius: 6,
  background: "var(--panel, #fff)",
  padding: "8px 10px",
  marginTop: 8,
  fontSize: 12,
};
