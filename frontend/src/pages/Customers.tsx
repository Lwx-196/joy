import { useState } from "react";
import { Link } from "react-router-dom";
import { Trans, useTranslation } from "react-i18next";
import { type CustomerSummary } from "../api";
import { useCustomers } from "../hooks/queries";
import { Ico } from "../components/atoms";

type SortMode = "cases" | "name" | "recent";

export default function Customers() {
  const { t } = useTranslation(["customers", "common"]);
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<SortMode>("cases");

  // q feeds directly into the queryKey — each search term caches independently.
  const customersQ = useCustomers(q || undefined);
  const list = customersQ.data ?? [];
  const loading = customersQ.isLoading;

  const sorted = [...list].sort((a, b) => {
    if (sort === "cases") return b.case_count - a.case_count;
    if (sort === "name") return a.canonical_name.localeCompare(b.canonical_name, "zh-Hans-CN");
    return b.id - a.id;
  });

  const sortLabel =
    sort === "cases" ? t("sort.byCases") : sort === "name" ? t("sort.byName") : t("sort.byRecent");

  return (
    <div style={{ height: "100%", display: "grid", gridTemplateRows: "auto 1fr", overflow: "hidden" }}>
      <div className="page-header">
        <div>
          <h1 className="page-title">
            {t("title")}{" "}
            <span style={{ fontFamily: "var(--mono)", color: "var(--ink-3)", fontSize: 14, fontWeight: 500, marginLeft: 6 }}>
              {sorted.length}
            </span>
          </h1>
          <div className="page-sub">
            {t("subtitle.prefix")} {sorted.length}{" "}
            · <Link to="/dict" style={{ color: "var(--ink-1)", textDecorationStyle: "dotted", textDecoration: "underline" }}>{t("subtitle.dictLink")}</Link>
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <div className="search">
            <Ico name="search" />
            <input
              placeholder={t("search.placeholder")}
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
          </div>
          <label className="select" style={{ minWidth: 130 }}>
            <span style={{ color: "var(--ink-3)", fontSize: 11.5 }}>{t("sort.label")}</span>
            <span style={{ color: "var(--ink-1)" }}>{sortLabel}</span>
            <select
              value={sort}
              onChange={(e) => setSort(e.target.value as SortMode)}
              style={{ position: "absolute", inset: 0, opacity: 0, cursor: "pointer", border: 0 }}
            >
              <option value="cases">{t("sort.byCases")}</option>
              <option value="name">{t("sort.byName")}</option>
              <option value="recent">{t("sort.byRecent")}</option>
            </select>
          </label>
        </div>
      </div>

      <div style={{ padding: "18px 24px", overflow: "auto" }}>
        {loading && sorted.length === 0 ? (
          <div className="empty">{t("common:common.loading")}</div>
        ) : sorted.length === 0 ? (
          <div className="empty">
            <Trans
              ns="customers"
              i18nKey="empty.title"
              components={{ dictLink: <Link to="/dict" style={{ color: "var(--ink-1)" }} /> }}
            />
          </div>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 14 }}>
            {sorted.map((c) => (
              <CustomerCard key={c.id} c={c} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function CustomerCard({ c }: { c: CustomerSummary }) {
  const { t } = useTranslation("customers");
  return (
    <div
      className="card"
      style={{ display: "grid", gridTemplateRows: "auto auto auto 1fr auto", minHeight: 180 }}
    >
      <div
        style={{
          padding: "12px 14px 8px",
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "space-between",
          gap: 8,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
          <div
            style={{
              width: 36,
              height: 36,
              borderRadius: 8,
              background: "var(--bg-2)",
              border: "1px solid var(--line)",
              display: "grid",
              placeItems: "center",
              fontSize: 14,
              fontWeight: 600,
              color: "var(--ink-2)",
              flexShrink: 0,
            }}
          >
            {c.canonical_name.slice(-1)}
          </div>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: 16, fontWeight: 600, letterSpacing: "-0.2px" }}>
              <Link to={`/customers/${c.id}`} style={{ color: "var(--ink-1)" }}>
                {c.canonical_name}
              </Link>
            </div>
            <div style={{ fontSize: 11, color: "var(--ink-4)", fontFamily: "var(--mono)" }}>
              id: {c.id}
            </div>
          </div>
        </div>
        <span
          className="badge"
          style={{
            background: "var(--ink-1)",
            color: "#fff",
            borderColor: "var(--ink-1)",
            fontFamily: "var(--mono)",
          }}
        >
          {c.case_count} <span style={{ opacity: 0.7, fontWeight: 400, marginLeft: 2 }}>{t("card.caseSuffix")}</span>
        </span>
      </div>

      {c.aliases.length > 0 && (
        <div style={{ padding: "0 14px 8px" }}>
          <div
            style={{
              fontSize: 10.5,
              color: "var(--ink-4)",
              textTransform: "uppercase",
              letterSpacing: 0.5,
              marginBottom: 4,
            }}
          >
            aliases · {c.aliases.length}
          </div>
          <div className="aliases">
            {c.aliases.map((a) => (
              <span key={a} className="alias">{a}</span>
            ))}
          </div>
        </div>
      )}

      <div style={{ padding: "0 14px 8px" }}>
        <div
          style={{
            fontSize: 11.5,
            color: c.notes ? "var(--ink-2)" : "var(--ink-4)",
            display: "-webkit-box",
            WebkitLineClamp: 2,
            WebkitBoxOrient: "vertical",
            overflow: "hidden",
          }}
        >
          {c.notes || <span style={{ fontStyle: "italic" }}>{t("card.noNotes")}</span>}
        </div>
      </div>

      <div></div>

      <div
        style={{
          padding: "8px 14px",
          borderTop: "1px solid var(--line-2)",
          background: "var(--panel-2)",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <div style={{ fontSize: 10.5, color: "var(--ink-3)" }}>
          {t("card.bound", { count: c.case_count })}
        </div>
        <Link to={`/customers/${c.id}`} className="btn sm">
          {t("card.detail")}<Ico name="arrow-r" size={10} />
        </Link>
      </div>
    </div>
  );
}
