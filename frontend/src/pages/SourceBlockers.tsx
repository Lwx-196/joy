import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  caseFileUrl,
  type CaseSourceProfile,
  type SourceBindingCandidate,
  type SourceBlockerItem,
  type SourceBlockerReason,
} from "../api";
import {
  useBindSourceDirectories,
  useClearSourceDirectoryBindings,
  useRescanCase,
  useSourceBindingCandidates,
  useSourceBlockerAction,
  useSourceBlockers,
} from "../hooks/queries";
import { Ico } from "../components/atoms";

type ReasonFilter = "all" | SourceBlockerReason;

const REASON_KEYS: ReasonFilter[] = [
  "all",
  "no_real_source_photos",
  "insufficient_source_photos",
  "missing_before_after_pair",
];

function reasonTone(reason: SourceBlockerReason) {
  if (reason === "no_real_source_photos") return { bg: "#FEF2F2", fg: "#B91C1C", border: "#FECACA" };
  if (reason === "insufficient_source_photos") return { bg: "#FFF7ED", fg: "#C2410C", border: "#FED7AA" };
  return { bg: "#FDF2F8", fg: "#BE185D", border: "#FBCFE8" };
}

function sampleFiles(item: SourceBlockerItem) {
  const profile = item.source_profile;
  const source = profile.source_samples ?? [];
  const generated = profile.generated_artifact_samples ?? [];
  if (item.reason === "no_real_source_photos" && generated.length > 0) return generated;
  return [...source, ...generated].slice(0, 6);
}

export default function SourceBlockers() {
  const { t } = useTranslation("sourceBlockers");
  const [reason, setReason] = useState<ReasonFilter>("all");
  const blockersQ = useSourceBlockers({ reason, limit: 300 });
  const actionMut = useSourceBlockerAction();
  const rescanMut = useRescanCase();
  const data = blockersQ.data;
  const counts = data?.counts ?? {};
  const items = data?.items ?? [];
  const busyCaseId = useMemo(() => {
    const actionVars = actionMut.variables as { caseId?: number } | undefined;
    return actionMut.isPending ? actionVars?.caseId ?? null : null;
  }, [actionMut.isPending, actionMut.variables]);

  const profileLine = (profile: CaseSourceProfile) =>
    t("profile.line", {
      source: profile.source_count,
      generated: profile.generated_artifact_count,
      before: profile.before_count,
      after: profile.after_count,
      unlabeled: profile.unlabeled_source_count,
    });

  const markNotSource = (item: SourceBlockerItem) => {
    const note = window.prompt(t("prompts.markNotSourceTitle"), t("prompts.markNotSourceDefault"));
    if (note === null) return;
    actionMut.mutate({
      caseId: item.case_id,
      payload: { action: "mark_not_source", reviewer: "source-blocker-workbench", note },
    });
  };

  const clearNotSource = (item: SourceBlockerItem) => {
    const ok = window.confirm(t("prompts.confirmClearNotSource", { id: item.case_id }));
    if (!ok) return;
    actionMut.mutate({
      caseId: item.case_id,
      payload: { action: "clear_not_source", reviewer: "source-blocker-workbench" },
    });
  };

  return (
    <div style={{ height: "100%", display: "grid", gridTemplateRows: "auto auto 1fr", overflow: "hidden" }}>
      <div className="page-header">
        <div>
          <h1 className="page-title">
            {t("header.title")}{" "}
            <span style={{ fontFamily: "var(--mono)", color: "var(--ink-3)", fontSize: 14, fontWeight: 500, marginLeft: 6 }}>
              {counts.total ?? 0}
            </span>
          </h1>
          <div className="page-sub">{t("header.subtitle")}</div>
        </div>
        <button className="btn sm" onClick={() => blockersQ.refetch()} disabled={blockersQ.isFetching}>
          <Ico name="refresh" size={12} />
          {blockersQ.isFetching ? t("header.refreshing") : t("header.refresh")}
        </button>
      </div>

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
        {REASON_KEYS.map((key) => (
          <button
            key={key}
            type="button"
            className={`btn sm ${reason === key ? "primary" : "ghost"}`}
            onClick={() => setReason(key)}
          >
            {t(`reasons.${key}` as never)}
            <span style={{ fontFamily: "var(--mono)", opacity: 0.75 }}>
              {key === "all" ? counts.total ?? 0 : counts[key] ?? 0}
            </span>
          </button>
        ))}
        <span style={{ marginLeft: "auto", color: "var(--ink-3)", fontSize: 12 }}>
          {t("counts.markedNotSource", { n: counts.marked_not_source ?? 0 })}
        </span>
      </div>

      <main style={{ overflow: "auto", padding: 24 }}>
        {blockersQ.isLoading ? (
          <div className="route-fallback">{t("states.loading")}</div>
        ) : blockersQ.isError ? (
          <div className="empty">
            <Ico name="alert" size={16} />
            {t("states.loadError")}
          </div>
        ) : items.length === 0 ? (
          <div className="empty">
            <Ico name="check" size={16} />
            {t("states.empty")}
          </div>
        ) : (
          <div style={{ display: "grid", gap: 10 }}>
            {items.map((item) => (
              <BlockerRow
                key={item.case_id}
                item={item}
                busy={busyCaseId === item.case_id || (rescanMut.isPending && rescanMut.variables === item.case_id)}
                onMarkNotSource={() => markNotSource(item)}
                onClearNotSource={() => clearNotSource(item)}
                onRescan={() => rescanMut.mutate(item.case_id)}
                profileLine={profileLine}
              />
            ))}
          </div>
        )}
      </main>
    </div>
  );
}

