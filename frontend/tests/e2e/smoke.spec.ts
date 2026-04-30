import { test, expect, type Page, type ConsoleMessage } from "@playwright/test";

/**
 * Smoke: every route lazy-loads its chunk, mounts the page <h1 class="page-title">,
 * and emits no console.error during load. Detail routes use a real id from the list.
 *
 * Routes covered:
 *   /                       Dashboard (eager)
 *   /cases                  Cases (lazy)
 *   /cases/:id              CaseDetail (lazy)
 *   /customers              Customers (lazy)
 *   /customers/:id          CustomerDetail (lazy)
 *   /dict                   Dict (lazy)
 *   /evaluations            Evaluations (lazy)
 *   /jobs/batches/:batchId  JobBatch (lazy) — chunk-load smoke only, fake id is fine
 */

function attachConsoleErrorCollector(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (msg: ConsoleMessage) => {
    if (msg.type() === "error") errors.push(msg.text());
  });
  page.on("pageerror", (err) => errors.push(err.message));
  return errors;
}

async function expectPageLoaded(page: Page) {
  // The app has long-running global pollers (BatchJobToast, etc.) that prevent
  // networkidle from ever firing. Wait for the page-title h1 instead — it
  // appears after the lazy chunk mounts and the initial query resolves.
  await expect(page.locator("h1.page-title").first()).toBeVisible({ timeout: 15_000 });
}

test.describe("smoke: route chunks load + h1 mounts", () => {
  test("/ Dashboard renders", async ({ page }) => {
    const errors = attachConsoleErrorCollector(page);
    await page.goto("/");
    await expectPageLoaded(page);
    expect(errors, errors.join("\n")).toEqual([]);
  });

  test("/cases Cases renders", async ({ page }) => {
    const errors = attachConsoleErrorCollector(page);
    await page.goto("/cases");
    await expectPageLoaded(page);
    await expect(page.locator("table.table thead").first()).toBeVisible();
    expect(errors, errors.join("\n")).toEqual([]);
  });

  test("/customers Customers renders", async ({ page }) => {
    const errors = attachConsoleErrorCollector(page);
    await page.goto("/customers");
    await expectPageLoaded(page);
    expect(errors, errors.join("\n")).toEqual([]);
  });

  test("/dict Dict renders", async ({ page }) => {
    const errors = attachConsoleErrorCollector(page);
    await page.goto("/dict");
    await expectPageLoaded(page);
    expect(errors, errors.join("\n")).toEqual([]);
  });

  test("/evaluations Evaluations renders", async ({ page }) => {
    const errors = attachConsoleErrorCollector(page);
    await page.goto("/evaluations");
    await expectPageLoaded(page);
    expect(errors, errors.join("\n")).toEqual([]);
  });

  test("/cases/:id CaseDetail renders (id from list)", async ({ page }) => {
    const errors = attachConsoleErrorCollector(page);
    await page.goto("/cases");
    const firstCaseLink = page.locator('table.table tbody a[href^="/cases/"]').first();
    await expect(firstCaseLink).toBeVisible({ timeout: 15_000 });
    const href = await firstCaseLink.getAttribute("href");
    expect(href).toMatch(/^\/cases\/\d+$/);
    await page.goto(href!);
    await expect(page.locator("main#main-content")).toBeVisible();
    await expect(page.locator("h1, h2").first()).toBeVisible({ timeout: 15_000 });
    expect(errors, errors.join("\n")).toEqual([]);
  });

  test("/customers/:id CustomerDetail renders (id from list)", async ({ page }) => {
    const errors = attachConsoleErrorCollector(page);
    await page.goto("/customers");
    const firstCustomerLink = page.locator('a[href^="/customers/"]').first();
    await expect(firstCustomerLink).toBeVisible({ timeout: 15_000 });
    const href = await firstCustomerLink.getAttribute("href");
    expect(href).toMatch(/^\/customers\/\d+$/);
    await page.goto(href!);
    await expect(page.locator("main#main-content")).toBeVisible();
    await expect(page.locator("h1, h2").first()).toBeVisible({ timeout: 15_000 });
    expect(errors, errors.join("\n")).toEqual([]);
  });

  test("/jobs/batches/:batchId JobBatch chunk loads (fake id)", async ({ page }) => {
    // Pure chunk-load smoke. The page may show an empty state for an unknown id —
    // the goal is to confirm the lazy chunk downloads and mounts without error.
    const errors = attachConsoleErrorCollector(page);
    await page.goto("/jobs/batches/999999?type=render");
    await expect(page.locator("main#main-content")).toBeVisible();
    // Filter out the expected 404-shaped network errors (backend may legitimately
    // not have batch 999999). We only care about JS errors / unhandled rejections.
    const jsErrors = errors.filter(
      (e) => !/Failed to load resource/i.test(e) && !/404/.test(e)
    );
    expect(jsErrors, jsErrors.join("\n")).toEqual([]);
  });
});
