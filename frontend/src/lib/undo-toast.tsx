/**
 * Undo toast — 30-second window for ⌘Z after a case mutation.
 *
 * How it's used:
 *   1. Mounted once in App.tsx as <UndoToast />
 *   2. Mutations call useUndoStore.getState().push({ caseIds, label }) on success
 *   3. The toast appears for 30s, listening for ⌘Z to call /api/cases/{id}/undo
 *   4. After 30s or after successful undo, the toast clears
 *
 * Single-toast model: a new mutation replaces the previous undo opportunity.
 * The previous mutation's revision still exists in DB, but we surface only
 * the most recent one (matches the plan: "只支持撤销最近一条").
 */
import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { create } from "zustand";
import { api } from "../api";
import { Ico } from "../components/atoms";

/** Which undo endpoint to call.
 * - "patch" → /api/cases/{id}/undo (v1.5 audit/undo for manual edits)
 * - "render" → /api/cases/{id}/render/undo (Phase 3: deletes the rendered artifact)
 * - "evaluation" → /api/evaluations/{evaluationId}/undo (阶段 3 评估台)
 */
export type UndoKind = "patch" | "render" | "evaluation";

interface UndoEntry {
  /** Case ids touched. Used by 'patch' / 'render' kinds. For 'evaluation', this
   * carries the originating case_id (if the subject is a case) so we can
   * invalidate per-case caches; for render-subject evaluations it's empty. */
  caseIds: number[];
  /** Used by 'evaluation' kind only — the evaluation row id to undo. */
  evaluationId?: number;
  label: string;
  // Issued at — used to compute remaining ms in the countdown.
  issuedAt: number;
  kind: UndoKind;
}

interface UndoStore {
  current: UndoEntry | null;
  push: (entry: Omit<UndoEntry, "issuedAt" | "kind"> & { kind?: UndoKind }) => void;
  clear: () => void;
}

export const useUndoStore = create<UndoStore>((set) => ({
  current: null,
  push: (entry) =>
    set({
      current: { ...entry, kind: entry.kind ?? "patch", issuedAt: Date.now() },
    }),
  clear: () => set({ current: null }),
}));

/** Window for which the toast is alive — 30s per plan. */
const TTL_MS = 30_000;

/**
 * Call the undo endpoint for each case in the batch and refresh the React Query cache.
 * Sequential rather than parallel because the user mostly does single-case patches;
 * even the bulk path (5-20 ids in a batch) is fast enough.
 */
async function performUndo(
  entry: UndoEntry,
  qc: ReturnType<typeof useQueryClient>
) {
  if (entry.kind === "evaluation") {
    if (entry.evaluationId == null) {
      console.warn("[undo] evaluation entry missing evaluationId");
      return;
    }
    try {
      await api.post(`/api/evaluations/${entry.evaluationId}/undo`);
    } catch (e) {
      console.warn("[undo] evaluation", entry.evaluationId, "failed:", e);
    }
    qc.invalidateQueries({ queryKey: ["evaluations"] });
    // Case-subject evaluations also touch case_revisions for the drawer.
    for (const cid of entry.caseIds) {
      qc.invalidateQueries({ queryKey: ["cases", cid, "revisions"] });
      qc.invalidateQueries({ queryKey: ["cases", cid] });
    }
    return;
  }

  const path = entry.kind === "render" ? "render/undo" : "undo";
  for (const id of entry.caseIds) {
    try {
      await api.post(`/api/cases/${id}/${path}`);
    } catch (e) {
      // 409 = nothing to undo (already undone or never had a revision); skip.
      console.warn("[undo]", entry.kind, "case", id, "failed:", e);
    }
  }
  // Touch all the same caches the mutation invalidated.
  qc.invalidateQueries({ queryKey: ["cases"] });
  qc.invalidateQueries({ queryKey: ["stats"] });
  qc.invalidateQueries({ queryKey: ["customers"] });
  if (entry.kind === "render") {
    qc.invalidateQueries({ queryKey: ["render"] });
    qc.invalidateQueries({ queryKey: ["evaluations"] });
  }
}

export function UndoToast() {
  const current = useUndoStore((s) => s.current);
  const clear = useUndoStore((s) => s.clear);
  const qc = useQueryClient();
  const [now, setNow] = useState(Date.now());
  const undoingRef = useRef(false);

  // Tick every 500ms while toast is alive — drives the countdown UI.
  useEffect(() => {
    if (!current) return;
    const t = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(t);
  }, [current]);

  // Auto-dismiss after TTL.
  useEffect(() => {
    if (!current) return;
    const elapsed = Date.now() - current.issuedAt;
    const remaining = TTL_MS - elapsed;
    if (remaining <= 0) {
      clear();
      return;
    }
    const t = setTimeout(() => clear(), remaining);
    return () => clearTimeout(t);
  }, [current, clear]);

  const doUndo = async () => {
    if (!current || undoingRef.current) return;
    undoingRef.current = true;
    try {
      await performUndo(current, qc);
    } finally {
      undoingRef.current = false;
      clear();
    }
  };

  // Cmd/Ctrl + Z: trigger undo while toast is alive.
  useEffect(() => {
    if (!current) return;
    const handler = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      if (e.key !== "z" && e.key !== "Z") return;
      // Don't steal undo inside text inputs/textareas/contenteditable.
      const tgt = e.target as HTMLElement | null;
      if (tgt) {
        const tag = tgt.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || tgt.isContentEditable) return;
      }
      e.preventDefault();
      void doUndo();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current]);

  if (!current) return null;
  const remainingMs = Math.max(0, TTL_MS - (now - current.issuedAt));
  const remainingSec = Math.ceil(remainingMs / 1000);
  const pct = (remainingMs / TTL_MS) * 100;

  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        position: "fixed",
        bottom: 16,
        left: "50%",
        transform: "translateX(-50%)",
        zIndex: 1000,
        background: "var(--ink-1)",
        color: "#FAFAF9",
        borderRadius: 10,
        boxShadow: "0 8px 24px rgba(28,25,23,.18), 0 2px 4px rgba(28,25,23,.12)",
        padding: "10px 14px",
        display: "flex",
        alignItems: "center",
        gap: 14,
        fontSize: 12.5,
        minWidth: 320,
        overflow: "hidden",
      }}
    >
      <Ico name="check" size={13} style={{ color: "var(--ok)" }} />
      <span style={{ flex: 1, minWidth: 0 }}>
        {current.label}
        <span style={{ color: "rgba(250,250,249,0.55)", marginLeft: 8 }}>
          {remainingSec}s
        </span>
      </span>
      <button
        type="button"
        onClick={doUndo}
        disabled={undoingRef.current}
        style={{
          background: "transparent",
          color: "#FAFAF9",
          border: "1px solid rgba(250,250,249,0.3)",
          borderRadius: 6,
          padding: "4px 10px",
          fontSize: 12,
          cursor: "pointer",
          fontFamily: "inherit",
        }}
        title="撤销最近一次变更"
      >
        撤销 (⌘Z)
      </button>
      <button
        type="button"
        onClick={clear}
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
      {/* Progress bar */}
      <div
        style={{
          position: "absolute",
          bottom: 0,
          left: 0,
          height: 2,
          width: `${pct}%`,
          background: "var(--ok)",
          transition: "width 500ms linear",
        }}
      />
    </div>
  );
}
