import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

/**
 * Cross-route axe-core a11y scan. Phase 4 manually verified 11 routes are 0
 * violations; this codifies it as automated regression.
 *
 * Scope: 5 main routes + 2 detail routes (where a real id is available).
 */

async function scan(page: Page, label: string) {
  // Don't wait for networkidle — global pollers (BatchJobToast etc.) prevent it.
  // Instead wait for the page heading to confirm the route mounted.
  await expect(page.locator("h1, h2").first()).toBeVisible({ timeout: 15_000 });
  const results = await new AxeBuilder({ page }).analyze();
  expect(
    results.violations,
    `${label} a11y violations:\n${JSON.stringify(results.violations, null, 2)}`
  ).toEqual([]);
}

test.describe("a11y: 0 axe violations across routes", () => {
  test("/ Dashboard", async ({ page }) => {
    await page.goto("/");
    await scan(page, "/");
  });

  test("/cases Cases", async ({ page }) => {
    await page.goto("/cases");
    await scan(page, "/cases");
  });

  test("/customers Customers", async ({ page }) => {
    await page.goto("/customers");
    await scan(page, "/customers");
  });

  test("/dict Dict", async ({ page }) => {
    await page.goto("/dict");
    await scan(page, "/dict");
  });

  test("/evaluations Evaluations", async ({ page }) => {
    await page.goto("/evaluations");
    await scan(page, "/evaluations");
  });

  test("/cases/:id CaseDetail", async ({ page }) => {
    await page.goto("/cases");
    const firstCaseLink = page.locator('table.table tbody a[href^="/cases/"]').first();
    await expect(firstCaseLink).toBeVisible({ timeout: 15_000 });
    const href = await firstCaseLink.getAttribute("href");
    expect(href).toMatch(/^\/cases\/\d+$/);
    await page.goto(href!);
    await scan(page, href!);
  });

  test("/cases/:id CaseDetail with RenderHistoryDrawer open", async ({ page }) => {
    await page.goto("/cases");
    const firstCaseLink = page.locator('table.table tbody a[href^="/cases/"]').first();
    await expect(firstCaseLink).toBeVisible({ timeout: 15_000 });
    const href = await firstCaseLink.getAttribute("href");
    await page.goto(href!);
    await expect(page.locator("h1, h2").first()).toBeVisible({ timeout: 15_000 });
    const trigger = page.locator('[data-testid="render-history-trigger"]');
    await expect(trigger).toBeVisible({ timeout: 15_000 });
    await trigger.click();
    await expect(page.locator('[data-testid="render-history-drawer"]')).toBeVisible();
    const results = await new AxeBuilder({ page }).analyze();
    expect(
      results.violations,
      `${href} (drawer open) a11y violations:\n${JSON.stringify(results.violations, null, 2)}`
    ).toEqual([]);
  });

  test("/customers/:id CustomerDetail", async ({ page }) => {
    await page.goto("/customers");
    const firstCustomerLink = page.locator('a[href^="/customers/"]').first();
    await expect(firstCustomerLink).toBeVisible({ timeout: 15_000 });
    const href = await firstCustomerLink.getAttribute("href");
    expect(href).toMatch(/^\/customers\/\d+$/);
    await page.goto(href!);
    await scan(page, href!);
  });

  // === 阶段 12 新增 ===
  test("/cases/126 CaseDetail with Lightbox open", async ({ page }) => {
    await page.goto("/cases/126");
    await expect(page.locator("h1, h2").first()).toBeVisible({ timeout: 15_000 });
    await page.locator('[data-testid="render-history-trigger"]').click();
    await expect(page.locator('[data-testid="render-history-drawer"]')).toBeVisible();
    const items = page.locator('[data-testid="render-history-item"]');
    await expect(items.first()).toBeVisible({ timeout: 10_000 });
    await items.first().locator("button").first().click();
    await expect(page.locator('[data-testid="lightbox"]')).toBeVisible();

    const results = await new AxeBuilder({ page }).analyze();
    expect(
      results.violations,
      `/cases/126 lightbox open a11y violations:\n${JSON.stringify(results.violations, null, 2)}`
    ).toEqual([]);
  });

  test("/cases/126 CaseDetail with Restore confirm modal open", async ({ page }) => {
    await page.goto("/cases/126");
    await expect(page.locator("h1, h2").first()).toBeVisible({ timeout: 15_000 });
    await page.locator('[data-testid="render-history-trigger"]').click();
    await expect(page.locator('[data-testid="render-history-drawer"]')).toBeVisible();
    const items = page.locator('[data-testid="render-history-item"]');
    await expect(items.first()).toBeVisible({ timeout: 10_000 });
    await items.first().locator('[data-testid="render-history-restore"]').click();
    await expect(page.locator('[data-testid="render-restore-confirm"]')).toBeVisible();

    const results = await new AxeBuilder({ page }).analyze();
    expect(
      results.violations,
      `/cases/126 restore-confirm open a11y violations:\n${JSON.stringify(results.violations, null, 2)}`
    ).toEqual([]);
  });
});
