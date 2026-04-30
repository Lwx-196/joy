/**
 * 「近期变更」drawer — secondary undo entry beyond the 30s toast window.
 *
 * Why this exists: the UndoToast disappears after 30s. If the user moves to
 * another case, makes another edit, or simply doesn't react in time, there's
 * no way to roll back without manual SQL. This drawer surfaces the per-case
 * audit log so any of the recent changes can be undone.
 *
 * Routing:
 *   - op='render' → POST /api/cases/{id}/render/undo (deletes artifact file)
 *   - everything else → POST /api/cases/{id}/undo (apply_undo on tracked cols)
 *
 * Only the topmost active revision of each kind (render vs non-render) gets
 * an "撤销" button — backend's undo endpoints always operate on the latest
 * active revision, so showing the button on older entries would be misleading.
 */
import { Fragment, useEffect } from "react";
import { useTranslation } from "react-i18next";
import type { CaseRevision } from "../api";
import { useCaseRevisions, useUndoCaseFromDrawer } from "../hooks/queries";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { Ico } from "./atoms";


interface DiffRow {
  col: string;
  before: unknown;
  after: unknown;
}

function diffRows(rev: CaseRevision): DiffRow[] {
  const out: DiffRow[] = [];
  const keys = new Set([...Object.keys(rev.before), ...Object.keys(rev.after)]);
  for (const k of keys) {
    const b = rev.before[k];
    const a = rev.after[k];
    if (JSON.stringify(b) === JSON.stringify(a)) continue;
    out.push({ col: k, before: b, after: a });
  }
  return out;
}

function fmt(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "string") return v.length > 30 ? v.slice(0, 27) + "…" : v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  const s = JSON.stringify(v);
  return s.length > 30 ? s.slice(0, 27) + "…" : s;
}

export interface RevisionsDrawerProps {
  caseId: number;
  open: boolean;
  onClose: () => void;
}

