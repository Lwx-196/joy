/**
 * 评估台 — 阶段 3 (Phase 2 评估台).
 *
 * Two parallel columns:
 *   - 案例评估 (case): 每条 case 一条评估流，subject_kind='case' subject_id=case.id
 *   - 出图评估 (render): 每个完成的 render job 一条评估流，subject_kind='render'
 *                       subject_id=render_job.id; filtered by global brand selector
 *
 * Each column has segmented control [待评 N | 已评 M] and a list of rows.
 * Pending rows show a single "评估" button to open EvaluateDialog. Evaluated
 * rows show the verdict pill + reviewer + note preview + "重新评估" / "撤销"
 * buttons.
 *
 * Decoupled from cases.review_status by design — see plan D5.
 */
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  CATEGORY_LABEL,
  VERDICT_LABEL,
  type EvaluationVerdict,
  type PendingCaseEvaluationItem,
  type PendingRenderEvaluationItem,
  type RecentCaseEvaluation,
  type RecentRenderEvaluation,
} from "../api";
import { useBrand } from "../lib/brand-context";
import {
  usePendingCaseEvaluations,
  usePendingRenderEvaluations,
  useRecentCaseEvaluations,
  useRecentRenderEvaluations,
  useUndoEvaluation,
} from "../hooks/queries";
import { EvaluateDialog } from "../components/EvaluateDialog";
import { Ico } from "../components/atoms";

type Mode = "pending" | "recent";

const VERDICT_TONE: Record<EvaluationVerdict, { bg: string; ink: string }> = {
  approved: { bg: "rgb(220, 252, 231)", ink: "rgb(22, 101, 52)" },
  needs_recheck: { bg: "rgb(254, 243, 199)", ink: "rgb(146, 64, 14)" },
  rejected: { bg: "rgb(254, 226, 226)", ink: "rgb(153, 27, 27)" },
};

function VerdictPill({ verdict }: { verdict: EvaluationVerdict }) {
  const tone = VERDICT_TONE[verdict];
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        padding: "2px 8px",
        borderRadius: 999,
        background: tone.bg,
        color: tone.ink,
        fontSize: 11,
        fontWeight: 600,
        whiteSpace: "nowrap",
      }}
    >
      {VERDICT_LABEL[verdict]}
    </span>
  );
}

function lastSegment(absPath: string): string {
  const parts = absPath.split("/").filter(Boolean);
  if (parts.length === 0) return absPath;
  if (parts.length === 1) return parts[0];
  return parts.slice(-2).join("/");
}

function blockingCount(jsonStr: string | null): number {
  if (!jsonStr) return 0;
  try {
    const parsed = JSON.parse(jsonStr);
    if (Array.isArray(parsed)) {
      return parsed.filter((it) => {
        if (typeof it === "string") return true;
        return !it.severity || it.severity === "block";
      }).length;
    }
  } catch {
    /* ignore */
  }
  return 0;
}

interface DialogTarget {
  subjectKind: "case" | "render";
  subjectId: number;
  caseId?: number;
  subjectSummary: string;
  defaultVerdict?: EvaluationVerdict;
  defaultNote?: string;
}

