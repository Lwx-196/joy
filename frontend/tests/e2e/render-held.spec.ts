import { test, expect, type Route } from "@playwright/test";

/**
 * WP2g aligned-render-pipeline: G1/G2 质量门 HELD → RenderStatusCard 显示「质量保留 blocked」
 * 而非「渲染失败」。
 *
 * 测法（与既有 e2e 一致：真实后端经 playwright webServer 自动起）：
 *   导航到真实 case 页（case 126，与 ai-enhance-board-toggle / source-image-override 同源），
 *   仅拦截单个 `GET /api/cases/:id/render/latest` 注入 HELD 作业**状态契约**（非业务数据，
 *   等价于后端 render_queue._finish_result 落库后前端读到的 job 形状）。页面其余数据走真实后端。
 *   G2 诊断板缩略图请求拦截返回 1×1 PNG 以稳定断言（强制走 job-specific /file 分支，不碰真实源图）。
 *
 * 覆盖 RenderStatusCard 的 HELD/失败/通用 blocked 三分支：
 *   1) G1 angle HELD → render-held + 角度覆盖门 + 无诊断板（G1 不出板）
 *   2) G2 pair  HELD → render-held + 配对一致性门 + 诊断板缩略可见（G2 保留诊断板）
 *   3) 真 failed       → 仍走失败分支，不出 render-held（区别于质量保留）
 *   4) blocked 无 held_gate → 走通用 blocked 分支，不出 render-held（验证 heldGate 二分）
 *
 * 该 e2e 零烧钱、确定性。真实全链路 G1 zero-cost 点击（角度门在 AI 增强前触发、不调图像 API）
 * 交 owner 目检（handoff §A）。
 */

const CASE_ID = 126;
const FIXTURE_JOB_ID = 999001;

// 1×1 透明 PNG：让诊断板缩略 <img> onload 成功，避免 onError 收起 render-held-thumb（消除竞态）。
const PNG_1x1 = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
  "base64",
);

type Json = Record<string, unknown>;

function heldJob(overrides: Json): Json {
  return {
    id: FIXTURE_JOB_ID,
    case_id: CASE_ID,
    brand: "fumei",
    template: "standard",
    status: "blocked",
    batch_id: null,
    enqueued_at: "2026-06-14T08:00:00Z",
    started_at: "2026-06-14T08:00:01Z",
    finished_at: "2026-06-14T08:00:30Z",
    output_path: null,
    output_mtime: null,
    manifest_path: null,
    error_message: null,
    semantic_judge: "off",
    meta: { status: "ok", blocking_issue_count: 1, warning_count: 0 },
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

// 仅拦截本 fixture job 的成品文件请求（job id 唯一），不碰页面真实源图缩略。
async function routeHeldBoardImage(page: import("@playwright/test").Page) {
  await page.route(`**/api/render/jobs/${FIXTURE_JOB_ID}/file**`, (route: Route) =>
    route.fulfill({ status: 200, contentType: "image/png", body: PNG_1x1 }),
  );
}

test.describe("render HELD (质量保留) — WP2g", () => {
  test("G1 angle gate HELD → 质量保留，无诊断板缩略", async ({ page }) => {
    const errors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") errors.push(msg.text());
    });

    await routeLatestJob(
      page,
      heldJob({
        meta: { status: "ok", blocking_issue_count: 1, held_gate: "angle", held_reason: "缺少必需角度：正面" },
        error_message: "缺少必需角度：正面",
      }),
    );

    await page.goto(`/cases/${CASE_ID}`);

    const held = page.locator('[data-testid="render-held"]');
    await expect(held).toBeVisible();

    const gate = page.locator('[data-testid="render-held-gate"]');
    await expect(gate).toContainText("质量保留");
    await expect(gate).toContainText("角度覆盖门");

    await expect(page.locator('[data-testid="render-held-reason"]')).toContainText("缺少必需角度");

    // G1 不出板 → 无诊断板缩略
    await expect(page.locator('[data-testid="render-held-board"]')).toHaveCount(0);

    // 状态徽章是「质量阻塞」而非「失败」，且不走 failed 分支
    await expect(page.locator('[data-testid="render-status-card"]')).toContainText("质量阻塞");

    expect(errors, `console errors: ${errors.join(" | ")}`).toEqual([]);
  });

  test("G2 pair gate HELD → 质量保留，诊断板缩略可见", async ({ page }) => {
    await routeHeldBoardImage(page);
    await routeLatestJob(
      page,
      heldJob({
        // 非 case-relative 绝对路径 → renderJobOutputUrl 落 /api/render/jobs/:id/file 分支（被上面拦截）
        output_path: "/tmp/held-diagnostic/foo_standard_ai_enhanced.jpg",
        meta: {
          status: "ok",
          blocking_issue_count: 1,
          held_gate: "pair",
          held_reason: "配对一致性不达标：术前/术后脸尺寸偏差超阈",
        },
        error_message: "配对一致性不达标：术前/术后脸尺寸偏差超阈",
      }),
    );

    await page.goto(`/cases/${CASE_ID}`);

    await expect(page.locator('[data-testid="render-held"]')).toBeVisible();
    await expect(page.locator('[data-testid="render-held-gate"]')).toContainText("配对一致性门");

    // G2 保留诊断板 → 缩略块 + 缩略图链接可见
    await expect(page.locator('[data-testid="render-held-board"]')).toBeVisible();
    await expect(page.locator('[data-testid="render-held-thumb"]')).toBeVisible();
  });

  test("真 failed 仍显示失败分支，不出 render-held", async ({ page }) => {
    await routeLatestJob(
      page,
      heldJob({
        status: "failed",
        error_message: "produced no board",
        meta: { status: "error", blocking_issue_count: 0 }, // 无 held_gate
      }),
    );

    await page.goto(`/cases/${CASE_ID}`);

    const card = page.locator('[data-testid="render-status-card"]');
    await expect(card).toBeVisible();
    await expect(page.locator('[data-testid="render-held"]')).toHaveCount(0);
    await expect(card).toContainText("produced no board");
    await expect(card).toContainText("失败");
  });

  test("blocked 但无 held_gate → 通用 blocked 分支，不出 render-held", async ({ page }) => {
    await routeLatestJob(
      page,
      heldJob({
        status: "blocked",
        error_message: "产物没有通过质量门槛",
        meta: { status: "ok", blocking_issue_count: 1 }, // 无 held_gate
      }),
    );

    await page.goto(`/cases/${CASE_ID}`);

    const card = page.locator('[data-testid="render-status-card"]');
    await expect(card).toBeVisible();
    await expect(page.locator('[data-testid="render-held"]')).toHaveCount(0);
    await expect(card).toContainText("质量阻塞");
  });
});
