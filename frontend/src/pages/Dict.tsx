import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  resolveCandidates,
  type CandidateResult,
  type CaseSummary,
  type CustomerSummary,
} from "../api";
import {
  useCases,
  useCreateCustomer,
  useCustomers,
  useMergeCases,
} from "../hooks/queries";
import { Ico } from "../components/atoms";

interface UnboundGroup {
  customer_raw: string;
  case_ids: number[];
  cases: CaseSummary[];
  candidates: CandidateResult | null;
}

type Decision = "matched" | "candidates" | "new";

export default function Dict() {
  const { t } = useTranslation("dict");
  const [searchParams] = useSearchParams();
  const prefill = searchParams.get("prefill") ?? "";

  const casesQ = useCases({ limit: 2000 });
  const customersQ = useCustomers();
  const mergeMut = useMergeCases();
  const createMut = useCreateCustomer();

  const customers = customersQ.data ?? [];
  const loading = casesQ.isLoading || customersQ.isLoading;

  const [filter, setFilter] = useState("");
  const [decisionFilter, setDecisionFilter] = useState<"" | Decision>("");
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [createDraft, setCreateDraft] = useState({
    canonical_name: prefill,
    aliases: "",
  });

  // Group unbound cases by customer_raw — pure derivation from query data.
  const baseGroups = useMemo<UnboundGroup[]>(() => {
    const all = casesQ.data ?? [];
    const unbound = all.filter((c) => c.customer_id == null && c.customer_raw);
    const grouped = new Map<string, UnboundGroup>();
    for (const c of unbound) {
      const key = c.customer_raw!;
      const g = grouped.get(key) ?? {
        customer_raw: key,
        case_ids: [],
        cases: [],
        candidates: null,
      };
      g.case_ids.push(c.id);
      g.cases.push(c);
      grouped.set(key, g);
    }
    return Array.from(grouped.values()).sort((a, b) => b.cases.length - a.cases.length);
  }, [casesQ.data]);

  // Enrich with candidates (one-shot per group; not cached as a query because
  // the input space is unbounded and depends on group state).
  const [groups, setGroups] = useState<UnboundGroup[]>([]);
  useEffect(() => {
    setGroups(baseGroups);
    if (baseGroups.length === 0) return;
    let cancelled = false;
    Promise.all(
      baseGroups.map((g) =>
        resolveCandidates(g.customer_raw).catch(() => null)
      )
    ).then((cands) => {
      if (cancelled) return;
      setGroups(
        baseGroups.map((g, i) => ({ ...g, candidates: cands[i] }))
      );
    });
    return () => {
      cancelled = true;
    };
  }, [baseGroups]);

  useEffect(() => {
    if (prefill) {
      setShowCreate(true);
      setCreateDraft((d) => ({ ...d, canonical_name: prefill }));
    }
  }, [prefill]);

  const filtered = useMemo(() => {
    let list = groups;
    if (filter.trim()) list = list.filter((g) => g.customer_raw.includes(filter.trim()));
    if (decisionFilter)
      list = list.filter((g) => (g.candidates?.decision ?? "new") === decisionFilter);
    return list;
  }, [groups, filter, decisionFilter]);

  const totalUnbound = groups.reduce((a, g) => a + g.cases.length, 0);
  const highConf = groups.filter(
    (g) => g.candidates?.decision === "matched" && (g.candidates?.candidates[0]?.similarity ?? 0) >= 0.86,
  );

  const bindToExisting = async (group: UnboundGroup, customerId: number) => {
    setBusyKey(group.customer_raw);
    try {
      await mergeMut.mutateAsync({ customerId, caseIds: group.case_ids });
    } finally {
      setBusyKey(null);
    }
  };

  const createAndBind = async (
    group: UnboundGroup | null,
    payload: { canonical_name: string; aliases: string[] },
  ) => {
    setBusyKey(group?.customer_raw ?? "create");
    try {
      const customer = await createMut.mutateAsync({
        canonical_name: payload.canonical_name,
        aliases: payload.aliases,
      });
      if (group) {
        await mergeMut.mutateAsync({
          customerId: customer.id,
          caseIds: group.case_ids,
        });
      }
      setShowCreate(false);
      setCreateDraft({ canonical_name: "", aliases: "" });
    } finally {
      setBusyKey(null);
    }
  };

  const applyHighConfidence = async () => {
    setBusyKey("__bulk__");
    try {
      for (const g of highConf) {
        const top = g.candidates?.candidates[0];
        if (top) {
          await mergeMut.mutateAsync({
            customerId: top.id,
            caseIds: g.case_ids,
          });
        }
      }
    } finally {
      setBusyKey(null);
    }
  };

  return (
    <div style={{ height: "100%", display: "grid", gridTemplateRows: "auto 1fr", overflow: "hidden" }}>
      <div className="page-header">
        <div>
          <h1 className="page-title">{t("title")}</h1>
          <div className="page-sub">
            {t("subtitle")}
          </div>
        </div>
      </div>

      <div
        style={{
          padding: "16px 24px",
          overflow: "auto",
          display: "grid",
          gap: 14,
          alignContent: "start",
        }}
      >
        {/* Banner */}
        {totalUnbound > 0 && (
          <div className="banner">
            <div className="ico-bg">
              <Ico name="alert" size={20} />
            </div>
            <div style={{ flex: 1 }}>
              <div className="ttl">
                {t("banner.title", { cases: totalUnbound, groups: groups.length })}
              </div>
              <div className="sub">
                {t("banner.subtitle")}
              </div>
            </div>
            <div className="right">
              <button
                className="btn"
                onClick={() => casesQ.refetch()}
                disabled={loading || casesQ.isFetching}
              >
                <Ico name="refresh" size={12} />
                {loading || casesQ.isFetching ? t("banner.refreshing") : t("banner.rematch")}
              </button>
              <button className="btn primary" onClick={() => setShowCreate(true)}>
                <Ico name="plus" size={12} />
                {t("banner.newCustomer")}
              </button>
            </div>
          </div>
        )}

        {/* Create panel */}
        {showCreate && (
          <div className="card" style={{ borderColor: "var(--ink-1)" }}>
            <div className="card-h">
              <div className="t">
                <Ico name="plus" size={13} />
                {t("create.title")}
              </div>
            </div>
            <div className="card-b" style={{ display: "grid", gap: 10 }}>
              <div>
                <label style={{ fontSize: 11.5, color: "var(--ink-3)" }}>{t("create.canonical")}</label>
                <input
                  value={createDraft.canonical_name}
                  onChange={(e) => setCreateDraft({ ...createDraft, canonical_name: e.target.value })}
                  style={{ width: "100%", marginTop: 4 }}
                  // eslint-disable-next-line jsx-a11y/no-autofocus -- intentional: focus first field when create-customer dialog opens
                  autoFocus
                />
              </div>
              <div>
                <label style={{ fontSize: 11.5, color: "var(--ink-3)" }}>{t("create.aliases")}</label>
                <textarea
                  value={createDraft.aliases}
                  onChange={(e) => setCreateDraft({ ...createDraft, aliases: e.target.value })}
                  style={{ width: "100%", marginTop: 4, minHeight: 60 }}
                />
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  className="btn primary"
                  disabled={!createDraft.canonical_name.trim() || busyKey === "create"}
                  onClick={() =>
                    createAndBind(null, {
                      canonical_name: createDraft.canonical_name.trim(),
                      aliases: createDraft.aliases
                        .split("\n")
                        .map((s) => s.trim())
                        .filter(Boolean),
                    })
                  }
                >
                  <Ico name="check" size={12} />
                  {t("create.submit")}
                </button>
                <button className="btn" onClick={() => setShowCreate(false)}>
                  {t("create.cancel")}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Filter row */}
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div className="search">
            <Ico name="search" />
            <input
              placeholder={t("filter.placeholder")}
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          </div>
          <label className="select" style={{ minWidth: 140 }}>
            <span style={{ color: "var(--ink-3)", fontSize: 11.5 }}>{t("filter.decisionLabel")}</span>
            <span style={{ color: "var(--ink-1)" }}>
              {decisionFilter === ""
                ? t("filter.all")
                : decisionFilter === "matched"
                  ? t("filter.matched")
                  : decisionFilter === "candidates"
                    ? t("filter.candidates")
                    : t("filter.new")}
            </span>
            <select
              value={decisionFilter}
              onChange={(e) => setDecisionFilter(e.target.value as Decision | "")}
              style={{ position: "absolute", inset: 0, opacity: 0, cursor: "pointer", border: 0 }}
            >
              <option value="">{t("filter.all")}</option>
              <option value="matched">{t("filter.matched")}</option>
              <option value="candidates">{t("filter.candidates")}</option>
              <option value="new">{t("filter.new")}</option>
            </select>
          </label>
          <div
            style={{
              marginLeft: "auto",
              display: "flex",
              alignItems: "center",
              gap: 6,
              fontSize: 11.5,
            }}
          >
            <span style={{ color: "var(--ink-3)" }}>{t("filter.bulkLabel")}</span>
            <button
              className="btn sm primary"
              disabled={highConf.length === 0 || busyKey === "__bulk__"}
              onClick={applyHighConfidence}
            >
              <Ico name="check" size={11} />
              {busyKey === "__bulk__"
                ? t("filter.applying")
                : t("filter.applyBulk", { n: highConf.length })}
            </button>
          </div>
        </div>

        {/* Cards grid */}
        {filtered.length === 0 ? (
          <div className="card empty">
            {loading ? t("empty.loading") : t("empty.allBound")}
          </div>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 12 }}>
            {filtered.map((g) => (
              <DictCard
                key={g.customer_raw}
                group={g}
                customers={customers}
                busy={busyKey === g.customer_raw}
                onBind={bindToExisting}
                onCreate={(name, aliases) => createAndBind(g, { canonical_name: name, aliases })}
              />
            ))}
          </div>
        )}

        <div style={{ fontSize: 11.5, color: "var(--ink-3)", textAlign: "center", padding: "8px 0 4px" }}>
          {t("footer")}
        </div>
      </div>
    </div>
  );
}