export function RevisionsDrawer({ caseId, open, onClose }: RevisionsDrawerProps) {
  const { t } = useTranslation("revisions");
  const revQ = useCaseRevisions(open ? caseId : null);
  const undoMut = useUndoCaseFromDrawer();
  const revisions = revQ.data ?? [];
  const drawerRef = useFocusTrap<HTMLDivElement>(open);

  const relativeTime = (iso: string): string => {
    const ts = new Date(iso).getTime();
    if (Number.isNaN(ts)) return iso;
    const diff = Date.now() - ts;
    const sec = Math.floor(diff / 1000);
    if (sec < 5) return t("rel.justNow");
    if (sec < 60) return t("rel.secAgo", { n: sec });
    const min = Math.floor(sec / 60);
    if (min < 60) return t("rel.minAgo", { n: min });
    const hr = Math.floor(min / 60);
    if (hr < 24) return t("rel.hrAgo", { n: hr });
    const day = Math.floor(hr / 24);
    return t("rel.dayAgo", { n: day });
  };
  const opLabel = (op: CaseRevision["op"]): string => t(`op.${op}` as never);

  // ESC closes drawer.
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onClose]);

  if (!open) return null;

  // Compute undoable rows: topmost active render + topmost active non-render.
  // Mirrors backend behaviour: /undo undoes latest non-render active,
  // /render/undo undoes latest active render — both can fire independently.
  let foundTopRender = false;
  let foundTopOther = false;
  const undoableSet = new Set<number>();
  for (const r of revisions) {
    if (r.undone_at) continue;
    if (r.op === "undo" || r.op === "undo_render" || r.op === "undo_evaluate") continue;
    // Evaluations have their own undo path (/api/evaluations/{id}/undo) — drawer
    // can't route to it without the evaluation id, so we don't surface a button
    // here. Users go to the 评估台 tab to undo a missed evaluation.
    if (r.op === "evaluate") continue;
    // 阶段 12: restore_render is its own redo-by-restore loop — the user can
    // re-restore the previous_archived_at to undo. before/after carry file
    // paths, not TRACKED_COLUMNS, so routing through apply_undo would null
    // every tracked column. Display the row read-only.
    if (r.op === "restore_render") continue;
    if (r.op === "render") {
      if (!foundTopRender) {
        undoableSet.add(r.id);
        foundTopRender = true;
      }
    } else {
      if (!foundTopOther) {
        undoableSet.add(r.id);
        foundTopOther = true;
      }
    }
  }

  return (
    <>
      <div
        onClick={onClose}
        aria-hidden
        style={{
          position: "fixed",
          inset: 0,
          background: "rgba(28, 25, 23, 0.32)",
          zIndex: 900,
        }}
      />
      <div
        ref={drawerRef}
        role="dialog"
        aria-modal="true"
        aria-label={t("title")}
        style={{
          position: "fixed",
          top: 0,
          right: 0,
          bottom: 0,
          width: 420,
          maxWidth: "92vw",
          background: "var(--panel)",
          boxShadow: "var(--shadow-pop)",
          zIndex: 901,
          display: "flex",
          flexDirection: "column",
          borderLeft: "1px solid var(--line)",
        }}
      >
        <header
          style={{
            padding: "14px 18px",
            borderBottom: "1px solid var(--line)",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            flexShrink: 0,
          }}
        >
          <div>
            <div style={{ fontSize: 14, fontWeight: 600 }}>{t("title")}</div>
            <div style={{ fontSize: 11.5, color: "var(--ink-3)", marginTop: 2 }}>
              {t("subtitle")}
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="btn sm ghost"
            aria-label={t("close")}
            title={t("closeTitle")}
            style={{ padding: 6 }}
          >
            <Ico name="x" size={12} />
          </button>
        </header>

        {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable region must be focusable for keyboard access (WCAG 2.1.1 / axe scrollable-region-focusable) */}
        <div style={{ flex: 1, overflowY: "auto" }} tabIndex={0}>
          {revQ.isLoading ? (
            <div style={{ padding: 20, fontSize: 12, color: "var(--ink-3)" }}>
              {t("loading")}
            </div>
          ) : revQ.isError ? (
            <div style={{ padding: 20, fontSize: 12, color: "var(--err)" }}>
              {t("loadError")}
            </div>
          ) : revisions.length === 0 ? (
            <div style={{ padding: 20, fontSize: 12, color: "var(--ink-3)" }}>
              {t("empty")}
            </div>
          ) : (
            revisions.map((rev) => {
              const undone = !!rev.undone_at;
              const isUndoMarker =
                rev.op === "undo" ||
                rev.op === "undo_render" ||
                rev.op === "undo_evaluate";
              const undoable = undoableSet.has(rev.id);
              const diff = diffRows(rev);
              return (
                <div
                  key={rev.id}
                  style={{
                    padding: "12px 18px",
                    borderBottom: "1px solid var(--line-2)",
                    opacity: undone ? 0.55 : 1,
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "baseline",
                      gap: 8,
                    }}
                  >
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <span style={{ fontSize: 12.5, fontWeight: 600 }}>
                        {opLabel(rev.op) ?? rev.op}
                      </span>
                      <span
                        style={{
                          fontSize: 11,
                          color: "var(--ink-3)",
                          marginLeft: 8,
                        }}
                      >
                        {relativeTime(rev.changed_at)}
                      </span>
                      {rev.actor !== "user" && (
                        <span
                          style={{
                            fontSize: 10.5,
                            color: "var(--ink-3)",
                            marginLeft: 6,
                          }}
                        >
                          · {rev.actor}
                        </span>
                      )}
                    </div>
                    {undoable && !undone ? (
                      <button
                        type="button"
                        className="btn sm"
                        onClick={() => undoMut.mutate({ caseId, op: rev.op })}
                        disabled={undoMut.isPending}
                        title={t("undoBtnTitle", {
                          caseId,
                          path: rev.op === "render" ? "render/undo" : "undo",
                        })}
                      >
                        <Ico name="refresh" size={11} />
                        {undoMut.isPending ? t("undoBtnLoading") : t("undoBtn")}
                      </button>
                    ) : undone ? (
                      <span style={{ fontSize: 11, color: "var(--ink-3)" }}>
                        {t("undonePill")}
                      </span>
                    ) : isUndoMarker ? (
                      <span style={{ fontSize: 11, color: "var(--ink-3)" }}>
                        {t("undoMarker")}
                      </span>
                    ) : null}
                  </div>
                  {diff.length > 0 && (
                    <div
                      style={{
                        marginTop: 6,
                        display: "grid",
                        gridTemplateColumns: "max-content 1fr",
                        gap: "2px 10px",
                        fontSize: 11,
                        color: "var(--ink-2)",
                        fontFamily: "var(--mono)",
                      }}
                    >
                      {diff.slice(0, 6).map((d) => (
                        <Fragment key={d.col}>
                          <span style={{ color: "var(--ink-3)" }}>{d.col}</span>
                          <span style={{ minWidth: 0, overflow: "hidden" }}>
                            <span style={{ color: "var(--warn)" }}>
                              {fmt(d.before)}
                            </span>
                            {" → "}
                            <span style={{ color: "var(--ok)" }}>
                              {fmt(d.after)}
                            </span>
                          </span>
                        </Fragment>
                      ))}
                      {diff.length > 6 && (
                        <span
                          style={{
                            gridColumn: "1 / -1",
                            color: "var(--ink-3)",
                          }}
                        >
                          {t("moreFields", { n: diff.length - 6 })}
                        </span>
                      )}
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>
      </div>
    </>
  );
}
