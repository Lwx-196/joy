/**
 * 渲染历史快照 drawer — surfaces the LRU-archived final-board.jpg backups
 * that render_executor writes to <case>/.case-layout-output/<brand>/<template>/render/.history/
 * before each new render.
 *
 * 阶段 12 升级：
 *  - 列表项点击不再 target=_blank，改弹站内 lightbox（带 ←/→ 切张）
 *  - 抽屉内独立 brand 选择器（不影响全局 BrandContext）
 *  - 每条快照尾部「恢复」按钮 + 二次确认 modal，调 useRestoreSnapshot mutation
 *
 * 抽屉的 Esc 处理逻辑：
 *  - lightbox 打开时 → lightbox 自管 Esc + stopPropagation，drawer 不响应
 *  - 确认 modal 打开时 → drawer Esc 关闭 modal（confirm 没有自己的 keydown）
 *  - 都未打开 → drawer Esc 关闭 drawer
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { renderHistorySnapshotUrl, type RenderHistorySnapshot } from "../api";
import { useRenderHistory, useRestoreSnapshot } from "../hooks/queries";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { Ico } from "./atoms";
import { RenderSnapshotLightbox } from "./RenderSnapshotLightbox";

export interface RenderHistoryDrawerProps {
  caseId: number;
  brand: string;
  template: string;
  open: boolean;
  onClose: () => void;
}

const BRAND_OPTIONS = ["fumei", "shimei", "芙美", "莳美"] as const;

/** Map brand value → i18n key for the selector option label. Keeps the
 * non-ASCII entries readable in the UI (English strings annotate Pinyin pair).
 */
const BRAND_OPTION_KEY: Record<string, string> = {
  fumei: "brandOptionFumeiPinyin",
  shimei: "brandOptionShimeiPinyin",
  "芙美": "brandOptionFumeiCN",
  "莳美": "brandOptionShimeiCN",
};

/** Parse "20260429T143022Z" → Date → locale string. Returns the input on
 * failure so callers always get a printable value. */
function parseTs(raw: string): string {
  const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(raw);
  if (!m) return raw;
  const iso = `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}Z`;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return raw;
  return d.toLocaleString("zh-CN", { hour12: false });
}

function errorStatus(err: unknown): number | null {
  if (err && typeof err === "object" && "response" in err) {
    const r = (err as { response?: { status?: number } }).response;
    return r?.status ?? null;
  }
  return null;
}

function errorMessage(err: unknown): string {
  if (err && typeof err === "object") {
    const r = (err as { response?: { data?: { detail?: string } }; message?: string });
    if (r.response?.data?.detail) return String(r.response.data.detail);
    if (r.message) return String(r.message);
  }
  return String(err);
}

