/**
 * EvaluateDialog — modal for creating an evaluation against a case or render.
 *
 * Trigger points:
 *   - CaseDetail "评估" button → subjectKind='case', subjectId=case_id
 *   - JobBatch row "评估" button → subjectKind='render', subjectId=job_id
 *   - Evaluations page row "重新评估" button → seeds existing verdict
 *
 * The reviewer field persists in localStorage so a daily reviewer doesn't
 * retype their name every time. The persisted value can still be edited
 * inline, and the new value is saved on submit.
 */
import { useEffect, useState } from "react";
import {
  VERDICT_LABEL,
  type EvaluationSubjectKind,
  type EvaluationVerdict,
} from "../api";
import { useCreateEvaluation } from "../hooks/queries";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { Ico } from "./atoms";

const REVIEWER_KEY = "case-workbench:reviewer";

const VERDICT_TONE: Record<EvaluationVerdict, { bg: string; ink: string; border: string }> = {
  approved: { bg: "rgba(220, 252, 231, 0.7)", ink: "rgb(22, 101, 52)", border: "rgb(187, 247, 208)" },
  needs_recheck: { bg: "rgba(254, 243, 199, 0.7)", ink: "rgb(146, 64, 14)", border: "rgb(253, 230, 138)" },
  rejected: { bg: "rgba(254, 226, 226, 0.7)", ink: "rgb(153, 27, 27)", border: "rgb(254, 202, 202)" },
};

export interface EvaluateDialogProps {
  open: boolean;
  onClose: () => void;
  subjectKind: EvaluationSubjectKind;
  subjectId: number;
  /** Optional case_id to invalidate per-case caches (pass for render-subject too). */
  caseId?: number;
  /** Brief subject summary shown at the top of the dialog. */
  subjectSummary: string;
  /** Pre-fill verdict when re-evaluating an existing record. */
  defaultVerdict?: EvaluationVerdict;
  defaultNote?: string;
}

