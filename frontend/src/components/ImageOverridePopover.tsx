/**
 * Stage B: 单张源图 phase / view 手动覆盖弹窗。
 *
 * - 渲染为绝对定位的浮层,定位在 anchor 元素的下方对齐(用 anchor.getBoundingClientRect)
 * - phase / view 各一组按钮:点击 = 切换该值;再点同值 = 清除(传空字符串)
 * - 「清除全部」按钮 = 同时清 phase + view (后端会删除整行)
 * - 点击外部 / Esc 关闭;阻止冒泡到 RenderSnapshotLightbox 等
 *
 * 不依赖任何 popover 库。close() 通过 useEffect 监听 document mousedown / keydown。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type {
  ImageOverridePhase,
  ImageOverrideView,
  SkillImageMetadata,
} from "../api";
import { useUpdateImageOverride } from "../hooks/queries";
import { Ico } from "./atoms";

interface Props {
  caseId: number;
  filename: string;
  meta: SkillImageMetadata | undefined;
  /** Anchor element — popover positions itself below it. */
  anchorEl: HTMLElement | null;
  onClose(): void;
}

type PhaseValue = NonNullable<ImageOverridePhase>;
type ViewValue = NonNullable<ImageOverrideView>;

export function ImageOverridePopover({ caseId, filename, meta, anchorEl, onClose }: Props) {
  const { t } = useTranslation("caseDetail");
  const popRef = useRef<HTMLDivElement>(null);
  const mut = useUpdateImageOverride();
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);

  // Compute position once anchor is mounted; recompute on resize/scroll.
  // Auto-flip: if popover would overflow the viewport bottom, render above anchor.
  // After mounting, measure the popover and adjust if needed.
  useEffect(() => {
    if (!anchorEl) return;
    const place = () => {
      const r = anchorEl.getBoundingClientRect();
      const popH = popRef.current?.offsetHeight ?? 240;
      const popW = popRef.current?.offsetWidth ?? 240;
      const margin = 8;
      let top = r.bottom + 4;
      if (top + popH + margin > window.innerHeight) {
        top = Math.max(margin, r.top - popH - 4);
      }
      let left = r.left;
      if (left + popW + margin > window.innerWidth) {
        left = Math.max(margin, window.innerWidth - popW - margin);
      }
      setPos({ top, left });
    };
    place();
    // Re-place after mount (popover height now measurable)
    const raf = window.requestAnimationFrame(place);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.cancelAnimationFrame(raf);
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [anchorEl]);

  // Click-outside + Esc to close.
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (popRef.current && popRef.current.contains(e.target as Node)) return;
      if (anchorEl && anchorEl.contains(e.target as Node)) return;
      onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [anchorEl, onClose]);

  const apply = useCallback(
    (patch: { manual_phase?: string; manual_view?: string }) => {
      mut.mutate({ caseId, filename, payload: patch });
    },
    [caseId, filename, mut],
  );

  const onPhaseClick = (v: NonNullable<ImageOverridePhase>) => {
    // Toggle: if currently == v (manual override), clear it
    const isCurrent = (meta?.phase_override_source === "manual") && meta?.phase === v;
    apply({ manual_phase: isCurrent ? "" : v });
  };
  const onViewClick = (v: NonNullable<ImageOverrideView>) => {
    const isCurrent =
      (meta?.view_override_source === "manual") &&
      (meta?.view_bucket === v || meta?.angle === v);
    apply({ manual_view: isCurrent ? "" : v });
  };
  const onClearAll = () => apply({ manual_phase: "", manual_view: "" });

  if (!pos) return null;

  const hasManual =
    meta?.phase_override_source === "manual" || meta?.view_override_source === "manual";

  return (
    <div
      ref={popRef}
      role="dialog"
      aria-label={t("imageOverride.title")}
      data-testid="image-override-popover"
      onClick={(e) => e.stopPropagation()}
      style={{
        position: "fixed",
        top: pos.top,
        left: pos.left,
        zIndex: 1000,
        background: "var(--panel)",
        border: "1px solid var(--line)",
        borderRadius: 6,
        boxShadow: "0 6px 20px rgba(0,0,0,.12)",
        padding: 10,
        minWidth: 220,
        fontSize: 12,
      }}
    >
      <div style={{ fontWeight: 600, marginBottom: 6, color: "var(--ink-1)", fontSize: 11.5 }}>
        {t("imageOverride.title")}
        <div style={{ fontFamily: "var(--mono)", fontWeight: 400, fontSize: 10.5, color: "var(--ink-3)", marginTop: 2 }}>
          {filename}
        </div>
      </div>

      <div style={{ marginBottom: 6 }}>
        <div style={{ fontSize: 10.5, color: "var(--ink-3)", marginBottom: 3 }}>
          {t("imageOverride.phaseLabel")}
        </div>
        <div style={{ display: "flex", gap: 4 }}>
          {(["before", "after"] as PhaseValue[]).map((v) => {
            const active = meta?.phase === v && meta?.phase_override_source === "manual";
            const label = v === "before" ? t("imageOverride.phaseBefore") : t("imageOverride.phaseAfter");
            return (
              <button
                key={v}
                type="button"
                className={`btn sm ${active ? "primary" : "ghost"}`}
                onClick={() => onPhaseClick(v)}
                disabled={mut.isPending}
                data-testid={`override-phase-${v}`}
                data-active={active ? "1" : "0"}
              >
                {label}
              </button>
            );
          })}
        </div>
      </div>

      <div style={{ marginBottom: 8 }}>
        <div style={{ fontSize: 10.5, color: "var(--ink-3)", marginBottom: 3 }}>
          {t("imageOverride.viewLabel")}
        </div>
        <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
          {(["front", "oblique", "side"] as ViewValue[]).map((v) => {
            const active =
              (meta?.view_bucket === v || meta?.angle === v) &&
              meta?.view_override_source === "manual";
            const label =
              v === "front"
                ? t("imageOverride.viewFront")
                : v === "oblique"
                  ? t("imageOverride.viewOblique")
                  : t("imageOverride.viewSide");
            return (
              <button
                key={v}
                type="button"
                className={`btn sm ${active ? "primary" : "ghost"}`}
                onClick={() => onViewClick(v)}
                disabled={mut.isPending}
                data-testid={`override-view-${v}`}
                data-active={active ? "1" : "0"}
              >
                {label}
              </button>
            );
          })}
        </div>
      </div>

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", borderTop: "1px solid var(--line-2)", paddingTop: 6 }}>
        <button
          type="button"
          className="btn sm ghost"
          onClick={onClearAll}
          disabled={mut.isPending || !hasManual}
          data-testid="override-clear-all"
          title={t("imageOverride.clearTitle")}
        >
          <Ico name="x" size={11} />
          {t("imageOverride.clearAll")}
        </button>
        <button
          type="button"
          className="btn sm ghost"
          onClick={onClose}
          data-testid="override-close"
        >
          {t("imageOverride.close")}
        </button>
      </div>

      {mut.isError && (
        <div
          style={{ marginTop: 6, fontSize: 10.5, color: "var(--err)" }}
          data-testid="override-error"
        >
          {t("imageOverride.error")}: {(mut.error as Error)?.message ?? ""}
        </div>
      )}
    </div>
  );
}
