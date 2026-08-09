import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const frontend = fileURLToPath(new URL("..", import.meta.url));
const origin = "http://127.0.0.1:4174";
const staticCapability = "s".repeat(43);
const apiCapability = "a".repeat(43);
const appUrl = `${origin}/_app/${staticCapability}/#token=${apiCapability}`;
const browserCandidates = [
  process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
  process.env.CHROME_PATH,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
].filter(Boolean);
const browserExecutable = browserCandidates.find(existsSync);

const session = {
  session_id: "browser-session",
  workspace: "/work/acme",
  title: "Ship navigation",
  mode: "default",
  context_mode: "compaction",
  created_at: "2026-08-09T04:00:00.000000+00:00",
  updated_at: "2026-08-09T04:00:00.000000+00:00",
  last_opened_at: "2026-08-09T04:00:00.000000+00:00",
  archived_at: null,
};

const config = {
  base_workspace: "/work/acme",
  default_mode: "default",
  default_context_mode: "compaction",
  modes: ["default", "acceptAll", "readOnly"],
  context_modes: ["compaction", "folding"],
};

function channel(value) {
  const normalized = value / 255;
  return normalized <= 0.04045
    ? normalized / 12.92
    : ((normalized + 0.055) / 1.055) ** 2.4;
}

function rgb(value) {
  const match = value.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
  assert(match, `Expected an rgb color, received ${value}`);
  return match.slice(1, 4).map(Number);
}

function contrast(first, second) {
  const luminance = (color) => {
    const [red, green, blue] = rgb(color).map(channel);
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
  };
  const values = [luminance(first), luminance(second)].sort((a, b) => b - a);
  return (values[0] + 0.05) / (values[1] + 0.05);
}

async function waitForServer() {
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(origin);
      if (response.ok) return;
    } catch {
      // The dev server has not bound yet.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("Timed out waiting for the Task 4 browser probe server.");
}

