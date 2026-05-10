import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { CATEGORY_LABEL, TIER_LABEL, type CaseGroupSummary } from "../api";
import {
  useCaseGroupDiagnosis,
  useCaseGroups,
  useConfirmCaseGroup,
  useRenderCaseGroup,
  useRescanCaseGroups,
  useSimulateCaseGroupAfter,
} from "../hooks/queries";
import { useBrand } from "../lib/brand-context";
import { Ico } from "../components/atoms";

function scoreColor(score: number) {
  if (score >= 0.8) return "var(--ok)";
  if (score >= 0.6) return "var(--amber-ink)";
  return "var(--err)";
}

export default function CaseGroups() {
  const { t } = useTranslation("caseGroups");
  const [status, setStatus] = useState("");
  const groupsQ = useCaseGroups({ status: status || undefined, limit: 300 });
  const groupItems = groupsQ.data?.items;
  const groups = useMemo(() => groupItems ?? [], [groupItems]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const selected = selectedId ?? groups[0]?.id ?? null;
  const diagnosisQ = useCaseGroupDiagnosis(selected);
  const rescanMut = useRescanCaseGroups();
  const confirmMut = useConfirmCaseGroup();
  const renderMut = useRenderCaseGroup();
  const simulateMut = useSimulateCaseGroupAfter();
  const brand = useBrand();

  const statusLabel = (s: string) => {
    if (s === "confirmed") return t("status.confirmed");
    if (s === "needs_review") return t("status.needs_review");
    return t("status.auto");
  };

  const stats = useMemo(() => {
    const low = groups.filter((g) => g.status === "needs_review").length;
    const confirmed = groups.filter((g) => g.status === "confirmed").length;
    return { low, confirmed, total: groups.length };
  }, [groups]);

  const diagnosis = diagnosisQ.data ?? null;
  const group = diagnosis?.group ?? groups.find((g) => g.id === selected) ?? null;

  const runSimulation = () => {
    if (!group) return;
    const raw = window.prompt(t("simulate.promptTargets"));
    if (!raw) return;
    const focus_targets = raw.split(/[,，\n]/).map((x) => x.trim()).filter(Boolean);
    simulateMut.mutate(
      {
        groupId: group.id,
        payload: {
          focus_targets,
          ai_generation_authorized: true,
          provider: "ps_model_router",
          note: t("simulate.note"),
        },
      },
      {
        onSuccess: (data) => {
          window.alert(
            data.error_message
              ? t("simulate.successBlocked", { message: data.error_message })
              : t("simulate.successCreated"),
          );
        },
        onError: (err) => {
          window.alert(err instanceof Error ? err.message : t("simulate.failed"));
        },
      },
    );
  };

  return (
    <div style={{ height: "100%", display: "grid", gridTemplateRows: "auto 1fr", overflow: "hidden" }}>
      <div className="page-header">
        <div>
          <h1 className="page-title">
            {t("header.title")}
            <span style={{ fontFamily: "var(--mono)", color: "var(--ink-3)", fontSize: 14, fontWeight: 500, marginLeft: 6 }}>
              {stats.total}
            </span>
          </h1>
          <div className="page-sub">{t("header.subtitle")}</div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <label className="select" style={{ minWidth: 148, position: "relative" }}>
            <span style={{ color: "var(--ink-3)", fontSize: 11.5 }}>{t("filter.queueLabel")}</span>
            <span>{status ? statusLabel(status) : t("filter.all")}</span>
            <select value={status} onChange={(e) => setStatus(e.target.value)} style={{ position: "absolute", inset: 0, opacity: 0 }}>
              <option value="">{t("filter.all")}</option>
              <option value="needs_review">{t("status.needs_review")}</option>
              <option value="auto">{t("status.auto")}</option>
              <option value="confirmed">{t("status.confirmed")}</option>
            </select>
          </label>
          <span className="badge">{t("stats.lowConfidence", { n: stats.low })}</span>
          <span className="badge">{t("stats.confirmed", { n: stats.confirmed })}</span>
          <button className="btn sm primary" onClick={() => rescanMut.mutate()} disabled={rescanMut.isPending}>
            <Ico name="scan" size={12} />
            {rescanMut.isPending ? t("actions.rescanRunning") : t("actions.rescan")}
          </button>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "360px 1fr", minHeight: 0, overflow: "hidden" }}>
        <aside style={{ borderRight: "1px solid var(--line)", overflow: "auto", padding: 12, background: "var(--panel-2)" }}>
          {groups.map((g) => (
            <button
              key={g.id}
              type="button"
              onClick={() => setSelectedId(g.id)}
              style={{
                width: "100%",
                textAlign: "left",
                display: "grid",
                gap: 4,
                padding: 10,
                marginBottom: 8,
                border: `1px solid ${selected === g.id ? "var(--cyan-200)" : "var(--line)"}`,
                background: selected === g.id ? "var(--cyan-50)" : "var(--panel)",
                borderRadius: 6,
                cursor: "pointer",
              }}
            >
              <GroupTitle g={g} />
            </button>
          ))}
          {!groupsQ.isLoading && groups.length === 0 && <div className="empty">{t("states.emptyList")}</div>}
        </aside>

        <main style={{ overflow: "auto", padding: 18 }}>
          {!group ? (
            <div className="empty">{t("states.emptyDetail")}</div>
          ) : (
            <div style={{ display: "grid", gap: 12 }}>
              <section className="card">
                <div className="card-h">
                  <div className="t">
                    <Ico name="folder" size={13} />
                    {group.title}
                    <span className="badge">{statusLabel(group.status)}</span>
                  </div>
                  <div style={{ display: "flex", gap: 6 }}>
                    {group.primary_case_id && (
                      <Link className="btn sm" to={`/cases/${group.primary_case_id}`}>
                        <Ico name="link" size={11} />
                        {t("actions.detail")}
                      </Link>
                    )}
                    <button className="btn sm" onClick={() => confirmMut.mutate({ groupId: group.id, payload: { status: "confirmed" } })}>
                      <Ico name="check" size={11} />
                      {t("actions.confirm")}
                    </button>
                    <button className="btn sm primary" onClick={() => renderMut.mutate({ groupId: group.id, payload: { brand, template: "tri-compare", semantic_judge: "auto" } })}>
                      <Ico name="image" size={11} />
                      {t("actions.renderByDiagnosis")}
                    </button>
                    <button className="btn sm danger" onClick={runSimulation}>
                      <Ico name="edit" size={11} />
                      {t("actions.simulate")}
                    </button>
                  </div>
                </div>
                <div className="card-b" style={{ display: "grid", gap: 10 }}>
                  <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--ink-3)", wordBreak: "break-all" }}>
                    {group.root_path}
                  </div>
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                    <span className="badge">{t("diagnosis.imageCount", { n: group.diagnosis.image_count ?? 0 })}</span>
                    <span className="badge">{t("diagnosis.lowConfidence", { n: group.diagnosis.low_confidence_count ?? 0 })}</span>
                    <span className="badge">{t("diagnosis.blockingPair", { n: group.diagnosis.blocking_pair_count ?? 0 })}</span>
                    <span className="badge">{t("diagnosis.suggestedTemplate", { template: group.diagnosis.suggested_template ?? t("diagnosis.suggestedTemplateFallback") })}</span>
                    {group.category && <span className="badge">{CATEGORY_LABEL[group.category as keyof typeof CATEGORY_LABEL] ?? group.category}</span>}
                    {group.template_tier && <span className="badge">{TIER_LABEL[group.template_tier] ?? group.template_tier}</span>}
                  </div>
                </div>
              </section>

              <section className="card">
                <div className="card-h">
                  <div className="t"><Ico name="split" size={13} />{t("pairs.title")}</div>
                </div>
                <div className="card-b">
                  <table className="table" style={{ tableLayout: "fixed" }}>
                    <thead>
                      <tr>
                        <th>{t("pairs.headers.view")}</th>
                        <th>{t("pairs.headers.before")}</th>
                        <th>{t("pairs.headers.after")}</th>
                        <th>{t("pairs.headers.score")}</th>
                        <th>{t("pairs.headers.status")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(diagnosis?.pair_candidates ?? []).map((p) => (
                        <tr key={p.id}>
                          <td>{p.slot}</td>
                          <td><code>{p.before_image_path ?? t("pairs.missing")}</code></td>
                          <td><code>{p.after_image_path ?? t("pairs.missing")}</code></td>
                          <td style={{ color: scoreColor(p.score), fontFamily: "var(--mono)" }}>{Math.round(p.score * 100)}</td>
                          <td>{p.status}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>

              <section className="card">
                <div className="card-h">
                  <div className="t"><Ico name="scan" size={13} />{t("observations.title")}</div>
                </div>
                <div className="card-b">
                  <table className="table" style={{ tableLayout: "fixed" }}>
                    <thead>
                      <tr>
                        <th>{t("observations.headers.image")}</th>
                        <th>{t("observations.headers.phase")}</th>
                        <th>{t("observations.headers.view")}</th>
                        <th>{t("observations.headers.bodyPart")}</th>
                        <th>{t("observations.headers.confidence")}</th>
                        <th>{t("observations.headers.sourceReason")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(diagnosis?.image_observations ?? []).map((obs) => (
                        <tr key={obs.id}>
                          <td style={{ wordBreak: "break-all" }}><code>{obs.image_path}</code></td>
                          <td>{obs.phase}</td>
                          <td>{obs.view}</td>
                          <td>{obs.body_part}</td>
                          <td style={{ color: scoreColor(obs.confidence), fontFamily: "var(--mono)" }}>{Math.round(obs.confidence * 100)}</td>
                          <td style={{ fontSize: 11, color: "var(--ink-3)" }}>{obs.source} · {obs.reasons.slice(0, 3).join(" / ")}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

function GroupTitle({ g }: { g: CaseGroupSummary }) {
  const { t } = useTranslation("caseGroups");
  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <strong style={{ fontSize: 13, color: "var(--ink-1)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {g.title}
        </strong>
        {g.status === "needs_review" && (
          <span className="badge" style={{ background: "var(--amber-50)", color: "var(--amber-ink)", borderColor: "var(--amber-200)" }}>
            {t("groupTitle.needsReview")}
          </span>
        )}
      </div>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", fontSize: 11 }}>
        <span className="badge">{t("groupTitle.imageShort", { n: g.diagnosis.image_count ?? 0 })}</span>
        <span className="badge">{t("groupTitle.lowShort", { n: g.diagnosis.low_confidence_count ?? 0 })}</span>
        <span className="badge">{g.diagnosis.suggested_template ?? t("groupTitle.suggestedTemplateFallback")}</span>
      </div>
      <div style={{ fontFamily: "var(--mono)", fontSize: 10.5, color: "var(--ink-4)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {g.root_path}
      </div>
    </>
  );
}
