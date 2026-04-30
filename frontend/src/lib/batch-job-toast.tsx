/**
 * Global batch-job toast (bottom-right).
 *
 * Stacks one progress row per active batch, keyed by (jobType, batchId). Render
 * and upgrade batches can both display at once when running concurrently — the
 * shared worker pool processes them serially, but the user still sees both
 * queues progressing.
 *
 * Each row auto-dismisses 6 seconds after its batch reaches a terminal state
 * (no queued / running jobs remaining). Clicking opens the unified JobBatch
 * detail page.
 */
import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { create } from "zustand";
import {
  useJobStream,
  useRenderBatch,
  useUpgradeBatch,
} from "../hooks/queries";
import type { RenderStatus, UpgradeStatus } from "../api";
import { Ico } from "../components/atoms";

export type BatchJobType = "render" | "upgrade";
type AnyStatus = RenderStatus | UpgradeStatus;

interface BatchToastEntry {
  jobType: BatchJobType;
  batchId: string;
  totalAtEnqueue: number;
  startedAt: number;
  terminalDismissAt: number | null;
}

interface BatchJobToastStore {
  entries: BatchToastEntry[];
  show: (jobType: BatchJobType, batchId: string, total: number) => void;
  clear: (jobType: BatchJobType, batchId: string) => void;
  setTerminal: (
    jobType: BatchJobType,
    batchId: string,
    dismissAtMs: number
  ) => void;
}

const keyOf = (e: { jobType: BatchJobType; batchId: string }) =>
  `${e.jobType}:${e.batchId}`;

export const useBatchJobToastStore = create<BatchJobToastStore>((set) => ({
  entries: [],
  show: (jobType, batchId, total) =>
    set((s) => {
      const existing = s.entries.findIndex(
        (e) => e.jobType === jobType && e.batchId === batchId
      );
      const next: BatchToastEntry = {
        jobType,
        batchId,
        totalAtEnqueue: total,
        startedAt: Date.now(),
        terminalDismissAt: null,
      };
      if (existing >= 0) {
        const arr = [...s.entries];
        arr[existing] = next;
        return { entries: arr };
      }
      return { entries: [...s.entries, next] };
    }),
  clear: (jobType, batchId) =>
    set((s) => ({
      entries: s.entries.filter(
        (e) => !(e.jobType === jobType && e.batchId === batchId)
      ),
    })),
  setTerminal: (jobType, batchId, dismissAtMs) =>
    set((s) => ({
      entries: s.entries.map((e) =>
        e.jobType === jobType && e.batchId === batchId
          ? { ...e, terminalDismissAt: dismissAtMs }
          : e
      ),
    })),
}));

const TERMINAL_LINGER_MS = 6000;
const STATUS_KEYS: AnyStatus[] = [
  "queued",
  "running",
  "done",
  "failed",
  "cancelled",
  "undone",
];

export function BatchJobToast() {
  const entries = useBatchJobToastStore((s) => s.entries);
  // One global SSE subscription so the toast stays fresh.
  useJobStream();

  if (entries.length === 0) return null;

  return (
    <div
      style={{
        position: "fixed",
        bottom: 16,
        right: 16,
        zIndex: 999,
        display: "flex",
        flexDirection: "column",
        gap: 8,
        alignItems: "flex-end",
      }}
    >
      {entries.map((e) => (
        <BatchToastRow key={keyOf(e)} entry={e} />
      ))}
    </div>
  );
}

