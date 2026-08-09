import { readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, test } from "./fixtures";

const axeSource = readFileSync(join(process.cwd(), "node_modules/axe-core/axe.min.js"), "utf8");

async function assertAxe(page: Parameters<Parameters<typeof test>[1]>[0]["page"]) {
  await page.addScriptTag({ content: axeSource });
  const violations = await page.evaluate(async () => {
    const axe = (window as unknown as { axe: { run: () => Promise<{ violations: Array<{ id: string; impact: string | null; nodes: Array<{ target: unknown; failureSummary: string }> }> }> } }).axe;
    return (await axe.run()).violations.map(({ id, impact, nodes }) => ({
      id,
      impact,
      nodes: nodes.map(({ target, failureSummary }) => ({ target, failureSummary })),
    }));
  });
  expect(violations).toEqual([]);
}

for (const width of [1440, 1100, 900, 720]) {
  test(`responsive shell remains functional at ${width}px`, async ({ page, authority }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(authority.entryPath);
    await expect.poll(authority.socketConnections).toBe(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    const sidebar = await page.getByRole("navigation", { name: "Sessions" }).boundingBox();
    expect(sidebar).not.toBeNull();
    if (width < 1000) expect(sidebar!.width).toBeLessThanOrEqual(60);
    else expect(sidebar!.width).toBeGreaterThanOrEqual(200);
    await expect(page.getByRole("button", { name: "New chat" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Search sessions" })).toBeVisible();
    await expect(page.getByRole("button", { name: /^Fixture session, Workspace/ })).toBeVisible();
    await expect(page.getByRole("button", { name: "Settings" })).toBeVisible();
    const textbox = page.getByRole("textbox", { name: "Message" });
    await expect(textbox).toBeInViewport();
    await textbox.fill("Target size evidence");
    const send = await page.getByRole("button", { name: "Send message" }).boundingBox();
    expect(send).not.toBeNull();
    expect(send!.height).toBeGreaterThanOrEqual(width === 720 ? 44 : 32);
    const mode = await page.getByRole("button", { name: "Base mode" }).boundingBox();
    expect(mode!.height).toBeGreaterThanOrEqual(32);
    if (width >= 1000) {
      const more = await page.getByRole("button", { name: "More actions for Fixture session" }).boundingBox();
      expect(more!.height).toBeGreaterThanOrEqual(32);
    }
    await expect(page.getByRole("dialog", { name: "Activity inspector" })).toHaveCount(0);
    await page.getByRole("button", { name: "Activity" }).click();
    const inspector = page.getByRole("dialog", { name: "Activity inspector" });
    const box = await inspector.boundingBox();
    expect(box).not.toBeNull();
    if (width < 800) {
      expect(box!.width).toBeGreaterThan(width - 32);
      expect(box!.y).toBeGreaterThan(100);
    } else {
      expect(box!.x).toBeGreaterThan(width / 2);
    }
  });
}

test("wide light and narrow dark states have no axe violations", async ({ page, authority }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(authority.entryPath);
  await expect.poll(authority.socketConnections).toBe(1);
  await assertAxe(page);
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByRole("radio", { name: "Dark" }).click();
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 720, height: 900 });
  await assertAxe(page);
});

test("permission, error, inspector, first-run, and increased-contrast states have no axe violations", async ({ page, authority }) => {
  await page.emulateMedia({ contrast: "more" });
  await page.goto(authority.entryPath);
  await expect.poll(authority.socketConnections).toBe(1);
  authority.emit({ type: "turn_started", turn_id: "axe-turn", mode: "base", submission_id: null });
  authority.emit({
    type: "permission_requested", turn_id: "axe-turn", request_id: "axe-permission",
    action: "write_file", scope: "{\"path\":\"README.md\"}", reason: "Fixture permission",
  });
  await expect(page.getByRole("group", { name: "Permission decision" })).toBeVisible();
  await assertAxe(page);
  authority.emit({
    type: "permission_resolved", turn_id: "axe-turn", request_id: "axe-permission", answer: "no",
  });
  authority.emit({
    type: "activity_completed", turn_id: "axe-turn", activity_id: "axe-activity", parent_activity_id: null,
    actor: "tool", name: "write_file", args: { path: "README.md" }, result: "fixture failure",
    is_error: true, started_at: "2026-08-08T08:00:00Z", duration_ms: 18,
  });
  await page.getByRole("button", { name: /Open activity: write file/ }).click();
  await assertAxe(page);
  await page.getByRole("button", { name: "Close activity inspector" }).click();
  authority.emit({
    type: "turn_failed", turn_id: "axe-turn", error_category: "turn_failure", message: "fixture detail",
  });
  await expect(page.getByRole("heading", { name: "Turn stopped" })).toBeVisible();
  await assertAxe(page);
});

test("first-run state has no axe violations", async ({ page, authority }) => {
  authority.setSessions([]);
  await page.goto(authority.entryPath);
  await expect(page.getByRole("heading", { name: "Start locally" })).toBeVisible();
  await assertAxe(page);
});

test("reduced motion removes interface transitions", async ({ page, authority }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(authority.entryPath);
  await expect.poll(authority.socketConnections).toBe(1);
  const durations = await page.evaluate(() => [...document.querySelectorAll("*")]
    .map((element) => getComputedStyle(element).transitionDuration)
    .filter((duration) => duration !== "0s" && duration !== "0.001ms"));
  expect(durations).toEqual([]);
});

test("approved deterministic visual states", async ({ page, authority }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(authority.entryPath);
  await expect.poll(authority.socketConnections).toBe(1);
  await expect.soft(page).toHaveScreenshot("primary-1440-light.png", { animations: "disabled", caret: "hide" });

  authority.emit({ type: "turn_started", turn_id: "visual-turn", mode: "base", submission_id: null });
  authority.emit({ type: "assistant_delta", turn_id: "visual-turn", text: "Streaming fixture response…" });
  await page.setViewportSize({ width: 1100, height: 900 });
  await expect.soft(page).toHaveScreenshot("streaming-1100.png", { animations: "disabled", caret: "hide" });

  authority.emit({
    type: "permission_requested", turn_id: "visual-turn", request_id: "visual-permission",
    action: "write_file", scope: "{\"path\":\"README.md\"}", reason: "Update the local fixture",
  });
  await page.setViewportSize({ width: 900, height: 900 });
  await expect.soft(page).toHaveScreenshot("permission-900.png", { animations: "disabled", caret: "hide" });

  authority.emit({
    type: "permission_resolved", turn_id: "visual-turn", request_id: "visual-permission", answer: "no",
  });
  authority.emit({
    type: "activity_completed", turn_id: "visual-turn", activity_id: "visual-error", parent_activity_id: null,
    actor: "tool", name: "write_file", args: { path: "README.md" }, result: "Fixture tool failure",
    is_error: true, started_at: "2026-08-08T08:00:00Z", duration_ms: 21,
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole("button", { name: /Open activity: write file/ }).click();
  await expect.soft(page).toHaveScreenshot("tool-failure-inspector-1440.png", { animations: "disabled", caret: "hide" });
});

test("approved dark, reconnect, first-run, and reduced-motion visuals", async ({ page, authority }) => {
  await page.setViewportSize({ width: 720, height: 900 });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto(authority.entryPath);
  await expect.poll(authority.socketConnections).toBe(1);
  await expect.soft(page).toHaveScreenshot("primary-720-dark.png", { animations: "disabled", caret: "hide" });
  authority.withholdNextSnapshots();
  authority.closeSocket();
  await expect(page.getByRole("status", { name: "Local service reconnecting" })).toBeVisible();
  await page.setViewportSize({ width: 1100, height: 900 });
  await expect.soft(page).toHaveScreenshot("reconnect-1100.png", { animations: "disabled", caret: "hide" });
});

test("approved first-run and reduced-motion visuals", async ({ page, authority }) => {
  authority.setSessions([]);
  await page.setViewportSize({ width: 720, height: 900 });
  await page.goto(authority.entryPath);
  await expect(page.getByRole("heading", { name: "Start locally" })).toBeVisible();
  await expect.soft(page).toHaveScreenshot("first-run-720.png", { animations: "disabled", caret: "hide" });

  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: 900, height: 900 });
  await expect.soft(page).toHaveScreenshot("reduced-motion-900.png", { animations: "disabled", caret: "hide" });
});
