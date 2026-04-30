/**
 * Unified job batch detail page — handles both render and upgrade batches.
 *
 * Route: /jobs/batches/:batchId?type=render|upgrade  (type defaults to render)
 *
 * For render batches: per-row retry uses useRenderCase (re-enqueues with the
 * same brand/template/semantic_judge), per-row cancel uses useCancelRenderJob.
 * The thumbnail column shows final-board.jpg.
 *
 * For upgrade batches: per-row retry uses useRetryUpgradeJob (server creates a
 * new job with the same case_id + brand), per-row cancel uses
 * useCancelUpgradeJob. The summary column shows category / template_tier /
 * blocking count from upgrade_jobs.meta_json. The page header gets a
 * "撤销整批 v3 升级" button which calls useUndoUpgradeBatch.
 */
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  renderOutputUrl,
  type RenderJob,
  type RenderStatus,
  type UpgradeJob,
  type UpgradeStatus,
} from "../api";
import {
  useCancelRenderJob,
  useCancelUpgradeJob,
  useJobStream,
  useRenderBatch,
  useRenderCase,
  useRetryUpgradeJob,
  useUndoUpgradeBatch,
  useUpgradeBatch,
} from "../hooks/queries";
import { Ico } from "../components/atoms";
import { EvaluateDialog } from "../components/EvaluateDialog";

type AnyStatus = RenderStatus | UpgradeStatus;
type BatchType = "render" | "upgrade";

