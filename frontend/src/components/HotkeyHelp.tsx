/**
 * HotkeyHelp — modal listing global keyboard shortcuts.
 *
 * Triggered by pressing `?` anywhere outside an editable element. Closes on
 * Esc or click outside. Uses the same focus-trap + a11y pattern as the other
 * modals in this app.
 *
 * The list is hardcoded here rather than collected from a registry — Layout
 * declares the actual bindings, so any drift surfaces during code review of
 * either file.
 */
import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { Ico } from "./atoms";

interface Entry {
  combo: string;
  i18nKey: string;
  scope: "navigation" | "global" | "cases" | "caseDetail";
}

const ENTRIES: Entry[] = [
  { combo: "?", i18nKey: "showHelp", scope: "global" },
  { combo: "Esc", i18nKey: "closeOverlay", scope: "global" },
  { combo: "⌘K", i18nKey: "focusSearch", scope: "global" },
  { combo: "g d", i18nKey: "gotoDashboard", scope: "navigation" },
  { combo: "g c", i18nKey: "gotoCases", scope: "navigation" },
  { combo: "g u", i18nKey: "gotoCustomers", scope: "navigation" },
  { combo: "g e", i18nKey: "gotoEvaluations", scope: "navigation" },
  { combo: "g v", i18nKey: "gotoDict", scope: "navigation" },
  { combo: "j", i18nKey: "casesNext", scope: "cases" },
  { combo: "k", i18nKey: "casesPrev", scope: "cases" },
  { combo: "Home", i18nKey: "casesFirst", scope: "cases" },
  { combo: "End / G", i18nKey: "casesLast", scope: "cases" },
  { combo: "Enter", i18nKey: "casesOpen", scope: "cases" },
  { combo: "x", i18nKey: "casesToggle", scope: "cases" },
  { combo: "h", i18nKey: "caseDetailHistory", scope: "caseDetail" },
];

export interface HotkeyHelpProps {
  open: boolean;
  onClose: () => void;
}

export function HotkeyHelp({ open, onClose }: HotkeyHelpProps) {
  const { t } = useTranslation("hotkeys");
  const dialogRef = useFocusTrap<HTMLDivElement>(open);

  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onClose]);

  if (!open) return null;

  const grouped: Record<string, Entry[]> = { navigation: [], global: [], cases: [], caseDetail: [] };
  for (const e of ENTRIES) grouped[e.scope].push(e);

  return (
    <>
      <div
        onClick={onClose}
        aria-hidden
        style={{
          position: "fixed",
          inset: 0,
          background: "rgba(28, 25, 23, 0.32)",
          zIndex: 1100,
        }}
      />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="hotkey-help-title"
        style={{
          position: "fixed",
          top: "50%",
          left: "50%",
          transform: "translate(-50%, -50%)",
          width: 480,
          maxWidth: "92vw",
          background: "var(--panel)",
          borderRadius: 12,
          boxShadow: "var(--shadow-pop)",
          zIndex: 1101,
          padding: 22,
          display: "flex",
          flexDirection: "column",
          gap: 14,
        }}
      >
        <header style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <div id="hotkey-help-title" style={{ fontSize: 14, fontWeight: 600 }}>
            {t("title")}
          </div>
          <button
            type="button"
            className="btn sm ghost"
            onClick={onClose}
            aria-label={t("close")}
            title={t("closeHint")}
            style={{ padding: 6 }}
          >
            <Ico name="x" size={12} />
          </button>
        </header>

        {(["global", "navigation", "cases", "caseDetail"] as const).map((scope) => (
          <section key={scope} aria-labelledby={`hk-${scope}`}>
            <div
              id={`hk-${scope}`}
              style={{ fontSize: 11.5, color: "var(--ink-3)", marginBottom: 6, fontWeight: 500 }}
            >
              {t(`scope.${scope}` as never)}
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", rowGap: 6, columnGap: 14 }}>
              {grouped[scope].map((e) => (
                <div key={e.combo} style={{ display: "contents" }}>
                  <kbd
                    style={{
                      fontFamily: "var(--mono)",
                      fontSize: 11.5,
                      background: "var(--bg-2)",
                      color: "var(--ink-1)",
                      border: "1px solid var(--line)",
                      borderRadius: 4,
                      padding: "1px 6px",
                      whiteSpace: "nowrap",
                      justifySelf: "start",
                    }}
                  >
                    {e.combo}
                  </kbd>
                  <div style={{ fontSize: 12, color: "var(--ink-2)" }}>
                    {t(`labels.${e.i18nKey}` as never)}
                  </div>
                </div>
              ))}
            </div>
          </section>
        ))}
      </div>
    </>
  );
}
