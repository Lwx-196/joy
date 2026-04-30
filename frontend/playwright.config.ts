import { defineConfig, devices } from "@playwright/test";

const BACKEND_URL = "http://127.0.0.1:5174";
const FRONTEND_URL = "http://127.0.0.1:5175";

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
  },

  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],

  webServer: [
    {
      command: process.env.CI
        ? "python -m uvicorn backend.main:app --host 127.0.0.1 --port 5174"
        : "./start.sh",
      cwd: "..",
      url: `${BACKEND_URL}/openapi.json`,
      reuseExistingServer: true,
      timeout: 30_000,
      stdout: "ignore",
      stderr: "pipe",
    },
    {
      command: "npm run dev",
      url: FRONTEND_URL,
      reuseExistingServer: true,
      timeout: 30_000,
      stdout: "ignore",
      stderr: "pipe",
    },
  ],
});