export default function Evaluations() {
  const { t } = useTranslation("evaluations");
  const brand = useBrand();
  const [caseMode, setCaseMode] = useState<Mode>("pending");
  const [renderMode, setRenderMode] = useState<Mode>("pending");
  const [dialog, setDialog] = useState<DialogTarget | null>(null);

  const pendingCaseQ = usePendingCaseEvaluations(50);
  const pendingRenderQ = usePendingRenderEvaluations(brand, 50);
  const recentCaseQ = useRecentCaseEvaluations(50);
  const recentRenderQ = useRecentRenderEvaluations(brand, 50);
  const undoMut = useUndoEvaluation();

  const caseTotal = pendingCaseQ.data?.total ?? 0;
  const renderTotal = pendingRenderQ.data?.total ?? 0;
  const caseRecentCount = recentCaseQ.data?.items.length ?? 0;
  const renderRecentCount = recentRenderQ.data?.items.length ?? 0;

  return (
    <div className="page" style={{ padding: 24 }}>
      <header style={{ marginBottom: 18 }}>
        <h1 className="page-title" style={{ fontSize: 18, fontWeight: 600, margin: 0 }}>{t("title")}</h1>
        <p
          style={{
            fontSize: 12,
            color: "var(--ink-3)",
            marginTop: 4,
            marginBottom: 0,
          }}
        >
          {t("subtitle")}
        </p>
      </header>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)",
          gap: 18,
        }}
      >
        {/* 案例评估 */}
        <Column
          title={t("case.title")}
          subtitle={t("case.subtitle")}
          mode={caseMode}
          onModeChange={setCaseMode}
          pendingTotal={caseTotal}
          recentTotal={caseRecentCount}
        >
          {caseMode === "pending" ? (
            <PendingList
              loading={pendingCaseQ.isLoading}
              error={pendingCaseQ.isError}
              empty={t("empty.casePending")}
              items={pendingCaseQ.data?.items ?? []}
              renderItem={(item) => (
                <CasePendingRow
                  key={item.subject_id}
                  item={item}
                  onEvaluate={() =>
                    setDialog({
                      subjectKind: "case",
                      subjectId: item.subject_id,
                      caseId: item.case_id,
                      subjectSummary: subjectSummaryForCase(item),
                    })
                  }
                />
              )}
            />
          ) : (
            <RecentList
              loading={recentCaseQ.isLoading}
              error={recentCaseQ.isError}
              empty={t("empty.recent")}
              items={recentCaseQ.data?.items ?? []}
              renderItem={(item) => (
                <CaseRecentRow
                  key={item.id}
                  item={item}
                  onReEvaluate={() =>
                    setDialog({
                      subjectKind: "case",
                      subjectId: item.subject_id,
                      caseId: item.case_id,
                      subjectSummary: subjectSummaryForRecentCase(item),
                      defaultVerdict: item.verdict,
                      defaultNote: item.note ?? "",
                    })
                  }
                  onUndo={() =>
                    undoMut.mutate({ evaluationId: item.id, caseId: item.case_id })
                  }
                  undoing={undoMut.isPending}
                />
              )}
            />
          )}
        </Column>

        {/* 出图评估 */}
        <Column
          title={t("render.title")}
          subtitle={t("render.subtitle", { brand })}
          mode={renderMode}
          onModeChange={setRenderMode}
          pendingTotal={renderTotal}
          recentTotal={renderRecentCount}
        >
          {renderMode === "pending" ? (
            <PendingList
              loading={pendingRenderQ.isLoading}
              error={pendingRenderQ.isError}
              empty={t("empty.renderPending")}
              items={pendingRenderQ.data?.items ?? []}
              renderItem={(item) => (
                <RenderPendingRow
                  key={item.subject_id}
                  item={item}
                  onEvaluate={() =>
                    setDialog({
                      subjectKind: "render",
                      subjectId: item.subject_id,
                      caseId: item.case_id,
                      subjectSummary: subjectSummaryForRender(item),
                    })
                  }
                />
              )}
            />
          ) : (
            <RecentList
              loading={recentRenderQ.isLoading}
              error={recentRenderQ.isError}
              empty={t("empty.recent")}
              items={recentRenderQ.data?.items ?? []}
              renderItem={(item) => (
                <RenderRecentRow
                  key={item.id}
                  item={item}
                  onReEvaluate={() =>
                    setDialog({
                      subjectKind: "render",
                      subjectId: item.subject_id,
                      caseId: item.case_id,
                      subjectSummary: subjectSummaryForRecentRender(item),
                      defaultVerdict: item.verdict,
                      defaultNote: item.note ?? "",
                    })
                  }
                  onUndo={() =>
                    undoMut.mutate({ evaluationId: item.id, caseId: item.case_id })
                  }
                  undoing={undoMut.isPending}
                />
              )}
            />
          )}
        </Column>
      </div>

      {dialog && (
        <EvaluateDialog
          open={true}
          onClose={() => setDialog(null)}
          subjectKind={dialog.subjectKind}
          subjectId={dialog.subjectId}
          caseId={dialog.caseId}
          subjectSummary={dialog.subjectSummary}
          defaultVerdict={dialog.defaultVerdict}
          defaultNote={dialog.defaultNote}
        />
      )}
    </div>
  );
}

