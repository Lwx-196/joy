import { test } from "@playwright/test";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

/**
 * Capture a golden-path screenshot of OpsConsole to docs/operations/screenshots.
 * Used by Phase C3.0.2 exit criterion: "Dashboard at least 1 usable... screenshot
 * 存档". Run manually via:
 *   npx playwright test tests/e2e/ops-console-screenshot.spec.ts --headed --update-snapshots
 */

test("capture ops console golden-path screenshot @snapshot", async ({ page }) => {
  await page.goto("/ops");
  // networkidle never settles because useOpsStatus refetches every 30s.
  // Wait on the grid testid instead — it appears once the first fetch lands.
  await page.getByTestId("ops-console-grid").waitFor({ state: "visible" });
  await page.screenshot({
    path: path.join(
      __dirname,
      "../../../docs/operations/screenshots/ops-console-empty-db.png"
    ),
    fullPage: true,
  });
});
