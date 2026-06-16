import { useTranslation } from "react-i18next";

import {
  CATEGORY_LABEL,
  TIER_LABEL,
  type BlockingIssue,
  type CaseDetail,
  type Category,
  type ReviewStatus,
} from "../../api";
import { CategoryPill, Ico, TierPill } from "../../components/atoms";
import type { CaseDetailDraft } from "./hooks";

type ManualEditCardProps = {
  data: CaseDetail;
  draft: CaseDetailDraft;
  editing: boolean;
  isOverridden: boolean;
  reviewKey: ReviewStatus | "unreviewed";
  isHeldNow: boolean;
  issueDict: BlockingIssue[];
  rescanning: boolean;
  upgrading: boolean;
  enqueueingRender: boolean;
  saving: boolean;
  renderGateBlocked: boolean;
  renderGateTitle: string;
  onDraftChange: (draft: CaseDetailDraft) => void;
  onToggleExtraBlocking: (code: string) => void;
  onRescan: () => void;
  onUpgrade: () => void;
  onRender: () => void;
  onForceRender: () => void;
  onSetEditing: (editing: boolean) => void;
  onClearOverrides: () => void;
  onSaveEdits: () => void;
  onSetReview: (status: ReviewStatus) => void;
  onHoldCase: () => void;
};

