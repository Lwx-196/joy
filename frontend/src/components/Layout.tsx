import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { BRAND_LABEL, fetchStats, type Brand, type Stats } from "../api";
import { useBrandSelector, VALID_BRANDS } from "../lib/brand-context";
import { Ico } from "./atoms";
import { HotkeyHelp } from "./HotkeyHelp";

/** g-chord pending window in ms. After pressing `g`, any of g/d/c/u/e/v
 *  pressed within this window triggers navigation; otherwise the chord
 *  resets so isolated `g` keystrokes don't accumulate. */
const CHORD_TIMEOUT_MS = 1500;

const CHORD_ROUTES: Record<string, string> = {
  d: "/",
  c: "/cases",
  i: "/images",
  b: "/source-blockers",
  o: "/case-groups",
  u: "/customers",
  e: "/evaluations",
  q: "/quality",
  v: "/dict",
};

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT") return true;
  if (target.isContentEditable) return true;
  return false;
}

export default function Layout() {
  const { t } = useTranslation("common");
  const navigate = useNavigate();
  const [stats, setStats] = useState<Stats | null>(null);
  const [unboundCount, setUnboundCount] = useState<number | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);
  const chordPending = useRef<boolean>(false);
  const chordTimer = useRef<number | null>(null);

  useEffect(() => {
    fetchStats().then(setStats).catch(() => {});
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isEditableTarget(e.target)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;

      // `?` opens help overlay (shift+/ → "?" key).
      if (e.key === "?" && !chordPending.current) {
        e.preventDefault();
        setHelpOpen(true);
        return;
      }

      // Resolve a pending g-chord.
      if (chordPending.current) {
        const route = CHORD_ROUTES[e.key.toLowerCase()];
        chordPending.current = false;
        if (chordTimer.current !== null) {
          window.clearTimeout(chordTimer.current);
          chordTimer.current = null;
        }
        if (route) {
          e.preventDefault();
          navigate(route);
        }
        return;
      }

      // Start a g-chord.
      if (e.key === "g" && !e.shiftKey) {
        chordPending.current = true;
        chordTimer.current = window.setTimeout(() => {
          chordPending.current = false;
          chordTimer.current = null;
        }, CHORD_TIMEOUT_MS);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (chordTimer.current !== null) {
        window.clearTimeout(chordTimer.current);
        chordTimer.current = null;
      }
    };
  }, [navigate]);

  const tabs = useMemo(
    () => [
      { to: "/", label: t("nav.overview"), end: true, ico: "home" as const, key: "overview" },
      { to: "/cases", label: t("nav.cases"), ico: "list" as const, key: "cases" },
      { to: "/images", label: t("nav.images"), ico: "image" as const, key: "images" },
      { to: "/source-blockers", label: t("nav.sourceBlockers"), ico: "alert" as const, key: "sourceBlockers" },
      { to: "/case-groups", label: t("nav.caseGroups"), ico: "split" as const, key: "caseGroups" },
      { to: "/customers", label: t("nav.customers"), ico: "users" as const, key: "customers" },
      { to: "/evaluations", label: t("nav.evaluations"), ico: "check" as const, key: "evaluations" },
      { to: "/quality", label: t("nav.quality"), ico: "eye" as const, key: "quality" },
      { to: "/dict", label: t("nav.dict"), ico: "merge" as const, key: "dict" },
    ],
    [t]
  );

  const counts: Record<string, number | undefined> = {
    cases: stats?.total,
    customers: undefined,
    dict: unboundCount ?? undefined,
  };
  // dict alert if >0 unbound (best-effort, no extra API call here)
  void setUnboundCount;

  return (
    <div className="app-shell">
      <a href="#main-content" className="skip-link">{t("skipLink")}</a>
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">案</div>
          <span>{t("brand.title")}</span>
          <span className="brand-meta">{t("brand.phase")}</span>
        </div>
        <nav className="nav-tabs">
          {tabs.map((tab) => (
            <NavLink
              key={tab.key}
              to={tab.to}
              end={tab.end}
              className={({ isActive }) => "tab" + (isActive ? " active" : "")}
            >
              <Ico name={tab.ico} size={13} />
              <span>{tab.label}</span>
              {typeof counts[tab.key] === "number" && (
                <span className="count">{counts[tab.key]}</span>
              )}
            </NavLink>
          ))}
        </nav>
        <div className="top-right" style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <LanguageSelector />
          <BrandSelector />
          <div className="search">
            <Ico name="search" />
            <input aria-label={t("search.aria")} placeholder={t("search.placeholder")} />
            <span
              style={{
                position: "absolute",
                right: 8,
                top: "50%",
                transform: "translateY(-50%)",
                fontFamily: "var(--mono)",
                fontSize: 10.5,
                color: "var(--ink-4)",
                background: "var(--bg-2)",
                padding: "1px 5px",
                borderRadius: 4,
                border: "1px solid var(--line)",
                pointerEvents: "none",
              }}
            >
              ⌘K
            </span>
          </div>
        </div>
      </header>
      <main className="app-main" id="main-content" tabIndex={-1}>
        <Suspense fallback={<RouteFallback />}>
          <Outlet />
        </Suspense>
      </main>
      <HotkeyHelp open={helpOpen} onClose={() => setHelpOpen(false)} />
    </div>
  );
}

/** Topbar language selector — switches i18n language; persists to localStorage via detector. */
function LanguageSelector() {
  const { i18n } = useTranslation();
  const current = (i18n.resolvedLanguage || i18n.language || "zh").startsWith("en") ? "en" : "zh";
  return (
    <select
      value={current}
      onChange={(e) => {
        void i18n.changeLanguage(e.target.value);
      }}
      aria-label="Language / 语言"
      style={{
        background: "var(--bg-2)",
        border: "1px solid var(--line)",
        borderRadius: 6,
        padding: "4px 8px",
        fontSize: 12,
        color: "var(--ink-2)",
        fontFamily: "inherit",
        cursor: "pointer",
      }}
    >
      <option value="zh">中文</option>
      <option value="en">EN</option>
    </select>
  );
}

/** Topbar brand selector — drives all render/upgrade mutations globally. */
function BrandSelector() {
  const { t } = useTranslation("common");
  const { brand, setBrand } = useBrandSelector();
  return (
    <label
      style={{
        display: "flex",
        alignItems: "center",
        gap: 6,
        fontSize: 12,
        color: "var(--ink-3)",
        background: "var(--bg-2)",
        border: "1px solid var(--line)",
        borderRadius: 6,
        padding: "4px 8px",
      }}
      title={t("brand.selectorTitle")}
    >
      <Ico name="tag" size={12} />
      <span>{t("brand.selectorLabel")}</span>
      <select
        value={brand}
        onChange={(e) => setBrand(e.target.value as Brand)}
        style={{
          background: "transparent",
          border: 0,
          fontSize: 12,
          color: "var(--ink-1)",
          fontFamily: "inherit",
          cursor: "pointer",
          padding: 0,
        }}
      >
        {VALID_BRANDS.map((b) => (
          <option key={b} value={b}>
            {BRAND_LABEL[b]}
          </option>
        ))}
      </select>
    </label>
  );
}

/** Suspense fallback for lazy-loaded route chunks. Centered, min 60vh so topbar
 * doesn't jump while a route bundle downloads. */
function RouteFallback() {
  const { t } = useTranslation("common");
  return (
    <div role="status" aria-live="polite" className="route-fallback">
      {t("common.loading")}
    </div>
  );
}