export function RenderHistoryDrawer({
  caseId,
  brand,
  template,
  open,
  onClose,
}: RenderHistoryDrawerProps) {
  const { t } = useTranslation("renderHistory");
  const [localBrand, setLocalBrand] = useState(brand);
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);
  const [restoreTarget, setRestoreTarget] = useState<RenderHistorySnapshot | null>(null);
  const restoreMut = useRestoreSnapshot();
  const q = useRenderHistory(caseId, localBrand, template, open);
  const drawerRef = useFocusTrap<HTMLDivElement>(open && lightboxIndex === null && !restoreTarget);
  const confirmRef = useFocusTrap<HTMLDivElement>(open && !!restoreTarget);

  // Reset transient state every time the drawer reopens or the parent's brand
  // prop changes. localBrand resets to the parent's choice — drawer is a
  // viewer, not a brand-context editor.
  useEffect(() => {
    if (open) {
      setLocalBrand(brand);
      setLightboxIndex(null);
      setRestoreTarget(null);
      restoreMut.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentional: do
    // not reset state when restoreMut identity flips between renders.
  }, [open, brand]);

  // ESC handler. Lightbox handles its own Esc with stopPropagation, so when
  // it's open, this listener never sees the event. When the confirm modal is
  // open, Esc closes it. Otherwise Esc closes the drawer itself.
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (lightboxIndex !== null) return; // lightbox handles & stops propagation
      if (restoreTarget) {
        e.preventDefault();
        setRestoreTarget(null);
        return;
      }
      onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onClose, lightboxIndex, restoreTarget]);

  if (!open) return null;

  const snapshots = q.data?.snapshots ?? [];
  const status = errorStatus(q.error);

  const handleConfirm = () => {
    if (!restoreTarget) return;
    restoreMut.mutate(
      {
        caseId,
        brand: localBrand,
        template,
        archivedAt: restoreTarget.archived_at,
      },
      {
        onSuccess: () => {
          // Close confirm; list will refresh via invalidation. Keep drawer open
          // so user sees the new auto-archived row in the list.
          setRestoreTarget(null);
        },
        // onError: keep modal open, show inline message via restoreMut.error
      },
    );
  };

  return (
    <>
      <div
        onClick={onClose}
        aria-hidden
        style={{
          position: "fixed",
          inset: 0,
          background: "rgba(28, 25, 23, 0.32)",
          zIndex: 900,
        }}
      />
      <div
        ref={drawerRef}
        role="dialog"
        aria-modal="true"
        aria-label={t("title")}
        data-testid="render-history-drawer"
        style={{
          position: "fixed",
          top: 0,
          right: 0,
          bottom: 0,
          width: 420,
          maxWidth: "92vw",
          background: "var(--panel)",
          boxShadow: "var(--shadow-pop)",
          zIndex: 901,
          display: "flex",
          flexDirection: "column",
          borderLeft: "1px solid var(--line)",
        }}
      >
        <header
          style={{
            padding: "14px 18px",
            borderBottom: "1px solid var(--line)",
            display: "flex",
            flexDirection: "column",
            gap: 8,
            flexShrink: 0,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 14, fontWeight: 600 }}>{t("title")}</div>
              <div style={{ fontSize: 11.5, color: "var(--ink-3)", marginTop: 2 }}>
                {t("subtitle", { brand: localBrand, template })}
              </div>
            </div>
            <button
              type="button"
              onClick={onClose}
              className="btn sm ghost"
              aria-label={t("close")}
              title={t("closeTitle")}
              style={{ padding: 6 }}
            >
              <Ico name="x" size={12} />
            </button>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <label
              htmlFor="render-history-brand-select"
              style={{ fontSize: 11, color: "var(--ink-3)", flexShrink: 0 }}
            >
              {t("brandSelectorLabel")}
            </label>
            <select
              id="render-history-brand-select"
              data-testid="render-history-brand-select"
              value={localBrand}
              onChange={(e) => setLocalBrand(e.target.value)}
              style={{
                flex: 1,
                fontSize: 12,
                padding: "4px 8px",
                border: "1px solid var(--line)",
                borderRadius: 4,
                background: "var(--panel)",
                color: "var(--ink-1)",
              }}
            >
              {BRAND_OPTIONS.map((b) => {
                const key = BRAND_OPTION_KEY[b] ?? "brandOptionFumeiPinyin";
                return (
                  <option key={b} value={b}>
                    {t(key as never)}
                  </option>
                );
              })}
            </select>
          </div>
        </header>

        {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable region must be focusable for keyboard access (WCAG 2.1.1 / axe scrollable-region-focusable) */}
        <div style={{ flex: 1, overflowY: "auto" }} tabIndex={0}>
          {q.isLoading ? (
            <div style={{ padding: 20, fontSize: 12, color: "var(--ink-3)" }}>
              {t("loading")}
            </div>
          ) : q.isError ? (
            <div style={{ padding: 20, fontSize: 12, color: "var(--err)" }}>
              {status === 404 ? t("notFound") : t("loadError")}
            </div>
          ) : snapshots.length === 0 ? (
            <div style={{ padding: 20, fontSize: 12, color: "var(--ink-3)" }}>
              {t("empty")}
            </div>
          ) : (
            snapshots.map((s, i) => {
              const url = renderHistorySnapshotUrl(caseId, localBrand, template, s.filename);
              const ts = parseTs(s.archived_at);
              return (
                <div
                  key={s.filename}
                  data-testid="render-history-item"
                  data-index={i}
                  style={{
                    display: "flex",
                    gap: 8,
                    padding: "12px 16px",
                    borderBottom: "1px solid var(--line-2)",
                    alignItems: "stretch",
                  }}
                >
                  <button
                    type="button"
                    onClick={() => setLightboxIndex(i)}
                    title={t("openInLightbox")}
                    style={{
                      flex: 1,
                      display: "flex",
                      gap: 12,
                      padding: 0,
                      background: "transparent",
                      border: "none",
                      textAlign: "left",
                      cursor: "pointer",
                      color: "inherit",
                      minWidth: 0,
                    }}
                  >
                    <img
                      src={url}
                      alt={t("snapshotAlt", { ts })}
                      loading="lazy"
                      style={{
                        width: 120,
                        height: 80,
                        objectFit: "cover",
                        border: "1px solid var(--line)",
                        borderRadius: 4,
                        flexShrink: 0,
                        background: "var(--bg-2)",
                      }}
                    />
                    <div style={{ minWidth: 0, fontSize: 12 }}>
                      <div style={{ color: "var(--ink-1)", fontWeight: 500 }}>{ts}</div>
                      <div style={{ color: "var(--ink-3)", marginTop: 2 }}>
                        {t("sizeKb", { kb: Math.round(s.size_bytes / 1024) })}
                      </div>
                      <div
                        style={{
                          color: "var(--ink-3)",
                          fontFamily: "var(--mono)",
                          marginTop: 2,
                          fontSize: 11,
                          wordBreak: "break-all",
                        }}
                      >
                        {s.filename}
                      </div>
                    </div>
                  </button>
                  <button
                    type="button"
                    onClick={() => setRestoreTarget(s)}
                    className="btn xs ghost"
                    data-testid="render-history-restore"
                    title={t("restoreTitle")}
                    style={{
                      flexShrink: 0,
                      alignSelf: "flex-start",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {t("restoreLabel")}
                  </button>
                </div>
              );
            })
          )}
        </div>
      </div>

      {/* Restore confirm modal — sits between drawer (901) and lightbox (1200). */}
      {restoreTarget && (
        <>
          <div
            onClick={() => !restoreMut.isPending && setRestoreTarget(null)}
            aria-hidden
            style={{
              position: "fixed",
              inset: 0,
              background: "rgba(0, 0, 0, 0.45)",
              zIndex: 1000,
            }}
          />
          <div
            ref={confirmRef}
            role="dialog"
            aria-modal="true"
            aria-label={t("restoreConfirmTitle")}
            data-testid="render-restore-confirm"
            style={{
              position: "fixed",
              top: "50%",
              left: "50%",
              transform: "translate(-50%, -50%)",
              width: "min(440px, 92vw)",
              background: "var(--panel)",
              border: "1px solid var(--line)",
              borderRadius: 8,
              boxShadow: "var(--shadow-pop)",
              zIndex: 1001,
              padding: 20,
              display: "flex",
              flexDirection: "column",
              gap: 12,
            }}
          >
            <div style={{ fontSize: 14, fontWeight: 600 }}>{t("restoreConfirmTitle")}</div>
            <div style={{ fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.55 }}>
              {t("restoreConfirmBody", { ts: parseTs(restoreTarget.archived_at) })}
            </div>
            {restoreMut.isError && (
              <div
                role="alert"
                style={{ fontSize: 12, color: "var(--err)" }}
                data-testid="render-restore-error"
              >
                {t("restoreErrorInline", { message: errorMessage(restoreMut.error) })}
              </div>
            )}
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 4 }}>
              <button
                type="button"
                className="btn sm ghost"
                onClick={() => setRestoreTarget(null)}
                disabled={restoreMut.isPending}
                data-testid="render-restore-cancel"
              >
                {t("restoreConfirmCancel")}
              </button>
              <button
                type="button"
                className="btn sm"
                onClick={handleConfirm}
                disabled={restoreMut.isPending}
                data-testid="render-restore-confirm-ok"
                data-autofocus
              >
                {restoreMut.isPending ? t("restoreInProgress") : t("restoreConfirmOk")}
              </button>
            </div>
          </div>
        </>
      )}

      {lightboxIndex !== null && (
        <RenderSnapshotLightbox
          open={lightboxIndex !== null}
          initialIndex={lightboxIndex}
          onClose={() => setLightboxIndex(null)}
          caseId={caseId}
          brand={localBrand}
          template={template}
          snapshots={snapshots}
        />
      )}
    </>
  );
}