function BlockerRow({
  item,
  busy,
  onMarkNotSource,
  onClearNotSource,
  onRescan,
  profileLine,
}: {
  item: SourceBlockerItem;
  busy: boolean;
  onMarkNotSource: () => void;
  onClearNotSource: () => void;
  onRescan: () => void;
  profileLine: (profile: CaseSourceProfile) => string;
}) {
  const { t } = useTranslation("sourceBlockers");
  const tone = reasonTone(item.reason);
  const files = sampleFiles(item);
  const bindingQ = useSourceBindingCandidates(
    item.reason === "missing_before_after_pair" ? item.case_id : null,
    { limit: 3 },
  );
  const bindMut = useBindSourceDirectories();
  const clearBindMut = useClearSourceDirectoryBindings();
  const candidates = bindingQ.data?.candidates ?? [];
  const reasonsJoin = t("binding.reasonsJoin");
  const confirmBind = (candidate: SourceBindingCandidate) => {
    const reasonsText = candidate.match_reasons.join(reasonsJoin);
    const ok = window.confirm(
      t("prompts.confirmBind", {
        caseId: item.case_id,
        candidateId: candidate.case_id,
        reasons: reasonsText,
      }),
    );
    if (!ok) return;
    bindMut.mutate({
      caseId: item.case_id,
      sourceCaseIds: [candidate.case_id],
      note: reasonsText,
    });
  };
  const clearBinding = () => {
    const ok = window.confirm(t("prompts.confirmClearBinding", { id: item.case_id }));
    if (!ok) return;
    clearBindMut.mutate(item.case_id);
  };
  return (
    <article
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(260px, 1fr) minmax(280px, 360px) auto",
        gap: 14,
        alignItems: "center",
        minHeight: 126,
        border: "1px solid var(--line)",
        borderRadius: 8,
        background: "var(--panel)",
        padding: 12,
      }}
    >
      <div style={{ minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span
            className="badge"
            style={{ background: tone.bg, color: tone.fg, borderColor: tone.border }}
          >
            {item.reason_label}
          </span>
          {item.marked_not_source && (
            <span className="badge" style={{ background: "#F5F5F4", color: "#57534E" }}>
              {t("badge.markedNotSource")}
            </span>
          )}
          <Link to={`/cases/${item.case_id}`} style={{ fontWeight: 650, color: "var(--ink-1)" }}>
            #{item.case_id} {item.case_title}
          </Link>
        </div>
        <div style={{ marginTop: 8, color: "var(--ink-2)", fontSize: 13, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {item.abs_path}
        </div>
        <div style={{ marginTop: 8, color: "var(--ink-3)", fontSize: 12 }}>
          {profileLine(item.source_profile)}
        </div>
        <div style={{ marginTop: 8, color: "var(--ink-2)", fontSize: 12 }}>
          {item.recommended_action}
        </div>
        {item.bound_case_ids && item.bound_case_ids.length > 0 && (
          <div style={{ marginTop: 8, color: "var(--ok)", fontSize: 12 }}>
            {t("row.boundLabel")}
            {item.bound_case_ids.map((id) => `#${id}`).join(t("row.boundJoin"))}
          </div>
        )}
      </div>

      <div style={{ display: "grid", gap: 8 }}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 6 }}>
          {files.slice(0, 6).map((file) => (
            <div
              key={file}
              title={file}
              style={{
                height: 52,
                border: "1px solid var(--line)",
                borderRadius: 6,
                background: "var(--bg-2)",
                overflow: "hidden",
                display: "grid",
                placeItems: "center",
              }}
            >
              <img
                src={caseFileUrl(item.case_id, file)}
                alt={file}
                loading="lazy"
                style={{ width: "100%", height: "100%", objectFit: "contain", display: "block" }}
              />
            </div>
          ))}
        </div>
        {item.reason === "missing_before_after_pair" && (
          <BindingSuggestions
            candidates={candidates}
            loading={bindingQ.isLoading}
            onBind={confirmBind}
            binding={bindMut.isPending}
            profileLine={profileLine}
          />
        )}
      </div>

      <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 8, flexWrap: "wrap" }}>
        <Link className="btn sm" to={`/cases/${item.case_id}`}>
          <Ico name="arrow" size={12} />
          {t("actions.detail")}
        </Link>
        <button className="btn sm" onClick={onRescan} disabled={busy}>
          <Ico name="scan" size={12} />
          {t("actions.rescan")}
        </button>
        {item.reason === "no_real_source_photos" && !item.marked_not_source && (
          <button className="btn sm danger" onClick={onMarkNotSource} disabled={busy}>
            <Ico name="flag" size={12} />
            {t("actions.markNotSource")}
          </button>
        )}
        {item.marked_not_source && (
          <button className="btn sm ghost" onClick={onClearNotSource} disabled={busy}>
            <Ico name="refresh" size={12} />
            {t("actions.clearNotSource")}
          </button>
        )}
        {item.bound_case_ids && item.bound_case_ids.length > 0 && (
          <button className="btn sm ghost" onClick={clearBinding} disabled={clearBindMut.isPending}>
            <Ico name="x" size={12} />
            {t("actions.clearBinding")}
          </button>
        )}
      </div>
    </article>
  );
}

