import { test, expect, type Page, type Response } from "@playwright/test";

function collectRuntimeFailures(page: Page) {
  const consoleErrors: string[] = [];
  const serverErrors: string[] = [];
  page.on("console", (msg) => {
    const text = msg.text();
    if (msg.type() === "error" && !/Failed to load resource: the server responded with a status of (403|404)/i.test(text)) {
      consoleErrors.push(text);
    }
  });
  page.on("pageerror", (err) => consoleErrors.push(err.message));
  page.on("response", (resp: Response) => {
    if (resp.status() >= 500) serverErrors.push(`${resp.status()} ${resp.url()}`);
  });
  return { consoleErrors, serverErrors };
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => Math.max(0, document.documentElement.scrollWidth - window.innerWidth));
  expect(overflow).toBeLessThanOrEqual(2);
}

async function expectVisibleImagesLoaded(page: Page, min = 1) {
  const viewportStats = () =>
    page.locator("img:visible").evaluateAll((imgs) =>
      imgs
        .filter((img) => {
          const rect = (img as HTMLImageElement).getBoundingClientRect();
          return rect.bottom >= 0 && rect.right >= 0 && rect.top <= window.innerHeight && rect.left <= window.innerWidth;
        })
        .map((img) => ({
          src: (img as HTMLImageElement).currentSrc || (img as HTMLImageElement).src,
          width: (img as HTMLImageElement).naturalWidth,
          height: (img as HTMLImageElement).naturalHeight,
        })),
    );
  await expect.poll(async () => (await viewportStats()).filter((item) => item.width > 0 && item.height > 0).length).toBeGreaterThanOrEqual(min);
  const stats = await viewportStats();
  expect(stats.length).toBeGreaterThanOrEqual(min);
  const broken = stats.filter((item) => item.width <= 0 || item.height <= 0);
  expect(broken, JSON.stringify(broken.slice(0, 5), null, 2)).toEqual([]);
}

async function capture(page: Page, name: string) {
  const root = process.env.STRESS_SCREENSHOT_DIR;
  if (!root) return;
  await page.screenshot({ path: `${root}/${name}.png`, fullPage: true });
}

// Requires real DB + real case fixtures populated by scripts/stress/prepare_data.py.
// Set SMOKE_STRESS=1 to run locally after preparing data; CI lacks the fixture.
// Long-term: hardening N19 will fixture this with synthetic data so CI can run.
test.describe("stress smoke: real full-chain surfaces", () => {
  test.skip(!process.env.SMOKE_STRESS, "Set SMOKE_STRESS=1 with prepared stress fixture to run");
  test("main pages, images, render history, supplement candidates, and AI QA load cleanly", async ({ page }) => {
    const failures = collectRuntimeFailures(page);

    await page.goto("/cases");
    await expect(page.locator("h1.page-title").first()).toBeVisible({ timeout: 15_000 });
    await expect(page.locator("table.table tbody tr").first()).toBeVisible({ timeout: 15_000 });
    await expectNoHorizontalOverflow(page);
    await capture(page, "cases");

    await page.goto("/images");
    await expect(page.locator("h1.page-title").first()).toBeVisible({ timeout: 15_000 });
    await page.locator(".image-workbench-filters select").first().selectOption("all");
    await page.getByPlaceholder("搜索案例 / 文件 / 部位").fill("术前术中术后即刻");
    await expect(page.locator(".image-workbench-card").first()).toBeVisible({ timeout: 15_000 });
    await expectVisibleImagesLoaded(page, 4);
    await expectNoHorizontalOverflow(page);
    await capture(page, "images");

    await page.goto("/cases/126");
    await expect(page.locator("main#main-content")).toBeVisible({ timeout: 15_000 });
    await expect(page.locator("h1, h2").first()).toBeVisible({ timeout: 15_000 });
    const renderHistory = page.locator('[data-testid="render-history-trigger"]');
    await expect(renderHistory).toBeVisible({ timeout: 15_000 });
    await renderHistory.click();
    await expect(page.locator('[data-testid="render-history-drawer"]')).toBeVisible({ timeout: 10_000 });
    const firstSnapshot = page.locator('[data-testid="render-history-item"]').first();
    if (await firstSnapshot.count()) {
      await firstSnapshot.locator("button").first().click();
      await expect(page.locator('[data-testid="lightbox"]')).toBeVisible({ timeout: 10_000 });
      await expectVisibleImagesLoaded(page, 1);
      await page.keyboard.press("Escape");
    }
    await page.keyboard.press("Escape");
    await expectNoHorizontalOverflow(page);
    await capture(page, "case-126");

    await page.goto("/cases/1");
    await expect(page.locator("main#main-content")).toBeVisible({ timeout: 15_000 });
    const supplementTrigger = page.getByRole("button", { name: /查找可补图|收起补图候选/ }).first();
    if (await supplementTrigger.count()) {
      await supplementTrigger.click();
      await expect(page.locator(".supplement-candidate-card").first()).toBeVisible({ timeout: 15_000 });
      await expectVisibleImagesLoaded(page, 1);
    }
    await expectNoHorizontalOverflow(page);
    await capture(page, "supplement-candidates");

    await page.goto("/quality");
    await expect(page.locator("h1.page-title").first()).toBeVisible({ timeout: 15_000 });
    const aiTab = page.getByRole("button", { name: /AI 增强|AI/ }).first();
    if (await aiTab.count()) {
      await aiTab.click();
    }
    await expect(page.locator("main#main-content")).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await capture(page, "quality-ai");

    expect(failures.consoleErrors, failures.consoleErrors.join("\n")).toEqual([]);
    expect(failures.serverErrors, failures.serverErrors.join("\n")).toEqual([]);
  });
});
