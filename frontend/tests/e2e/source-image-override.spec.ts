import { test, expect } from "@playwright/test";

/**
 * Stage B: 单张源图 phase / view 手动覆盖。
 *
 * Uses 术中3.jpeg on case 126 — that image has phase=null (skill couldn't
 * label it) so applying manual_phase moves it cleanly between groups.
 *
 * Each test cleans its own override in afterEach so the DB is restored
 * (other suites assume read-only against case-workbench.db).
 */

const CASE_ID = 126;
const TARGET = "术中3.jpeg";

async function clearOverride(request: import("@playwright/test").APIRequestContext) {
  // PATCH with empty strings drops both dimensions and deletes the row.
  await request.patch(
    `/api/cases/${CASE_ID}/images/${encodeURIComponent(TARGET)}`,
    { data: { manual_phase: "", manual_view: "" } },
  );
}

test.describe("source-image-override", () => {
  test.afterEach(async ({ request }) => {
    await clearOverride(request);
  });

  test("apply manual phase override moves image to POST group + reload persists", async ({
    page,
    request,
  }) => {
    await clearOverride(request); // ensure clean baseline
    await page.goto(`/cases/${CASE_ID}`);
    const target = page.locator(`.thumb[data-source-file="${TARGET}"]`);
    await target.scrollIntoViewIfNeeded();
    await expect(target).toBeVisible();

    // Open popover
    await target.locator('[data-testid="thumb-edit-btn"]').click({ force: true });
    const pop = page.locator('[data-testid="image-override-popover"]');
    await expect(pop).toBeVisible();

    // Click 术后 (after)
    await pop.locator('[data-testid="override-phase-after"]').click({ force: true });
    // Mutation invalidates query → wait for re-fetch
    await expect(target).toHaveAttribute("data-manual", "1");
    await expect(target.locator('[data-testid="phase-manual-marker"]')).toBeVisible();
    // Role badge should now be POST
    await expect(target.locator(".role")).toHaveClass(/role post/);

    // Hard reload — override must persist
    await page.reload();
    const reloaded = page.locator(`.thumb[data-source-file="${TARGET}"]`);
    await expect(reloaded).toHaveAttribute("data-manual", "1");
    await expect(reloaded.locator(".role")).toHaveClass(/role post/);
  });

  test("clear-all returns image to skill auto group", async ({ page, request }) => {
    // Pre-seed an override directly via API
    await request.patch(
      `/api/cases/${CASE_ID}/images/${encodeURIComponent(TARGET)}`,
      { data: { manual_phase: "before", manual_view: "side" } },
    );
    await page.goto(`/cases/${CASE_ID}`);
    const target = page.locator(`.thumb[data-source-file="${TARGET}"]`);
    await target.scrollIntoViewIfNeeded();
    await expect(target).toHaveAttribute("data-manual", "1");

    await target.locator('[data-testid="thumb-edit-btn"]').click({ force: true });
    const pop = page.locator('[data-testid="image-override-popover"]');
    await pop.locator('[data-testid="override-clear-all"]').click({ force: true });
    await expect(target).toHaveAttribute("data-manual", "0");
    // Reload + still cleared
    await page.reload();
    await expect(
      page.locator(`.thumb[data-source-file="${TARGET}"]`),
    ).toHaveAttribute("data-manual", "0");
  });

  test("invalid PATCH returns 400 (defense in depth)", async ({ request }) => {
    const resp = await request.patch(
      `/api/cases/${CASE_ID}/images/${encodeURIComponent(TARGET)}`,
      { data: { manual_phase: "operative" } },
    );
    expect(resp.status()).toBe(400);
  });
});
