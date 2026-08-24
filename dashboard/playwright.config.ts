import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  use: {
    baseURL: "http://127.0.0.1:8931",
    channel: "chrome",
    headless: true,
  },
  webServer: {
    command: "../.venv/bin/python e2e/seed_server.py --port 8931",
    url: "http://127.0.0.1:8931/api/system/status",
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
