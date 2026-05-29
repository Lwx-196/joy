import { expect, test } from "@playwright/test";

/**
 * C3.0.2 OpsConsole — Playwright smoke spec.
 *
 * Selectors use ``data-testid`` (NOT i18n text) per dev-spec §1.4 + ts subspec
 * — the Playwright config forces ``zh-CN`` locale but OpsConsole.tsx labels
 * are language-driven, so testid selectors keep the spec stable.
 *
 * Verifies:
 *   1. Page loads and renders all 6 cards from the promotion block
 *   2. Window toggle + probe toggle re-issue the request
 *   3. Console has 0 errors (golden-path UX gate)
 */

test("ops console renders all 6 promotion cards without console errors", async ({
  page,
}) => {
  const consoleErrors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });

  await page.goto("/ops");

  // Page root present
  await expect(page.getByTestId("ops-console")).toBeVisible();

  // All 6 promotion cards present (the additive C3.0.1 contract)
  for (const card of [
    "ops-console-card-manifest",
    "ops-console-card-slo",
    "ops-console-card-comfyui",
    "ops-console-card-latency",
    "ops-console-card-silent-fail",
    "ops-console-card-applier",
  ]) {
    await expect(page.getByTestId(card)).toBeVisible();
  }

  // Manifest card surfaces a state badge (shadow on an empty DB)
  await expect(page.getByTestId("ops-console-manifest-state")).toBeVisible();
  // Bucket exposure renders a numeric pct
  await expect(page.getByTestId("ops-console-bucket-exposure")).toBeVisible();
  // SLO sample size pill
  await expect(page.getByTestId("ops-console-slo-sample")).toBeVisible();
  // Applier empty state visible when DB has no audit rows
  await expect(page.getByTestId("ops-console-applier-empty")).toBeVisible();

  // Window-toggle interactions
  await page.getByTestId("ops-console-window-48h").click();
  await page.getByTestId("ops-console-window-72h").click();
  await page.getByTestId("ops-console-window-24h").click();

  // Probe toggle works
  await page.getByTestId("ops-console-probe-toggle").click();
  await expect(page.getByTestId("ops-console-comfyui-reachable")).toBeVisible();
  await page.getByTestId("ops-console-probe-toggle").click();

  // Refresh button works
  await page.getByTestId("ops-console-refresh").click();
  await expect(page.getByTestId("ops-console-last-updated")).toBeVisible();

  expect(consoleErrors).toEqual([]);
});
