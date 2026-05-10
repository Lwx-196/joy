import { defineConfig, devices } from "@playwright/test";

const BACKEND_URL = process.env.API_PROXY_TARGET || process.env.VITE_API_PROXY_TARGET || "http://127.0.0.1:5291";
const FRONTEND_URL = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:5292";
const FRONTEND_PORT = new URL(FRONTEND_URL).port || "5292";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: [
    ["html", { open: "never" }],
    ["list"],
  ],
  timeout: 30_000,
  expect: { timeout: 5_000 },

  use: {
    baseURL: FRONTEND_URL,
    trace: "retain-on-failure",
    video: "retain-on-failure",
    screenshot: "only-on-failure",
    // Force zh-CN locale so existing specs that use Chinese text selectors (e.g.
    // getByPlaceholder("搜索…")) keep working after wave-1 i18n extraction.
    // Long-term fix is N14 hardening: switch all interactive elements to data-testid.
    locale: "zh-CN",
    extraHTTPHeaders: { "Accept-Language": "zh-CN,zh;q=0.9" },
  },

  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],

  webServer: [
    {
      command: process.env.CI
        ? `python -m uvicorn backend.main:app --host 127.0.0.1 --port ${new URL(BACKEND_URL).port || "5291"}`
        : `PORT=${new URL(BACKEND_URL).port || "5291"} ./start.sh`,
      cwd: "..",
      url: `${BACKEND_URL}/openapi.json`,
      reuseExistingServer: true,
      timeout: 30_000,
      stdout: "ignore",
      stderr: "pipe",
    },
    {
      command: `VITE_API_PROXY_TARGET=${BACKEND_URL} npm run dev -- --host 127.0.0.1 --port ${FRONTEND_PORT}`,
      url: FRONTEND_URL,
      reuseExistingServer: true,
      timeout: 30_000,
      stdout: "ignore",
      stderr: "pipe",
    },
  ],
});
