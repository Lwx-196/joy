import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  type CaseSummary,
  type Category,
} from "../api";
import { useCustomerDetail, useUpdateCustomer } from "../hooks/queries";
import {
  CategoryPill,
  Ico,
  IssueCountBadge,
  ReviewPill,
  TierPill,
} from "../components/atoms";

export default function CustomerDetail() {
  const { t } = useTranslation(["customerDetail", "common"]);
  const { id } = useParams<{ id: string }>();
  const customerId = Number(id);

  const detailQ = useCustomerDetail(customerId || null);
  const updateMut = useUpdateCustomer();
  const data = detailQ.data ?? null;
  const saving = updateMut.isPending;

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<{ canonical_name: string; aliases: string; notes: string }>({
    canonical_name: "",
    aliases: "",
    notes: "",
  });

  // Sync draft from data when not editing.
  useEffect(() => {
    if (!data || editing) return;
    setDraft({
      canonical_name: data.canonical_name,
      aliases: data.aliases.join("\n"),
      notes: data.notes ?? "",
    });
  }, [data, editing]);

  const save = () => {
    updateMut.mutate(
      {
        id: customerId,
        payload: {
          canonical_name: draft.canonical_name.trim(),
          aliases: draft.aliases
            .split("\n")
            .map((s) => s.trim())
            .filter(Boolean),
          notes: draft.notes.trim() || undefined,
        },
      },
      { onSuccess: () => setEditing(false) }
    );
  };

  const stats = useMemo(() => {
    if (!data) return null;
    const dates = data.cases
      .map((c) => new Date(c.last_modified).getTime())
      .filter((t) => !isNaN(t));
    const blocking = data.cases.reduce((a, c) => a + c.blocking_issue_count, 0);
    return {
      first: dates.length ? new Date(Math.min(...dates)) : null,
      last: dates.length ? new Date(Math.max(...dates)) : null,
      blocking,
    };
  }, [data]);

  if (!data) return <div className="empty">{t("common:common.loading")}</div>;

  // Group cases by category, preserve order
  const byCategory = data.cases.reduce<Record<string, CaseSummary[]>>((acc, c) => {
    (acc[c.category] = acc[c.category] || []).push(c);
    return acc;
  }, {});

  const monthsBetween = (a: Date, b: Date) => {
    const diff = (b.getTime() - a.getTime()) / (1000 * 60 * 60 * 24 * 30.4);
    return diff < 1 ? t("stats.spanLessMonth") : t("stats.spanMonths", { n: diff.toFixed(1) });
  };
  const fmtDate = (d: Date | null) => (d ? d.toLocaleDateString("zh-CN") : "—");

  return (
    <div style={{ height: "100%", display: "grid", gridTemplateRows: "auto 1fr", overflow: "hidden" }}>
      {/* Breadcrumb */}
      <div
        style={{
          padding: "12px 24px",
          borderBottom: "1px solid var(--line-2)",
          background: "var(--panel)",
          fontSize: 11.5,
          color: "var(--ink-3)",
          display: "flex",
          alignItems: "center",
          gap: 6,
        }}
      >
        <Ico name="arrow-r" size={10} style={{ transform: "rotate(180deg)" }} />
        <Link to="/customers" style={{ color: "var(--ink-1)" }}>{t("breadcrumb")}</Link>
        <span style={{ color: "var(--ink-5)" }}>/</span>
        <span style={{ color: "var(--ink-1)" }}>{data.canonical_name}</span>
      </div>

      <div style={{ padding: 20, display: "grid", gridTemplateRows: "auto 1fr", gap: 16, overflow: "auto" }}>
        {/* Header card */}
        <div className="card" style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: 0 }}>
          <div style={{ padding: "18px 20px", display: "flex", alignItems: "center", gap: 16 }}>
            <div
              style={{
                width: 56,
                height: 56,
                borderRadius: 12,
                background: "var(--ink-1)",
                color: "#fff",
                display: "grid",
                placeItems: "center",
                fontSize: 22,
                fontWeight: 600,
                flexShrink: 0,
              }}
            >
              {data.canonical_name.slice(-1)}
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              {editing ? (
                <input
                  value={draft.canonical_name}
                  onChange={(e) => setDraft({ ...draft, canonical_name: e.target.value })}
                  style={{ fontSize: 22, fontWeight: 600, height: 36, marginBottom: 6 }}
                />
              ) : (
                <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
                  <h1 style={{ fontSize: 22, fontWeight: 600, letterSpacing: "-0.3px", margin: 0 }}>
                    {data.canonical_name}
                  </h1>
                  <button className="btn sm ghost" onClick={() => setEditing(true)}>
                    <Ico name="edit" size={11} />
                    {t("edit")}
                  </button>
                  <span
                    className="badge"
                    style={{
                      background: "var(--ink-1)",
                      color: "#fff",
                      borderColor: "var(--ink-1)",
                      fontFamily: "var(--mono)",
                    }}
                  >
                    {data.case_count} {t("caseSuffix")}
                  </span>
                </div>
              )}

              {editing ? (
                <>
                  <textarea
                    value={draft.aliases}
                    onChange={(e) => setDraft({ ...draft, aliases: e.target.value })}
                    placeholder={t("aliasesPlaceholder")}
                    style={{ width: "100%", minHeight: 60, marginBottom: 6 }}
                  />
                  <textarea
                    value={draft.notes}
                    onChange={(e) => setDraft({ ...draft, notes: e.target.value })}
                    placeholder={t("notesPlaceholder")}
                    style={{ width: "100%", minHeight: 50, marginBottom: 8 }}
                  />
                  <div style={{ display: "flex", gap: 8 }}>
                    <button className="btn primary" onClick={save} disabled={saving}>
                      {saving ? t("saving") : t("save")}
                    </button>
                    <button className="btn" onClick={() => setEditing(false)} disabled={saving}>
                      {t("cancel")}
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 8 }}>
                    <span
                      style={{
                        fontSize: 10.5,
                        color: "var(--ink-4)",
                        textTransform: "uppercase",
                        letterSpacing: 0.5,
                        marginRight: 4,
                        alignSelf: "center",
                      }}
                    >
                      aliases
                    </span>
                    {data.aliases.length === 0 && (
                      <span style={{ color: "var(--ink-4)", fontSize: 11.5, fontStyle: "italic" }}>
                        {t("aliasesEmpty")}
                      </span>
                    )}
                    {data.aliases.map((a) => (
                      <span key={a} className="alias">
                        {a}
                      </span>
                    ))}
                  </div>
                  <div style={{ fontSize: 12.5, color: "var(--ink-2)", maxWidth: 760 }}>
                    {data.notes || (
                      <span style={{ color: "var(--ink-4)", fontStyle: "italic" }}>{t("noNotes")}</span>
                    )}
                  </div>
                </>
              )}
            </div>
          </div>

          <div
            style={{
              padding: "18px 20px",
              borderLeft: "1px solid var(--line-2)",
              background: "var(--panel-2)",
              display: "grid",
              gap: 10,
              alignContent: "center",
              minWidth: 220,
            }}
          >
            <div className="kv">
              <span className="k">{t("stats.first")}</span>
              <span className="v" style={{ fontFamily: "var(--mono)" }}>
                {fmtDate(stats?.first ?? null)}
              </span>
              <span className="k">{t("stats.last")}</span>
              <span className="v" style={{ fontFamily: "var(--mono)" }}>
                {fmtDate(stats?.last ?? null)}
              </span>
              <span className="k">{t("stats.span")}</span>
              <span className="v" style={{ fontFamily: "var(--mono)" }}>
                {stats?.first && stats?.last ? monthsBetween(stats.first, stats.last) : "—"}
              </span>
              <span className="k">{t("stats.blocking")}</span>
              <span className="v">
                {stats && stats.blocking > 0 ? (
                  <span
                    className="badge"
                    style={{
                      background: "var(--err-50)",
                      color: "var(--err)",
                      borderColor: "var(--err-100)",
                    }}
                  >
                    <Ico name="alert" size={10} />
                    {t("stats.blockingCount", { n: stats.blocking })}
                  </span>
                ) : (
                  <span style={{ color: "var(--ink-4)", fontFamily: "var(--mono)" }}>{t("stats.blockingNone")}</span>
                )}
              </span>
            </div>
          </div>
        </div>

        {/* Grouped tables */}
        {data.cases.length === 0 ? (
          <div className="card empty">{t("noCases")}</div>
        ) : (
          <div style={{ display: "grid", gap: 14 }}>
            {Object.entries(byCategory).map(([cat, cases]) => {
              const reviewed = cases.filter((c) => c.review_status === "reviewed").length;
              return (
                <div key={cat} className="card">
                  <div className="card-h">
                    <div className="t">
                      <CategoryPill value={cat as Category} />
                      <span style={{ marginLeft: 4 }}>{t("group.caseCount", { n: cases.length })}</span>
                    </div>
                    <div className="meta" style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      <span>
                        {t("group.reviewed", { r: reviewed, total: cases.length })}
                      </span>
                      <Link to={`/cases?category=${cat}`} className="btn sm ghost">
                        {t("group.filterLink")} <Ico name="arrow-r" size={10} />
                      </Link>
                    </div>
                  </div>
                  <table className="table" style={{ borderRadius: "0 0 10px 10px", overflow: "hidden" }}>
                    <thead>
                      <tr>
                        <th>{t("table.caseDir")}</th>
                        <th style={{ width: 130 }}>{t("table.template")}</th>
                        <th style={{ width: 90 }}>{t("table.source")}</th>
                        <th style={{ width: 80 }}>{t("table.blocking")}</th>
                        <th style={{ width: 110 }}>{t("table.reviewStatus")}</th>
                        <th style={{ width: 130 }}>{t("table.lastModified")}</th>
                        <th style={{ width: 50 }}><span className="sr-only">{t("table.openColumn")}</span></th>
                      </tr>
                    </thead>
                    <tbody>
                      {cases.map((c) => {
                        const overridden = c.manual_category != null || c.manual_template_tier != null;
                        return (
                          <tr key={c.id} className={overridden ? "row-manual" : "row-auto"}>
                            <td>
                              <Link to={`/cases/${c.id}`} style={{ color: "var(--ink-1)" }}>
                                <span className="path">{c.abs_path.split("/").pop()}</span>
                              </Link>
                              {overridden && (
                                <span
                                  className="badge"
                                  style={{
                                    marginLeft: 6,
                                    height: 17,
                                    background: "var(--amber-50)",
                                    color: "var(--amber-ink)",
                                    borderColor: "var(--amber-200)",
                                  }}
                                >
                                  {t("table.manualOverride")}
                                </span>
                              )}
                            </td>
                            <td>
                              <TierPill value={c.template_tier} />
                            </td>
                            <td>
                              <span className="num">
                                {c.labeled_count ?? 0}
                                <span style={{ color: "var(--ink-4)" }}>
                                  {" "}/ {c.source_count ?? 0}
                                </span>
                              </span>
                            </td>
                            <td>
                              <IssueCountBadge count={c.blocking_issue_count} />
                            </td>
                            <td>
                              <ReviewPill status={c.review_status ?? "unreviewed"} />
                            </td>
                            <td>
                              <span className="num">
                                {new Date(c.last_modified).toLocaleDateString("zh-CN")}
                              </span>
                            </td>
                            <td>
                              <Link to={`/cases/${c.id}`} aria-label={t("table.openCase", { id: c.id })}>
                                <Ico name="arrow-r" size={12} style={{ color: "var(--ink-4)" }} />
                              </Link>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
