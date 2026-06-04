import { useTranslation } from "react-i18next";

import type { CaseDetail } from "../../api";
import { Ico } from "../../components/atoms";

type DiagnosticsCardProps = {
  data: CaseDetail;
  open: boolean;
  onToggle: () => void;
};

export function DiagnosticsCard({ data, open, onToggle }: DiagnosticsCardProps) {
  const { t } = useTranslation("caseDetail");

  if (data.blocking_issues.length === 0) return null;

  const blocks = data.blocking_issues.filter((issue) => (issue.severity ?? "block") === "block");
  const warns = data.blocking_issues.filter((issue) => issue.severity === "warn");
  const autoCodes = new Set(data.auto_blocking_issues.map((issue) => issue.code));

  const renderIssue = (issue: CaseDetail["blocking_issues"][number], index: number) => {
    const isManual = data.manual_blocking_codes.includes(issue.code);
    const isAuto = autoCodes.has(issue.code);
    const isBlock = (issue.severity ?? "block") === "block";
    const accent = isManual ? "var(--amber-ink)" : isBlock ? "var(--err)" : "var(--amber-ink)";
    const bg = isManual ? "var(--amber-50)" : isBlock ? "var(--err-50)" : "var(--amber-50)";
    const border = isManual ? "var(--amber-200)" : isBlock ? "var(--err-100)" : "var(--amber-200)";

    return (
      <div
        key={`${issue.code}-${index}`}
        style={{
          display: "grid",
          gridTemplateColumns: "auto 1fr auto",
          gap: 8,
          padding: 8,
          background: bg,
          border: `1px solid ${border}`,
          borderRadius: 6,
        }}
      >
        <Ico
          name={isBlock ? "alert" : "dot"}
          size={14}
          style={{ color: accent, flexShrink: 0, marginTop: 2 }}
        />
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 12.5, fontWeight: 500, color: accent }}>
            {issue.zh}
          </div>
          <div
            style={{
              fontFamily: "var(--mono)",
              fontSize: 10.5,
              color: "var(--ink-3)",
              marginTop: 1,
            }}
          >
            {issue.code} · {t("diagnostics.suggestionPrefix")}{issue.next}
          </div>
          {issue.files && issue.files.length > 0 && (
            <div
              style={{
                marginTop: 6,
                display: "flex",
                flexWrap: "wrap",
                gap: 4,
                alignItems: "center",
              }}
            >
              <span
                style={{
                  fontSize: 10.5,
                  color: "var(--ink-3)",
                  marginRight: 2,
                }}
              >
                {t("diagnostics.affectedFiles")}
              </span>
              {issue.files.slice(0, 6).map((filename) => (
                <button
                  key={filename}
                  type="button"
                  onClick={() => {
                    const selector = `[data-source-file="${CSS.escape(filename)}"]`;
                    const el = document.querySelector<HTMLElement>(selector);
                    if (el) {
                      el.scrollIntoView({ behavior: "smooth", block: "center" });
                      el.classList.add("flash-highlight");
                      setTimeout(() => el.classList.remove("flash-highlight"), 1500);
                    }
                  }}
                  className="chip"
                  style={{
                    fontFamily: "var(--mono)",
                    fontSize: 10.5,
                    cursor: "pointer",
                    border: "1px solid var(--line)",
                    padding: "2px 6px",
                  }}
                  title={t("diagnostics.highlightTooltip")}
                >
                  {filename}
                </button>
              ))}
              {issue.files.length > 6 && (
                <span style={{ fontSize: 10.5, color: "var(--ink-4)" }}>
                  … +{issue.files.length - 6}
                </span>
              )}
            </div>
          )}
        </div>
        <span
          className="badge"
          style={{
            background: isManual ? "var(--amber-100)" : "var(--cyan-50)",
            borderColor: isManual ? "var(--amber-200)" : "var(--cyan-200)",
            color: isManual ? "var(--amber-ink)" : "var(--cyan-ink)",
            height: 18,
            alignSelf: "start",
          }}
        >
          {isManual && isAuto
            ? t("diagnostics.autoManual")
            : isManual
              ? t("diagnostics.manual")
              : t("diagnostics.auto")}
        </span>
      </div>
    );
  };

  return (
    <div className="card" style={{ borderColor: blocks.length > 0 ? "var(--err-100)" : "var(--amber-200)" }}>
      <div
        className="card-h"
        style={{
          background: blocks.length > 0 ? "var(--err-50)" : "var(--amber-50)",
          borderBottom: `1px solid ${blocks.length > 0 ? "var(--err-100)" : "var(--amber-200)"}`,
        }}
      >
        <div className="t" style={{ color: blocks.length > 0 ? "var(--err)" : "var(--amber-ink)" }}>
          <Ico name="alert" size={13} />
          {t("diagnostics.cardTitle")}
          {blocks.length > 0 && <span>{t("diagnostics.blockingCount", { count: blocks.length })}</span>}
          {warns.length > 0 && <span>{t("diagnostics.warningCount", { count: warns.length })}</span>}
        </div>
        <button type="button" className="btn sm ghost" onClick={onToggle}>
          <Ico name={open ? "down" : "arrow-r"} size={11} />
          {open ? t("diagnostics.collapse") : t("diagnostics.expand")}
        </button>
      </div>
      <div className="card-b" style={{ display: "grid", gap: 8 }}>
        <div
          style={{
            display: "grid",
            gap: 4,
            padding: "8px 10px",
            border: "1px solid var(--line-2)",
            borderRadius: 6,
            background: "var(--panel-2)",
            color: "var(--ink-3)",
            fontSize: 11.5,
          }}
        >
          <div>{t("diagnostics.scopeHint")}</div>
          <div style={{ fontFamily: "var(--mono)", color: "var(--ink-4)" }}>
            {t("diagnostics.caseDiagnosisSummary", {
              auto: data.auto_blocking_issues.length,
              manual: data.manual_blocking_codes.length,
            })}
            {data.latest_render_status && (
              <span>
                {" · "}
                {t("diagnostics.latestRender", {
                  status: data.latest_render_status,
                  quality: data.latest_render_quality_status ?? "—",
                })}
              </span>
            )}
          </div>
        </div>
        {open && (
          <>
            {blocks.length > 0 && (
              <>
                {blocks.length > 0 && warns.length > 0 && (
                  <div
                    style={{
                      fontSize: 10.5,
                      color: "var(--err)",
                      textTransform: "uppercase",
                      letterSpacing: 0.4,
                    }}
                  >
                    {t("diagnostics.blockingSection", { count: blocks.length })}
                  </div>
                )}
                {blocks.map(renderIssue)}
              </>
            )}
            {warns.length > 0 && (
              <>
                {blocks.length > 0 && warns.length > 0 && (
                  <div
                    style={{
                      fontSize: 10.5,
                      color: "var(--amber-ink)",
                      textTransform: "uppercase",
                      letterSpacing: 0.4,
                      marginTop: 4,
                    }}
                  >
                    {t("diagnostics.warningSection", { count: warns.length })}
                  </div>
                )}
                {warns.map(renderIssue)}
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}
