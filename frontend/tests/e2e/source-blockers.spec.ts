import { expect, test } from "@playwright/test";

test("source blocker workbench loads real queue without broken thumbnails", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });

  await page.goto("/source-blockers");
  await expect(page.getByRole("heading", { name: /源目录阻断/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /全部阻断/ })).toBeVisible();
  await page.getByRole("button", { name: /缺术前\/术后/ }).click();

  const cards = page.locator("article");
  if ((await cards.count()) > 0) {
    await expect(cards.first()).toBeVisible();
    await expect(page.getByRole("button", { name: "绑定" }).first()).toBeVisible();
    const thumbs = cards.first().locator("img");
    if ((await thumbs.count()) > 0) {
      await expect
        .poll(async () => thumbs.first().evaluate((img) => (img as HTMLImageElement).naturalWidth))
        .toBeGreaterThan(0);
    }
  }

  expect(consoleErrors).toEqual([]);
});
