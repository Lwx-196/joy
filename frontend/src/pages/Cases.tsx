import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { useVirtualizer } from "@tanstack/react-virtual";
import { CATEGORY_LABEL, TIER_LABEL, type Category, type CaseSummary } from "../api";
import {
  useBatchRenderCases,
  useBatchUpdateCases,
  useBatchUpgradeCases,
  useCases,
  useCustomers,
} from "../hooks/queries";
import { useBrand } from "../lib/brand-context";
import { useBatchJobToastStore } from "../lib/batch-job-toast";
import { isHeld } from "../lib/work-queue";
import { useHotkey } from "../hooks/useHotkey";

const MAX_BATCH_RENDER = 50;
const MAX_BATCH_UPGRADE = 50;
/** Activate row virtualization when list grows beyond this. Below the
 * threshold, plain table rendering wins (DOM count is small, and virtualization
 * adds ResizeObserver overhead + breaks browser find-on-page). */
const VIRTUALIZE_THRESHOLD = 80;
const VIRTUAL_ROW_HEIGHT = 56;

/** Column widths shared between thead and virtualized rows. Each virtualized
 * tr is its own `display: table` (a side-effect of mixing virtualization with
 * <table> + position:absolute), so it can't inherit thead's column sizing.
 * We mirror these widths onto the tds via inline style so virtualized rows
 * line up with thead.
 *
 * All widths are explicit (no auto-fill column) — under `tableLayout: fixed`
 * the auto column collapses to 0 when the sum equals tr width. The 案例目录
 * column gets a generous 480px since case names can be long; horizontal scroll
 * (scrollerRef has overflow:auto) handles the case where the total exceeds
 * viewport width. */
const COL_WIDTHS: number[] = [
  36,   // checkbox
  130,  // 客户
  480,  // 案例目录 (case path)
  196,  // 类别
  196,  // 模板
  78,   // 源图
  72,   // 阻塞
  110,  // 审核状态
  116,  // 最后修改
];
const TABLE_MIN_WIDTH = COL_WIDTHS.reduce((a, b) => a + b, 0); // 1414px
import { ImportCsvModal } from "../components/ImportCsvModal";
import {
  CategoryPill,
  Check,
  Ico,
  IssueCountBadge,
  ReviewPill,
  TierPill,
} from "../components/atoms";

/** Decide which manual-edit state a row falls into for visual classification. */
function rowStateClass(c: CaseSummary, now: Date): string {
  if (isHeld(c, now)) return "row-held";
  const overridden = c.manual_category != null || c.manual_template_tier != null;
  if (overridden) return "row-overridden";
  const supplemented =
    !!c.notes ||
    c.tags.length > 0 ||
    // manual_blocking_codes is exposed as part of blocking_issue_count via the
    // backend merge; we approximate by checking notes/tags here. The detail page
    // shows the precise breakdown.
    false;
  return supplemented ? "row-supplemented" : "row-auto";
}

