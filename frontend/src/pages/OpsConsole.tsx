import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Ico } from "../components/atoms";
import { useOpsStatus } from "../hooks/queries";
import type { PromotionBlock, SloRecommendation } from "../api";

type SloWindow = 24 | 48 | 72;

const RECOMMENDATION_TONE: Record<string, { bg: string; fg: string; border: string }> = {
  continue: { bg: "#ECFDF5", fg: "#047857", border: "#A7F3D0" },
  rollback: { bg: "#FEF2F2", fg: "#B91C1C", border: "#FECACA" },
  insufficient_data: { bg: "#F5F5F4", fg: "#57534E", border: "#E7E5E4" },
  monitoring_paused: { bg: "#FFF7ED", fg: "#C2410C", border: "#FED7AA" },
  stop_loss_halt: { bg: "#FEE2E2", fg: "#991B1B", border: "#FCA5A5" },
  unknown: { bg: "#F5F5F4", fg: "#57534E", border: "#E7E5E4" },
};

const STATE_TONE: Record<string, { bg: string; fg: string; border: string }> = {
  shadow: { bg: "#F1F5F9", fg: "#475569", border: "#CBD5E1" },
  p10: { bg: "#ECFEFF", fg: "#0E7490", border: "#A5F3FC" },
  p25: { bg: "#EFF6FF", fg: "#1D4ED8", border: "#BFDBFE" },
  p50: { bg: "#EEF2FF", fg: "#4338CA", border: "#C7D2FE" },
  p100: { bg: "#F0FDF4", fg: "#15803D", border: "#BBF7D0" },
  rolled_back: { bg: "#FEF2F2", fg: "#B91C1C", border: "#FECACA" },
};

function recommendationTone(rec: SloRecommendation | null | undefined) {
  if (!rec) return RECOMMENDATION_TONE.unknown;
  return RECOMMENDATION_TONE[rec] ?? RECOMMENDATION_TONE.unknown;
}

function stateTone(state: string) {
  return STATE_TONE[state] ?? STATE_TONE.shadow;
}

export default function OpsConsole() {
  const { t } = useTranslation("opsConsole");
  const [sloWindow, setSloWindow] = useState<SloWindow>(24);
  const [probeOn, setProbeOn] = useState(true);
  const opsQ = useOpsStatus({ sloWindowHours: sloWindow, probeComfyui: probeOn });

  const lastUpdatedLabel = useMemo(() => {
    const computedAt = opsQ.data?.promotion?.computed_at;
    if (!computedAt) return null;
    try {
      return new Date(computedAt).toLocaleTimeString();
    } catch {
      return computedAt;
    }
  }, [opsQ.data?.promotion?.computed_at]);

  return (
    <div
      data-testid="ops-console"
      style={{
        height: "100%",
        display: "grid",
        gridTemplateRows: "auto auto 1fr",
        overflow: "hidden",
      }}
    >
      <div className="page-header">
        <div>
          <h1 className="page-title">{t("header.title")}</h1>
          <div className="page-sub">{t("header.subtitle")}</div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {lastUpdatedLabel && (
            <span
              style={{
                color: "var(--ink-3)",
                fontSize: 12,
                fontFamily: "var(--mono)",
              }}
              data-testid="ops-console-last-updated"
            >
              {t("header.lastUpdated", { when: lastUpdatedLabel })}
            </span>
          )}
          <button
            className="btn sm"
            data-testid="ops-console-refresh"
            onClick={() => opsQ.refetch()}
            disabled={opsQ.isFetching}
          >
            <Ico name="refresh" size={12} />
            {opsQ.isFetching ? t("header.refreshing") : t("header.refresh")}
          </button>
        </div>
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
        {([24, 48, 72] as const).map((win) => (
          <button
            key={win}
            type="button"
            className={`btn sm ${sloWindow === win ? "primary" : "ghost"}`}
            data-testid={`ops-console-window-${win}h`}
            onClick={() => setSloWindow(win)}
          >
            {t(`controls.slo${win}h` as never)}
          </button>
        ))}
        <button
          type="button"
          className={`btn sm ${probeOn ? "primary" : "ghost"}`}
          data-testid="ops-console-probe-toggle"
          onClick={() => setProbeOn((v) => !v)}
        >
          {probeOn ? t("controls.probeOn") : t("controls.probeOff")}
        </button>
      </div>

      <main style={{ overflow: "auto", padding: 24 }}>
        {opsQ.isLoading ? (
          <div className="route-fallback" data-testid="ops-console-loading">
            {t("states.loading")}
          </div>
        ) : opsQ.isError ? (
          <div className="empty" data-testid="ops-console-error">
            <Ico name="alert" size={16} />
            {t("states.loadError")}
            <button
              className="btn sm"
              style={{ marginLeft: 12 }}
              onClick={() => opsQ.refetch()}
            >
              {t("states.errorRetry")}
            </button>
          </div>
        ) : opsQ.data ? (
          <PromotionGrid promotion={opsQ.data.promotion} />
        ) : null}
      </main>
    </div>
  );
}

