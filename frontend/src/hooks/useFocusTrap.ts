/**
 * useFocusTrap — accessibility helper for modal dialogs and drawers.
 *
 * What it does when `active` becomes true:
 *   1. Records `document.activeElement` (the trigger button).
 *   2. Moves focus into the container — first to a `data-autofocus` element
 *      if present, else to the first focusable descendant.
 *   3. Listens for Tab / Shift+Tab and wraps focus inside the container so
 *      keyboard users can't drift into the inert background.
 *
 * When `active` becomes false:
 *   4. Returns focus to the originally-recorded trigger element.
 *
 * Why a custom hook instead of `focus-trap-react`: zero deps, ~50 lines, and
 * the project's modals are simple (no nested traps, no special render-roots).
 *
 * Usage:
 *   const ref = useFocusTrap<HTMLDivElement>(open);
 *   return open ? <div ref={ref} role="dialog" aria-modal="true">…</div> : null;
 */
import { useEffect, useRef } from "react";

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

function getFocusable(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (el) => !el.hasAttribute("disabled") && el.tabIndex !== -1
  );
}

export function useFocusTrap<T extends HTMLElement>(active: boolean) {
  const containerRef = useRef<T | null>(null);
  const triggerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!active) return;
    const container = containerRef.current;
    if (!container) return;

    // Remember the element that triggered the modal so we can restore focus
    // when the modal closes.
    triggerRef.current = (document.activeElement as HTMLElement | null) ?? null;

    // Move focus into the container. Prefer an explicit autofocus target.
    const autofocusEl = container.querySelector<HTMLElement>("[data-autofocus]");
    const focusables = getFocusable(container);
    const firstTarget = autofocusEl ?? focusables[0] ?? container;
    // Fall back to making the container itself focusable so screen readers
    // announce the dialog even if it has no focusable children yet.
    if (firstTarget === container && !container.hasAttribute("tabindex")) {
      container.tabIndex = -1;
    }
    firstTarget.focus({ preventScroll: true });

    const handler = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const els = getFocusable(container);
      if (els.length === 0) {
        e.preventDefault();
        return;
      }
      const first = els[0];
      const last = els[els.length - 1];
      const activeEl = document.activeElement as HTMLElement | null;
      if (e.shiftKey) {
        if (activeEl === first || !container.contains(activeEl)) {
          e.preventDefault();
          last.focus({ preventScroll: true });
        }
      } else {
        if (activeEl === last || !container.contains(activeEl)) {
          e.preventDefault();
          first.focus({ preventScroll: true });
        }
      }
    };
    document.addEventListener("keydown", handler);

    return () => {
      document.removeEventListener("keydown", handler);
      // Restore focus to the trigger so keyboard users land where they were.
      const trigger = triggerRef.current;
      triggerRef.current = null;
      if (trigger && typeof trigger.focus === "function" && document.contains(trigger)) {
        trigger.focus({ preventScroll: true });
      }
    };
  }, [active]);

  return containerRef;
}