export default function Cases() {
  const { t } = useTranslation("cases");
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [category, setCategory] = useState<string>(searchParams.get("category") ?? "");
  const [tier, setTier] = useState<string>("");
  const [customerId, setCustomerId] = useState<string>("");
  const [reviewStatus, setReviewStatus] = useState<string>(searchParams.get("review") ?? "");
  const [keyword, setKeyword] = useState<string>("");
  // Dashboard work-queue lanes pass these as URL params; we filter client-side.
  const sinceFilter = searchParams.get("since"); // "today" | null
  const blockingFilter = searchParams.get("blocking"); // "open" | null
  // 挂起的 case 默认隐藏（"挂起" 的语义就是"我现在不想看到它"）。
  const [showHeld, setShowHeld] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [importOpen, setImportOpen] = useState(false);
  // j/k row navigation. -1 = no row highlighted; first j makes it 0.
  const [currentIndex, setCurrentIndex] = useState(-1);
  const [batchDraft, setBatchDraft] = useState({
    manual_category: "" as "" | Category,
    manual_template_tier: "",
    review_status: "",
    customer_id: "",
    notes: "",
  });

  // Build query params object — useCases keys on this so changes auto-refetch.
  const params = useMemo(() => {
    const p: Parameters<typeof useCases>[0] = { limit: 1000 };
    if (category) p.category = category;
    if (tier) p.tier = tier;
    if (customerId) p.customer_id = Number(customerId);
    if (reviewStatus) p.review_status = reviewStatus;
    return p;
  }, [category, tier, customerId, reviewStatus]);

  const casesQ = useCases(params);
  const customersQ = useCustomers();
  const batchMut = useBatchUpdateCases();
  // Phase 3: batch render hook + global toast.
  const batchRenderMut = useBatchRenderCases();
  // Stage 2: batch v3 upgrade hook (shares the same job toast).
  const batchUpgradeMut = useBatchUpgradeCases();
  const showBatchToast = useBatchJobToastStore((s) => s.show);
  const brand = useBrand();

  const cases = casesQ.data ?? [];
  const customers = customersQ.data ?? [];
  const loading = casesQ.isLoading;
  const busy = batchMut.isPending;

  // Prune selection when underlying list shifts (mutation refetched).
  useEffect(() => {
    setSelected((prev) => {
      const ids = new Set(cases.map((c) => c.id));
      const next = new Set([...prev].filter((id) => ids.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [cases]);

  // Reset selection when filter context changes (dropdowns / lane filters / showHeld).
  // Keyword search is intentionally excluded — typing into the search box would
  // otherwise wipe selection mid-keystroke. The dropdown-style filters are
  // discrete context switches, so any change there means the user is looking
  // at a different scope and stale selections become misleading.
  useEffect(() => {
    setSelected((prev) => (prev.size === 0 ? prev : new Set()));
    setCurrentIndex(-1);
  }, [category, tier, customerId, reviewStatus, showHeld, sinceFilter, blockingFilter]);

  const heldCount = useMemo(() => {
    const now = new Date();
    return cases.filter((c) => isHeld(c, now)).length;
  }, [cases]);

  const filtered = useMemo(() => {
    let list = cases;
    const now = new Date();
    // 默认隐藏挂起；用户点 "查看挂起" 切换。
    if (!showHeld) {
      list = list.filter((c) => !isHeld(c, now));
    }
    // Dashboard work-queue lane filters (client-side).
    if (sinceFilter === "today") {
      list = list.filter((c) => {
        const t = new Date(c.last_modified);
        return (
          t.getFullYear() === now.getFullYear() &&
          t.getMonth() === now.getMonth() &&
          t.getDate() === now.getDate()
        );
      });
    }
    if (blockingFilter === "open") {
      list = list.filter(
        (c) => c.blocking_issue_count > 0 && c.review_status !== "reviewed"
      );
    }
    if (keyword.trim()) {
      const k = keyword.trim();
      list = list.filter(
        (c) =>
          c.abs_path.includes(k) ||
          (c.customer_raw ?? "").includes(k) ||
          (c.customer_canonical ?? "").includes(k) ||
          (c.notes ?? "").includes(k) ||
          c.tags.some((t) => t.includes(k)),
      );
    }
    return list;
  }, [cases, keyword, sinceFilter, blockingFilter, showHeld]);

  // Row virtualization for large lists (>80). Track 2 / Stage 6 — verified via
  // /_playground/virtualization 4-phase repro before landing here.
  const scrollerRef = useRef<HTMLDivElement>(null);
  const useVirtual = filtered.length > VIRTUALIZE_THRESHOLD;
  const virtualizer = useVirtualizer({
    count: filtered.length,
    getScrollElement: () => scrollerRef.current,
    estimateSize: () => VIRTUAL_ROW_HEIGHT,
    overscan: 8,
  });

  // Clamp currentIndex when filter narrows the list out from under us.
  useEffect(() => {
    if (currentIndex >= filtered.length) {
      setCurrentIndex(filtered.length === 0 ? -1 : filtered.length - 1);
    }
  }, [filtered.length, currentIndex]);

  const allSelected = filtered.length > 0 && filtered.every((c) => selected.has(c.id));
  const someSelected = selected.size > 0 && !allSelected;
  const toggleAll = () => {
    setSelected((prev) => {
      if (allSelected) {
        const next = new Set(prev);
        filtered.forEach((c) => next.delete(c.id));
        return next;
      }
      const next = new Set(prev);
      filtered.forEach((c) => next.add(c.id));
      return next;
    });
  };
  const toggleOne = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // j/k row navigation. ignoreInEditable=true (default in useHotkey) means
  // typing in the keyword search box won't trigger a move.
  const moveRow = useCallback(
    (delta: number) => {
      if (filtered.length === 0) return;
      setCurrentIndex((prev) => {
        if (prev < 0) return delta > 0 ? 0 : filtered.length - 1;
        return Math.max(0, Math.min(filtered.length - 1, prev + delta));
      });
    },
    [filtered.length]
  );
  useHotkey("j", () => moveRow(1), { preventDefault: true });
  useHotkey("k", () => moveRow(-1), { preventDefault: true });
  // Home/End jump to first/last row. Replaces vim-style `gg` because the
  // global Layout owns the `g`-chord prefix for route navigation; reusing `g`
  // here would race with that.
  useHotkey("home", () => filtered.length > 0 && setCurrentIndex(0), {
    preventDefault: true,
  });
  useHotkey("end", () => filtered.length > 0 && setCurrentIndex(filtered.length - 1), {
    preventDefault: true,
  });
  // Shift+G also jumps to last (vim convention; no chord conflict because
  // Layout's g-chord requires !shiftKey).
  useHotkey("shift+g", () => filtered.length > 0 && setCurrentIndex(filtered.length - 1), {
    preventDefault: true,
  });
  // Enter opens CaseDetail for the highlighted row.
  useHotkey(
    "enter",
    () => {
      const c = filtered[currentIndex];
      if (c) navigate(`/cases/${c.id}`);
    },
    { preventDefault: true }
  );
  // x toggles the highlighted row's checkbox (multi-select).
  useHotkey(
    "x",
    () => {
      const c = filtered[currentIndex];
      if (c) toggleOne(c.id);
    },
    { preventDefault: true }
  );

  // Scroll the highlighted row into view. Virtualizer handles offscreen rows
  // that aren't mounted yet; for the plain tbody we walk the DOM by data-index.
  useEffect(() => {
    if (currentIndex < 0) return;
    if (useVirtual) {
      virtualizer.scrollToIndex(currentIndex, { align: "auto" });
    } else {
      const tr = scrollerRef.current?.querySelector(
        `tr[data-index="${currentIndex}"]`
      );
      tr?.scrollIntoView({ block: "nearest" });
    }
  }, [currentIndex, useVirtual, virtualizer]);

  const applyBatch = () => {
    if (selected.size === 0) return;
    const update: Parameters<typeof batchMut.mutate>[0]["update"] = {};
    if (batchDraft.manual_category) update.manual_category = batchDraft.manual_category;
    if (batchDraft.manual_template_tier) update.manual_template_tier = batchDraft.manual_template_tier;
    if (batchDraft.review_status) update.review_status = batchDraft.review_status as never;
    if (batchDraft.customer_id) update.customer_id = Number(batchDraft.customer_id);
    if (batchDraft.notes) update.notes = batchDraft.notes;
    if (Object.keys(update).length === 0) return;
    batchMut.mutate(
      { caseIds: [...selected], update },
      {
        onSuccess: () => {
          setBatchDraft({
            manual_category: "",
            manual_template_tier: "",
            review_status: "",
            customer_id: "",
            notes: "",
          });
          setSelected(new Set());
        },
      }
    );
  };

  const clearOverrides = () => {
    if (selected.size === 0) return;
    batchMut.mutate({
      caseIds: [...selected],
      update: { clear_fields: ["manual_category", "manual_template_tier"] },
    });
  };

  return (
    <div style={{ height: "100%", display: "grid", gridTemplateRows: "auto auto 1fr", overflow: "hidden" }}>
      {/* Header */}
      <div className="page-header">
        <div>
          <h1 className="page-title">
            {t("title")}{" "}
            <span style={{ fontFamily: "var(--mono)", color: "var(--ink-3)", fontSize: 14, fontWeight: 500, marginLeft: 6 }}>
              {filtered.length}
            </span>
          </h1>
          <div className="page-sub">{t("subtitle")}</div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button
            type="button"
            className="btn sm ghost"
            onClick={() => setImportOpen(true)}
            title={t("headerActions.importCsvHint")}
          >
            <Ico name="copy" size={12} />
            {t("headerActions.importCsv")}
          </button>
          <button className="btn sm ghost">
            <Ico name="copy" size={12} />
            {t("headerActions.exportCsv")}
          </button>
          <button className="btn sm">
            <Ico name="filter" size={12} />
            {t("headerActions.columnSettings")}
          </button>
        </div>
      </div>

      {/* Filters */}
      <div
        style={{
          padding: "10px 24px",
          display: "flex",
          alignItems: "center",
          gap: 8,
          background: "var(--panel-2)",
          borderBottom: "1px solid var(--line)",
          flexWrap: "wrap",
        }}
      >
        <FilterSelect label={t("filter.category")} value={category} onChange={setCategory} options={[["", t("filter.all")], ...Object.entries(CATEGORY_LABEL)]} />
        <FilterSelect label={t("filter.tier")} value={tier} onChange={setTier} options={[["", t("filter.all")], ...Object.entries(TIER_LABEL)]} />
        <FilterSelect
          label={t("filter.customer")}
          value={customerId}
          onChange={setCustomerId}
          options={[
            ["", t("filter.all")],
            ...customers.map((c) => [String(c.id), `${c.canonical_name} (${c.case_count})`] as [string, string]),
          ]}
        />
        <FilterSelect
          label={t("filter.review")}
          value={reviewStatus}
          onChange={setReviewStatus}
          options={[
            ["", t("filter.all")],
            ["unreviewed", t("filter.unreviewed")],
            ["pending", t("filter.pending")],
            ["reviewed", t("filter.reviewed")],
            ["needs_recheck", t("filter.needsRecheck")],
          ]}
        />
        <div className="search" style={{ marginLeft: 4 }}>
          <Ico name="search" />
          <input
            placeholder={t("filter.searchPlaceholder")}
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            style={{ width: 220 }}
          />
        </div>
        {(category || tier || customerId || reviewStatus || keyword) && (
          <button
            className="btn sm ghost"
            onClick={() => {
              setCategory("");
              setTier("");
              setCustomerId("");
              setReviewStatus("");
              setKeyword("");
            }}
          >
            {t("filter.clear")}
          </button>
        )}
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 6, fontSize: 11.5 }}>
          {heldCount > 0 && (
            <button
              className="btn sm ghost"
              onClick={() => setShowHeld((v) => !v)}
              title={showHeld ? t("filter.showHeldHint") : t("filter.hideHeldHint")}
              style={{ borderStyle: "dashed", color: "var(--ink-3)" }}
            >
              <Ico name="eye" size={11} />
              {showHeld ? t("filter.hideHeld", { n: heldCount }) : t("filter.showHeld", { n: heldCount })}
            </button>
          )}
          <span className="badge" style={{ background: "var(--bg-2)" }}>
            {t("filter.displayLabel")} <span style={{ fontFamily: "var(--mono)", color: "var(--ink-1)", marginLeft: 4 }}>{filtered.length}</span> / {cases.length}
          </span>
        </div>
      </div>

      {/* Table area */}
      <div
        ref={scrollerRef}
        style={{ position: "relative", overflow: "auto", padding: "0 24px 24px" }}
      >
        {/* Sticky bulk bar when has selection */}
        {selected.size > 0 && (
          <div style={{ position: "sticky", top: 0, zIndex: 5, paddingTop: 12 }}>
            <div className="bulkbar">
              <Check state={allSelected ? "on" : someSelected ? "partial" : "off"} onClick={toggleAll} label={t("bulk.selectAll")} />
              <span style={{ fontSize: 12.5, color: "#FAFAF9" }}>
                {t("bulk.selected")}<b style={{ fontFamily: "var(--mono)" }}>{selected.size}</b>{t("bulk.selectedSuffix")}
              </span>
              <span className="sep"></span>
              <span className="lbl">{t("bulk.overrideCategory")}</span>
              <select
                value={batchDraft.manual_category}
                onChange={(e) => setBatchDraft({ ...batchDraft, manual_category: e.target.value as Category | "" })}
              >
                <option value="">{t("bulk.noChange")}</option>
                {Object.entries(CATEGORY_LABEL).map(([k, v]) => (
                  <option key={k} value={k}>{v}</option>
                ))}
              </select>
              <span className="lbl">{t("bulk.overrideTier")}</span>
              <select
                value={batchDraft.manual_template_tier}
                onChange={(e) => setBatchDraft({ ...batchDraft, manual_template_tier: e.target.value })}
              >
                <option value="">{t("bulk.noChange")}</option>
                {Object.entries(TIER_LABEL).map(([k, v]) => (
                  <option key={k} value={k}>{v}</option>
                ))}
              </select>
              <span className="lbl">{t("bulk.review")}</span>
              <select
                value={batchDraft.review_status}
                onChange={(e) => setBatchDraft({ ...batchDraft, review_status: e.target.value })}
              >
                <option value="">{t("bulk.noChange")}</option>
                <option value="pending">{t("filter.pending")}</option>
                <option value="reviewed">{t("filter.reviewed")}</option>
                <option value="needs_recheck">{t("filter.needsRecheck")}</option>
              </select>
              <span className="lbl">{t("bulk.bindCustomer")}</span>
              <select
                value={batchDraft.customer_id}
                onChange={(e) => setBatchDraft({ ...batchDraft, customer_id: e.target.value })}
                style={{ width: 130 }}
              >
                <option value="">{t("bulk.noChange")}</option>
                {customers.map((c) => (
                  <option key={c.id} value={c.id}>{c.canonical_name}</option>
                ))}
              </select>
              <span className="lbl">{t("bulk.notesLabel")}</span>
              <input
                placeholder={t("bulk.appendNotesPlaceholder")}
                value={batchDraft.notes}
                onChange={(e) => setBatchDraft({ ...batchDraft, notes: e.target.value })}
                style={{ width: 140 }}
              />
              <span className="sep"></span>
              <button className="btn primary" onClick={applyBatch} disabled={busy}>
                <Ico name="check" size={12} />
                {busy ? t("bulk.applying") : t("bulk.applyTo", { n: selected.size })}
              </button>
              <button className="btn danger" onClick={clearOverrides} disabled={busy}>
                <Ico name="x" size={12} />
                {t("bulk.clearOverride")}
              </button>
              <span className="sep"></span>
              <button
                className="btn"
                style={{ background: "var(--cyan-100, #ECFEFF)", color: "var(--cyan-ink, #0E7490)" }}
                onClick={() => {
                  if (selected.size === 0) return;
                  let ids = [...selected];
                  if (ids.length > MAX_BATCH_RENDER) {
                    const ok = window.confirm(
                      t("bulk.confirmRenderOver", { count: ids.length, max: MAX_BATCH_RENDER })
                    );
                    if (!ok) return;
                    ids = ids.slice(0, MAX_BATCH_RENDER);
                  } else {
                    const ok = window.confirm(
                      t("bulk.confirmRender", { count: ids.length, brand })
                    );
                    if (!ok) return;
                  }
                  batchRenderMut.mutate(
                    {
                      caseIds: ids,
                      payload: { brand, template: "tri-compare", semantic_judge: "off" },
                    },
                    {
                      onSuccess: (data) => {
                        showBatchToast("render", data.batch_id, ids.length);
                      },
                    }
                  );
                }}
                disabled={batchRenderMut.isPending}
              >
                <Ico name="image" size={12} />
                {batchRenderMut.isPending ? t("bulk.rendering") : t("bulk.batchRender", { n: selected.size })}
              </button>
              <button
                className="btn"
                style={{
                  background: "var(--purple-50, #FAF5FF)",
                  color: "var(--purple-ink, #6B21A8)",
                }}
                onClick={() => {
                  if (selected.size === 0) return;
                  let ids = [...selected];
                  if (ids.length > MAX_BATCH_UPGRADE) {
                    const ok = window.confirm(
                      t("bulk.confirmUpgradeOver", { count: ids.length, max: MAX_BATCH_UPGRADE })
                    );
                    if (!ok) return;
                    ids = ids.slice(0, MAX_BATCH_UPGRADE);
                  } else {
                    const ok = window.confirm(
                      t("bulk.confirmUpgrade", { count: ids.length, brand })
                    );
                    if (!ok) return;
                  }
                  batchUpgradeMut.mutate(
                    { caseIds: ids, brand },
                    {
                      onSuccess: (data) => {
                        showBatchToast("upgrade", data.batch_id, ids.length);
                      },
                    }
                  );
                }}
                disabled={batchUpgradeMut.isPending}
                title={t("bulk.batchUpgradeTitle")}
              >
                <Ico name="scan" size={12} />
                {batchUpgradeMut.isPending ? t("bulk.upgrading") : t("bulk.batchUpgrade", { n: selected.size })}
              </button>
            </div>
          </div>
        )}

        <table
          className="table"
          style={{
            marginTop: selected.size > 0 ? 14 : 12,
            background: "var(--panel)",
            border: "1px solid var(--line)",
            borderRadius: "var(--r-card)",
            overflow: "hidden",
            // tableLayout:fixed + minWidth forces the outer thead to respect
            // colgroup widths exactly, matching the virtualized tbody rows.
            // Without minWidth, auto layout scales colgroup hints down to fit
            // the parent's available width and thead drifts off tbody columns.
            tableLayout: "fixed",
            minWidth: TABLE_MIN_WIDTH,
            width: TABLE_MIN_WIDTH,
          }}
        >
          <colgroup>
            {COL_WIDTHS.map((w, i) => (
              <col key={i} style={w ? { width: w } : undefined} />
            ))}
          </colgroup>
          <thead>
            <tr>
              <th aria-label={t("bulk.selectAll")}>
                <Check state={allSelected ? "on" : someSelected ? "partial" : "off"} onClick={toggleAll} label={t("bulk.selectAll")} />
              </th>
              <th>{t("table.customer")}</th>
              <th>{t("table.caseDir")}</th>
              <th>{t("table.category")}</th>
              <th>{t("table.tier")}</th>
              <th>{t("table.source")}</th>
              <th>{t("table.blocking")}</th>
              <th>{t("table.review")}</th>
              <th>{t("table.lastModified")}</th>
            </tr>
          </thead>
          {useVirtual ? (
            <tbody
              style={{
                display: "block",
                position: "relative",
                width: "100%",
                height: virtualizer.getTotalSize(),
              }}
            >
              {virtualizer.getVirtualItems().map((vi) => {
                const c = filtered[vi.index];
                if (!c) return null;
                return (
                  <CaseRow
                    key={c.id}
                    c={c}
                    selected={selected}
                    toggleOne={toggleOne}
                    t={t}
                    isCurrent={vi.index === currentIndex}
                    virtualStyle={{
                      position: "absolute",
                      top: 0,
                      left: 0,
                      right: 0,
                      transform: `translateY(${vi.start}px)`,
                      display: "table",
                      width: "100%",
                      tableLayout: "fixed",
                    }}
                    dataIndex={vi.index}
                    measureRef={virtualizer.measureElement}
                  />
                );
              })}
            </tbody>
          ) : (
            <tbody>
              {filtered.map((c, idx) => (
                <CaseRow
                  key={c.id}
                  c={c}
                  selected={selected}
                  toggleOne={toggleOne}
                  t={t}
                  isCurrent={idx === currentIndex}
                  dataIndex={idx}
                />
              ))}
              {filtered.length === 0 && !loading && (
                <tr>
                  <td colSpan={9} className="empty">
                    {t("table.empty")}
                  </td>
                </tr>
              )}
            </tbody>
          )}
        </table>
      </div>
      <ImportCsvModal open={importOpen} onClose={() => setImportOpen(false)} />
    </div>
  );
}

/** Single case row — extracted so virtualized + plain tbody can share the
 *  exact same cell structure. `virtualStyle` is set when rendered inside the
 *  virtualizer (absolute positioning + transform); when set, the tr becomes
 *  its own `display: table` and won't inherit the outer table's <colgroup>,
 *  so we mirror COL_WIDTHS onto each td via the tdStyle helper. */
function CaseRow({
  c,
  selected,
  toggleOne,
  t,
  isCurrent,
  virtualStyle,
  dataIndex,
  measureRef,
}: {
  c: CaseSummary;
  selected: Set<number>;
  toggleOne: (id: number) => void;
  t: TFunction<"cases">;
  isCurrent: boolean;
  virtualStyle?: React.CSSProperties;
  dataIndex?: number;
  measureRef?: (el: HTMLTableRowElement | null) => void;
}) {
  const segments = c.abs_path.split("/");
  const caseName = segments[segments.length - 1];
  const stateClass = rowStateClass(c, new Date());
  const isVirtual = !!virtualStyle;
  const tdStyle = (idx: number, extra?: React.CSSProperties): React.CSSProperties | undefined => {
    if (isVirtual) {
      return { width: COL_WIDTHS[idx], ...extra };
    }
    return extra;
  };
  return (
    <tr
      ref={measureRef}
      className={`${isCurrent ? "row-current " : ""}${selected.has(c.id) ? "checked " : ""}${stateClass}`}
      style={virtualStyle}
      data-index={dataIndex}
    >
      <td style={tdStyle(0)}>
        <Check state={selected.has(c.id) ? "on" : "off"} onClick={() => toggleOne(c.id)} label={t("bulk.selectRow", { id: c.id })} />
      </td>
      <td style={tdStyle(1)}>
        {c.customer_canonical ? (
          <div>
            <div style={{ fontWeight: 500 }}>
              <Link to={`/customers/${c.customer_id}`} style={{ color: "var(--ink-1)" }}>
                {c.customer_canonical}
              </Link>
            </div>
            {c.customer_raw && c.customer_raw !== c.customer_canonical && (
              <div style={{ fontSize: 10.5, color: "var(--ink-4)", fontFamily: "var(--mono)" }}>
                {c.customer_raw}
              </div>
            )}
          </div>
        ) : (
          <div>
            <div style={{ color: "var(--err)", fontSize: 11.5 }}>{t("table.noCustomer")}</div>
            {c.customer_raw && (
              <div style={{ fontSize: 10.5, color: "var(--ink-4)", fontFamily: "var(--mono)" }}>
                {c.customer_raw}
              </div>
            )}
          </div>
        )}
      </td>
      <td style={tdStyle(2, { maxWidth: 480 })}>
        <Link to={`/cases/${c.id}`} style={{ color: "var(--ink-1)" }}>
          <span className="path" title={caseName}>{caseName}</span>
        </Link>
        {c.tags.length > 0 && (
          <span style={{ marginLeft: 6, display: "inline-flex", gap: 3 }}>
            {c.tags.slice(0, 3).map((tag) => (
              <span key={tag} className="tag">{tag}</span>
            ))}
          </span>
        )}
      </td>
      <td style={tdStyle(3)}>
        <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
          <span style={{ fontFamily: "var(--mono)", fontSize: 9.5, color: "var(--cyan-ink)", fontWeight: 600 }}>A</span>
          <CategoryPill value={c.auto_category} />
          {c.manual_category && (
            <>
              <Ico name="arrow-r" size={10} style={{ color: "var(--ink-4)" }} />
              <span style={{ fontFamily: "var(--mono)", fontSize: 9.5, color: "var(--amber-ink)", fontWeight: 600 }}>M</span>
              <CategoryPill value={c.manual_category} />
            </>
          )}
        </div>
      </td>
      <td style={tdStyle(4)}>
        <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
          <span style={{ fontFamily: "var(--mono)", fontSize: 9.5, color: "var(--cyan-ink)", fontWeight: 600 }}>A</span>
          <TierPill value={c.auto_template_tier} />
          {c.manual_template_tier && (
            <>
              <Ico name="arrow-r" size={10} style={{ color: "var(--ink-4)" }} />
              <span style={{ fontFamily: "var(--mono)", fontSize: 9.5, color: "var(--amber-ink)", fontWeight: 600 }}>M</span>
              <TierPill value={c.manual_template_tier} />
            </>
          )}
        </div>
      </td>
      <td style={tdStyle(5)}>
        <span className="num">
          {c.labeled_count ?? 0}
          <span style={{ color: "var(--ink-4)" }}> / {c.source_count ?? 0}</span>
        </span>
      </td>
      <td style={tdStyle(6)}>
        <IssueCountBadge count={c.blocking_issue_count} />
      </td>
      <td style={tdStyle(7)}>
        <ReviewPill status={c.review_status ?? "unreviewed"} />
      </td>
      <td style={tdStyle(8)}>
        <span className="num">
          {new Date(c.last_modified).toLocaleDateString("zh-CN")}
        </span>
      </td>
    </tr>
  );
}

function FilterSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: [string, string][];
}) {
  return (
    <label
      className="select"
      style={{ minWidth: 128, justifyContent: "flex-start", gap: 6, position: "relative" }}
    >
      <span style={{ color: "var(--ink-3)", fontSize: 11.5 }}>{label}</span>
      <span style={{ color: "var(--ink-1)", fontWeight: 500 }}>
        {options.find(([k]) => k === value)?.[1] ?? options[0]?.[1] ?? ""}
      </span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        style={{
          position: "absolute",
          inset: 0,
          opacity: 0,
          cursor: "pointer",
          width: "100%",
          height: "100%",
          border: 0,
        }}
      >
        {options.map(([k, v]) => (
          <option key={k} value={k}>
            {v}
          </option>
        ))}
      </select>
    </label>
  );
}