function PromotionGrid({ promotion }: { promotion: PromotionBlock }) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
        gap: 14,
      }}
      data-testid="ops-console-grid"
    >
      <ManifestCard promotion={promotion} />
      <SloCard promotion={promotion} />
      <ComfyUiCard promotion={promotion} />
      <LatencyCard promotion={promotion} />
      <SilentFailCard promotion={promotion} />
      <ApplierCard promotion={promotion} />
    </div>
  );
}

function CardShell({
  title,
  testId,
  children,
}: {
  title: string;
  testId: string;
  children: React.ReactNode;
}) {
  return (
    <section
      data-testid={testId}
      style={{
        border: "1px solid var(--line)",
        borderRadius: 8,
        background: "var(--panel)",
        padding: 14,
        display: "grid",
        gap: 10,
      }}
    >
      <h2
        style={{
          margin: 0,
          fontSize: 14,
          fontWeight: 650,
          color: "var(--ink-1)",
        }}
      >
        {title}
      </h2>
      {children}
    </section>
  );
}

function KV({
  label,
  value,
  testId,
  monospace = false,
}: {
  label: string;
  value: React.ReactNode;
  testId?: string;
  monospace?: boolean;
}) {
  return (
    <div
      style={{ display: "flex", justifyContent: "space-between", gap: 12, fontSize: 13 }}
    >
      <span style={{ color: "var(--ink-3)" }}>{label}</span>
      <span
        style={{
          color: "var(--ink-1)",
          fontFamily: monospace ? "var(--mono)" : undefined,
          maxWidth: 180,
          textAlign: "right",
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
        data-testid={testId}
      >
        {value}
      </span>
    </div>
  );
}

function ManifestCard({ promotion }: { promotion: PromotionBlock }) {
  const { t } = useTranslation("opsConsole");
  const tone = stateTone(promotion.manifest_state);
  const baseline = promotion.baseline_freshness;
  return (
    <CardShell title={t("manifest.sectionTitle")} testId="ops-console-card-manifest">
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span
          className="badge"
          data-testid="ops-console-manifest-state"
          style={{ background: tone.bg, color: tone.fg, borderColor: tone.border }}
        >
          {promotion.manifest_state}
        </span>
        <span
          style={{ fontFamily: "var(--mono)", color: "var(--ink-2)", fontSize: 13 }}
          data-testid="ops-console-bucket-exposure"
        >
          {promotion.bucket_exposure_pct}
          {t("manifest.exposureSuffix")}
        </span>
      </div>
      <KV
        label={t("manifest.approver")}
        value={baseline.approver ?? t("manifest.stateNotSet")}
        monospace
      />
      <KV
        label={t("manifest.approvedAt")}
        value={baseline.approved_at ?? t("manifest.stateNotSet")}
        monospace
      />
      <KV
        label={t("manifest.expiresAt")}
        value={baseline.expires_at ?? t("manifest.stateNotSet")}
        monospace
      />
      <KV
        label={baseline.bindings_present ? t("manifest.bindingsPresent") : t("manifest.bindingsMissing")}
        value=""
        testId="ops-console-bindings-present"
      />
      <KV
        label={t("manifest.rollbackBaselineAge", {
          days: baseline.rollback_baseline_age_days ?? "—",
        })}
        value={
          baseline.rollback_baseline_captured_at == null
            ? t("manifest.rollbackBaselineMissing")
            : ""
        }
      />
    </CardShell>
  );
}

function SloCard({ promotion }: { promotion: PromotionBlock }) {
  const { t } = useTranslation("opsConsole");
  const rec = promotion.slo_recommendation;
  const tone = recommendationTone(rec);
  const violations = promotion.violations;
  const withinKey =
    promotion.slo_within === true
      ? "slo.withinSlo_true"
      : promotion.slo_within === false
        ? "slo.withinSlo_false"
        : "slo.withinSlo_null";
  return (
    <CardShell title={t("slo.sectionTitle")} testId="ops-console-card-slo">
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span
          className="badge"
          data-testid="ops-console-slo-recommendation"
          style={{ background: tone.bg, color: tone.fg, borderColor: tone.border }}
        >
          {rec ? t(`recommendation.${rec}` as never) : "—"}
        </span>
        <span style={{ fontSize: 12, color: "var(--ink-3)" }}>
          {t(withinKey as never)}
        </span>
      </div>
      <KV
        label={t("slo.sampleLabel")}
        value={t("slo.sampleVsMin", {
          sample: promotion.sample_size,
          min: promotion.minimum_sample_size,
        })}
        testId="ops-console-slo-sample"
        monospace
      />
      <KV
        label={t("slo.windowLabel")}
        value={`${promotion.slo_window_hours}${t("slo.windowSuffix")}`}
        monospace
      />
      {promotion.slo_generated_at && (
        <KV
          label={t("slo.generatedAt")}
          value={new Date(promotion.slo_generated_at).toLocaleTimeString()}
          monospace
        />
      )}
      {promotion.slo_error && (
        <div
          data-testid="ops-console-slo-error"
          style={{ color: "#B91C1C", fontSize: 12 }}
        >
          {t("slo.errorBlock", { message: promotion.slo_error })}
        </div>
      )}
      <div
        style={{
          display: "grid",
          gap: 4,
          paddingTop: 6,
          borderTop: "1px solid var(--line)",
        }}
      >
        <span style={{ fontSize: 12, color: "var(--ink-2)" }}>
          {t("slo.violations", { count: violations.length })}
        </span>
        {violations.length === 0 ? (
          <span
            style={{ color: "var(--ink-3)", fontSize: 12 }}
            data-testid="ops-console-violations-empty"
          >
            {t("slo.noViolations")}
          </span>
        ) : (
          <ul
            style={{
              margin: 0,
              paddingLeft: 16,
              maxHeight: 120,
              overflow: "auto",
            }}
            data-testid="ops-console-violations-list"
          >
            {violations.map((v, idx) => (
              <li key={idx} style={{ fontSize: 12, color: "var(--ink-2)" }}>
                <code style={{ fontFamily: "var(--mono)" }}>
                  {JSON.stringify(v)}
                </code>
              </li>
            ))}
          </ul>
        )}
      </div>
    </CardShell>
  );
}

function ComfyUiCard({ promotion }: { promotion: PromotionBlock }) {
  const { t } = useTranslation("opsConsole");
  const probe = promotion.comfyui_live_probe;
  const status =
    probe.skipped
      ? "skipped"
      : probe.reachable === true
        ? "reachable"
        : "unreachable";
  const tone =
    status === "reachable"
      ? { bg: "#ECFDF5", fg: "#047857", border: "#A7F3D0" }
      : status === "skipped"
        ? { bg: "#F5F5F4", fg: "#57534E", border: "#E7E5E4" }
        : { bg: "#FEF2F2", fg: "#B91C1C", border: "#FECACA" };
  const label =
    status === "reachable"
      ? t("comfyui.reachableTrue")
      : status === "skipped"
        ? t("comfyui.reachableSkipped")
        : t("comfyui.reachableFalse");
  return (
    <CardShell title={t("comfyui.sectionTitle")} testId="ops-console-card-comfyui">
      <span
        className="badge"
        data-testid="ops-console-comfyui-reachable"
        style={{
          background: tone.bg,
          color: tone.fg,
          borderColor: tone.border,
          alignSelf: "flex-start",
        }}
      >
        {label}
      </span>
      <KV
        label={t("comfyui.baseUrl")}
        value={probe.base_url ?? "—"}
        monospace
        testId="ops-console-comfyui-baseurl"
      />
      {typeof probe.http_status === "number" && (
        <KV
          label={t("comfyui.httpStatus", { status: probe.http_status })}
          value=""
          monospace
        />
      )}
      <KV
        label={t("comfyui.queueRunning", { n: probe.queue_running ?? "—" })}
        value=""
        monospace
      />
      <KV
        label={t("comfyui.queuePending", { n: probe.queue_pending ?? "—" })}
        value=""
        monospace
      />
      {probe.probed_at && (
        <KV
          label={t("comfyui.probedAt")}
          value={new Date(probe.probed_at).toLocaleTimeString()}
          monospace
        />
      )}
      {probe.error && (
        <div
          data-testid="ops-console-comfyui-error"
          style={{ color: "#B91C1C", fontSize: 12 }}
        >
          {t("comfyui.errorBlock", { message: probe.error })}
        </div>
      )}
    </CardShell>
  );
}

function LatencyCard({ promotion }: { promotion: PromotionBlock }) {
  const { t } = useTranslation("opsConsole");
  const latency = promotion.render_latency;
  const modes = Object.entries(latency.by_render_mode ?? {});
  return (
    <CardShell title={t("latency.sectionTitle")} testId="ops-console-card-latency">
      {latency.error && (
        <div style={{ color: "#B91C1C", fontSize: 12 }}>
          {t("latency.errorBlock", { message: latency.error })}
        </div>
      )}
      {modes.length === 0 ? (
        <span
          style={{ color: "var(--ink-3)", fontSize: 12 }}
          data-testid="ops-console-latency-empty"
        >
          {t("latency.empty")}
        </span>
      ) : (
        <table
          style={{
            width: "100%",
            borderCollapse: "collapse",
            fontSize: 13,
          }}
          data-testid="ops-console-latency-table"
        >
          <thead>
            <tr style={{ color: "var(--ink-3)", textAlign: "left" }}>
              <th style={{ padding: "4px 0" }}>{t("latency.renderMode")}</th>
              <th style={{ padding: "4px 0" }}>{t("latency.count")}</th>
              <th style={{ padding: "4px 0" }}>{t("latency.p50")}</th>
              <th style={{ padding: "4px 0" }}>{t("latency.p95")}</th>
            </tr>
          </thead>
          <tbody>
            {modes.map(([mode, stats]) => (
              <tr key={mode}>
                <td style={{ padding: "4px 0", fontFamily: "var(--mono)" }}>
                  {mode}
                </td>
                <td style={{ padding: "4px 0", fontFamily: "var(--mono)" }}>
                  {stats.count}
                </td>
                <td style={{ padding: "4px 0", fontFamily: "var(--mono)" }}>
                  {stats.p50_seconds}
                  {t("latency.seconds")}
                </td>
                <td style={{ padding: "4px 0", fontFamily: "var(--mono)" }}>
                  {stats.p95_seconds}
                  {t("latency.seconds")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </CardShell>
  );
}

function SilentFailCard({ promotion }: { promotion: PromotionBlock }) {
  const { t } = useTranslation("opsConsole");
  const sf = promotion.silent_fail;
  return (
    <CardShell title={t("silentFail.sectionTitle")} testId="ops-console-card-silent-fail">
      <KV
        label={t("silentFail.count", { n: sf.count })}
        value=""
        testId="ops-console-silent-fail-count"
      />
      {typeof sf.window_hours === "number" && (
        <KV
          label={`${sf.window_hours}${t("silentFail.windowSuffix")}`}
          value=""
          monospace
        />
      )}
      {sf.error && (
        <div style={{ color: "#B91C1C", fontSize: 12 }}>
          {t("silentFail.errorBlock", { message: sf.error })}
        </div>
      )}
    </CardShell>
  );
}

function ApplierCard({ promotion }: { promotion: PromotionBlock }) {
  const { t } = useTranslation("opsConsole");
  const last = promotion.rollback_applier_last;
  if (last.error) {
    return (
      <CardShell title={t("applier.sectionTitle")} testId="ops-console-card-applier">
        <div style={{ color: "#B91C1C", fontSize: 12 }}>
          {t("applier.errorBlock", { message: last.error })}
        </div>
      </CardShell>
    );
  }
  if (last.last_outcome == null) {
    return (
      <CardShell title={t("applier.sectionTitle")} testId="ops-console-card-applier">
        <span
          style={{ color: "var(--ink-3)", fontSize: 12 }}
          data-testid="ops-console-applier-empty"
        >
          {t("applier.empty")}
        </span>
      </CardShell>
    );
  }
  return (
    <CardShell title={t("applier.sectionTitle")} testId="ops-console-card-applier">
      <KV
        label={t("applier.lastOutcome")}
        value={last.last_outcome}
        monospace
        testId="ops-console-applier-outcome"
      />
      {last.last_run_at && (
        <KV
          label={t("applier.lastRunAt")}
          value={new Date(last.last_run_at).toLocaleString()}
          monospace
        />
      )}
      {last.last_reason && (
        <KV
          label={t("applier.lastReason")}
          value={last.last_reason}
          monospace
        />
      )}
      {last.last_request_id && (
        <KV
          label={t("applier.lastRequestId")}
          value={last.last_request_id}
          monospace
        />
      )}
    </CardShell>
  );
}