// ---------------------------- Column shell ----------------------------

interface ColumnProps {
  title: string;
  subtitle: string;
  mode: Mode;
  onModeChange: (m: Mode) => void;
  pendingTotal: number;
  recentTotal: number;
  children: React.ReactNode;
}

function Column({
  title,
  subtitle,
  mode,
  onModeChange,
  pendingTotal,
  recentTotal,
  children,
}: ColumnProps) {
  const { t } = useTranslation("evaluations");
  return (
    <section
      style={{
        background: "var(--panel)",
        border: "1px solid var(--line)",
        borderRadius: 10,
        display: "flex",
        flexDirection: "column",
        minHeight: 520,
        overflow: "hidden",
      }}
    >
      <header
        style={{
          padding: "14px 16px",
          borderBottom: "1px solid var(--line)",
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "space-between",
          gap: 12,
        }}
      >
        <div>
          <div style={{ fontSize: 14, fontWeight: 600 }}>{title}</div>
          <div style={{ fontSize: 11.5, color: "var(--ink-3)", marginTop: 3 }}>
            {subtitle}
          </div>
        </div>
        <div
          role="tablist"
          aria-label={t("tab.ariaSwitch", { title })}
          style={{
            display: "flex",
            gap: 0,
            background: "var(--bg-2)",
            border: "1px solid var(--line)",
            borderRadius: 6,
            padding: 2,
            flexShrink: 0,
          }}
        >
          <SegBtn
            active={mode === "pending"}
            onClick={() => onModeChange("pending")}
          >
            {t("tab.pending")}
            <span style={{ marginLeft: 4, color: "var(--ink-3)" }}>
              {pendingTotal}
            </span>
          </SegBtn>
          <SegBtn
            active={mode === "recent"}
            onClick={() => onModeChange("recent")}
          >
            {t("tab.recent")}
            <span style={{ marginLeft: 4, color: "var(--ink-3)" }}>
              {recentTotal}
            </span>
          </SegBtn>
        </div>
      </header>
      <div style={{ flex: 1, overflowY: "auto" }}>{children}</div>
    </section>
  );
}

function SegBtn({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      role="tab"
      aria-selected={active}
      style={{
        background: active ? "var(--panel)" : "transparent",
        color: active ? "var(--ink-1)" : "var(--ink-3)",
        border: 0,
        padding: "4px 10px",
        fontSize: 11.5,
        fontFamily: "inherit",
        cursor: "pointer",
        borderRadius: 4,
        boxShadow: active ? "var(--shadow-1)" : "none",
        fontWeight: active ? 600 : 500,
      }}
    >
      {children}
    </button>
  );
}

// ---------------------------- List shells ----------------------------

interface ListShellProps<T> {
  loading: boolean;
  error: boolean;
  empty: string;
  items: T[];
  renderItem: (item: T) => React.ReactNode;
}

function PendingList<T>(props: ListShellProps<T>) {
  return <ListShell {...props} />;
}
function RecentList<T>(props: ListShellProps<T>) {
  return <ListShell {...props} />;
}

function ListShell<T>({ loading, error, empty, items, renderItem }: ListShellProps<T>) {
  const { t } = useTranslation("evaluations");
  if (loading) {
    return (
      <div style={{ padding: 24, fontSize: 12, color: "var(--ink-3)" }}>
        {t("loading")}
      </div>
    );
  }
  if (error) {
    return (
      <div style={{ padding: 24, fontSize: 12, color: "var(--err)" }}>
        {t("loadError")}
      </div>
    );
  }
  if (items.length === 0) {
    return (
      <div style={{ padding: 24, fontSize: 12, color: "var(--ink-3)" }}>
        {empty}
      </div>
    );
  }
  return <div>{items.map((it) => renderItem(it))}</div>;
}

