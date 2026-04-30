/**
 * useHotkey — minimal, dependency-free keyboard shortcut hook.
 *
 * Why home-grown: react-hotkeys-hook adds 8KB gzip and an event-prop API that
 * doesn't compose cleanly with our existing focus-trap / modal patterns. The
 * surface we need is small.
 *
 * Behavior:
 * - Handlers run on `keydown` at the document level.
 * - Typing in `<input>`/`<textarea>`/`contenteditable` skips the handler so
 *   "/" doesn't steal focus while the user is typing.
 * - Plain single-key bindings (e.g. "j", "x", "/") only fire when no modifier
 *   key is pressed; modifier bindings ("ctrl+r", "meta+r", "shift+?") match
 *   the exact set, case-insensitively.
 * - Set `enabled: false` to detach without unmounting the component.
 *
 * The hotkey help layer reads {@link HotkeyDescriptor} entries from a parallel
 * registry; this hook only handles dispatch.
 */
import { useEffect } from "react";

export interface HotkeyOptions {
  /** Skip when an editable element has focus. Default: true. */
  ignoreInEditable?: boolean;
  /** Set false to deregister the listener without unmounting. */
  enabled?: boolean;
  /** Call event.preventDefault() before the handler. */
  preventDefault?: boolean;
}

const EDITABLE_TAGS = new Set(["INPUT", "TEXTAREA", "SELECT"]);

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (EDITABLE_TAGS.has(target.tagName)) return true;
  if (target.isContentEditable) return true;
  return false;
}

interface ParsedKey {
  key: string;
  ctrl: boolean;
  meta: boolean;
  shift: boolean;
  alt: boolean;
}

function parseKey(combo: string): ParsedKey {
  const parts = combo.toLowerCase().split("+").map((p) => p.trim()).filter(Boolean);
  const out: ParsedKey = { key: "", ctrl: false, meta: false, shift: false, alt: false };
  for (const p of parts) {
    if (p === "ctrl" || p === "control") out.ctrl = true;
    else if (p === "meta" || p === "cmd" || p === "command") out.meta = true;
    else if (p === "shift") out.shift = true;
    else if (p === "alt" || p === "option") out.alt = true;
    else out.key = p;
  }
  return out;
}

function eventMatches(parsed: ParsedKey, e: KeyboardEvent): boolean {
  if (e.ctrlKey !== parsed.ctrl) return false;
  if (e.metaKey !== parsed.meta) return false;
  if (e.altKey !== parsed.alt) return false;
  // Shift handling is special: when the user binds "shift+?" the parsed key is
  // already "?" (the shifted glyph), and e.shiftKey is true. So we accept either
  // exact-shift-match OR the unshifted key matching when shift wasn't requested.
  const key = e.key.toLowerCase();
  if (parsed.shift !== e.shiftKey && parsed.shift) return false;
  return key === parsed.key;
}

/** Register a keyboard shortcut. The handler is auto-deregistered on unmount or
 * when `enabled` flips to false. The `combo` is parsed once per render. */
export function useHotkey(
  combo: string,
  handler: (e: KeyboardEvent) => void,
  options: HotkeyOptions = {}
): void {
  const { ignoreInEditable = true, enabled = true, preventDefault = false } = options;

  useEffect(() => {
    if (!enabled) return;
    const parsed = parseKey(combo);
    if (!parsed.key) return;

    const onKey = (e: KeyboardEvent) => {
      if (ignoreInEditable && isEditableTarget(e.target)) return;
      if (!eventMatches(parsed, e)) return;
      if (preventDefault) e.preventDefault();
      handler(e);
    };

    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [combo, handler, ignoreInEditable, enabled, preventDefault]);
}

export interface HotkeyDescriptor {
  combo: string;
  /** i18n key under the `hotkeys` namespace, e.g. "cases.next". */
  i18nKey: string;
  /** Optional scope label so the help dialog can group entries. */
  scope?: string;
}