async function preparePage(context, width) {
  const page = await context.newPage();
  const diagnostics = [];
  page.on("console", (message) => {
    if (["error", "warning"].includes(message.type())) diagnostics.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => diagnostics.push(`page: ${error.message}`));
  page.on("requestfailed", (request) =>
    diagnostics.push(`request: ${request.method()} ${request.url()} ${request.failure()?.errorText}`),
  );
  page.on("response", (response) => {
    if (new URL(response.url()).pathname.startsWith("/api/")) {
      diagnostics.push(`response: ${response.status()} ${response.request().method()} ${response.url()}`);
    }
  });
  await page.setViewportSize({ width, height: 800 });
  await page.route("**/api/config", (route) => route.fulfill({ json: config }));
  await page.route("**/api/sessions", (route) => route.fulfill({ json: [session] }));
  await page.goto(appUrl);
  try {
    await page
      .getByRole("button", { name: /Ship navigation.*\/work\/acme/i })
      .waitFor({ timeout: 10_000 });
  } catch (error) {
    const body = (await page.locator("body").innerText()).slice(0, 1_000);
    throw new Error(
      `Task 4 browser fixture did not bootstrap. ${diagnostics.join(" | ")} | body: ${body}`,
      { cause: error },
    );
  }
  return page;
}

async function stopServer(serverProcess) {
  if (serverProcess.exitCode !== null || serverProcess.signalCode !== null) return;
  const exited = new Promise((resolve) => serverProcess.once("exit", resolve));
  const kill = (signal) => {
    try {
      if (process.platform === "win32") serverProcess.kill(signal);
      else process.kill(-serverProcess.pid, signal);
    } catch (error) {
      if (error.code !== "ESRCH") throw error;
    }
  };
  kill("SIGTERM");
  const timeout = new Promise((resolve) => setTimeout(resolve, 2_000, "timeout"));
  if ((await Promise.race([exited, timeout])) === "timeout") {
    kill("SIGKILL");
    await exited;
  }
}

async function sidebarWidth(page) {
  return page.getByRole("navigation", { name: "Sessions" }).evaluate((element) =>
    Number.parseFloat(getComputedStyle(element).width),
  );
}

const connectionLabels = {
  connecting: "Local service connecting",
  connected: "Local service connected",
  disconnected: "Local service disconnected",
};

async function verifyConnectionTreatment(page, state, theme) {
  await page.locator("html").evaluate((element, value) => {
    element.dataset.theme = value;
  }, theme);
  const sidebar = page.getByRole("navigation", { name: "Sessions" });
  const status = page.getByRole("status", { name: connectionLabels[state] });
  const icon = status.locator(`svg.lucide[data-connection-icon="${state}"]`);
  await icon.waitFor({ timeout: 3_000 });
  assert.equal(await icon.getAttribute("aria-hidden"), "true", `${state} icon must be decorative`);
  assert.equal(await icon.getAttribute("stroke"), "currentColor", `${state} must use Lucide color`);
  const box = await icon.boundingBox();
  assert(box && box.width >= 14 && box.height >= 14, `${state} icon must remain visible in the rail`);
  const [iconColor, background] = await Promise.all([
    icon.evaluate((element) => getComputedStyle(element).color),
    sidebar.evaluate((element) => getComputedStyle(element).backgroundColor),
  ]);
  assert(
    contrast(iconColor, background) >= 3,
    `${theme} ${state} icon must meet 3:1 against the sidebar`,
  );
  return icon.evaluate((element) =>
    Array.from(element.children, (child) => child.outerHTML).join(""),
  );
}

async function prepareDisconnectedPage(context, width) {
  const page = await context.newPage();
  await page.setViewportSize({ width, height: 800 });
  await page.goto(origin);
  await page.getByRole("status", { name: connectionLabels.disconnected }).waitFor();
  return page;
}

async function prepareConnectingPage(context, width) {
  const page = await context.newPage();
  let releasePlatform;
  const platformGate = new Promise((resolve) => {
    releasePlatform = resolve;
  });
  await page.setViewportSize({ width, height: 800 });
  await page.route("**/src/platform/index.ts", async (route) => {
    await platformGate;
    await route.continue();
  });
  await page.goto(appUrl);
  await page.getByRole("status", { name: connectionLabels.connecting }).waitFor();
  return {
    page,
    release: async () => {
      releasePlatform();
      await page.unrouteAll({ behavior: "ignoreErrors" });
    },
  };
}

async function verifyContrast(page, theme) {
  await page.locator("html").evaluate((element, value) => {
    element.dataset.theme = value;
  }, theme);
  const sidebar = page.getByRole("navigation", { name: "Sessions" });
  const workspace = page
    .getByRole("button", { name: /Ship navigation.*\/work\/acme/i })
    .getByText("acme", { exact: true });
  const muted = await workspace.evaluate((element) => getComputedStyle(element).color);
  const sidebarBackground = await sidebar.evaluate((element) => getComputedStyle(element).backgroundColor);
  assert(
    contrast(muted, sidebarBackground) >= 4.5,
    `${theme} 11px muted copy must meet 4.5:1`,
  );

  const search = page.getByRole("button", { name: "Search sessions" });
  await search.focus();
  const shadow = await search.evaluate((element) => getComputedStyle(element).boxShadow);
  const ringMatch = shadow.match(/rgba?\([^)]*\)/);
  assert(ringMatch, `${theme} focus must expose a computed ring color`);
  assert(
    contrast(ringMatch[0], sidebarBackground) >= 3,
    `${theme} focus ring must meet 3:1 against the sidebar`,
  );

  await page.getByRole("button", { name: "Permission mode: Default" }).click();
  await page.getByRole("menuitemradio", { name: "Accept all" }).click();
  const danger = page.getByRole("button", { name: "Enable accept all" });
  const colors = await danger.evaluate((element) => {
    const style = getComputedStyle(element);
    return [style.color, style.backgroundColor];
  });
  assert(contrast(colors[0], colors[1]) >= 4.5, `${theme} danger button must meet 4.5:1`);
  await page.keyboard.press("Escape");
}

const server = spawn(
  process.platform === "win32" ? "npm.cmd" : "npm",
  [
    "run",
    "dev",
    "--",
    "--base",
    "/",
    "--host",
    "127.0.0.1",
    "--port",
    "4174",
    "--strictPort",
  ],
  {
    cwd: frontend,
    detached: process.platform !== "win32",
    stdio: ["ignore", "pipe", "pipe"],
  },
);