// ---------------------------- Rows: pending case ----------------------------

function CasePendingRow({
  item,
  onEvaluate,
}: {
  item: PendingCaseEvaluationItem;
  onEvaluate: () => void;
}) {
  const { t } = useTranslation("evaluations");
  const blocking = useMemo(
    () => blockingCount(item.blocking_issues_json),
    [item.blocking_issues_json]
  );
  return (
    <div
      style={{
        padding: "12px 16px",
        borderBottom: "1px solid var(--line-2)",
        display: "flex",
        alignItems: "center",
        gap: 12,
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 12.5, fontWeight: 600, color: "var(--ink-1)" }}>
          {item.customer_name || item.customer_raw || "—"}
          <span
            style={{
              fontSize: 10.5,
              color: "var(--ink-3)",
              marginLeft: 6,
              fontWeight: 500,
            }}
          >
            #{item.case_id}
          </span>
        </div>
        <div
          style={{
            fontSize: 11,
            color: "var(--ink-3)",
            marginTop: 3,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
          title={item.abs_path}
        >
          {lastSegment(item.abs_path)}
        </div>
        <div style={{ fontSize: 11, marginTop: 4, display: "flex", gap: 8 }}>
          <span style={{ color: "var(--ink-2)" }}>
            {CATEGORY_LABEL[item.category] ?? item.category}
          </span>
          {item.template_tier && (
            <span style={{ color: "var(--ink-3)" }}>· {item.template_tier}</span>
          )}
          {blocking > 0 && (
            <span style={{ color: "var(--err)" }}>· {t("row.blocking", { n: blocking })}</span>
          )}
        </div>
      </div>
      <button type="button" className="btn sm" onClick={onEvaluate}>
        <Ico name="check" size={11} />
        {t("row.evaluateBtn")}
      </button>
    </div>
  );
}

// ---------------------------- Rows: pending render ----------------------------

function RenderPendingRow({
  item,
  onEvaluate,
}: {
  item: PendingRenderEvaluationItem;
  onEvaluate: () => void;
}) {
  const { t } = useTranslation("evaluations");
  return (
    <div
      style={{
        padding: "12px 16px",
        borderBottom: "1px solid var(--line-2)",
        display: "flex",
        alignItems: "center",
        gap: 12,
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 12.5, fontWeight: 600 }}>
          {item.customer_name || item.customer_raw || "—"}
          <span
            style={{
              fontSize: 10.5,
              color: "var(--ink-3)",
              marginLeft: 6,
              fontWeight: 500,
            }}
          >
            job #{item.subject_id} · case #{item.case_id}
          </span>
        </div>
        <div
          style={{
            fontSize: 11,
            color: "var(--ink-3)",
            marginTop: 3,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
          title={item.abs_path}
        >
          {lastSegment(item.abs_path)}
        </div>
        <div style={{ fontSize: 11, marginTop: 4, color: "var(--ink-2)" }}>
          {item.brand} · {item.template}
        </div>
      </div>
      <button type="button" className="btn sm" onClick={onEvaluate}>
        <Ico name="check" size={11} />
        {t("row.evaluateBtn")}
      </button>
    </div>
  );
}

// ---------------------------- Rows: recent case ----------------------------

function CaseRecentRow({
  item,
  onReEvaluate,
  onUndo,
  undoing,
}: {
  item: RecentCaseEvaluation;
  onReEvaluate: () => void;
  onUndo: () => void;
  undoing: boolean;
}) {
  const { t } = useTranslation("evaluations");
  return (
    <div
      style={{
        padding: "12px 16px",
        borderBottom: "1px solid var(--line-2)",
        display: "flex",
        alignItems: "center",
        gap: 12,
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: 12.5,
            fontWeight: 600,
            display: "flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          <VerdictPill verdict={item.verdict} />
          <span>{item.customer_name || item.customer_raw || "—"}</span>
          <span
            style={{ fontSize: 10.5, color: "var(--ink-3)", fontWeight: 500 }}
          >
            #{item.case_id}
          </span>
        </div>
        <div
          style={{
            fontSize: 11,
            color: "var(--ink-3)",
            marginTop: 3,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {item.reviewer} · {new Date(item.created_at).toLocaleString()}
        </div>
        {item.note && (
          <div
            style={{
              fontSize: 11,
              marginTop: 4,
              color: "var(--ink-2)",
              maxHeight: 36,
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
            title={item.note}
          >
            {item.note}
          </div>
        )}
      </div>
      <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
        <button
          type="button"
          className="btn sm ghost"
          onClick={onReEvaluate}
          title={t("row.reEvaluateTitle")}
        >
          {t("row.reEvaluateBtn")}
        </button>
        <button
          type="button"
          className="btn sm ghost"
          onClick={onUndo}
          disabled={undoing}
          title={t("row.undoTitle")}
        >
          {undoing ? t("row.undoLoading") : t("row.undoBtn")}
        </button>
      </div>
    </div>
  );
}

// ---------------------------- Rows: recent render ----------------------------

function RenderRecentRow({
  item,
  onReEvaluate,
  onUndo,
  undoing,
}: {
  item: RecentRenderEvaluation;
  onReEvaluate: () => void;
  onUndo: () => void;
  undoing: boolean;
}) {
  const { t } = useTranslation("evaluations");
  return (
    <div
      style={{
        padding: "12px 16px",
        borderBottom: "1px solid var(--line-2)",
        display: "flex",
        alignItems: "center",
        gap: 12,
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: 12.5,
            fontWeight: 600,
            display: "flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          <VerdictPill verdict={item.verdict} />
          <span>{item.customer_name || item.customer_raw || "—"}</span>
          <span
            style={{ fontSize: 10.5, color: "var(--ink-3)", fontWeight: 500 }}
          >
            job #{item.subject_id} · case #{item.case_id}
          </span>
        </div>
        <div
          style={{
            fontSize: 11,
            color: "var(--ink-3)",
            marginTop: 3,
          }}
        >
          {item.reviewer} · {item.brand} · {item.template} ·{" "}
          {new Date(item.created_at).toLocaleString()}
        </div>
        {item.note && (
          <div
            style={{
              fontSize: 11,
              marginTop: 4,
              color: "var(--ink-2)",
              maxHeight: 36,
              overflow: "hidden",
            }}
            title={item.note}
          >
            {item.note}
          </div>
        )}
      </div>
      <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
        <button
          type="button"
          className="btn sm ghost"
          onClick={onReEvaluate}
        >
          {t("row.reEvaluateBtn")}
        </button>
        <button
          type="button"
          className="btn sm ghost"
          onClick={onUndo}
          disabled={undoing}
        >
          {undoing ? t("row.undoLoading") : t("row.undoBtn")}
        </button>
      </div>
    </div>
  );
}

// ---------------------------- Subject summary builders ----------------------------

function subjectSummaryForCase(item: PendingCaseEvaluationItem): string {
  const cust = item.customer_name || item.customer_raw || "—";
  const cat = CATEGORY_LABEL[item.category] ?? item.category;
  return `#${item.case_id} · ${cust} · ${cat}`;
}

function subjectSummaryForRecentCase(item: RecentCaseEvaluation): string {
  const cust = item.customer_name || item.customer_raw || "—";
  const cat = CATEGORY_LABEL[item.category] ?? item.category;
  return `#${item.case_id} · ${cust} · ${cat}`;
}

function subjectSummaryForRender(item: PendingRenderEvaluationItem): string {
  const cust = item.customer_name || item.customer_raw || "—";
  return `job #${item.subject_id} · case #${item.case_id} · ${cust} · ${item.brand}/${item.template}`;
}

function subjectSummaryForRecentRender(item: RecentRenderEvaluation): string {
  const cust = item.customer_name || item.customer_raw || "—";
  return `job #${item.subject_id} · case #${item.case_id} · ${cust} · ${item.brand}/${item.template}`;
}
