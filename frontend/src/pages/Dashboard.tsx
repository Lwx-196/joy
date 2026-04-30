import { useMemo } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { CATEGORY_LABEL, TIER_LABEL, type CaseSummary } from "../api";
import {
  useBatchRenderCases,
  useBatchUpgradeCases,
  useCases,
  useScanLatest,
  useStats,
  useTriggerScan,
} from "../hooks/queries";
import { Bar, CategoryPill, Ico, ReviewPill, TierPill } from "../components/atoms";
import { useBrand } from "../lib/brand-context";
import { useBatchJobToastStore } from "../lib/batch-job-toast";
import { deriveLanes, isHeld, isToday, readLastVisitedCase, type LaneDef } from "../lib/work-queue";

const MAX_BATCH_RENDER = 50;
const MAX_BATCH_UPGRADE = 50;

function formatTime(iso: string | null | undefined) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("zh-CN", { hour12: false });
}

const CAT_COLORS: Record<string, string> = {
  body: "var(--cat-body)",
  standard_face: "var(--cat-stdface)",
  non_labeled: "var(--cat-nonlabeled)",
  fragment_only: "var(--cat-fragment)",
  unsupported: "var(--cat-unsupported)",
};
const TIER_COLORS: Record<string, string> = {
  tri: "var(--tier-tri)",
  bi: "var(--tier-bi)",
  single: "var(--tier-single)",
  "body-dual-compare": "var(--tier-bdc)",
  unsupported: "var(--tier-unsup)",
};

