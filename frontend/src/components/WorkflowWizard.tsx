import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useBatchRenderCases, useCases } from "../hooks/queries";
import { useBrand } from "../lib/brand-context";
import { useBatchJobToastStore } from "../lib/batch-job-toast";
import { isHeld } from "../lib/work-queue";
import { Ico } from "./atoms";

type Step = "pick" | "wait" | "review";

interface Props {
  onClose: () => void;
}

export function WorkflowWizard({ onClose }: Props) {
  const { t } = useTranslation("dashboard");
  const navigate = useNavigate();
  const brand = useBrand();
  const [step, setStep] = useState<Step>("pick");
  const [batchId, setBatchId] = useState<string | null>(null);

  const batchRenderMut = useBatchRenderCases();
  const showBatchToast = useBatchJobToastStore((s) => s.show);
  // Narrow selector: subscribe only to the boolean we need so re-renders are
  // scoped. We drive UI off this signal instead of mirroring it into local
  // state via a useEffect (which would trigger set-state-in-effect cascade).
  const isBatchTerminal = useBatchJobToastStore((s) =>
    batchId != null &&
    s.entries.some(
      (e) =>
        e.jobType === "render" &&
        e.batchId === batchId &&
        e.terminalDismissAt != null,
    ),
  );

  const casesQ = useCases({ limit: 2000 });
  const allCases = casesQ.data ?? [];
  const renderableCases = allCases.filter((c) => !isHeld(c));

  const handleRender = () => {
    if (renderableCases.length === 0) return;
    const caseIds = renderableCases.map((c) => c.id);
    batchRenderMut.mutate(
      { caseIds, payload: { brand, template: "tri-compare", semantic_judge: "auto" } },
      {
        onSuccess: (data) => {
          showBatchToast("render", data.batch_id, caseIds.length);
          setBatchId(data.batch_id);
          setStep("wait");
        },
      }
    );
  };

  return (
    <div
      style={{
        background: "var(--bg-1)",
        border: "1px solid var(--line)",
        borderRadius: 10,
        padding: "20px 24px",
        marginBottom: 16,
        position: "relative",
      }}
    >
      <button
        className="btn sm ghost"
        style={{ position: "absolute", top: 12, right: 12 }}
        onClick={onClose}
        aria-label={t("wizard.close")}
      >
        <Ico name="x" size={12} />
      </button>

      {/* Step indicator */}
      <div style={{ display: "flex", gap: 8, marginBottom: 20, alignItems: "center" }}>
        {(["pick", "wait", "review"] as Step[]).map((s, i) => (
          <div key={s} style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <div
              style={{
                width: 24,
                height: 24,
                borderRadius: "50%",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 11,
                fontWeight: 600,
                background:
                  s === step
                    ? "var(--cyan)"
                    : stepIndex(step) > i
                      ? "var(--ok)"
                      : "var(--bg-3)",
                color:
                  s === step || stepIndex(step) > i ? "#fff" : "var(--ink-3)",
              }}
            >
              {stepIndex(step) > i ? "✓" : i + 1}
            </div>
            <span
              style={{
                fontSize: 12,
                color: s === step ? "var(--ink-1)" : "var(--ink-4)",
                fontWeight: s === step ? 600 : 400,
              }}
            >
              {t(`wizard.steps.${s}`)}
            </span>
            {i < 2 && (
              <div
                style={{
                  width: 24,
                  height: 1,
                  background: stepIndex(step) > i ? "var(--ok)" : "var(--line)",
                }}
              />
            )}
          </div>
        ))}
      </div>

      {/* Step content */}
      {step === "pick" && (
        <StepPick
          t={t}
          count={renderableCases.length}
          brand={brand}
          submitting={batchRenderMut.isPending}
          onRender={handleRender}
        />
      )}
      {step === "wait" && (
        <StepWait
          t={t}
          batchId={batchId}
          terminal={isBatchTerminal}
          onSkip={() => setStep("review")}
        />
      )}
      {step === "review" && (
        <StepReview
          t={t}
          onGo={() => { navigate("/quality"); onClose(); }}
          onClose={onClose}
        />
      )}
    </div>
  );
}

function stepIndex(step: Step): number {
  return ["pick", "wait", "review"].indexOf(step);
}

function StepPick({
  t, count, brand, submitting, onRender,
}: {
  t: ReturnType<typeof useTranslation<"dashboard">>["t"];
  count: number;
  brand: string;
  submitting: boolean;
  onRender: () => void;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
      <Ico name="image" size={18} style={{ color: "var(--cyan-ink)", flexShrink: 0 }} />
      <div style={{ flex: 1 }}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>
          {t("wizard.pick.title")}
        </div>
        <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
          {t("wizard.pick.desc", { count, brand })}
        </div>
      </div>
      <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
        <button
          className="btn sm primary"
          onClick={onRender}
          disabled={submitting || count === 0}
        >
          <Ico name="image" size={11} />
          {submitting
            ? t("wizard.pick.submitting")
            : t("wizard.pick.render", { count })}
        </button>
      </div>
    </div>
  );
}

function StepWait({
  t, batchId, terminal, onSkip,
}: {
  t: ReturnType<typeof useTranslation<"dashboard">>["t"];
  batchId: string | null;
  terminal: boolean;
  onSkip: () => void;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
      <Ico
        name={terminal ? "check" : "refresh"}
        size={18}
        style={{
          color: terminal ? "var(--ok)" : "var(--cyan-ink)",
          flexShrink: 0,
          animation: terminal ? undefined : "spin 1.2s linear infinite",
        }}
      />
      <div style={{ flex: 1 }}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>
          {terminal ? t("wizard.wait.titleDone") : t("wizard.wait.title")}
        </div>
        <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
          {terminal ? t("wizard.wait.descDone") : t("wizard.wait.desc")}
          {batchId && (
            <span style={{ fontFamily: "var(--mono)", marginLeft: 6, color: "var(--ink-4)" }}>
              #{batchId.slice(0, 8)}
            </span>
          )}
        </div>
      </div>
      <button className={terminal ? "btn sm primary" : "btn sm ghost"} onClick={onSkip}>
        {t("wizard.wait.goReview")}
      </button>
    </div>
  );
}

function StepReview({
  t, onGo, onClose,
}: {
  t: ReturnType<typeof useTranslation<"dashboard">>["t"];
  onGo: () => void;
  onClose: () => void;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
      <Ico name="check" size={18} style={{ color: "var(--ok)", flexShrink: 0 }} />
      <div style={{ flex: 1 }}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>
          {t("wizard.review.title")}
        </div>
        <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
          {t("wizard.review.desc")}
        </div>
      </div>
      <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
        <button className="btn sm ghost" onClick={onClose}>
          {t("wizard.close")}
        </button>
        <button className="btn sm primary" onClick={onGo}>
          <Ico name="eye" size={11} />
          {t("wizard.review.go")}
        </button>
      </div>
    </div>
  );
}
