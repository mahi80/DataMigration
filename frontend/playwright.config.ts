import { defineConfig, devices } from "@playwright/test";
import { existsSync } from "node:fs";

const port = 3137;
const apiPort = 8137;
const baseURL = `http://127.0.0.1:${port}`;
const python = process.env.MIGRATION_E2E_PYTHON || (existsSync("../.venv/bin/python") ? "../.venv/bin/python" : "python");

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [["list"], ["html", { open: "never" }]],
  use: { baseURL, trace: "retain-on-failure", screenshot: { mode: "only-on-failure", fullPage: true } },
  projects: [{
    name: "chromium",
    use: { ...devices["Desktop Chrome"], launchOptions: {
      ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}),
    } },
  }],
  webServer: [
    {
      command: `${python} ../examples/console/serve_demo.py --api-only --e2e --api-port ${apiPort}`,
      url: `http://127.0.0.1:${apiPort}/health`,
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: `npm run dev -- --port ${port}`,
      url: `${baseURL}/api/health`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: {
        NEXT_TELEMETRY_DISABLED: "1",
        MIGRATION_API_URL: `http://127.0.0.1:${apiPort}`,
        MIGRATION_UI_ORIGIN: baseURL,
        MIGRATION_UI_SESSION_SECRET: "23".repeat(32),
      },
    },
  ],
});
