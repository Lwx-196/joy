import { test, expect } from "@playwright/test";

/**
 * Stage B: 单张源图 phase / view 手动覆盖。
 *
 * Uses a real current source image on case 126. The fixture/live DB has drifted
 * over time, so the test selects an existing auto-classified before image from
 * the API instead of pinning to a historical filename.
 *
 * Each test cleans its own override in afterEach so the DB is restored
 * (other suites assume read-only against case-workbench.db).
 */

const CASE_ID = 126;

async function pickTargetImage(request: import("@playwright/test").APIRequestContext) {
  const resp = await request.get(`/api/cases/${CASE_ID}`);
  expect(resp.ok()).toBeTruthy();
  const detail = await resp.json();
  const imageFiles: string[] = detail.meta?.image_files ?? [];
  const metadata = new Map<string, Record<string, unknown>>(
    (detail.skill_image_metadata ?? [])
      .filter((item: { filename?: string | null }) => item.filename)
      .map((item: { filename: string }) => [item.filename, item])
  );
  const target = imageFiles.find((filename) => {
    const meta = metadata.get(filename);
    return (
      meta?.phase === "before" &&
      meta?.phase_override_source !== "manual" &&
      meta?.view_override_source !== "manual"
    );
  });
  expect(target, "case 126 should have a real auto-classified before image").toBeTruthy();
  return target as string;
}

function sourceThumb(page: import("@playwright/test").Page, filename: string) {
  return page.locator(`.thumb[data-source-file="${filename.replace(/"/g, '\\"')}"]`);
}

async function clearOverride(request: import("@playwright/test").APIRequestContext, filename: string) {
  // PATCH with empty strings drops both dimensions and deletes the row.
  await request.patch(
    `/api/cases/${CASE_ID}/images/${encodeURIComponent(filename)}`,
    { data: { manual_phase: "", manual_view: "" } },
  );
}

test.describe("source-image-override", () => {
  test("apply manual phase override moves image to POST group + reload persists", async ({
    page,
    request,
  }) => {
    const targetImage = await pickTargetImage(request);
    await clearOverride(request, targetImage); // ensure clean baseline
    await page.goto(`/cases/${CASE_ID}`);
    const target = sourceThumb(page, targetImage);
    await target.scrollIntoViewIfNeeded();
    await expect(target).toBeVisible();

    try {
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
      const reloaded = sourceThumb(page, targetImage);
      await expect(reloaded).toHaveAttribute("data-manual", "1");
      await expect(reloaded.locator(".role")).toHaveClass(/role post/);
    } finally {
      await clearOverride(request, targetImage);
    }
  });

  test("clear-all returns image to skill auto group", async ({ page, request }) => {
    const targetImage = await pickTargetImage(request);
    // Pre-seed an override directly via API
    await request.patch(
      `/api/cases/${CASE_ID}/images/${encodeURIComponent(targetImage)}`,
      { data: { manual_phase: "before", manual_view: "side" } },
    );
    await page.goto(`/cases/${CASE_ID}`);
    const target = sourceThumb(page, targetImage);

    try {
      await target.scrollIntoViewIfNeeded();
      await expect(target).toHaveAttribute("data-manual", "1");

      await target.locator('[data-testid="thumb-edit-btn"]').click({ force: true });
      const pop = page.locator('[data-testid="image-override-popover"]');
      await pop.locator('[data-testid="override-clear-all"]').click({ force: true });
      await expect(target).toHaveAttribute("data-manual", "0");
      // Reload + still cleared
      await page.reload();
      await expect(sourceThumb(page, targetImage)).toHaveAttribute("data-manual", "0");
    } finally {
      await clearOverride(request, targetImage);
    }
  });

  test("invalid PATCH returns 400 (defense in depth)", async ({ request }) => {
    const targetImage = await pickTargetImage(request);
    const resp = await request.patch(
      `/api/cases/${CASE_ID}/images/${encodeURIComponent(targetImage)}`,
      { data: { manual_phase: "operative" } },
    );
    expect(resp.status()).toBe(400);
  });
});