export default function Dashboard() {
  const { t } = useTranslation("dashboard");
  const statsQ = useStats();
  const latestQ = useScanLatest();
  const recentQ = useCases({ limit: 8 });
  // Full list (cached for 30s) used to derive work-queue lane counts.
  // Same query key as Cases.tsx ({limit:2000}) so the cache is shared.
  const allQ = useCases({ limit: 2000 });
  const scanMut = useTriggerScan();

  const stats = statsQ.data;
  const latest = latestQ.data;
  const recent = recentQ.data ?? [];
  const scanning = scanMut.isPending;

  const lanes: LaneDef[] = useMemo(
    () => deriveLanes(allQ.data ?? []),
    [allQ.data]
  );
  const lastVisited = readLastVisitedCase();

  // Phase 3: batch render the cases that match a given lane.
  const brand = useBrand();
  const batchRenderMut = useBatchRenderCases();
  const batchUpgradeMut = useBatchUpgradeCases();
  const showBatchToast = useBatchJobToastStore((s) => s.show);

  const laneTargets = (laneKey: string): CaseSummary[] => {
    const live = (allQ.data ?? []).filter((c) => !isHeld(c));
    if (laneKey === "pendingReview") return live.filter((c) => c.review_status === "pending");
    if (laneKey === "todayNew") return live.filter((c) => isToday(c.last_modified));
    return [];
  };

  const handleBatchRenderLane = (laneKey: string) => {
    let target = laneTargets(laneKey);
    if (target.length === 0) return;
    if (target.length > MAX_BATCH_RENDER) {
      const ok = window.confirm(
        t("queue.confirmRenderOver", { count: target.length, max: MAX_BATCH_RENDER })
      );
      if (!ok) return;
      target = target.slice(0, MAX_BATCH_RENDER);
    } else {
      const ok = window.confirm(t("queue.confirmRender", { count: target.length, brand }));
      if (!ok) return;
    }
    batchRenderMut.mutate(
      {
        caseIds: target.map((c) => c.id),
        payload: { brand, template: "tri-compare", semantic_judge: "off" },
      },
      {
        onSuccess: (data) => {
          showBatchToast("render", data.batch_id, target.length);
        },
      }
    );
  };

  const handleBatchUpgradeLane = (laneKey: string) => {
    let target = laneTargets(laneKey);
    if (target.length === 0) return;
    if (target.length > MAX_BATCH_UPGRADE) {
      const ok = window.confirm(
        t("queue.confirmUpgradeOver", { count: target.length, max: MAX_BATCH_UPGRADE })
      );
      if (!ok) return;
      target = target.slice(0, MAX_BATCH_UPGRADE);
    } else {
      const ok = window.confirm(
        t("queue.confirmUpgrade", { count: target.length, brand })
      );
      if (!ok) return;
    }
    batchUpgradeMut.mutate(
      { caseIds: target.map((c) => c.id), brand },
      {
        onSuccess: (data) => {
          showBatchToast("upgrade", data.batch_id, target.length);
        },
      }
    );
  };

  const handleScan = (mode: "full" | "incremental") => {
    scanMut.mutate(mode);
  };

  if (!stats) {
    return <div className="empty">{t("loading")}</div>;
  }

  const reviewCounts = stats.by_review_status ?? {};
  const unreviewed = reviewCounts.unreviewed ?? 0;
  const pending = reviewCounts.pending ?? 0;
  const recheck = reviewCounts.needs_recheck ?? 0;
  const reviewed = reviewCounts.reviewed ?? 0;
  const total = stats.total || 1;
  const unrevPct = Math.round((unreviewed / total) * 1000) / 10;
  const reviewedPct = Math.round((reviewed / total) * 1000) / 10;

  const catEntries = Object.entries(stats.by_category as Record<string, number>).sort(
    (a, b) => b[1] - a[1],
  );
  const tierEntries = Object.entries(stats.by_tier).sort((a, b) => b[1] - a[1]);
  const tierTotal = Object.values(stats.by_tier).reduce((a, b) => a + b, 0);
  const unassignedTier = stats.total - tierTotal;

  return (
    <div style={{ height: "100%", display: "grid", gridTemplateRows: "auto 1fr", overflow: "hidden" }}>
      {/* Header */}
      <div className="page-header">
        <div>
          <h1 className="page-title">{t("title")}</h1>
          <div className="page-sub">
            {t("subtitle.prefix")}
            <span style={{ color: "var(--ink-1)" }}>
              {t("subtitle.totalLabel")}<span style={{ fontFamily: "var(--mono)" }}>{stats.total}</span>{t("subtitle.totalSuffix")}
            </span>
            {latest?.scan?.completed_at && (
              <>
                {t("subtitle.lastScanPrefix")}
                <span style={{ color: "var(--ink-1)" }}>{formatTime(latest.scan.completed_at)}</span>
              </>
            )}
            {stats.manual_override_count > 0 && (
              <>{t("subtitle.manualOverridePrefix")}<span style={{ color: "var(--ink-1)" }}>{stats.manual_override_count}</span>{t("subtitle.manualOverrideSuffix")}</>
            )}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn" onClick={() => handleScan("incremental")} disabled={scanning}>
            <Ico name="refresh" size={12} />
            {scanning ? t("scan.scanning") : t("scan.incremental")}
          </button>
          <button className="btn primary" onClick={() => handleScan("full")} disabled={scanning}>
            <Ico name="scan" size={12} />
            {scanning ? t("scan.scanning") : t("scan.full")}
          </button>
        </div>
      </div>

      {/* Body */}
      <div
        style={{
          padding: 20,
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: 16,
          overflow: "auto",
          alignContent: "start",
        }}
      >
        {/* Work queue v2 — 5 lanes for "what's next" */}
        <div className="card" style={{ gridColumn: "1 / span 2" }}>
          <div className="card-h">
            <div className="t">
              <Ico name="split" size={14} style={{ color: "var(--cyan-ink)" }} />
              {t("queue.title")}
              <span
                className="badge"
                style={{ background: "var(--cyan-50)", color: "var(--cyan-ink)", borderColor: "var(--cyan-200)" }}
              >
                {t("queue.byROI")}
              </span>
            </div>
            <div className="meta" style={{ display: "flex", gap: 8, alignItems: "center" }}>
              {lastVisited != null && (
                <Link to={`/cases/${lastVisited}`} className="btn sm">
                  <Ico name="arrow" size={11} />
                  {t("queue.lastVisit", { id: lastVisited })}
                </Link>
              )}
              <span>
                {allQ.isLoading ? t("queue.counting") : t("queue.totalCases", { n: allQ.data?.length ?? 0 })}
              </span>
            </div>
          </div>
          <div
            className="card-b"
            style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 10 }}
          >
            {lanes.map((lane) => (
              <Link
                key={lane.key}
                to={lane.route}
                className="stat-tile"
                style={{
                  display: "grid",
                  gap: 4,
                  borderColor:
                    lane.tone === "err"
                      ? "var(--err-100)"
                      : lane.tone === "amber"
                        ? "var(--amber-200)"
                        : lane.tone === "cyan"
                          ? "var(--cyan-200)"
                          : lane.tone === "ok"
                            ? "var(--ok-100)"
                            : "var(--line)",
                  background:
                    lane.tone === "err"
                      ? "var(--err-50)"
                      : lane.tone === "amber"
                        ? "var(--amber-50)"
                        : lane.tone === "cyan"
                          ? "var(--cyan-50)"
                          : lane.tone === "ok"
                            ? "var(--ok-50)"
                            : "var(--panel-2)",
                }}
              >
                <div
                  className="lbl"
                  style={{
                    color:
                      lane.tone === "err"
                        ? "var(--err)"
                        : lane.tone === "amber"
                          ? "var(--amber-ink)"
                          : lane.tone === "cyan"
                            ? "var(--cyan-ink)"
                            : lane.tone === "ok"
                              ? "var(--ok)"
                              : "var(--ink-2)",
                  }}
                >
                  {lane.label}
                </div>
                <div
                  className="v"
                  style={{
                    fontSize: 32,
                    color:
                      lane.tone === "err"
                        ? "var(--err)"
                        : lane.tone === "amber"
                          ? "var(--amber-ink)"
                          : lane.tone === "cyan"
                            ? "var(--cyan-ink)"
                            : lane.tone === "ok"
                              ? "var(--ok)"
                              : "var(--ink-1)",
                  }}
                >
                  {lane.count}
                </div>
                <div className="sub" style={{ fontSize: 10.5 }}>
                  {lane.desc}
                </div>
                {(lane.key === "pendingReview" || lane.key === "todayNew") && lane.count > 0 && (
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 8 }}>
                    <button
                      type="button"
                      className="btn sm"
                      onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        handleBatchRenderLane(lane.key);
                      }}
                      disabled={batchRenderMut.isPending}
                      title={t("queue.batchRenderTitle", { n: lane.count, brand })}
                      style={{ fontSize: 11, padding: "3px 8px" }}
                    >
                      <Ico name="image" size={11} />
                      {lane.count > MAX_BATCH_RENDER
                        ? t("queue.batchRenderLimit", { max: MAX_BATCH_RENDER })
                        : t("queue.batchRender", { n: lane.count })}
                    </button>
                    <button
                      type="button"
                      className="btn sm"
                      onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        handleBatchUpgradeLane(lane.key);
                      }}
                      disabled={batchUpgradeMut.isPending}
                      title={t("queue.batchUpgradeTitle", { n: lane.count, brand })}
                      style={{
                        fontSize: 11,
                        padding: "3px 8px",
                        background: "var(--purple-50, #FAF5FF)",
                        color: "var(--purple-ink, #6B21A8)",
                      }}
                    >
                      <Ico name="scan" size={11} />
                      {lane.count > MAX_BATCH_UPGRADE
                        ? t("queue.batchUpgradeLimit", { max: MAX_BATCH_UPGRADE })
                        : t("queue.batchUpgrade", { n: lane.count })}
                    </button>
                  </div>
                )}
              </Link>
            ))}
          </div>
        </div>

        {/* Worktray — review status breakdown */}
        <div className="card" style={{ gridColumn: "1 / span 2" }}>
          <div className="card-h">
            <div className="t">
              <Ico name="flag" size={14} style={{ color: "var(--amber)" }} />
              {t("worktray.title")}
              <span
                className="badge"
                style={{ background: "var(--amber-50)", borderColor: "var(--amber-200)", color: "var(--amber-ink)" }}
              >
                {t("worktray.needAttention")}
              </span>
            </div>
            <div className="meta" style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <span>
                {t("worktray.totalLabel")}<span style={{ fontFamily: "var(--mono)", color: "var(--ink-1)" }}>{stats.total}</span>{t("worktray.totalSuffix")}
              </span>
              <span style={{ color: "var(--ink-5)" }}>·</span>
              <span>
                {t("worktray.progressLabel")}<span style={{ fontFamily: "var(--mono)", color: "var(--ink-1)" }}>{reviewedPct}%</span>
              </span>
              <Link to="/cases?review=unreviewed" className="btn sm" style={{ marginLeft: 8 }}>
                {t("worktray.openQueue")}<Ico name="arrow" size={11} />
              </Link>
            </div>
          </div>
          <div
            className="card-b"
            style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr 1fr 1fr", gap: 12 }}
          >
            <Link to="/cases?review=unreviewed" className="stat-tile hot">
              <div className="lbl" style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <span>{t("worktray.unreviewed")}</span>
                <span className="rs unreviewed" style={{ height: 18, fontSize: 10.5 }}>
                  <span className="dot"></span>unreviewed
                </span>
              </div>
              <div className="v" style={{ fontSize: 44 }}>{unreviewed}</div>
              <div className="sub">{t("worktray.unreviewedSub", { pct: unrevPct })}</div>
              <div
                style={{
                  marginTop: 8,
                  height: 6,
                  background: "rgba(180,83,9,.1)",
                  borderRadius: 999,
                  overflow: "hidden",
                }}
              >
                <div style={{ width: `${unrevPct}%`, height: "100%", background: "var(--amber)", borderRadius: 999 }} />
              </div>
            </Link>
            <Link to="/cases?review=pending" className="stat-tile cyan-tile">
              <div className="lbl">{t("worktray.pending")}</div>
              <div className="v">{pending}</div>
              <div className="sub">{t("worktray.pendingSub")}</div>
            </Link>
            <Link to="/cases?review=needs_recheck" className="stat-tile">
              <div className="lbl" style={{ color: "var(--err)" }}>{t("worktray.recheck")}</div>
              <div className="v" style={{ color: "var(--err)" }}>{recheck}</div>
              <div className="sub">{t("worktray.recheckSub")}</div>
            </Link>
            <Link to="/cases?review=reviewed" className="stat-tile ok-tile">
              <div className="lbl">{t("worktray.reviewed")}</div>
              <div className="v">{reviewed}</div>
              <div className="sub">{t("worktray.reviewedSub", { n: reviewed })}</div>
            </Link>
          </div>
        </div>

        {/* By category */}
        <div className="card">
          <div className="card-h">
            <div className="t">
              <Ico name="folder" size={13} style={{ color: "var(--ink-3)" }} />
              {t("byCategory.title")}
            </div>
            <div className="meta">
              {t("byCategory.meta", { total: stats.total })}
            </div>
          </div>
          <div className="card-b">
            {catEntries.map(([key, val]) => (
              <Bar
                key={key}
                label={CATEGORY_LABEL[key as keyof typeof CATEGORY_LABEL] ?? key}
                value={val}
                total={stats.total}
                color={CAT_COLORS[key] ?? "var(--ink-3)"}
                badge={<CategoryPill value={key as never} />}
              />
            ))}
            {stats.manual_override_count > 0 && (
              <>
                <div className="divider" />
                <div
                  style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11.5, color: "var(--ink-3)" }}
                >
                  <span className="layer-chip auto" style={{ height: 18 }}>
                    <span className="lab">auto</span>{t("byCategory.autoLabel")}
                  </span>
                  <span style={{ margin: "0 4px" }}>·</span>
                  <span className="layer-chip manual" style={{ height: 18 }}>
                    <span className="lab">manual</span>{t("byCategory.manualLabel")}
                  </span>
                  {t("byCategory.footer", { n: stats.manual_override_count })}
                </div>
              </>
            )}
          </div>
        </div>

        {/* By tier */}
        <div className="card">
          <div className="card-h">
            <div className="t">
              <Ico name="split" size={13} style={{ color: "var(--ink-3)" }} />
              {t("byTier.title")}
            </div>
            <div className="meta">{t("byTier.meta")}</div>
          </div>
          <div className="card-b">
            {tierEntries.map(([key, val]) => (
              <Bar
                key={key}
                label={TIER_LABEL[key] ?? key}
                value={val}
                total={tierTotal || 1}
                color={TIER_COLORS[key] ?? "var(--ink-3)"}
                badge={<TierPill value={key} />}
              />
            ))}
            {unassignedTier > 0 && (
              <>
                <div className="divider" />
                <div style={{ fontSize: 11.5, color: "var(--ink-3)" }}>
                  {t("byTier.unassigned", { n: unassignedTier })}
                </div>
              </>
            )}
          </div>
        </div>

        {/* Recent table */}
        <div
          className="card"
          style={{ gridColumn: "1 / span 2", minHeight: 0, display: "grid", gridTemplateRows: "auto 1fr" }}
        >
          <div className="card-h">
            <div className="t">
              <Ico name="recheck" size={13} style={{ color: "var(--ink-3)" }} />
              {t("recent.title")}
            </div>
            <div className="meta" style={{ display: "flex", gap: 8 }}>
              <span>{t("recent.meta", { n: recent.length })}</span>
              <Link to="/cases" className="btn sm ghost">
                {t("recent.viewAll")} <Ico name="arrow-r" size={10} />
              </Link>
            </div>
          </div>
          <div style={{ overflow: "auto" }}>
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: 38 }}><span className="sr-only">{t("recent.th.indicator")}</span></th>
                  <th>{t("recent.th.caseDir")}</th>
                  <th style={{ width: 110 }}>{t("recent.th.customer")}</th>
                  <th style={{ width: 110 }}>{t("recent.th.category")}</th>
                  <th style={{ width: 110 }}>{t("recent.th.template")}</th>
                  <th style={{ width: 110 }}>{t("recent.th.review")}</th>
                  <th style={{ width: 110 }}>{t("recent.th.source")}</th>
                  <th style={{ width: 130 }}>{t("recent.th.changedAt")}</th>
                </tr>
              </thead>
              <tbody>
                {recent.map((c) => {
                  const overridden = !!c.manual_category || !!c.manual_template_tier;
                  const sourceLabel = overridden ? t("recent.sourceManual") : t("recent.sourceAuto");
                  return (
                    <tr key={c.id} className={overridden ? "row-manual" : "row-auto"}>
                      <td>
                        <Ico
                          name={overridden ? "edit" : "scan"}
                          size={13}
                          style={{ color: overridden ? "var(--amber)" : "var(--cyan)" }}
                        />
                      </td>
                      <td>
                        <Link to={`/cases/${c.id}`} style={{ color: "var(--ink-1)" }}>
                          <span className="path" title={c.abs_path}>
                            {c.abs_path.split("/").slice(-3).join(" / ")}
                          </span>
                        </Link>
                      </td>
                      <td>
                        {c.customer_canonical ?? c.customer_raw ? (
                          <span style={{ color: "var(--ink-1)" }}>
                            {c.customer_canonical ?? c.customer_raw}
                          </span>
                        ) : (
                          <span style={{ color: "var(--ink-4)", fontStyle: "italic" }}>{t("recent.noCustomer")}</span>
                        )}
                      </td>
                      <td><CategoryPill value={c.category} /></td>
                      <td><TierPill value={c.template_tier} /></td>
                      <td><ReviewPill status={c.review_status} /></td>
                      <td>
                        <span
                          style={{
                            fontSize: 11,
                            color: overridden ? "var(--amber-ink)" : "var(--cyan-ink)",
                          }}
                        >
                          {sourceLabel}
                        </span>
                      </td>
                      <td>
                        <span className="num">{formatTime(c.last_modified)}</span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
