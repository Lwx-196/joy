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
import { renderOutputUrl, type RenderJob } from "../api";
import {
  useCancelRenderJob,
  useJobStream,
  useLatestCaseRenderJob,
  useRenderCase,
} from "../hooks/queries";
import { useBrand } from "../lib/brand-context";
import { useUndoStore } from "../lib/undo-toast";
import { Ico } from "./atoms";

interface Props {
  caseId: number;
  /** Override the global brand (e.g., when the user opens the card from a per-case dropdown later). */
  brand?: string;
}

const STATUS_BG: Record<string, { bg: string; fg: string }> = {
  queued: { bg: "var(--bg-2)", fg: "var(--ink-2)" },
  running: { bg: "var(--bg-2)", fg: "var(--ink-2)" },
  done: { bg: "var(--ok-50, #DCFCE7)", fg: "var(--ok)" },
  failed: { bg: "var(--err-50)", fg: "var(--err)" },
  cancelled: { bg: "var(--bg-2)", fg: "var(--ink-3)" },
  undone: { bg: "var(--bg-2)", fg: "var(--ink-3)" },
};

function formatDuration(startIso: string | null, endIso: string | null): string {
  if (!startIso) return "—";
  const start = new Date(startIso).getTime();
  const end = endIso ? new Date(endIso).getTime() : Date.now();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return "—";
  const sec = Math.round((end - start) / 1000);
  if (sec < 60) return `${sec}s`;
  return `${Math.floor(sec / 60)}m${sec % 60}s`;
}

export function RenderStatusCard({ caseId, brand: brandOverride }: Props) {
  const { t } = useTranslation("render");
  const globalBrand = useBrand();
  const brand = brandOverride || globalBrand;
  const { data: job, refetch } = useLatestCaseRenderJob(caseId);
  const cancelMut = useCancelRenderJob();
  const renderMut = useRenderCase();
  const pushUndo = useUndoStore((s) => s.push);

  const statusLabelMap: Record<string, string> = {
    queued: t("status.queued"),
    running: t("status.running"),
    done: t("status.done"),
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
      if (ev.status === "done" && ev.job_id != null && !toastedRef.current.has(ev.job_id)) {
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
    if (!job || job.status !== "done") return null;
    return renderOutputUrl(caseId, job.brand, job.template, job.output_mtime);
  }, [job, caseId]);

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

  return (
    <div style={cardStyle} data-testid="render-status-card">
      <header style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
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
        {status === "done" && (
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
      {status === "done" && previewUrl && (
        <>
          <div style={{ display: "flex", gap: 12, alignItems: "flex-start" }}>
            <a
              href={previewUrl}
              target="_blank"
              rel="noreferrer"
              title={t("thumbnailTitle")}
              style={{
                display: "block",
                width: 120,
                flexShrink: 0,
                border: "1px solid var(--line)",
                borderRadius: 4,
                overflow: "hidden",
                background: "var(--bg-2)",
              }}
            >
              <img
                src={previewUrl}
                alt="final-board"
                style={{ width: "100%", display: "block" }}
              />
            </a>
            <div style={{ flex: 1, minWidth: 0, fontSize: 12 }}>
              <RenderSummary job={job} />
            </div>
          </div>
          <RenderBlockingDetail job={job} />
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
  return (
    <div>
      <div>
        <span style={{ color: "var(--ink-3)" }}>{t("skill.label")}</span>
        <span
          style={{
            fontFamily: "var(--mono)",
            color: skillStatus === "ok" ? "var(--ok)" : "var(--amber, #B45309)",
          }}
        >
          {skillStatus}
        </span>
        <span style={{ color: "var(--ink-4)", marginLeft: 8 }}>
          {tplList}
        </span>
      </div>
      <div style={{ color: "var(--ink-3)", fontSize: 11.5, marginTop: 2 }}>
        {t("skill.block")} <b style={{ color: blocking > 0 ? "var(--err)" : "var(--ink-2)" }}>{blocking}</b> ·
        {t("skill.warn")} <b style={{ color: warnings > 0 ? "var(--amber, #B45309)" : "var(--ink-2)" }}>{warnings}</b>
      </div>
      {job.output_path && (
        <div
          style={{
            color: "var(--ink-4)",
            fontFamily: "var(--mono)",
            fontSize: 10.5,
            marginTop: 4,
            wordBreak: "break-all",
          }}
        >
          {job.output_path}
        </div>
      )}
    </div>
  );
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
  const blocks = job.blocking_issues ?? [];
  const warns = job.warnings ?? [];
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
                  {t("detail.warningsTitle", { count: warns.length })}
                </span>
              </button>
              {warnExpanded && (
                <ul style={{ paddingLeft: 16, margin: 0, color: "var(--ink-3)", maxHeight: 220, overflowY: "auto" }}>
                  {warns.map((s, i) => (
                    <li key={i} style={{ marginBottom: 2, wordBreak: "break-all" }}>{s}</li>
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
  padding: "10px 12px",
  marginTop: 12,
  fontSize: 12,
};
