import { test, expect, type Route } from "@playwright/test";

/**
 * F2 frugal-cache-guard: AI 增强板 cache-miss → RenderStatusCard 弹「首次出图需调用 API」
 * amber 确认卡，用户「确认出图」后才带 confirm_burn 真烧；默认不静默烧钱。
 *
 * 测法（镜像 render-held.spec.ts）：导航到真实 case 页（case 126），仅拦截单个
 * `GET /api/cases/:id/render/latest` 注入 needs_confirmation 作业**状态契约**（等价于后端
 * render_queue._finish_result 落库后前端读到的 job 形状，meta 带 cache_miss_*）。其余走真实后端。
 *
 * 覆盖 RenderStatusCard 的 needs_confirmation 分支：
 *   1) 确认卡可见 + 缺槽/预估烧钱($)/耗时 文案 + 确认/取消 按钮
 *   2) 点「取消」→ 本地消解（render-cache-miss-dismissed），不发任何网络请求
 *
 * 零烧钱、确定性：契约注入，不触发真实出图。
 */

const CASE_ID = 126;
const FIXTURE_JOB_ID = 999101;

type Json = Record<string, unknown>;

function confirmJob(overrides: Json): Json {
  return {
    id: FIXTURE_JOB_ID,
    case_id: CASE_ID,
    brand: "fumei",
    template: "tri-compare",
    status: "needs_confirmation",
    batch_id: null,
    enqueued_at: "2026-06-14T08:00:00Z",
    started_at: "2026-06-14T08:00:01Z",
    finished_at: "2026-06-14T08:00:05Z",
    output_path: null,
    output_mtime: null,
    manifest_path: null,
    error_message: null,
    semantic_judge: "off",
    meta: {
      status: "needs_confirmation",
      cache_miss_count: 2,
      cache_miss_total: 3,
      cache_miss_est_cost_usd: 0.11,
      cache_miss_est_seconds: 300,
    },
    quality: null,
    blocking_issues: [],
    warnings: [],
    ...overrides,
  };
}

async function routeLatestJob(page: import("@playwright/test").Page, job: Json) {
  await page.route(`**/api/cases/${CASE_ID}/render/latest`, (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ job }),
    }),
  );
}

test.describe("render needs_confirmation (cache-miss 烧钱护栏) — F2", () => {
  test("cache-miss → 确认卡 + 预估($/时长) + 确认/取消 按钮", async ({ page }) => {
    const errors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") errors.push(msg.text());
    });

    await routeLatestJob(page, confirmJob({}));
    await page.goto(`/cases/${CASE_ID}`);

    const card = page.locator('[data-testid="render-cache-miss"]');
    await expect(card).toBeVisible();

    await expect(page.locator('[data-testid="render-cache-miss-badge"]')).toContainText("首次出图需调用 API");

    // 缺槽 2/3 + 预估烧钱 $0.11（toFixed(2)）+ 耗时 5 分钟（300s→分钟）
    const hint = page.locator('[data-testid="render-cache-miss-hint"]');
    await expect(hint).toContainText("2/3");
    await expect(hint).toContainText("$0.11");
    await expect(hint).toContainText("5 分钟");

    await expect(page.locator('[data-testid="render-cache-miss-confirm"]')).toBeVisible();
    await expect(page.locator('[data-testid="render-cache-miss-cancel"]')).toBeVisible();

    // 状态徽章是「待确认」，不走 failed/blocked
    await expect(page.locator('[data-testid="render-status-card"]')).toContainText("待确认");
    await expect(page.locator('[data-testid="render-held"]')).toHaveCount(0);

    expect(errors, `console errors: ${errors.join(" | ")}`).toEqual([]);
  });

  test("点「取消」→ 本地消解，不真烧（render-cache-miss-dismissed）", async ({ page }) => {
    // 确认/重烧会 POST /api/cases/:id/render —— 本用例只点「取消」，断言零网络副作用。
    let renderPosted = false;
    await page.route(`**/api/cases/${CASE_ID}/render`, (route: Route) => {
      renderPosted = true;
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ job_id: 1, batch_id: null }) });
    });
    await routeLatestJob(page, confirmJob({}));
    await page.goto(`/cases/${CASE_ID}`);

    await expect(page.locator('[data-testid="render-cache-miss-hint"]')).toBeVisible();
    await page.locator('[data-testid="render-cache-miss-cancel"]').click();

    await expect(page.locator('[data-testid="render-cache-miss-dismissed"]')).toBeVisible();
    await expect(page.locator('[data-testid="render-cache-miss-hint"]')).toHaveCount(0);
    expect(renderPosted, "取消不应触发任何出图 POST").toBe(false);
  });
});
