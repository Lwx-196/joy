import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  CATEGORY_LABEL,
  TIER_LABEL,
  caseFileUrl,
  resolveCandidates,
  type Category,
  type CandidateResult,
  type CaseUpdatePayload,
  type ReviewStatus,
} from "../api";
import {
  useCaseDetail,
  useCaseRename,
  useIssueDict,
  useMergeCases,
  useRenderCase,
  useRescanCase,
  useUpdateCase,
  useUpgradeCase,
} from "../hooks/queries";
import { useBrand } from "../lib/brand-context";
import { rememberCaseVisit } from "../lib/work-queue";
import {
  CategoryPill,
  Ico,
  LayerCompare,
  ReviewPill,
  TierPill,
} from "../components/atoms";
import { EvaluateDialog } from "../components/EvaluateDialog";
import { RenderHistoryDrawer } from "../components/RenderHistoryDrawer";
import { RenderStatusCard } from "../components/RenderStatusCard";
import { RevisionsDrawer } from "../components/RevisionsDrawer";
import { useHotkey } from "../hooks/useHotkey";

export default function CaseDetail() {
  const { t } = useTranslation(["caseDetail", "common", "renderHistory"]);
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const caseId = Number(id);

  const detailQ = useCaseDetail(caseId || null);
  const renameQ = useCaseRename(caseId || null);
  const issueDictQ = useIssueDict();
  const updateMut = useUpdateCase();
  const mergeMut = useMergeCases();
  const rescanMut = useRescanCase();
  const upgradeMut = useUpgradeCase();
  const renderMut = useRenderCase();
  const brand = useBrand();

  const data = detailQ.data ?? null;
  const renameHint = renameQ.data ?? null;
  const issueDict = issueDictQ.data ?? [];
  const merging = mergeMut.isPending;
  const saving = updateMut.isPending;
  const rescanning = rescanMut.isPending;
  const upgrading = upgradeMut.isPending;
  const enqueueingRender = renderMut.isPending;

  const [candidates, setCandidates] = useState<CandidateResult | null>(null);
  const [copied, setCopied] = useState(false);
  const [editing, setEditing] = useState(false);
  const [revisionsOpen, setRevisionsOpen] = useState(false);
  const [evaluateOpen, setEvaluateOpen] = useState(false);
  const [renderHistoryOpen, setRenderHistoryOpen] = useState(false);

  useHotkey("h", () => setRenderHistoryOpen((v) => !v), { ignoreInEditable: true });
  const [draft, setDraft] = useState({
    manual_category: "" as "" | Category,
    manual_template_tier: "",
    notes: "",
    tags: "",
    extra_blocking: [] as string[],
  });

  // Sync draft from server data when it (re)loads.
  // Only resets when caseId changes or when not editing — preserves edits during save.
  useEffect(() => {
    if (!data) return;
    if (editing) return;
    setDraft({
      manual_category: (data.manual_category as Category | null) ?? "",
      manual_template_tier: data.manual_template_tier ?? "",
      notes: data.notes ?? "",
      tags: data.tags.join(", "),
      extra_blocking: data.manual_blocking_codes,
    });
  }, [data, editing]);

  // Remember last visited case id for the dashboard "继续上次审核" affordance.
  useEffect(() => {
    if (caseId > 0) rememberCaseVisit(caseId);
  }, [caseId]);

  // Resolve customer candidates (one-shot, not cached — depends on detail).
  useEffect(() => {
    if (!data) return;
    if (!data.customer_id && data.customer_raw) {
      let cancelled = false;
      resolveCandidates(data.customer_raw).then((res) => {
        if (!cancelled) setCandidates(res);
      });
      return () => {
        cancelled = true;
      };
    }
    setCandidates(null);
  }, [data?.customer_id, data?.customer_raw]);

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* ignore */
    }
  };

  const bindToCustomer = (customerId: number) => {
    mergeMut.mutate({ customerId, caseIds: [caseId] });
  };

  const saveEdits = () => {
    const clear: string[] = [];
    const payload: CaseUpdatePayload = {
      notes: draft.notes,
      tags: draft.tags
        .split(/[,，\n]/)
        .map((s) => s.trim())
        .filter(Boolean),
      manual_blocking_codes: draft.extra_blocking,
    };
    if (draft.manual_category === "") clear.push("manual_category");
    else payload.manual_category = draft.manual_category;
    if (draft.manual_template_tier === "") clear.push("manual_template_tier");
    else payload.manual_template_tier = draft.manual_template_tier;
    if (clear.length) payload.clear_fields = clear;
    updateMut.mutate(
      { id: caseId, payload },
      { onSuccess: () => setEditing(false) }
    );
  };

  const setReview = (status: ReviewStatus) => {
    updateMut.mutate({ id: caseId, payload: { review_status: status } });
  };

  const clearOverrides = () => {
    updateMut.mutate(
      {
        id: caseId,
        payload: { clear_fields: ["manual_category", "manual_template_tier"] },
      },
      { onSuccess: () => setEditing(false) }
    );
  };

  const holdCase = () => {
    const reason = window.prompt(
      t("dialogs.holdPrompt"),
      t("dialogs.holdDefault")
    );
    if (reason == null) return; // cancel
    // 默认挂起到 90 天后；用户可在表里手动改时间。"挂起"语义本就是"我现在不想动它"。
    const until = new Date();
    until.setDate(until.getDate() + 90);
    updateMut.mutate({
      id: caseId,
      payload: {
        held_until: until.toISOString(),
        hold_reason: reason.trim() || t("dialogs.holdDefault"),
      },
    });
  };

  const unholdCase = () => {
    updateMut.mutate({
      id: caseId,
      payload: { clear_fields: ["held_until", "hold_reason"] },
    });
  };

  const toggleExtraBlocking = (code: string) => {
    setDraft((d) => ({
      ...d,
      extra_blocking: d.extra_blocking.includes(code)
        ? d.extra_blocking.filter((c) => c !== code)
        : [...d.extra_blocking, code],
    }));
  };

  if (!data) return <div className="empty">{t("common:common.loading")}</div>;

  const segments = data.abs_path.split("/");
  const caseName = segments[segments.length - 1];
  const parents = segments.slice(-3, -1);
  const reviewKey = (data.review_status ?? "unreviewed") as ReviewStatus | "unreviewed";
  const customerLabel = data.customer_canonical ?? data.customer_raw ?? t("customer.unbound");
  const isOverridden = data.manual_category != null || data.manual_template_tier != null;
  // 挂起判定：held_until 在未来即认为挂起。
  const heldUntilDate = data.held_until ? new Date(data.held_until) : null;
  const isHeldNow =
    heldUntilDate != null && !isNaN(heldUntilDate.getTime()) && heldUntilDate.getTime() > Date.now();

  // Group images
  const allImages = data.meta.image_files ?? [];
  const groups: { role: "pre" | "post" | "unl"; label: string; files: string[] }[] = [
    { role: "pre", label: t("images.groupPreOp"), files: [] },
    { role: "post", label: t("images.groupPostOp"), files: [] },
    { role: "unl", label: t("images.groupUnlabeled"), files: [] },
  ];
  for (const f of allImages) {
    const lower = f.toLowerCase();
    if (/术前|before|pre/i.test(lower)) groups[0].files.push(f);
    else if (/术后|after|post/i.test(lower)) groups[1].files.push(f);
    else groups[2].files.push(f);
  }

  return (
    <div style={{ height: "100%", display: "grid", gridTemplateRows: "auto 1fr", overflow: "hidden" }}>
      {/* Header — breadcrumb + layered title */}
      <div style={{ padding: "14px 24px 12px", borderBottom: "1px solid var(--line-2)", background: "var(--panel)" }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            fontSize: 11.5,
            color: "var(--ink-3)",
            marginBottom: 6,
          }}
        >
          <Ico name="arrow-r" size={10} style={{ transform: "rotate(180deg)" }} />
          <Link to="/cases" style={{ color: "var(--ink-1)" }}>{t("header.backLink")}</Link>
          {parents.map((p, i) => (
            <span key={i} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <span style={{ color: "var(--ink-5)" }}>/</span>
              <span style={{ color: data.customer_id || i < parents.length - 1 ? "var(--ink-3)" : "var(--err)" }}>{p}</span>
            </span>
          ))}
        </div>
        <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between", gap: 12 }}>
          <div style={{ minWidth: 0 }}>
            <h1 style={{ fontSize: 17, fontWeight: 600, marginBottom: 2, marginTop: 0 }}>{caseName}</h1>
            <div
              style={{
                fontFamily: "var(--mono)",
                fontSize: 11,
                color: "var(--ink-3)",
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
                maxWidth: 700,
              }}
              title={data.abs_path}
            >
              {data.abs_path}
            </div>
            <div
              style={{ display: "flex", gap: 14, marginTop: 10, alignItems: "center", flexWrap: "wrap" }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span style={{ fontSize: 11, color: "var(--ink-3)", minWidth: 28 }}>{t("header.categoryLabel")}</span>
                <LayerCompare<Category>
                  auto={data.auto_category}
                  manual={data.manual_category}
                  render={(v) => <CategoryPill value={v} />}
                />
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span style={{ fontSize: 11, color: "var(--ink-3)", minWidth: 28 }}>{t("header.templateLabel")}</span>
                <LayerCompare<string | null>
                  auto={data.auto_template_tier}
                  manual={data.manual_template_tier}
                  render={(v) => <TierPill value={v} />}
                />
              </div>
              <ReviewPill status={reviewKey} />
              {data.meta?.source === "skill_v3" && (
                <span
                  className="badge"
                  style={{
                    background: "var(--cyan-50)",
                    color: "var(--cyan-ink)",
                    borderColor: "var(--cyan-200)",
                    fontFamily: "var(--mono)",
                    fontSize: 10.5,
                    fontWeight: 600,
                    letterSpacing: 0.4,
                  }}
                  title={t("header.v3Tooltip")}
                >
                  V3
                </span>
              )}
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
            <button
              className="btn sm"
              onClick={() => rescanMut.mutate(caseId)}
              disabled={rescanning || upgrading}
              title={t("buttons.rescanTooltip")}
            >
              <Ico name="refresh" size={12} />
              {rescanning ? t("buttons.rescanning") : t("buttons.rescan")}
            </button>
            <button
              className="btn sm"
              onClick={() => upgradeMut.mutate({ caseId, brand })}
              disabled={rescanning || upgrading}
              title={t("buttons.upgradeTooltip")}
              style={{ borderColor: "var(--cyan-200)", color: "var(--cyan-ink)" }}
            >
              <Ico name="scan" size={12} />
              {upgrading ? t("buttons.upgrading") : t("buttons.upgradeTov3")}
            </button>
            <button
              className="btn sm primary"
              onClick={() =>
                renderMut.mutate({
                  caseId,
                  payload: { brand, template: "tri-compare", semantic_judge: "off" },
                })
              }
              disabled={enqueueingRender}
              title={t("buttons.renderTooltip", { brand })}
            >
              <Ico name="image" size={12} />
              {enqueueingRender ? t("buttons.enqueuing") : t("buttons.render")}
            </button>
            <button
              className="btn sm"
              onClick={() => setEvaluateOpen(true)}
              title={t("buttons.evaluateTooltip")}
            >
              <Ico name="check" size={12} />
              {t("buttons.evaluate")}
            </button>
            <button
              className="btn sm"
              onClick={() => setRevisionsOpen(true)}
              title={t("buttons.revisionsTooltip")}
            >
              <Ico name="list" size={12} />
              {t("buttons.revisions")}
            </button>
            <button
              type="button"
              className="btn sm"
              onClick={() => setRenderHistoryOpen(true)}
              title={t("renderHistory:buttonTitle")}
              data-testid="render-history-trigger"
            >
              <Ico name="recheck" size={12} />
              {t("renderHistory:buttonLabel")}
            </button>
            <button className="btn sm" onClick={() => copy(data.abs_path)}>
              <Ico name="copy" size={12} />
              {copied ? t("buttons.copied") : t("buttons.copyPath")}
            </button>
            <button className="btn sm" onClick={() => navigate(-1)}>
              <Ico name="arrow-r" size={11} style={{ transform: "rotate(180deg)" }} />
              {t("buttons.back")}
            </button>
          </div>
        </div>
        {isHeldNow && (
          <div
            style={{
              marginTop: 10,
              padding: "8px 12px",
              background: "var(--bg-2)",
              border: "1px dashed var(--line)",
              borderRadius: 6,
              display: "flex",
              alignItems: "center",
              gap: 10,
              fontSize: 12,
            }}
          >
            <Ico name="dot" size={9} style={{ color: "var(--ink-4)" }} />
            <span style={{ color: "var(--ink-2)" }}>
              {t("hold.status")}
              {data.hold_reason ? (
                <>：<span style={{ color: "var(--ink-1)", fontWeight: 500 }}>{data.hold_reason}</span></>
              ) : null}
            </span>
            <span style={{ color: "var(--ink-3)", fontSize: 11 }}>
              {t("hold.until")}{" "}
              <span style={{ fontFamily: "var(--mono)" }}>
                {heldUntilDate?.toLocaleDateString("zh-CN")}
              </span>
            </span>
            <button
              className="btn sm ghost"
              style={{ marginLeft: "auto" }}
              onClick={unholdCase}
              disabled={saving}
            >
              <Ico name="x" size={11} />
              {t("hold.cancel")}
            </button>
          </div>
        )}
        {/* Phase 3: render status card — shows latest render job + live SSE updates. */}
        <RenderStatusCard caseId={caseId} />
      </div>

      {/* Body 60/40 */}
      <div style={{ display: "grid", gridTemplateColumns: "60% 40%", minHeight: 0, overflow: "hidden" }}>
        {/* Sources grid */}
        <div
          style={{
            padding: "16px 18px 16px 24px",
            overflow: "auto",
            borderRight: "1px solid var(--line-2)",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              marginBottom: 10,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <div style={{ fontSize: 13, fontWeight: 600 }}>
                {t("images.title")}{" "}
                <span style={{ fontFamily: "var(--mono)", color: "var(--ink-3)", fontWeight: 500 }}>
                  {allImages.length}
                </span>
              </div>
              <span
                className="badge"
                style={{ background: "var(--cyan-50)", color: "var(--cyan-ink)", borderColor: "var(--cyan-200)" }}
              >
                <span style={{ width: 6, height: 6, background: "var(--cyan)", borderRadius: "50%" }}></span>
                {t("images.preOp")} {groups[0].files.length}
              </span>
              <span
                className="badge"
                style={{ background: "var(--amber-50)", color: "var(--amber-ink)", borderColor: "var(--amber-200)" }}
              >
                <span style={{ width: 6, height: 6, background: "var(--amber)", borderRadius: "50%" }}></span>
                {t("images.postOp")} {groups[1].files.length}
              </span>
              <span className="badge">
                <span style={{ width: 6, height: 6, background: "var(--ink-4)", borderRadius: "50%" }}></span>
                {t("images.unlabeled")} {groups[2].files.length}
              </span>
            </div>
          </div>

          {groups.map((g) =>
            g.files.length === 0 ? null : (
              <div key={g.role}>
                <div
                  style={{
                    fontSize: 11.5,
                    color:
                      g.role === "pre"
                        ? "var(--cyan-ink)"
                        : g.role === "post"
                          ? "var(--amber-ink)"
                          : "var(--ink-3)",
                    fontWeight: 600,
                    margin: "4px 0 6px",
                  }}
                >
                  {g.label} · {g.files.length}
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(6, 1fr)", gap: 8, marginBottom: 14 }}>
                  {g.files.map((name) => (
                    <a
                      key={name}
                      href={caseFileUrl(caseId, name)}
                      target="_blank"
                      rel="noreferrer"
                      className="thumb"
                      data-source-file={name}
                    >
                      <img src={caseFileUrl(caseId, name)} alt={name} loading="lazy" />
                      <span className={`role ${g.role}`}>
                        {g.role === "pre" ? "PRE" : g.role === "post" ? "POST" : "UNL"}
                      </span>
                      <div className="name">{name}</div>
                    </a>
                  ))}
                </div>
              </div>
            ),
          )}
          {allImages.length === 0 && <div className="empty">{t("images.empty")}</div>}
        </div>

        {/* Right rail */}
        <div
          style={{
            padding: "16px 24px 16px 16px",
            overflow: "auto",
            display: "grid",
            gap: 12,
            alignContent: "start",
          }}
        >
          {/* Manual edit card */}
          <div
            className="card"
            style={{ borderColor: "var(--amber-200)", boxShadow: "0 0 0 3px rgba(180,83,9,.04)" }}
          >
            <div
              className="card-h"
              style={{ background: "var(--amber-50)", borderBottom: "1px solid var(--amber-200)" }}
            >
              <div className="t">
                <Ico name="edit" size={13} style={{ color: "var(--amber-ink)" }} />
                <span style={{ color: "var(--amber-ink)" }}>{t("edit.cardTitle")}</span>
                {isOverridden && !editing && (
                  <span
                    className="badge"
                    style={{
                      background: "var(--amber-100)",
                      color: "var(--amber-ink)",
                      borderColor: "var(--amber-200)",
                    }}
                  >
                    {t("edit.hasOverride")}
                  </span>
                )}
              </div>
              <div style={{ display: "flex", gap: 6 }}>
                {!editing ? (
                  <>
                    <button className="btn sm" onClick={() => setEditing(true)}>
                      <Ico name="edit" size={11} />
                      {t("edit.editButton")}
                    </button>
                    {reviewKey !== "reviewed" && (
                      <button
                        className="btn sm"
                        style={{
                          background: "var(--ok-50)",
                          borderColor: "var(--ok-100)",
                          color: "var(--ok)",
                        }}
                        onClick={() => setReview("reviewed")}
                      >
                        <Ico name="check" size={11} />
                        {t("edit.markReviewed")}
                      </button>
                    )}
                    {reviewKey !== "needs_recheck" && (
                      <button className="btn sm danger" onClick={() => setReview("needs_recheck")}>
                        <Ico name="alert" size={11} />
                        {t("edit.needsRecheck")}
                      </button>
                    )}
                    {!isHeldNow && (
                      <button
                        className="btn sm ghost"
                        onClick={holdCase}
                        disabled={saving}
                        title={t("edit.holdTooltip")}
                        style={{ borderStyle: "dashed", color: "var(--ink-3)" }}
                      >
                        <Ico name="dot" size={11} />
                        {t("edit.hold")}
                      </button>
                    )}
                  </>
                ) : (
                  <>
                    <button className="btn sm ghost" onClick={() => setEditing(false)} disabled={saving}>
                      {t("edit.cancel")}
                    </button>
                    <button className="btn sm danger" onClick={clearOverrides} disabled={saving}>
                      {t("edit.clearOverride")}
                    </button>
                    <button className="btn sm amber" onClick={saveEdits} disabled={saving}>
                      <Ico name="check" size={11} />
                      {saving ? t("edit.saving") : t("edit.save")}
                    </button>
                  </>
                )}
              </div>
            </div>
            <div className="card-b" style={{ display: "grid", gap: 12, background: "#FFFEF7" }}>
              {editing && (
                <div
                  style={{ fontSize: 11, color: "var(--ink-3)", display: "flex", alignItems: "center", gap: 6 }}
                >
                  <Ico name="dot" size={8} style={{ color: "var(--amber)" }} />
                  {t("edit.modeWarning")}
                </div>
              )}

              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "76px 1fr",
                  gap: "10px 12px",
                  alignItems: "center",
                }}
              >
                <label style={{ fontSize: 11.5, color: "var(--ink-3)" }}>{t("edit.categoryLabel")}</label>
                {editing ? (
                  <select
                    value={draft.manual_category}
                    onChange={(e) =>
                      setDraft({ ...draft, manual_category: e.target.value as Category | "" })
                    }
                    style={{ background: "#fff", borderColor: "var(--amber-200)" }}
                  >
                    <option value="">{t("edit.followAuto")}</option>
                    {Object.entries(CATEGORY_LABEL).map(([k, v]) => (
                      <option key={k} value={k}>{v}</option>
                    ))}
                  </select>
                ) : (
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      padding: "5px 9px",
                      borderRadius: 6,
                      border: "1px solid var(--line)",
                      background: "var(--panel-2)",
                    }}
                  >
                    {data.manual_category ? (
                      <CategoryPill value={data.manual_category} />
                    ) : (
                      <span className="layer-chip empty" style={{ height: 20 }}>
                        <span className="lab">manual</span>
                        {t("edit.noOverride")}
                      </span>
                    )}
                    <span
                      style={{
                        marginLeft: "auto",
                        fontFamily: "var(--mono)",
                        fontSize: 10.5,
                        color: "var(--ink-4)",
                      }}
                    >
                      auto: {CATEGORY_LABEL[data.auto_category]}
                    </span>
                  </div>
                )}

                <label style={{ fontSize: 11.5, color: "var(--ink-3)" }}>{t("edit.templateLabel")}</label>
                {editing ? (
                  <select
                    value={draft.manual_template_tier}
                    onChange={(e) => setDraft({ ...draft, manual_template_tier: e.target.value })}
                    style={{ background: "#fff", borderColor: "var(--amber-200)" }}
                  >
                    <option value="">{t("edit.followAuto")}</option>
                    {Object.entries(TIER_LABEL).map(([k, v]) => (
                      <option key={k} value={k}>{v}</option>
                    ))}
                  </select>
                ) : (
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      padding: "5px 9px",
                      borderRadius: 6,
                      border: "1px solid var(--line)",
                      background: "var(--panel-2)",
                    }}
                  >
                    {data.manual_template_tier ? (
                      <TierPill value={data.manual_template_tier} />
                    ) : (
                      <span className="layer-chip empty" style={{ height: 20 }}>
                        <span className="lab">manual</span>
                        {t("edit.noOverride")}
                      </span>
                    )}
                    <span
                      style={{
                        marginLeft: "auto",
                        fontFamily: "var(--mono)",
                        fontSize: 10.5,
                        color: "var(--ink-4)",
                      }}
                    >
                      auto: {data.auto_template_tier ? TIER_LABEL[data.auto_template_tier] : "—"}
                    </span>
                  </div>
                )}

                <label style={{ fontSize: 11.5, color: "var(--ink-3)", alignSelf: "flex-start", marginTop: 6 }}>
                  {t("edit.notesLabel")}
                </label>
                {editing ? (
                  <textarea
                    style={{
                      minHeight: 56,
                      borderColor: "var(--amber-200)",
                      background: "#fff",
                    }}
                    value={draft.notes}
                    onChange={(e) => setDraft({ ...draft, notes: e.target.value })}
                    placeholder={t("edit.notesPlaceholder")}
                  />
                ) : (
                  <div
                    style={{
                      fontSize: 12.5,
                      color: data.notes ? "var(--ink-1)" : "var(--ink-4)",
                      padding: "6px 0",
                    }}
                  >
                    {data.notes || <span style={{ fontStyle: "italic" }}>—</span>}
                  </div>
                )}

                <label style={{ fontSize: 11.5, color: "var(--ink-3)", alignSelf: "flex-start", marginTop: 4 }}>
                  {t("edit.tagsLabel")}
                </label>
                {editing ? (
                  <input
                    value={draft.tags}
                    onChange={(e) => setDraft({ ...draft, tags: e.target.value })}
                    placeholder={t("edit.tagsPlaceholder")}
                    style={{ borderColor: "var(--amber-200)", background: "#fff" }}
                  />
                ) : data.tags.length > 0 ? (
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                    {data.tags.map((t) => (
                      <span key={t} className="chip on">
                        {t}
                      </span>
                    ))}
                  </div>
                ) : (
                  <span style={{ color: "var(--ink-4)", fontStyle: "italic" }}>—</span>
                )}

                <label style={{ fontSize: 11.5, color: "var(--ink-3)", alignSelf: "flex-start", marginTop: 4 }}>
                  {t("edit.blockingLabel")}
                </label>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                  {issueDict.map((iss) => {
                    const on = (editing ? draft.extra_blocking : data.manual_blocking_codes).includes(iss.code);
                    return (
                      <button
                        key={iss.code}
                        type="button"
                        className={`chip danger${on ? " on" : ""}`}
                        onClick={() => editing && toggleExtraBlocking(iss.code)}
                        style={{ cursor: editing ? "pointer" : "default", opacity: editing || on ? 1 : 0.5 }}
                      >
                        {iss.zh}
                        {on && editing && <Ico name="x" size={10} />}
                      </button>
                    );
                  })}
                </div>
              </div>
            </div>
          </div>

          {/* Customer binding card */}
          <div className="card">
            <div className="card-h">
              <div className="t">
                <Ico name="user" size={13} style={{ color: "var(--ink-3)" }} />
                {t("customer.cardTitle")}
              </div>
              {data.customer_id ? (
                <span
                  className="badge"
                  style={{ background: "var(--ok-50)", color: "var(--ok)", borderColor: "var(--ok-100)" }}
                >
                  <Ico name="check" size={10} />
                  {t("customer.bound")}
                </span>
              ) : (
                <span
                  className="badge"
                  style={{ background: "var(--err-50)", color: "var(--err)", borderColor: "var(--err-100)" }}
                >
                  <Ico name="alert" size={10} />
                  {t("customer.unbound")}
                </span>
              )}
            </div>
            <div className="card-b" style={{ display: "grid", gap: 8 }}>
              {data.customer_id ? (
                <div>
                  <Link
                    to={`/customers/${data.customer_id}`}
                    style={{ color: "var(--ink-1)", fontSize: 16, fontWeight: 600 }}
                  >
                    {data.customer_canonical}
                  </Link>
                  <div style={{ fontSize: 11.5, color: "var(--ink-3)", marginTop: 4 }}>
                    {t("customer.rawName")} <span style={{ fontFamily: "var(--mono)", color: "var(--ink-2)" }}>{data.customer_raw}</span>
                  </div>
                </div>
              ) : (
                <>
                  <div style={{ fontSize: 11.5, color: "var(--ink-3)" }}>
                    {t("customer.rawNameUnbound")}{" "}
                    <span style={{ fontFamily: "var(--mono)", color: "var(--ink-4)" }}>
                      {data.customer_raw ?? t("customer.noName")}
                    </span>
                    {candidates && candidates.candidates.length > 0 && t("customer.candidatesIntro")}
                  </div>
                  {candidates ? (
                    candidates.candidates.length > 0 ? (
                      <>
                        {candidates.candidates.map((c, i) => (
                          <button
                            key={c.id}
                            className={`cand-btn${i === 0 && candidates.decision === "matched" ? " suggest" : ""}`}
                            onClick={() => bindToCustomer(c.id)}
                            disabled={merging}
                          >
                            <span>
                              <span style={{ fontWeight: 600 }}>{c.canonical_name}</span>
                              <span style={{ fontSize: 11, color: "var(--ink-3)" }}>{t("customer.caseCountSuffix", { n: c.case_count })}</span>
                            </span>
                            <span className="sim">
                              {c.similarity != null ? t("customer.similarity", { similarity: Math.round((c.similarity ?? 0) * 100) }) : t("customer.knownAlias")}
                            </span>
                          </button>
                        ))}
                        <div className="divider" />
                      </>
                    ) : (
                      <div
                        style={{
                          fontSize: 11.5,
                          color: "var(--amber-ink)",
                          fontStyle: "italic",
                          padding: "6px 0",
                        }}
                      >
                        {candidates.suggestion}
                      </div>
                    )
                  ) : (
                    <div className="muted" style={{ fontSize: 12 }}>{t("customer.resolving")}</div>
                  )}
                  <Link
                    to={`/dict?prefill=${encodeURIComponent(candidates?.normalized ?? data.customer_raw ?? "")}`}
                    className="btn sm"
                    style={{ justifyContent: "center" }}
                  >
                    <Ico name="plus" size={11} />
                    {t("customer.createAndBind")}
                  </Link>
                </>
              )}
            </div>
          </div>

          {/* Diagnostics */}
          {data.blocking_issues.length > 0 && (() => {
            const blocks = data.blocking_issues.filter((i) => (i.severity ?? "block") === "block");
            const warns = data.blocking_issues.filter((i) => i.severity === "warn");
            const renderIssue = (issue: typeof data.blocking_issues[number], i: number) => {
              const isManual = data.manual_blocking_codes.includes(issue.code);
              const isBlock = (issue.severity ?? "block") === "block";
              const accent = isManual
                ? "var(--amber-ink)"
                : isBlock
                  ? "var(--err)"
                  : "var(--amber-ink)";
              const bg = isManual
                ? "var(--amber-50)"
                : isBlock
                  ? "var(--err-50)"
                  : "var(--amber-50)";
              const border = isManual
                ? "var(--amber-200)"
                : isBlock
                  ? "var(--err-100)"
                  : "var(--amber-200)";
              return (
                <div
                  key={`${issue.code}-${i}`}
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
                        {issue.files.slice(0, 6).map((fn) => (
                          <button
                            key={fn}
                            type="button"
                            onClick={() => {
                              const sel = `[data-source-file="${CSS.escape(fn)}"]`;
                              const el = document.querySelector<HTMLElement>(sel);
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
                            {fn}
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
                    {isManual ? t("diagnostics.manual") : t("diagnostics.auto")}
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
                </div>
                <div className="card-b" style={{ display: "grid", gap: 8 }}>
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
                </div>
              </div>
            );
          })()}

          {/* Rename suggestion */}
          {renameHint?.command && (
            <div className="card">
              <div className="card-h">
                <div className="t">
                  <Ico name="folder" size={13} style={{ color: "var(--ink-3)" }} />
                  {t("rename.cardTitle")}
                  <span
                    className="badge"
                    style={{
                      background: "var(--cyan-50)",
                      color: "var(--cyan-ink)",
                      borderColor: "var(--cyan-200)",
                      fontFamily: "var(--mono)",
                    }}
                  >
                    DRY-RUN
                  </span>
                </div>
                <button className="btn sm" onClick={() => copy(renameHint.command!)}>
                  <Ico name="copy" size={11} />
                  {copied ? t("rename.copied") : t("rename.copyAll")}
                </button>
              </div>
              <div className="card-b" style={{ padding: 0 }}>
                <div
                  style={{
                    padding: "8px 12px",
                    fontSize: 11.5,
                    color: "var(--ink-3)",
                    background: "var(--panel-2)",
                    borderBottom: "1px solid var(--line-2)",
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                  }}
                >
                  <Ico name="alert" size={11} style={{ color: "var(--cyan-ink)" }} />
                  <span>
                    {t("rename.affectsLabel")}{" "}
                    <span style={{ fontFamily: "var(--mono)", color: "var(--ink-1)", fontWeight: 500 }}>
                      {renameHint.affected_count}
                    </span>{" "}
                    {t("rename.disclaimerSuffix")}<span style={{ color: "var(--ok)" }}>{t("rename.wontModify")}</span>{t("rename.templateOnly")}
                  </span>
                </div>
                <pre className="code" style={{ borderRadius: 0, margin: 0 }}>
                  <span className="cmt"># {renameHint.note}</span>
                  {"\n"}
                  {renameHint.command}
                </pre>
              </div>
            </div>
          )}
          {/* show customer label discreetly when not bound and no candidates */}
          {!data.customer_id && !candidates && (
            <div style={{ fontSize: 11, color: "var(--ink-4)", textAlign: "center" }}>
              {customerLabel}
            </div>
          )}
        </div>
      </div>
      <EvaluateDialog
        open={evaluateOpen}
        onClose={() => setEvaluateOpen(false)}
        subjectKind="case"
        subjectId={caseId}
        caseId={caseId}
        subjectSummary={`#${caseId} · ${data.customer_canonical || data.customer_raw || "—"} · ${data.category}`}
      />
      <RevisionsDrawer
        caseId={caseId}
        open={revisionsOpen}
        onClose={() => setRevisionsOpen(false)}
      />
      <RenderHistoryDrawer
        caseId={caseId}
        brand={brand}
        template="tri-compare"
        open={renderHistoryOpen}
        onClose={() => setRenderHistoryOpen(false)}
      />
    </div>
  );
}
