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
import { useCallback, useEffect, useRef, useState } from "react";
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

// Zoom limits. 1.0 = fit-to-viewport (objectFit:contain baseline).
const ZOOM_MIN = 1;
const ZOOM_MAX = 5;
const ZOOM_STEP = 1.25; // multiplicative per +/- key press
const WHEEL_ZOOM_INTENSITY = 0.0015; // delta per wheel tick (smooth)

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
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [panning, setPanning] = useState(false);
  const imageAreaRef = useRef<HTMLDivElement>(null);
  // Mirror zoom/pan in a ref so rapid wheel events compute against fresh values
  // without depending on React render commit. Nested setState callbacks are
  // avoided because StrictMode dev-mode double-invokes them, doubling any side
  // effects (here, doubled setPan was making pan accumulate ~4× the correct
  // distance during a 4-event burst).
  const stateRef = useRef({ zoom: 1, pan: { x: 0, y: 0 } });
  const containerRef = useFocusTrap<HTMLDivElement>(open);

  // Resync index when reopening (drawer may pass a different starting snapshot
  // than the previous open's leftover state).
  useEffect(() => {
    if (open) {
      setIndex(initialIndex);
      setLoaded(false);
      stateRef.current = { zoom: 1, pan: { x: 0, y: 0 } };
      setZoom(1);
      setPan({ x: 0, y: 0 });
    }
  }, [open, initialIndex]);

  // Reset zoom + pan when changing snapshots — each image starts fit-to-viewport.
  // (loaded reset is co-batched in the goPrev/goNext handlers below.)

  // Apply a zoom delta keeping (mx, my) — cursor offset from container CENTER —
  // pinned to the same image pixel. With transformOrigin default (50% 50%) and
  // transform `translate(panX, panY) scale(zoom)`, a pixel at element-local
  // offset `d` from element center renders at `d*zoom + pan` from element
  // center. Solving for panX' that keeps the cursor's pixel fixed across zoom:
  //   panX' = mx - (mx - panX) * (newZoom / oldZoom)
  //
  // We accept a *factor* (relative) instead of an absolute newZoom so the
  // listener doesn't need to close over the latest `zoom` state — fresh
  // oldZoom comes from setZoom's callback. Without this, rapid wheel events
  // dispatched before React re-renders would compute newZoom against a stale
  // closure value, accumulating pan ~4× the correct distance.
  const applyZoomBy = useCallback(
    (factor: number, mx: number, my: number) => {
      const { zoom: oldZoom, pan: oldPan } = stateRef.current;
      const clamped = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, oldZoom * factor));
      if (clamped === oldZoom) return;
      const newPan =
        clamped === ZOOM_MIN
          ? { x: 0, y: 0 }
          : {
              x: mx - (mx - oldPan.x) * (clamped / oldZoom),
              y: my - (my - oldPan.y) * (clamped / oldZoom),
            };
      stateRef.current = { zoom: clamped, pan: newPan };
      setZoom(clamped);
      setPan(newPan);
    },
    [],
  );

  const zoomCentered = useCallback(
    (factor: number) => {
      // mx=my=0 → cursor at container center → pan delta is zero → image stays
      // centered (correct UX for keyboard +/- where there is no cursor pos).
      applyZoomBy(factor, 0, 0);
    },
    [applyZoomBy],
  );

  const resetZoom = useCallback(() => {
    stateRef.current = { zoom: 1, pan: { x: 0, y: 0 } };
    setZoom(1);
    setPan({ x: 0, y: 0 });
  }, []);

  // Native wheel listener (React's onWheel is passive — preventDefault is a no-op
  // there, which lets the page itself scroll behind the lightbox).
  useEffect(() => {
    const el = imageAreaRef.current;
    if (!el || !open) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      // Measure cursor relative to container center to match transformOrigin
      // default (50% 50%) used on the image — see applyZoomBy math comment.
      const mx = e.clientX - (rect.left + rect.width / 2);
      const my = e.clientY - (rect.top + rect.height / 2);
      const factor = Math.exp(-e.deltaY * WHEEL_ZOOM_INTENSITY);
      applyZoomBy(factor, mx, my);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [open, applyZoomBy]);

  // Drag-to-pan. Only active when zoomed in. Mouse move/up listeners attach to
  // document so dragging out of the image area still tracks correctly.
  const handleMouseDown = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    if (stateRef.current.zoom <= 1) return;
    // Ignore clicks that originated on the prev/next buttons.
    if ((e.target as HTMLElement).closest("button")) return;
    e.preventDefault();
    setPanning(true);
    const startX = e.clientX;
    const startY = e.clientY;
    const startPan = stateRef.current.pan;
    const onMove = (ev: MouseEvent) => {
      const newPan = {
        x: startPan.x + (ev.clientX - startX),
        y: startPan.y + (ev.clientY - startY),
      };
      stateRef.current = { zoom: stateRef.current.zoom, pan: newPan };
      setPan(newPan);
    };
    const onUp = () => {
      setPanning(false);
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }, []);

  // Keyboard nav. Listens on `document` to match useHotkey conventions (lesson
  // #11.3) — capture-phase guards aren't needed because no parent intercepts
  // these keys while open. setLoaded(false) lives here (not a separate effect)
  // so it's a state batch with setIndex — avoids a `set-state-in-effect` warning.
  useEffect(() => {
    if (!open) return;
    const goPrev = () => {
      setIndex((i) => Math.max(0, i - 1));
      setLoaded(false);
      resetZoom();
    };
    const goNext = () => {
      setIndex((i) => Math.min(snapshots.length - 1, i + 1));
      setLoaded(false);
      resetZoom();
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
      } else if (e.key === "+" || e.key === "=") {
        e.preventDefault();
        e.stopPropagation();
        zoomCentered(ZOOM_STEP);
      } else if (e.key === "-" || e.key === "_") {
        e.preventDefault();
        e.stopPropagation();
        zoomCentered(1 / ZOOM_STEP);
      } else if (e.key === "0") {
        e.preventDefault();
        e.stopPropagation();
        resetZoom();
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [open, onClose, snapshots.length, zoomCentered, resetZoom]);

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
            <span
              data-testid="lightbox-zoom-level"
              style={{ opacity: 0.7, fontFamily: "var(--mono)", fontSize: 11 }}
            >
              {t("lightboxZoomLevel", { percent: Math.round(zoom * 100) })}
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

        {/* Image area (with prev/next buttons positioned over it).
            mousedown drives drag-to-pan when zoomed; keyboard zoom (+/-/0) is
            registered at document level so users without a mouse can still
            operate the viewer. The div is not a control, so there's no
            semantic role to give it. */}
        {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
        <div
          ref={imageAreaRef}
          onMouseDown={handleMouseDown}
          data-testid="lightbox-image-area"
          data-zoom={Math.round(zoom * 100)}
          style={{
            position: "relative",
            flex: 1,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            minHeight: 0,
            background: "rgba(0,0,0,0.4)",
            borderRadius: 8,
            overflow: "hidden",
            cursor: zoom > 1 ? (panning ? "grabbing" : "grab") : "default",
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
            draggable={false}
            style={{
              maxWidth: "100%",
              maxHeight: "85vh",
              objectFit: "contain",
              opacity: loaded ? 1 : 0,
              transition: panning ? "opacity 120ms" : "opacity 120ms, transform 80ms",
              userSelect: "none",
              transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
              willChange: zoom > 1 ? "transform" : undefined,
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
