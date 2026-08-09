import { defineConfig, devices } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const offlineChromiumRevision = "1223";
const offlineChromiumVersion = "Google Chrome for Testing 148.0.7778.96";

function pinnedOfflineChromium(): string {
  const executable = join(
    homedir(),
    "Library",
    "Caches",
    "ms-playwright",
    `chromium_headless_shell-${offlineChromiumRevision}`,
    "chrome-headless-shell-mac-arm64",
    "chrome-headless-shell",
  );
  if (!existsSync(executable)) {
    throw new Error(`Required offline Chromium revision ${offlineChromiumRevision} is not installed at ${executable}`);
  }
  const actualVersion = execFileSync(executable, ["--version"], { encoding: "utf8" }).trim();
  if (actualVersion !== offlineChromiumVersion) {
    throw new Error(`Offline Chromium revision ${offlineChromiumRevision} reported unexpected version: ${actualVersion}`);
  }
  return executable;
}

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 15_000,
  expect: { timeout: 3_000 },
  outputDir: "test-results",
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [
    {
      name: "bundled-chromium",
      use: {
        ...devices["Desktop Chrome"],
        browserName: "chromium",
        launchOptions: { executablePath: pinnedOfflineChromium() },
      },
    },
  ],
  webServer: {
    command: "npm run build && npx vite preview --host 127.0.0.1 --port 4173 --strictPort",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
