import { test, expect } from "@playwright/test";

/**
 * 第三条出图选项「术后AI增强板」开关 — UI 冒烟（只读：不点出图、不写 DB）。
 *
 * 验证：出图面板里 AI 增强板 toggle 渲染、勾选后模型下拉出现且默认 gemini、取消后下拉消失、
 * console 无 error。后端真实数据（case 126，与 source-image-override 同源）。
 */
const CASE_ID = 126;

test.describe("ai-enhance-board toggle", () => {
  test("toggle renders in manual render panel and reveals model select", async ({ page }) => {
    const errors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") errors.push(msg.text());
    });

    await page.goto(`/cases/${CASE_ID}`);

    const picker = page.locator('[data-testid="manual-render-picker"]');
    await expect(picker).toBeVisible();

    const toggle = page.locator('[data-testid="ai-enhance-board-toggle"]');
    await toggle.scrollIntoViewIfNeeded();
    await expect(toggle).toBeVisible();

    // 模型下拉默认隐藏，勾选后才出现。
    await expect(page.locator('[data-testid="ai-enhance-board-model"]')).toHaveCount(0);

    await toggle.check();
    const model = page.locator('[data-testid="ai-enhance-board-model"]');
    await expect(model).toBeVisible();
    // gemini 为默认主力模型（owner 裁决）。
    await expect(model).toHaveValue("gemini-3-pro-image-preview");

    await toggle.uncheck();
    await expect(page.locator('[data-testid="ai-enhance-board-model"]')).toHaveCount(0);

    expect(errors, `console errors: ${errors.join(" | ")}`).toEqual([]);
  });
});