export function EvaluateDialog({
  open,
  onClose,
  subjectKind,
  subjectId,
  caseId,
  subjectSummary,
  defaultVerdict,
  defaultNote,
}: EvaluateDialogProps) {
  const create = useCreateEvaluation();
  const [verdict, setVerdict] = useState<EvaluationVerdict>(
    defaultVerdict ?? "approved"
  );
  const [reviewer, setReviewer] = useState<string>("");
  const [note, setNote] = useState<string>(defaultNote ?? "");
  const dialogRef = useFocusTrap<HTMLDivElement>(open);

  // Reset form on open with persisted reviewer.
  useEffect(() => {
    if (!open) return;
    const stored =
      typeof window !== "undefined"
        ? window.localStorage.getItem(REVIEWER_KEY) || ""
        : "";
    setReviewer(stored);
    setVerdict(defaultVerdict ?? "approved");
    setNote(defaultNote ?? "");
  }, [open, defaultVerdict, defaultNote]);

  // ESC closes.
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onClose]);

  if (!open) return null;

  const submit = async () => {
    const r = reviewer.trim();
    if (!r) {
      const el = dialogRef.current?.querySelector<HTMLInputElement>("#eval-reviewer");
      el?.focus();
      return;
    }
    try {
      window.localStorage.setItem(REVIEWER_KEY, r);
    } catch {
      /* SSR / privacy mode — silently ignore */
    }
    await create.mutateAsync({
      payload: {
        subject_kind: subjectKind,
        subject_id: subjectId,
        verdict,
        reviewer: r,
        note: note.trim() || null,
      },
      caseId,
    });
    onClose();
  };

  return (
    <>
      <div
        onClick={onClose}
        aria-hidden
        style={{
          position: "fixed",
          inset: 0,
          background: "rgba(28, 25, 23, 0.32)",
          zIndex: 1100,
        }}
      />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="eval-dialog-title"
        style={{
          position: "fixed",
          top: "50%",
          left: "50%",
          transform: "translate(-50%, -50%)",
          width: 480,
          maxWidth: "92vw",
          background: "var(--panel)",
          borderRadius: 12,
          boxShadow: "var(--shadow-pop)",
          zIndex: 1101,
          padding: 22,
          display: "flex",
          flexDirection: "column",
          gap: 16,
        }}
      >
        <header
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
          }}
        >
          <div>
            <div id="eval-dialog-title" style={{ fontSize: 14, fontWeight: 600 }}>
              {subjectKind === "case" ? "案例评估" : "出图评估"}
            </div>
            <div
              style={{
                fontSize: 11.5,
                color: "var(--ink-3)",
                marginTop: 4,
                maxWidth: 380,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
              title={subjectSummary}
            >
              {subjectSummary}
            </div>
          </div>
          <button
            type="button"
            className="btn sm ghost"
            onClick={onClose}
            aria-label="关闭"
            title="关闭 (Esc)"
            style={{ padding: 6 }}
          >
            <Ico name="x" size={12} />
          </button>
        </header>

        {/* Verdict */}
        <div>
          <div style={{ fontSize: 11.5, color: "var(--ink-3)", marginBottom: 6 }}>
            结论
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            {(Object.keys(VERDICT_LABEL) as EvaluationVerdict[]).map((v) => {
              const tone = VERDICT_TONE[v];
              const active = verdict === v;
              return (
                <button
                  key={v}
                  type="button"
                  onClick={() => setVerdict(v)}
                  style={{
                    flex: 1,
                    padding: "8px 12px",
                    background: active ? tone.bg : "var(--bg-2)",
                    color: active ? tone.ink : "var(--ink-2)",
                    border: `1px solid ${active ? tone.border : "var(--line)"}`,
                    borderRadius: 8,
                    fontSize: 12.5,
                    fontWeight: active ? 600 : 500,
                    cursor: "pointer",
                    fontFamily: "inherit",
                    transition: "all 120ms",
                  }}
                >
                  {VERDICT_LABEL[v]}
                </button>
              );
            })}
          </div>
        </div>

        {/* Reviewer */}
        <div>
          <label
            htmlFor="eval-reviewer"
            style={{
              fontSize: 11.5,
              color: "var(--ink-3)",
              display: "block",
              marginBottom: 6,
            }}
          >
            评审人
          </label>
          <input
            id="eval-reviewer"
            type="text"
            value={reviewer}
            onChange={(e) => setReviewer(e.target.value)}
            placeholder="必填"
            maxLength={64}
            data-autofocus={reviewer === "" ? "true" : undefined}
            style={{
              width: "100%",
              padding: "7px 10px",
              border: "1px solid var(--line)",
              borderRadius: 6,
              background: "var(--bg)",
              fontSize: 12.5,
              fontFamily: "inherit",
            }}
          />
        </div>

        {/* Note */}
        <div>
          <label
            htmlFor="eval-note"
            style={{
              fontSize: 11.5,
              color: "var(--ink-3)",
              display: "block",
              marginBottom: 6,
            }}
          >
            评语 <span style={{ color: "var(--ink-4)" }}>（可选）</span>
          </label>
          <textarea
            id="eval-note"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="不通过/需重审时建议写明原因"
            maxLength={2000}
            rows={3}
            style={{
              width: "100%",
              padding: "8px 10px",
              border: "1px solid var(--line)",
              borderRadius: 6,
              background: "var(--bg)",
              fontSize: 12.5,
              fontFamily: "inherit",
              resize: "vertical",
              minHeight: 64,
            }}
          />
        </div>

        {/* Footer */}
        <footer
          style={{
            display: "flex",
            justifyContent: "flex-end",
            gap: 8,
            paddingTop: 4,
          }}
        >
          <button type="button" className="btn sm ghost" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn sm primary"
            onClick={submit}
            disabled={create.isPending || !reviewer.trim()}
          >
            {create.isPending ? "提交中…" : "提交评估"}
          </button>
        </footer>
      </div>
    </>
  );
}
