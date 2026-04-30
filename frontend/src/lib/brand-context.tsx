/**
 * Global brand selection — drives all v3 upgrade and render mutations.
 *
 * Persisted to localStorage so the user doesn't have to re-pick on every reload.
 * Single source of truth: hooks read context.brand, not localStorage directly.
 *
 * Allowed values are the keys of case-layout-board's BRANDS dict (fumei / shimei).
 * Adding a brand at the skill layer is sufficient; this layer only validates the
 * subset the topbar exposes.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import type { Brand } from "../api";

const STORAGE_KEY = "case-workbench:brand";
const DEFAULT_BRAND: Brand = "fumei";
const VALID_BRANDS: readonly Brand[] = ["fumei", "shimei"] as const;

function readStoredBrand(): Brand {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw && (VALID_BRANDS as readonly string[]).includes(raw)) {
      return raw as Brand;
    }
  } catch {
    /* private mode / quota — fall through to default */
  }
  return DEFAULT_BRAND;
}

interface BrandContextValue {
  brand: Brand;
  setBrand: (next: Brand) => void;
}

const BrandContext = createContext<BrandContextValue | null>(null);

export function BrandProvider({ children }: { children: ReactNode }) {
  const [brand, setBrandState] = useState<Brand>(() => readStoredBrand());

  const setBrand = useCallback((next: Brand) => {
    if (!(VALID_BRANDS as readonly string[]).includes(next)) return;
    setBrandState(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* ignore */
    }
  }, []);

  // Sync across tabs: another tab editing localStorage updates this one too.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key !== STORAGE_KEY) return;
      const next = e.newValue;
      if (next && (VALID_BRANDS as readonly string[]).includes(next)) {
        setBrandState(next as Brand);
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const value = useMemo<BrandContextValue>(() => ({ brand, setBrand }), [brand, setBrand]);
  return <BrandContext.Provider value={value}>{children}</BrandContext.Provider>;
}

/** Read the currently-selected brand. Throws if used outside BrandProvider. */
export function useBrand(): Brand {
  const ctx = useContext(BrandContext);
  if (!ctx) throw new Error("useBrand must be used inside <BrandProvider>");
  return ctx.brand;
}

/** Read+set brand. Use this in the topbar selector. */
export function useBrandSelector(): BrandContextValue {
  const ctx = useContext(BrandContext);
  if (!ctx) throw new Error("useBrandSelector must be used inside <BrandProvider>");
  return ctx;
}

export { VALID_BRANDS };