export function ManualEditCard({
  data,
  draft,
  editing,
  isOverridden,
  reviewKey,
  isHeldNow,
  issueDict,
  rescanning,
  upgrading,
  enqueueingRender,
  saving,
  renderGateBlocked,
  renderGateTitle,
  onDraftChange,
  onToggleExtraBlocking,
  onRescan,
  onUpgrade,
  onRender,
  onForceRender,
  onSetEditing,
  onClearOverrides,
  onSaveEdits,
  onSetReview,
  onHoldCase,
}: ManualEditCardProps) {
  const { t } = useTranslation("caseDetail");

  return (
    <div
      className="card"
      style={{ borderColor: "var(--cyan-200)", boxShadow: "0 0 0 3px rgba(8,145,178,.04)" }}
    >
      <div
        className="card-h"
        style={{ background: "var(--cyan-50)", borderBottom: "1px solid var(--cyan-200)" }}
      >
        <div className="t">
          <Ico name="scan" size={13} style={{ color: "var(--cyan-ink)" }} />
          <span style={{ color: "var(--cyan-ink)" }}>{t("edit.cardTitle")}</span>
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
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
          {!editing ? (
            <>
              <button
                className="btn sm"
                onClick={onRescan}
                disabled={rescanning || upgrading || enqueueingRender}
                title={t("buttons.rescanTooltip")}
              >
                <Ico name="refresh" size={11} />
                {rescanning ? t("buttons.rescanning") : t("edit.autoJudge")}
              </button>
              <button
                className="btn sm"
                onClick={onUpgrade}
                disabled={rescanning || upgrading || enqueueingRender}
                title={t("buttons.upgradeTooltip")}
                style={{ borderColor: "var(--cyan-200)", color: "var(--cyan-ink)" }}
              >
                <Ico name="scan" size={11} />
                {upgrading ? t("buttons.upgrading") : t("edit.deepJudge")}
              </button>
              <button
                className="btn sm primary"
                data-testid="render-btn"
                onClick={onRender}
                disabled={enqueueingRender || renderGateBlocked}
                title={renderGateTitle}
              >
                <Ico name="image" size={11} />
                {enqueueingRender ? t("buttons.enqueuing") : t("edit.autoRender")}
              </button>
              {renderGateBlocked && (
                <button
                  className="btn sm danger"
                  data-testid="force-render-btn"
                  onClick={onForceRender}
                  disabled={enqueueingRender}
                  title={t("edit.forceRenderTooltip")}
                >
                  <Ico name="alert" size={11} />
                  {t("edit.forceRender")}
                </button>
              )}
              <button className="btn sm ghost" onClick={() => onSetEditing(true)}>
                <Ico name="edit" size={11} />
                {t("edit.editButton")}
              </button>
            </>
          ) : (
            <>
              <button className="btn sm ghost" onClick={() => onSetEditing(false)} disabled={saving}>
                {t("edit.cancel")}
              </button>
              <button className="btn sm danger" onClick={onClearOverrides} disabled={saving}>
                {t("edit.clearOverride")}
              </button>
              <button className="btn sm amber" onClick={onSaveEdits} disabled={saving}>
                <Ico name="check" size={11} />
                {saving ? t("edit.saving") : t("edit.save")}
              </button>
            </>
          )}
        </div>
      </div>
      <div className="card-b" style={{ display: "grid", gap: 12 }}>
        {!editing && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              flexWrap: "wrap",
              gap: 8,
              padding: "8px 10px",
              border: "1px solid var(--cyan-200)",
              borderRadius: 6,
              background: "var(--cyan-50)",
              color: "var(--cyan-ink)",
              fontSize: 11.5,
            }}
          >
            <span>{t("edit.autoSummary")}</span>
            <div style={{ display: "flex", gap: 6, flexShrink: 0, flexWrap: "wrap" }}>
              {reviewKey !== "reviewed" && (
                <button
                  className="btn sm"
                  style={{
                    background: "#fff",
                    borderColor: "var(--ok-100)",
                    color: "var(--ok)",
                  }}
                  onClick={() => onSetReview("reviewed")}
                >
                  <Ico name="check" size={11} />
                  {t("edit.markReviewed")}
                </button>
              )}
              {reviewKey !== "needs_recheck" && (
                <button className="btn sm danger" onClick={() => onSetReview("needs_recheck")}>
                  <Ico name="alert" size={11} />
                  {t("edit.needsRecheck")}
                </button>
              )}
              {!isHeldNow && (
                <button
                  className="btn sm ghost"
                  onClick={onHoldCase}
                  disabled={saving}
                  title={t("edit.holdTooltip")}
                  style={{ borderStyle: "dashed", color: "var(--ink-3)", background: "#fff" }}
                >
                  <Ico name="dot" size={11} />
                  {t("edit.hold")}
                </button>
              )}
            </div>
          </div>
        )}
        {editing && (
          <div style={{ fontSize: 11, color: "var(--ink-3)", display: "flex", alignItems: "center", gap: 6 }}>
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
                onDraftChange({ ...draft, manual_category: e.target.value as Category | "" })
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
              onChange={(e) => onDraftChange({ ...draft, manual_template_tier: e.target.value })}
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
              onChange={(e) => onDraftChange({ ...draft, notes: e.target.value })}
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
              onChange={(e) => onDraftChange({ ...draft, tags: e.target.value })}
              placeholder={t("edit.tagsPlaceholder")}
              style={{ borderColor: "var(--amber-200)", background: "#fff" }}
            />
          ) : data.tags.length > 0 ? (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
              {data.tags.map((tag) => (
                <span key={tag} className="chip on">
                  {tag}
                </span>
              ))}
            </div>
          ) : (
            <span style={{ color: "var(--ink-4)", fontStyle: "italic" }}>—</span>
          )}

          <label style={{ fontSize: 11.5, color: "var(--ink-3)", alignSelf: "flex-start", marginTop: 4 }}>
            {t("edit.blockingLabel")}
          </label>
          <div style={{ display: "grid", gap: 6 }}>
            {editing && (
              <select
                value=""
                onChange={(e) => {
                  if (e.target.value) onToggleExtraBlocking(e.target.value);
                }}
                style={{ background: "#fff", borderColor: "var(--amber-200)" }}
              >
                <option value="">{t("edit.blockingSelectPlaceholder")}</option>
                {issueDict.map((iss) => (
                  <option
                    key={iss.code}
                    value={iss.code}
                    disabled={draft.extra_blocking.includes(iss.code)}
                  >
                    {iss.zh}
                  </option>
                ))}
              </select>
            )}
            <div style={{ display: "flex", flexWrap: "wrap", gap: 4, minHeight: 24, alignItems: "center" }}>
              {(editing ? draft.extra_blocking : data.manual_blocking_codes).length > 0 ? (
                (editing ? draft.extra_blocking : data.manual_blocking_codes).map((code) => {
                  const iss = issueDict.find((item) => item.code === code);
                  return (
                    <button
                      key={code}
                      type="button"
                      className="chip danger on"
                      onClick={() => editing && onToggleExtraBlocking(code)}
                      style={{ cursor: editing ? "pointer" : "default" }}
                      title={code}
                    >
                      {iss?.zh ?? code}
                      {editing && <Ico name="x" size={10} />}
                    </button>
                  );
                })
              ) : (
                <span style={{ color: "var(--ink-4)", fontSize: 12 }}>{t("edit.noExtraBlocking")}</span>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
