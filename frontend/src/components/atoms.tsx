import { CSSProperties, ReactNode } from "react";
import { CATEGORY_LABEL, TIER_LABEL, type Category, type ReviewStatus } from "../api";

/* ===== Icons ===== */
type IcoName =
  | "search" | "scan" | "refresh" | "plus" | "check" | "x" | "edit" | "recheck"
  | "alert" | "copy" | "link" | "user" | "users" | "folder" | "image" | "dot"
  | "arrow" | "arrow-r" | "down" | "filter" | "tag" | "list" | "merge" | "eye"
  | "flag" | "home" | "database" | "book" | "split";

interface IcoProps {
  name: IcoName;
  size?: number;
  stroke?: number;
  style?: CSSProperties;
  className?: string;
}

export function Ico({ name, size = 14, stroke = 1.6, style, className }: IcoProps) {
  const common = {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none" as const,
    stroke: "currentColor",
    strokeWidth: stroke,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    style,
    className,
  };
  switch (name) {
    case "search":   return (<svg {...common}><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>);
    case "scan":     return (<svg {...common}><path d="M3 7V5a2 2 0 0 1 2-2h2M21 7V5a2 2 0 0 0-2-2h-2M3 17v2a2 2 0 0 0 2 2h2M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M3 12h18"/></svg>);
    case "refresh":  return (<svg {...common}><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v5h-5"/></svg>);
    case "plus":     return (<svg {...common}><path d="M12 5v14M5 12h14"/></svg>);
    case "check":    return (<svg {...common}><path d="m4 12 5 5L20 6"/></svg>);
    case "x":        return (<svg {...common}><path d="M6 6l12 12M18 6 6 18"/></svg>);
    case "edit":     return (<svg {...common}><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 1 1 3 3L7 19l-4 1 1-4Z"/></svg>);
    case "recheck":  return (<svg {...common}><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>);
    case "alert":    return (<svg {...common}><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/></svg>);
    case "copy":     return (<svg {...common}><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>);
    case "link":     return (<svg {...common}><path d="M10 13a5 5 0 0 0 7.07 0l3-3a5 5 0 0 0-7.07-7.07l-1.5 1.5"/><path d="M14 11a5 5 0 0 0-7.07 0l-3 3a5 5 0 0 0 7.07 7.07l1.5-1.5"/></svg>);
    case "user":     return (<svg {...common}><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>);
    case "users":    return (<svg {...common}><circle cx="9" cy="8" r="4"/><path d="M2 21a7 7 0 0 1 14 0"/><path d="M22 21a6 6 0 0 0-6-6"/><circle cx="17" cy="7" r="3"/></svg>);
    case "folder":   return (<svg {...common}><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>);
    case "image":    return (<svg {...common}><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/></svg>);
    case "dot":      return (<svg {...common}><circle cx="12" cy="12" r="4"/></svg>);
    case "arrow":    return (<svg {...common}><path d="M5 12h14"/><path d="m13 6 6 6-6 6"/></svg>);
    case "arrow-r":  return (<svg {...common}><path d="m9 18 6-6-6-6"/></svg>);
    case "down":     return (<svg {...common}><path d="m6 9 6 6 6-6"/></svg>);
    case "filter":   return (<svg {...common}><path d="M3 5h18l-7 9v6l-4-2v-4z"/></svg>);
    case "tag":      return (<svg {...common}><path d="M20.6 13.4 12 22l-9-9V3h10z"/><circle cx="7.5" cy="7.5" r="1.5"/></svg>);
    case "list":     return (<svg {...common}><path d="M8 6h13M8 12h13M8 18h13"/><path d="M3 6h.01M3 12h.01M3 18h.01"/></svg>);
    case "merge":    return (<svg {...common}><path d="M6 3v6a4 4 0 0 0 4 4h4a4 4 0 0 1 4 4v4"/><path d="M3 6l3-3 3 3"/><path d="M15 21l3-3-3-3"/></svg>);
    case "eye":      return (<svg {...common}><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>);
    case "flag":     return (<svg {...common}><path d="M4 22V4"/><path d="M4 4h13l-2 4 2 4H4"/></svg>);
    case "home":     return (<svg {...common}><path d="m3 11 9-7 9 7v9a2 2 0 0 1-2 2h-3v-7h-8v7H5a2 2 0 0 1-2-2z"/></svg>);
    case "database": return (<svg {...common}><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v6c0 1.7 4 3 9 3s9-1.3 9-3V5"/><path d="M3 11v6c0 1.7 4 3 9 3s9-1.3 9-3v-6"/></svg>);
    case "book":     return (<svg {...common}><path d="M4 19V5a2 2 0 0 1 2-2h13v18H6a2 2 0 0 1-2-2zM4 19a2 2 0 0 1 2-2h13"/></svg>);
    case "split":    return (<svg {...common}><path d="M4 4v6a4 4 0 0 0 4 4h8a4 4 0 0 1 4 4v2"/><path d="M16 8 20 4l-4-4M16 4h4"/></svg>);
    default: return null;
  }
}

/* ===== Pill (category / tier) ===== */
export function CategoryPill({ value }: { value: Category | string }) {
  return <span className={`badge cat-${value}`}>{CATEGORY_LABEL[value as Category] ?? value}</span>;
}
export function TierPill({ value }: { value: string | null }) {
  if (!value) return <span className="muted" style={{ color: "var(--ink-4)", fontFamily: "var(--mono)" }}>—</span>;
  return <span className={`badge tier-${value}`}>{TIER_LABEL[value] ?? value}</span>;
}

/* ===== Review status pill ===== */
const RS_LABEL: Record<string, string> = {
  unreviewed: "未审核",
  pending: "待审核",
  reviewed: "已审核",
  needs_recheck: "需复检",
};
export function ReviewPill({ status }: { status: ReviewStatus | "unreviewed" | null | undefined }) {
  const s = status ?? "unreviewed";
  return (
    <span className={`rs ${s}`}>
      <span className="dot"></span>
      {RS_LABEL[s] ?? s}
    </span>
  );
}

/* ===== auto / manual layered comparison ===== */
interface LayerCompareProps<T> {
  auto: T;
  manual: T | null | undefined;
  render?: (v: T) => ReactNode;
}
export function LayerCompare<T>({ auto, manual, render }: LayerCompareProps<T>) {
  const Auto = render ? render(auto) : <span>{String(auto || "—")}</span>;
  const hasManual = manual != null && manual !== auto;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <span className="layer-chip auto">
        <span className="lab">auto</span>
        {Auto}
      </span>
      <Ico name="arrow-r" size={11} style={{ color: "var(--ink-4)" }} />
      {hasManual ? (
        <span className="layer-chip manual">
          <span className="lab">manual</span>
          {render ? render(manual as T) : <span>{String(manual)}</span>}
        </span>
      ) : (
        <span className="layer-chip empty">
          <span className="lab">manual</span>
          <span style={{ fontStyle: "italic" }}>未覆盖</span>
        </span>
      )}
    </span>
  );
}

/* ===== Distribution bar row ===== */
interface BarProps {
  label: string;
  value: number;
  total: number;
  color: string;
  badge?: ReactNode;
}
export function Bar({ label, value, total, color, badge }: BarProps) {
  const pct = total ? Math.round((value / total) * 100) : 0;
  return (
    <div className="bar-row">
      <div className="lbl">
        {badge ?? null}
        <span>{label}</span>
      </div>
      <div className="bar">
        <i style={{ width: pct + "%", background: color }} />
      </div>
      <div className="num">{value}</div>
      <div className="pct">{pct}%</div>
    </div>
  );
}

/* ===== Checkbox visual ===== */
interface CheckProps {
  state: "on" | "off" | "partial";
  onClick?: (e: React.MouseEvent) => void;
  label?: string;
}
export function Check({ state, onClick, label }: CheckProps) {
  const cls = "checkbox" + (state === "on" ? " on" : state === "partial" ? " partial" : "");
  return (
    <span
      className={cls}
      role="checkbox"
      aria-checked={state === "on" ? true : state === "partial" ? "mixed" : false}
      aria-label={label ?? "选择"}
      tabIndex={onClick ? 0 : -1}
      onClick={onClick}
      onKeyDown={(e) => {
        if (onClick && (e.key === " " || e.key === "Enter")) {
          e.preventDefault();
          onClick(e as unknown as React.MouseEvent);
        }
      }}
    ></span>
  );
}

/* ===== Issue count badge (legacy) ===== */
export function IssueCountBadge({ count }: { count: number }) {
  if (count === 0) return <span style={{ color: "var(--ink-4)", fontFamily: "var(--mono)" }}>—</span>;
  return (
    <span className="badge" style={{ background: "var(--err-50)", color: "var(--err)", borderColor: "var(--err-100)" }}>
      <Ico name="alert" size={10} />
      {count}
    </span>
  );
}