function DecisionBadge({ d, count }: { d: Decision; count: number }) {
  const { t } = useTranslation("dict");
  if (d === "matched")
    return (
      <span className="badge" style={{ background: "var(--ok-50)", color: "var(--ok)", borderColor: "var(--ok-100)" }}>
        <Ico name="check" size={10} />
        {t("card.decisionMatched")}
      </span>
    );
  if (d === "candidates")
    return (
      <span className="badge" style={{ background: "var(--cyan-50)", color: "var(--cyan-ink)", borderColor: "var(--cyan-200)" }}>
        <Ico name="users" size={10} />
        {count} {t("card.candidateCountSuffix")}
      </span>
    );
  return (
    <span className="badge" style={{ background: "var(--amber-50)", color: "var(--amber-ink)", borderColor: "var(--amber-200)" }}>
      <Ico name="plus" size={10} />
      {t("card.decisionNewSuggest")}
    </span>
  );
}

function DictCard({
  group,
  customers,
  busy,
  onBind,
  onCreate,
}: {
  group: UnboundGroup;
  customers: CustomerSummary[];
  busy: boolean;
  onBind: (g: UnboundGroup, customerId: number) => void;
  onCreate: (name: string, aliases: string[]) => void;
}) {
  const { t } = useTranslation("dict");
  const cands = group.candidates?.candidates ?? [];
  const decision: Decision = group.candidates?.decision ?? "new";
  const normalized = group.candidates?.normalized ?? group.customer_raw;
  const [showAll, setShowAll] = useState(false);

  return (
    <div
      className="card"
      style={{ borderColor: decision === "new" ? "var(--amber-200)" : "var(--line)" }}
    >
      <div
        className="card-h"
        style={{
          background:
            decision === "matched"
              ? "var(--ok-50)"
              : decision === "new"
                ? "var(--amber-50)"
                : "var(--panel-2)",
          borderBottom:
            "1px solid " +
            (decision === "matched"
              ? "var(--ok-100)"
              : decision === "new"
                ? "var(--amber-200)"
                : "var(--line-2)"),
        }}
      >
        <div className="t" style={{ minWidth: 0, gap: 10 }}>
          <span
            style={{
              fontFamily: "var(--mono)",
              color: "var(--ink-1)",
              fontSize: 13.5,
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {group.customer_raw}
          </span>
          <span className="badge">
            <Ico name="folder" size={10} />
            {group.cases.length} {t("card.caseSuffix")}
          </span>
        </div>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <DecisionBadge d={decision} count={cands.length} />
        </div>
      </div>

      <div className="card-b" style={{ display: "grid", gap: 10 }}>
        {/* Direction line */}
        <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12.5 }}>
          <span style={{ fontFamily: "var(--mono)", color: "var(--ink-3)", fontSize: 11.5 }}>
            {group.customer_raw}
          </span>
          <Ico name="arrow" size={12} style={{ color: "var(--ink-4)" }} />
          {decision === "matched" && cands[0] && (
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <span style={{ fontWeight: 600 }}>{cands[0].canonical_name}</span>
              <span
                className="badge"
                style={{
                  background: "var(--ok-50)",
                  color: "var(--ok)",
                  borderColor: "var(--ok-100)",
                }}
              >
                {t("card.regularized")}
              </span>
            </span>
          )}
          {decision === "candidates" && (
            <span style={{ color: "var(--ink-3)", fontStyle: "italic" }}>
              {t("card.pickCandidate")}
            </span>
          )}
          {decision === "new" && (
            <span style={{ color: "var(--amber-ink)", fontStyle: "italic" }}>
              {t("card.needCreate", { name: normalized })}
            </span>
          )}
        </div>

        {/* Candidates */}
        {cands.length > 0 && (
          <div style={{ display: "grid", gap: 5 }}>
            <div
              style={{
                fontSize: 10.5,
                color: "var(--ink-4)",
                textTransform: "uppercase",
                letterSpacing: 0.4,
              }}
            >
              {t("card.candidatesSection", { n: cands.length })}
            </div>
            {cands.map((c, j) => {
              const sim = Math.round((c.similarity ?? 0) * 100);
              return (
                <button
                  key={c.id}
                  className={"cand-btn" + (j === 0 && decision === "matched" ? " suggest" : "")}
                  disabled={busy}
                  onClick={() => onBind(group, c.id)}
                >
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                    <span style={{ fontWeight: 600 }}>{c.canonical_name}</span>
                    <span style={{ fontSize: 11, color: "var(--ink-3)" }}>{t("card.candidateCases", { n: c.case_count })}</span>
                  </span>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    <span
                      style={{
                        width: 56,
                        height: 4,
                        background: "var(--bg-2)",
                        borderRadius: 999,
                        overflow: "hidden",
                      }}
                    >
                      <span
                        style={{
                          display: "block",
                          width: sim + "%",
                          height: "100%",
                          background:
                            sim > 80 ? "var(--ok)" : sim > 60 ? "var(--cyan)" : "var(--ink-4)",
                          borderRadius: 999,
                        }}
                      ></span>
                    </span>
                    <span className="sim">
                      {c.similarity != null ? `${sim}%` : "alias"}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        )}

        {/* Actions */}
        <div
          style={{
            display: "flex",
            gap: 6,
            paddingTop: 4,
            borderTop: "1px solid var(--line-2)",
            marginTop: 2,
            flexWrap: "wrap",
          }}
        >
          {decision === "matched" && cands[0] && (
            <button
              className="btn sm primary"
              style={{ flex: 1, justifyContent: "center" }}
              disabled={busy}
              onClick={() => onBind(group, cands[0].id)}
            >
              <Ico name="check" size={11} />
              {busy ? t("card.actionMatchedBusy") : t("card.actionMatchedConfirm", { name: cands[0].canonical_name })}
            </button>
          )}
          {decision === "candidates" && (
            <button
              className="btn sm"
              style={{ flex: 1, justifyContent: "center" }}
              disabled
            >
              {t("card.actionPickAbove")}
            </button>
          )}
          {decision === "new" && (
            <button
              className="btn sm"
              style={{
                flex: 1,
                justifyContent: "center",
                background: "var(--ok-50)",
                color: "var(--ok)",
                borderColor: "var(--ok-100)",
              }}
              disabled={busy}
              onClick={() => onCreate(normalized, [group.customer_raw])}
            >
              <Ico name="plus" size={11} />
              {t("card.actionCreateBind", { n: group.cases.length })}
            </button>
          )}
          {customers.length > 0 && (
            <button className="btn sm" onClick={() => setShowAll(!showAll)}>
              {showAll ? t("card.manualPickHide") : t("card.manualPickShow")}
            </button>
          )}
        </div>

        {showAll && customers.length > 0 && (
          <div
            style={{
              marginTop: 4,
              display: "flex",
              gap: 4,
              flexWrap: "wrap",
              padding: 8,
              background: "var(--bg-2)",
              borderRadius: 6,
            }}
          >
            {customers.map((c) => (
              <button
                key={c.id}
                className="chip"
                disabled={busy}
                onClick={() => onBind(group, c.id)}
              >
                {c.canonical_name} ({c.case_count})
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
