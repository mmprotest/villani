import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  snapshotPathTemplate: "{testDir}/__screenshots__/{testFileName}/{arg}{ext}",
  expect: {
    toHaveScreenshot: { maxDiffPixelRatio: 0.005 },
  },
  use: {
    baseURL: "http://127.0.0.1:4173",
    colorScheme: "light",
    locale: "en-AU",
    timezoneId: "Australia/Sydney",
    viewport: { width: 1280, height: 800 },
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1",
    port: 4173,
    reuseExistingServer: true,
  },
});
