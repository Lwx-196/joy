/**
 * 渲染快照 lightbox — 全屏预览 .history/<ts>.jpg。
 *
 * 设计：
 *  - 不自管 fetch — snapshots 由 RenderHistoryDrawer 通过 prop 传入。这样切换
 *    image 是 0 网络代价（同一份 useRenderHistory 缓存）。
 *  - 黑遮罩 zIndex 1200，高于 drawer (901) 和 HotkeyHelp (~1101)，确保覆盖所有
 *    其他 modal/drawer。
 *  - ←/→ 在快照间切换；Esc 关闭；点遮罩关闭。
 *  - useFocusTrap 复用；data-autofocus 放在 close 按钮（避免 ←/→ 焦点漂到
 *    prev/next 按钮然后 Enter 误关）。
 *  - 切张时 setLoaded(false) 显示 skeleton，避免上一张被新一张闪现替换。
 *
 * 依赖：
 *  - api.ts: RenderHistorySnapshot type, renderHistorySnapshotUrl
 *  - useFocusTrap, react-i18next (renderHistory namespace)
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { renderHistorySnapshotUrl, type RenderHistorySnapshot } from "../api";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { Ico } from "./atoms";

export interface RenderSnapshotLightboxProps {
  open: boolean;
  onClose: () => void;
  caseId: number;
  brand: string;
  template: string;
  snapshots: RenderHistorySnapshot[];
  initialIndex: number;
}

function parseTs(raw: string): string {
  const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(raw);
  if (!m) return raw;
  const iso = `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}Z`;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return raw;
  return d.toLocaleString("zh-CN", { hour12: false });
}

export function RenderSnapshotLightbox({
  open,
  onClose,
  caseId,
  brand,
  template,
  snapshots,
  initialIndex,
}: RenderSnapshotLightboxProps) {
  const { t } = useTranslation("renderHistory");
  const [index, setIndex] = useState(initialIndex);
  const [loaded, setLoaded] = useState(false);
  const containerRef = useFocusTrap<HTMLDivElement>(open);

  // Resync index when reopening (drawer may pass a different starting snapshot
  // than the previous open's leftover state).
  useEffect(() => {
    if (open) {
      setIndex(initialIndex);
      setLoaded(false);
    }
  }, [open, initialIndex]);

  // Keyboard nav. Listens on `document` to match useHotkey conventions (lesson
  // #11.3) — capture-phase guards aren't needed because no parent intercepts
  // these keys while open. setLoaded(false) lives here (not a separate effect)
  // so it's a state batch with setIndex — avoids a `set-state-in-effect` warning.
  useEffect(() => {
    if (!open) return;
    const goPrev = () => {
      setIndex((i) => Math.max(0, i - 1));
      setLoaded(false);
    };
    const goNext = () => {
      setIndex((i) => Math.min(snapshots.length - 1, i + 1));
      setLoaded(false);
    };
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        // stopPropagation prevents the parent drawer's window-level Esc from
        // also firing and closing the drawer behind us.
        e.preventDefault();
        e.stopPropagation();
        onClose();
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        e.stopPropagation();
        goPrev();
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        e.stopPropagation();
        goNext();
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [open, onClose, snapshots.length]);

  if (!open || snapshots.length === 0) return null;

  // Clamp defensively in case the drawer mutates snapshots while open.
  const safeIndex = Math.min(Math.max(index, 0), snapshots.length - 1);
  const current = snapshots[safeIndex];
  const url = renderHistorySnapshotUrl(caseId, brand, template, current.filename);
  const ts = parseTs(current.archived_at);
  const atFirst = safeIndex === 0;
  const atLast = safeIndex === snapshots.length - 1;

  return (
    <>
      <div
        onClick={onClose}
        aria-hidden
        style={{
          position: "fixed",
          inset: 0,
          background: "rgba(0, 0, 0, 0.85)",
          zIndex: 1200,
        }}
      />
      <div
        ref={containerRef}
        role="dialog"
        aria-modal="true"
        aria-label={t("lightboxTitle")}
        data-testid="lightbox"
        data-current-index={safeIndex}
        style={{
          position: "fixed",
          top: "50%",
          left: "50%",
          transform: "translate(-50%, -50%)",
          width: "min(90vw, 1600px)",
          maxHeight: "90vh",
          zIndex: 1201,
          display: "flex",
          flexDirection: "column",
          gap: 8,
          color: "#fff",
        }}
      >
        {/* Top chrome: ts + index + close */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
            fontSize: 12.5,
            padding: "0 4px",
          }}
        >
          <div style={{ minWidth: 0, display: "flex", alignItems: "baseline", gap: 12 }}>
            <span style={{ fontWeight: 600, color: "#fff" }}>{ts}</span>
            <span style={{ opacity: 0.7, fontFamily: "var(--mono)", fontSize: 11 }}>
              {t("lightboxIndex", { current: safeIndex + 1, total: snapshots.length })}
            </span>
          </div>
          <button
            type="button"
            onClick={onClose}
            data-testid="lightbox-close"
            data-autofocus
            aria-label={t("lightboxClose")}
            title={t("lightboxClose")}
            style={{
              background: "rgba(255,255,255,0.12)",
              border: "1px solid rgba(255,255,255,0.25)",
              color: "#fff",
              padding: "6px 10px",
              borderRadius: 6,
              cursor: "pointer",
              fontSize: 12,
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
            }}
          >
            <Ico name="x" size={12} />
          </button>
        </div>

        {/* Image area (with prev/next buttons positioned over it) */}
        <div
          style={{
            position: "relative",
            flex: 1,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            minHeight: 0,
            background: "rgba(0,0,0,0.4)",
            borderRadius: 8,
          }}
        >
          {!loaded && (
            <div
              style={{
                position: "absolute",
                inset: 0,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                color: "rgba(255,255,255,0.7)",
                fontSize: 12,
              }}
            >
              {t("lightboxLoading")}
            </div>
          )}
          <img
            key={current.filename}
            src={url}
            alt={t("lightboxImageAlt", { ts })}
            data-testid="lightbox-image"
            data-snapshot-filename={current.filename}
            onLoad={() => setLoaded(true)}
            style={{
              maxWidth: "100%",
              maxHeight: "85vh",
              objectFit: "contain",
              opacity: loaded ? 1 : 0,
              transition: "opacity 120ms",
              userSelect: "none",
            }}
          />

          <button
            type="button"
            onClick={() => {
              setIndex((i) => Math.max(0, i - 1));
              setLoaded(false);
            }}
            disabled={atFirst}
            data-testid="lightbox-prev"
            aria-label={t("lightboxPrev")}
            title={t("lightboxPrev")}
            style={{
              position: "absolute",
              left: 12,
              top: "50%",
              transform: "translateY(-50%)",
              width: 40,
              height: 40,
              borderRadius: "50%",
              border: "1px solid rgba(255,255,255,0.25)",
              background: "rgba(0,0,0,0.5)",
              color: "#fff",
              cursor: atFirst ? "not-allowed" : "pointer",
              opacity: atFirst ? 0.35 : 1,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 18,
              fontWeight: 600,
              lineHeight: 1,
            }}
          >
            ‹
          </button>
          <button
            type="button"
            onClick={() => {
              setIndex((i) => Math.min(snapshots.length - 1, i + 1));
              setLoaded(false);
            }}
            disabled={atLast}
            data-testid="lightbox-next"
            aria-label={t("lightboxNext")}
            title={t("lightboxNext")}
            style={{
              position: "absolute",
              right: 12,
              top: "50%",
              transform: "translateY(-50%)",
              width: 40,
              height: 40,
              borderRadius: "50%",
              border: "1px solid rgba(255,255,255,0.25)",
              background: "rgba(0,0,0,0.5)",
              color: "#fff",
              cursor: atLast ? "not-allowed" : "pointer",
              opacity: atLast ? 0.35 : 1,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 18,
              fontWeight: 600,
              lineHeight: 1,
            }}
          >
            ›
          </button>
        </div>
      </div>
    </>
  );
}
