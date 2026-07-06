import { test, expect, type Page, type ConsoleMessage } from "@playwright/test";

/**
 * Dashboard work-queue lanes are driven by GET /api/work-queue/summary
 * (server-side aggregation, backend/routes/work_queue.py) instead of the old
 * client-side derivation over a 2000-row case list.
 *
 * Asserts:
 *   1. the summary request fires on Dashboard load, and the old
 *      page_size=2000 list request does NOT (that was the whole point);
 *   2. the five lane tiles render exactly the server-side counts;
 *   3. the header total matches summary.total;
 *   4. zero console errors.
 */

function attachConsoleErrorCollector(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (msg: ConsoleMessage) => {
    if (msg.type() === "error") errors.push(msg.text());
  });
  page.on("pageerror", (err) => errors.push(err.message));
  return errors;
}

const LANE_LABELS: Record<string, string> = {
  todayNew: "今日新增",
  missingLabel: "缺命名",
  blockingOpen: "阻塞待处理",
  unboundCustomer: "客户待绑定",
  pendingReview: "等审核确认",
};

test("Dashboard lanes render from /api/work-queue/summary", async ({ page }) => {
  const errors = attachConsoleErrorCollector(page);
  const fullListRequests: string[] = [];
  page.on("request", (req) => {
    const url = req.url();
    if (url.includes("/api/cases") && url.includes("page_size=2000")) {
      fullListRequests.push(url);
    }
  });

  const summaryResponse = page.waitForResponse((r) =>
    r.url().includes("/api/work-queue/summary")
  );
  await page.goto("/");
  const summary = await (await summaryResponse).json();

  await expect(page.locator("h1.page-title").first()).toBeVisible({ timeout: 15_000 });

  // Lane tiles show the server counts.
  for (const [key, label] of Object.entries(LANE_LABELS)) {
    const tile = page.locator(".stat-tile", { hasText: label }).first();
    await expect(tile).toBeVisible();
    await expect(tile.locator(".v")).toHaveText(String(summary.lanes[key].count));
  }

  // Header total comes from summary.total (zh: "共 {{n}} 个案例").
  // .first(): the worktray card shows the same "共 N 个案例" via stats.total.
  await expect(page.getByText(`共 ${summary.total} 个案例`).first()).toBeVisible();

  // Dashboard no longer pulls the 2000-row case list.
  expect(fullListRequests, fullListRequests.join("\n")).toEqual([]);

  expect(errors, errors.join("\n")).toEqual([]);
});