function BatchToastRow({ entry }: { entry: BatchToastEntry }) {
  const setTerminal = useBatchJobToastStore((s) => s.setTerminal);
  const clear = useBatchJobToastStore((s) => s.clear);
  const navigate = useNavigate();

  const renderQ = useRenderBatch(
    entry.jobType === "render" ? entry.batchId : undefined
  );
  const upgradeQ = useUpgradeBatch(
    entry.jobType === "upgrade" ? entry.batchId : undefined
  );

  const data =
    entry.jobType === "render"
      ? (renderQ.data as
          | { total: number; counts: Partial<Record<AnyStatus, number>> }
          | undefined)
      : (upgradeQ.data as
          | { total: number; counts: Partial<Record<AnyStatus, number>> }
          | undefined);

  const counts = data?.counts ?? {};
  const total = data?.total ?? entry.totalAtEnqueue;
  const done = counts.done ?? 0;
  const failed = counts.failed ?? 0;
  const cancelled = counts.cancelled ?? 0;
  const pending = (counts.queued ?? 0) + (counts.running ?? 0);
  const isTerminal = total > 0 && pending === 0;

  useEffect(() => {
    if (!isTerminal) return;
    if (entry.terminalDismissAt) return;
    setTerminal(entry.jobType, entry.batchId, Date.now() + TERMINAL_LINGER_MS);
  }, [
    isTerminal,
    entry.terminalDismissAt,
    entry.jobType,
    entry.batchId,
    setTerminal,
  ]);

  useEffect(() => {
    if (!entry.terminalDismissAt) return;
    const remaining = entry.terminalDismissAt - Date.now();
    if (remaining <= 0) {
      clear(entry.jobType, entry.batchId);
      return;
    }
    const t = setTimeout(
      () => clear(entry.jobType, entry.batchId),
      remaining
    );
    return () => clearTimeout(t);
  }, [entry.terminalDismissAt, entry.jobType, entry.batchId, clear]);

  const overallPct =
    total > 0 ? Math.round(((done + failed + cancelled) / total) * 100) : 0;

  const opLabel = entry.jobType === "render" ? "批量出图" : "批量升级 v3";
  const headline = isTerminal
    ? `${opLabel}完成 · ${done}/${total} 成功${failed ? ` · ${failed} 失败` : ""}${cancelled ? ` · ${cancelled} 取消` : ""}`
    : `${opLabel}进行中 · ${done + failed + cancelled}/${total}（队列 ${pending}）`;

  const goToBatch = () =>
    navigate(`/jobs/batches/${entry.batchId}?type=${entry.jobType}`);
  return (
    <div
      role="button"
      tabIndex={0}
      aria-live="polite"
      aria-label={headline}
      onClick={goToBatch}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          goToBatch();
        }
      }}
      style={{
        background: "var(--ink-1)",
        color: "#FAFAF9",
        borderRadius: 10,
        boxShadow:
          "0 8px 24px rgba(28,25,23,.18), 0 2px 4px rgba(28,25,23,.12)",
        padding: "10px 14px",
        display: "flex",
        flexDirection: "column",
        gap: 6,
        fontSize: 12.5,
        minWidth: 320,
        maxWidth: 420,
        cursor: "pointer",
        overflow: "hidden",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <Ico
          name={
            isTerminal && failed === 0
              ? "check"
              : isTerminal
                ? "alert"
                : entry.jobType === "render"
                  ? "image"
                  : "scan"
          }
          size={13}
          style={{
            color:
              isTerminal && failed === 0
                ? "var(--ok)"
                : isTerminal
                  ? "var(--err)"
                  : entry.jobType === "render"
                    ? "var(--cyan, #0EA5E9)"
                    : "var(--purple, #A855F7)",
          }}
        />
        <span style={{ flex: 1, minWidth: 0 }}>{headline}</span>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            clear(entry.jobType, entry.batchId);
          }}
          style={{
            background: "transparent",
            color: "rgba(250,250,249,0.55)",
            border: 0,
            padding: 4,
            cursor: "pointer",
            fontFamily: "inherit",
          }}
          aria-label="关闭"
        >
          <Ico name="x" size={12} />
        </button>
      </div>
      <div
        style={{
          display: "flex",
          gap: 12,
          fontFamily: "var(--mono)",
          fontSize: 11,
          color: "rgba(250,250,249,0.7)",
        }}
      >
        {STATUS_KEYS.map((s) => {
          const n = counts[s] ?? 0;
          if (n === 0) return null;
          return (
            <span key={s}>
              <span style={{ color: "rgba(250,250,249,0.5)" }}>
                {statusZh(s)}
              </span>{" "}
              <b style={{ color: statusColor(s) }}>{n}</b>
            </span>
          );
        })}
      </div>
      <div
        style={{
          height: 3,
          borderRadius: 3,
          background: "rgba(250,250,249,0.15)",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            height: "100%",
            width: `${overallPct}%`,
            background: failed > 0 ? "var(--err)" : "var(--ok)",
            transition: "width 500ms linear",
          }}
        />
      </div>
      <div style={{ fontSize: 10.5, color: "rgba(250,250,249,0.5)" }}>
        点击查看详情
      </div>
    </div>
  );
}

function statusZh(s: AnyStatus): string {
  return (
    {
      queued: "排队",
      running: "处理",
      done: "成功",
      failed: "失败",
      cancelled: "取消",
      undone: "撤销",
    } as Record<AnyStatus, string>
  )[s];
}

function statusColor(s: AnyStatus): string {
  return (
    {
      queued: "rgba(250,250,249,0.7)",
      running: "var(--cyan, #67E8F9)",
      done: "var(--ok)",
      failed: "var(--err)",
      cancelled: "rgba(250,250,249,0.55)",
      undone: "rgba(250,250,249,0.55)",
    } as Record<AnyStatus, string>
  )[s];
}