let browser;
try {
  await waitForServer();
  browser = await chromium.launch({
    headless: true,
    ...(browserExecutable ? { executablePath: browserExecutable } : {}),
  });

  const wideContext = await browser.newContext();
  await wideContext.addInitScript(() => localStorage.clear());
  const wide = await preparePage(wideContext, 1100);
  assert.equal(await sidebarWidth(wide), 224, "1100px layout must use the expanded sidebar");
  await wide.getByRole("button", { name: "Settings" }).waitFor();
  await wide.getByRole("status", { name: "Local service connected" }).waitFor();
  await assert.doesNotReject(() => verifyContrast(wide, "light"));
  await assert.doesNotReject(() => verifyContrast(wide, "dark"));
  await wide.locator("html").evaluate((element) => {
    element.dataset.theme = "light";
  });

  await wide.getByRole("button", { name: "Collapse sidebar" }).click();
  assert.equal(await sidebarWidth(wide), 56, "manual collapse must use the 56px rail");
  await wide.getByRole("button", { name: "Settings" }).waitFor();
  const connection = wide.getByRole("status", { name: "Local service connected" });
  assert((await connection.boundingBox())?.width, "connection status must remain visible in the rail");
  const sessionButton = wide.getByRole("button", { name: /Ship navigation.*\/work\/acme/i });
  await sessionButton.focus();
  const focusedIdentity = wide.getByRole("tooltip");
  await focusedIdentity.waitFor({ timeout: 3_000 });
  assert.match(await focusedIdentity.innerText(), /Ship navigation.*\/work\/acme/is);
  const connectedIcon = await verifyConnectionTreatment(wide, "connected", "light");
  assert.equal(
    await verifyConnectionTreatment(wide, "connected", "dark"),
    connectedIcon,
    "connected icon geometry must remain stable across themes",
  );
  await wide.locator("html").evaluate((element) => {
    element.dataset.theme = "light";
  });
  const more = wide.getByRole("button", { name: "More actions for Ship navigation" });
  await more.click();
  await wide.getByRole("menuitem", { name: "Rename" }).click();
  const renameDialog = wide.getByRole("dialog", { name: "Rename Ship navigation" });
  const renameBox = await renameDialog.boundingBox();
  assert(renameBox && renameBox.x >= 56, "rename UI must not be clipped inside the rail");
  await wide.keyboard.press("Escape");
  await wide.waitForFunction(
    () => document.activeElement?.getAttribute("aria-label") === "More actions for Ship navigation",
  );
  await wideContext.close();

  const narrowContext = await browser.newContext();
  await narrowContext.addInitScript(() => localStorage.clear());
  const narrow = await preparePage(narrowContext, 900);
  assert.equal(await sidebarWidth(narrow), 56, "900px layout must collapse automatically");
  assert.equal(
    await narrow.evaluate(() => localStorage.getItem("harness.sidebar.collapsed")),
    null,
    "automatic collapse must not overwrite the manual preference",
  );
  await narrow.getByRole("button", { name: "Settings" }).waitFor();
  assert(
    (await narrow.getByRole("status", { name: "Local service connected" }).boundingBox())?.width,
    "connection status must remain visible in the automatic rail",
  );
  const narrowSession = narrow.getByRole("button", { name: /Ship navigation.*\/work\/acme/i });
  await narrowSession.hover();
  const hoveredIdentity = narrow.getByRole("tooltip");
  await hoveredIdentity.waitFor({ timeout: 3_000 });
  assert.match(await hoveredIdentity.innerText(), /Ship navigation.*\/work\/acme/is);
  await narrowContext.close();

  const connectingContext = await browser.newContext();
  await connectingContext.addInitScript(() => localStorage.clear());
  const connecting = await prepareConnectingPage(connectingContext, 900);
  const connectingIcon = await verifyConnectionTreatment(connecting.page, "connecting", "light");
  assert.equal(
    await verifyConnectionTreatment(connecting.page, "connecting", "dark"),
    connectingIcon,
    "connecting icon geometry must remain stable across themes",
  );
  await connecting.release();
  await connectingContext.close();

  const disconnectedContext = await browser.newContext();
  await disconnectedContext.addInitScript(() => localStorage.clear());
  const disconnected = await prepareDisconnectedPage(disconnectedContext, 900);
  const disconnectedIcon = await verifyConnectionTreatment(disconnected, "disconnected", "light");
  assert.equal(
    await verifyConnectionTreatment(disconnected, "disconnected", "dark"),
    disconnectedIcon,
    "disconnected icon geometry must remain stable across themes",
  );
  assert.equal(
    new Set([connectingIcon, connectedIcon, disconnectedIcon]).size,
    3,
    "connection states must use three distinct Lucide silhouettes",
  );
  await disconnectedContext.close();
  console.log("Task 4 browser probe passed at 1100px and 900px.");
} finally {
  await browser?.close();
  await stopServer(server);
}