export default function JobBatch() {
  const { t } = useTranslation("jobBatch");
  const { batchId } = useParams<{ batchId: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const jobType: BatchType =
    searchParams.get("type") === "upgrade" ? "upgrade" : "render";

  // Keep React Query caches fresh from SSE.
  useJobStream({ jobType });

  const renderQ = useRenderBatch(jobType === "render" ? batchId : undefined);
  const upgradeQ = useUpgradeBatch(jobType === "upgrade" ? batchId : undefined);

  const renderRetryMut = useRenderCase();
  const renderCancelMut = useCancelRenderJob();
  const upgradeRetryMut = useRetryUpgradeJob();
  const upgradeCancelMut = useCancelUpgradeJob();
  const undoBatchMut = useUndoUpgradeBatch();

  const [confirmingUndo, setConfirmingUndo] = useState(false);
  const [evaluateJobId, setEvaluateJobId] = useState<number | null>(null);

  if (!batchId) return null;

  const isLoading = jobType === "render" ? renderQ.isLoading : upgradeQ.isLoading;
  const isError = jobType === "render" ? renderQ.isError : upgradeQ.isError;
  const data =
    jobType === "render"
      ? (renderQ.data as
          | { batch_id: string; total: number; counts: Partial<Record<AnyStatus, number>>; jobs: RenderJob[] }
          | undefined)
      : (upgradeQ.data as
          | { batch_id: string; total: number; counts: Partial<Record<AnyStatus, number>>; jobs: UpgradeJob[] }
          | undefined);

  if (isLoading) {
    return (
      <div style={{ padding: 24, fontSize: 12, color: "var(--ink-3)" }}>
        {t("loading")}
      </div>
    );
  }

  if (isError || !data) {
    return (
      <div style={{ padding: 24 }}>
        <button className="btn sm" onClick={() => navigate(-1)}>
          <Ico name="arrow-r" size={11} style={{ transform: "rotate(180deg)" }} />
          {t("back")}
        </button>
        <h1 style={{ marginTop: 16, color: "var(--err)", fontSize: 16, fontWeight: 600 }}>
          {t("notFound", { batchId })}
        </h1>
      </div>
    );
  }

  const counts = data.counts;
  const done = counts.done ?? 0;
  const failed = counts.failed ?? 0;
  const cancelled = counts.cancelled ?? 0;
  const queued = counts.queued ?? 0;
  const running = counts.running ?? 0;
  const undone = counts.undone ?? 0;
  const settled = done + failed + cancelled + undone;
  const pct = data.total > 0 ? Math.round((settled / data.total) * 100) : 0;

  const headerLabel = jobType === "render" ? t("header.render") : t("header.upgrade");
  const canUndoBatch = jobType === "upgrade" && done > 0 && !undoBatchMut.isPending;

  const handleUndoBatch = () => {
    if (!confirmingUndo) {
      setConfirmingUndo(true);
      window.setTimeout(() => setConfirmingUndo(false), 5000);
      return;
    }
    setConfirmingUndo(false);
    undoBatchMut.mutate(batchId);
  };

  return (
    <div style={{ padding: 24, maxWidth: 1080, margin: "0 auto" }}>
      <button className="btn sm" onClick={() => navigate(-1)}>
        <Ico name="arrow-r" size={11} style={{ transform: "rotate(180deg)" }} />
        {t("back")}
      </button>

      <header style={{ marginTop: 16, marginBottom: 18 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <h1 style={{ fontSize: 18, margin: 0 }}>{headerLabel}</h1>
          {jobType === "upgrade" && (
            <button
              type="button"
              className="btn sm"
              onClick={handleUndoBatch}
              disabled={!canUndoBatch && !confirmingUndo}
              style={{
                background: confirmingUndo ? "var(--err-50)" : undefined,
                color: confirmingUndo ? "var(--err)" : undefined,
              }}
              title={
                done === 0
                  ? t("header.undoNoneTitle")
                  : t("header.undoTitle")
              }
            >
              <Ico name="refresh" size={11} />
              {undoBatchMut.isPending
                ? t("header.undoing")
                : confirmingUndo
                  ? t("header.undoConfirm", { n: done })
                  : t("header.undoBtn")}
            </button>
          )}
        </div>
        <div
          style={{
            fontFamily: "var(--mono)",
            fontSize: 12,
            color: "var(--ink-3)",
            marginTop: 4,
          }}
        >
          {batchId}
        </div>
        <div
          style={{
            display: "flex",
            gap: 16,
            marginTop: 12,
            alignItems: "center",
            fontSize: 12.5,
          }}
        >
          <CountBadge label={t("counts.total")} value={data.total} color="var(--ink-1)" />
          {queued > 0 && <CountBadge label={t("counts.queued")} value={queued} color="var(--ink-3)" />}
          {running > 0 && <CountBadge label={t("counts.running")} value={running} color="var(--cyan, #0EA5E9)" />}
          <CountBadge label={t("counts.done")} value={done} color="var(--ok)" />
          {failed > 0 && <CountBadge label={t("counts.failed")} value={failed} color="var(--err)" />}
          {cancelled > 0 && <CountBadge label={t("counts.cancelled")} value={cancelled} color="var(--ink-3)" />}
          {undone > 0 && <CountBadge label={t("counts.undone")} value={undone} color="var(--ink-3)" />}
        </div>
        <div
          style={{
            height: 4,
            borderRadius: 4,
            background: "var(--bg-2)",
            overflow: "hidden",
            marginTop: 12,
          }}
        >
          <div
            style={{
              height: "100%",
              width: `${pct}%`,
              background: failed > 0 ? "var(--err)" : "var(--ok)",
              transition: "width 500ms linear",
            }}
          />
        </div>
        {undoBatchMut.isError && (
          <div style={{ marginTop: 8, color: "var(--err)", fontSize: 11.5 }}>
            {t("header.undoFail", { message: (undoBatchMut.error as Error)?.message ?? t("header.undoUnknown") })}
          </div>
        )}
        {undoBatchMut.isSuccess && undoBatchMut.data && (
          <div style={{ marginTop: 8, fontSize: 11.5, color: "var(--ink-3)" }}>
            {t("header.undoneCount", { n: undoBatchMut.data.undone.length })}
            {undoBatchMut.data.skipped.length > 0 &&
              t("header.undoneSkipped", { n: undoBatchMut.data.skipped.length })}
            {undoBatchMut.data.errors.length > 0 &&
              t("header.undoneErrors", { n: undoBatchMut.data.errors.length })}
          </div>
        )}
      </header>

      <table
        style={{
          width: "100%",
          fontSize: 12.5,
          borderCollapse: "collapse",
          background: "var(--panel, #fff)",
          border: "1px solid var(--line)",
          borderRadius: 6,
        }}
      >
        <thead>
          <tr style={{ background: "var(--bg-2)" }}>
            <Th>{t("table.status")}</Th>
            <Th>{t("table.case")}</Th>
            <Th>{jobType === "render" ? t("table.brandTpl") : t("table.brand")}</Th>
            <Th>{t("table.elapsed")}</Th>
            <Th>{jobType === "render" ? t("table.outputRender") : t("table.outputUpgrade")}</Th>
            <Th>{t("table.actions")}</Th>
          </tr>
        </thead>
        <tbody>
          {data.jobs.map((j) =>
            jobType === "render" ? (
              <RenderJobRow
                key={j.id}
                job={j as RenderJob}
                onRetry={() =>
                  renderRetryMut.mutate({
                    caseId: j.case_id,
                    payload: {
                      brand: (j as RenderJob).brand,
                      template: (j as RenderJob).template,
                      semantic_judge:
                        (j as RenderJob).semantic_judge === "auto"
                          ? "auto"
                          : "off",
                    },
                  })
                }
                onCancel={() => renderCancelMut.mutate(j.id)}
                onEvaluate={() => setEvaluateJobId(j.id)}
              />
            ) : (
              <UpgradeJobRow
                key={j.id}
                job={j as UpgradeJob}
                onRetry={() => upgradeRetryMut.mutate(j.id)}
                onCancel={() => upgradeCancelMut.mutate(j.id)}
              />
            )
          )}
        </tbody>
      </table>
      {evaluateJobId !== null &&
        (() => {
          const j = (data.jobs as RenderJob[]).find((x) => x.id === evaluateJobId);
          if (!j) return null;
          return (
            <EvaluateDialog
              open={true}
              onClose={() => setEvaluateJobId(null)}
              subjectKind="render"
              subjectId={j.id}
              caseId={j.case_id}
              subjectSummary={`job #${j.id} · case #${j.case_id} · ${j.brand}/${j.template}`}
            />
          );
        })()}
    </div>
  );
}

function CountBadge({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <span
      style={{
        display: "inline-flex",
        gap: 6,
        alignItems: "baseline",
        fontFamily: "var(--mono)",
      }}
    >
      <span style={{ color: "var(--ink-3)", fontSize: 11 }}>{label}</span>
      <b style={{ color, fontSize: 14 }}>{value}</b>
    </span>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return (
    <th
      style={{
        textAlign: "left",
        fontWeight: 600,
        padding: "8px 12px",
        borderBottom: "1px solid var(--line)",
        fontSize: 11.5,
        color: "var(--ink-3)",
      }}
    >
      {children}
    </th>
  );
}

function Td({ children }: { children: React.ReactNode }) {
  return (
    <td
      style={{
        padding: "10px 12px",
        verticalAlign: "middle",
      }}
    >
      {children}
    </td>
  );
}

function StatusPill({ status }: { status: AnyStatus }) {
  const { t } = useTranslation("jobBatch");
  const map: Record<AnyStatus, { bg: string; fg: string }> = {
    queued: { bg: "var(--bg-2)", fg: "var(--ink-2)" },
    running: { bg: "var(--cyan-50, #ECFEFF)", fg: "var(--cyan-ink, #0E7490)" },
    done: { bg: "var(--ok-50, #DCFCE7)", fg: "var(--ok)" },
    failed: { bg: "var(--err-50)", fg: "var(--err)" },
    cancelled: { bg: "var(--bg-2)", fg: "var(--ink-3)" },
    undone: { bg: "var(--bg-2)", fg: "var(--ink-3)" },
  };
  const c = map[status];
  return (
    <span
      className="badge"
      style={{
        background: c.bg,
        color: c.fg,
        fontFamily: "var(--mono)",
        fontSize: 11,
      }}
    >
      {t(`status.${status}` as never)}
    </span>
  );
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

function RenderJobRow({
  job,
  onRetry,
  onCancel,
  onEvaluate,
}: {
  job: RenderJob;
  onRetry: () => void;
  onCancel: () => void;
  onEvaluate: () => void;
}) {
  const { t } = useTranslation("jobBatch");
  const previewUrl =
    job.status === "done" ? renderOutputUrl(job.case_id, job.brand, job.template) : null;
  const elapsed = formatDuration(job.started_at, job.finished_at);
  return (
    <tr style={{ borderBottom: "1px solid var(--line-2)" }}>
      <Td>
        <StatusPill status={job.status} />
      </Td>
      <Td>
        <Link
          to={`/cases/${job.case_id}`}
          style={{ color: "var(--ink-1)", fontFamily: "var(--mono)" }}
        >
          #{job.case_id}
        </Link>
      </Td>
      <Td>
        <span style={{ fontFamily: "var(--mono)", color: "var(--ink-2)" }}>
          {job.brand} · {job.template}
        </span>
      </Td>
      <Td>
        <span style={{ fontFamily: "var(--mono)", color: "var(--ink-3)" }}>
          {elapsed}
        </span>
      </Td>
      <Td>
        {previewUrl ? (
          <a href={previewUrl} target="_blank" rel="noreferrer">
            <img
              src={previewUrl}
              alt="final"
              style={{
                height: 56,
                border: "1px solid var(--line)",
                borderRadius: 4,
                display: "block",
              }}
            />
          </a>
        ) : job.status === "failed" ? (
          <span
            style={{
              fontFamily: "var(--mono)",
              fontSize: 11,
              color: "var(--err)",
              maxWidth: 360,
              display: "block",
              wordBreak: "break-all",
            }}
          >
            {(job.error_message || "").slice(0, 200)}
          </span>
        ) : (
          <span style={{ color: "var(--ink-4)" }}>—</span>
        )}
      </Td>
      <Td>
        <div style={{ display: "flex", gap: 6 }}>
          {(job.status === "failed" || job.status === "cancelled") && (
            <button className="btn sm" onClick={onRetry}>
              <Ico name="refresh" size={11} />
              {t("row.retry")}
            </button>
          )}
          {job.status === "queued" && (
            <button className="btn sm ghost" onClick={onCancel}>
              <Ico name="x" size={11} />
              {t("row.cancel")}
            </button>
          )}
          {job.status === "done" && (
            <button
              className="btn sm ghost"
              onClick={onEvaluate}
              title={t("row.evaluateTitle")}
            >
              <Ico name="check" size={11} />
              {t("row.evaluate")}
            </button>
          )}
        </div>
      </Td>
    </tr>
  );
}

function UpgradeJobRow({
  job,
  onRetry,
  onCancel,
}: {
  job: UpgradeJob;
  onRetry: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation("jobBatch");
  const elapsed = formatDuration(job.started_at, job.finished_at);
  return (
    <tr style={{ borderBottom: "1px solid var(--line-2)" }}>
      <Td>
        <StatusPill status={job.status} />
      </Td>
      <Td>
        <Link
          to={`/cases/${job.case_id}`}
          style={{ color: "var(--ink-1)", fontFamily: "var(--mono)" }}
        >
          #{job.case_id}
        </Link>
      </Td>
      <Td>
        <span style={{ fontFamily: "var(--mono)", color: "var(--ink-2)" }}>
          {job.brand}
        </span>
      </Td>
      <Td>
        <span style={{ fontFamily: "var(--mono)", color: "var(--ink-3)" }}>
          {elapsed}
        </span>
      </Td>
      <Td>
        {job.status === "done" ? (
          <span style={{ fontSize: 11.5, color: "var(--ink-2)" }}>
            <span style={{ fontFamily: "var(--mono)", color: "var(--ink-1)" }}>
              {job.meta.category ?? "—"}
            </span>
            {" / "}
            <span style={{ fontFamily: "var(--mono)" }}>
              {job.meta.template_tier ?? "—"}
            </span>
            {(job.meta.blocking_count ?? 0) > 0 && (
              <span style={{ marginLeft: 8, color: "var(--err)" }}>
                {t("row.blocking", { n: job.meta.blocking_count })}
              </span>
            )}
            {(job.meta.warning_count ?? 0) > 0 && (
              <span style={{ marginLeft: 8, color: "var(--amber-ink)" }}>
                {t("row.warning", { n: job.meta.warning_count })}
              </span>
            )}
          </span>
        ) : job.status === "failed" ? (
          <span
            style={{
              fontFamily: "var(--mono)",
              fontSize: 11,
              color: "var(--err)",
              maxWidth: 360,
              display: "block",
              wordBreak: "break-all",
            }}
          >
            {(job.error_message || "").slice(0, 200)}
          </span>
        ) : (
          <span style={{ color: "var(--ink-4)" }}>—</span>
        )}
      </Td>
      <Td>
        {(job.status === "failed" || job.status === "cancelled") && (
          <button className="btn sm" onClick={onRetry}>
            <Ico name="refresh" size={11} />
            {t("row.retry")}
          </button>
        )}
        {job.status === "queued" && (
          <button className="btn sm ghost" onClick={onCancel}>
            <Ico name="x" size={11} />
            {t("row.cancel")}
          </button>
        )}
      </Td>
    </tr>
  );
}