function BindingSuggestions({
  candidates,
  loading,
  binding,
  onBind,
  profileLine,
}: {
  candidates: SourceBindingCandidate[];
  loading: boolean;
  binding: boolean;
  onBind: (candidate: SourceBindingCandidate) => void;
  profileLine: (profile: CaseSourceProfile) => string;
}) {
  const { t } = useTranslation("sourceBlockers");
  if (loading) {
    return <div style={{ color: "var(--ink-3)", fontSize: 12 }}>{t("binding.loading")}</div>;
  }
  if (candidates.length === 0) {
    return <div style={{ color: "var(--ink-4)", fontSize: 12 }}>{t("binding.empty")}</div>;
  }
  const reasonsJoin = t("binding.reasonsJoin");
  return (
    <div style={{ display: "grid", gap: 6 }}>
      {candidates.slice(0, 2).map((candidate) => (
        <div
          key={candidate.case_id}
          style={{
            border: "1px solid var(--line)",
            borderRadius: 6,
            padding: "6px 8px",
            display: "grid",
            gap: 4,
            background: candidate.can_complete_pair ? "#F0FDF4" : "var(--bg-2)",
          }}
        >
          <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "center" }}>
            <Link to={`/cases/${candidate.case_id}`} style={{ color: "var(--ink-1)", fontWeight: 600, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              #{candidate.case_id} {candidate.case_title}
            </Link>
            <button className="btn sm primary" onClick={() => onBind(candidate)} disabled={binding || candidate.already_bound}>
              <Ico name="link" size={11} />
              {candidate.already_bound ? t("binding.alreadyBound") : t("binding.bind")}
            </button>
          </div>
          <div style={{ color: "var(--ink-3)", fontSize: 11 }}>
            {candidate.match_reasons.join(reasonsJoin)}
          </div>
          <div style={{ color: "var(--ink-3)", fontSize: 11 }}>
            {t("binding.mergedPrefix")}
            {profileLine(candidate.merged_source_profile)}
          </div>
        </div>
      ))}
    </div>
  );
}
