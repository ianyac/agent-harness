import type { Locator } from "@playwright/test";

import { expect, test } from "./fixtures";

async function expectVisibleFocus(locator: Locator) {
  await expect(locator).toBeFocused();
  expect(await locator.evaluate((element) => getComputedStyle(element).boxShadow)).not.toBe("none");
}

test("primary shortcuts stay scoped and restore their focus origins", async ({ page, authority }) => {
  await page.goto(authority.entryPath);
  await expect.poll(authority.socketConnections).toBe(1);
  const textbox = page.getByRole("textbox", { name: "Message" });
  await textbox.focus();
  await page.keyboard.press("Meta+k");
  await expect(page.getByRole("dialog", { name: "Command palette" })).toBeVisible();
  await expect(page.getByRole("searchbox", { name: "Search commands and sessions" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(textbox).toBeFocused();

  await page.keyboard.press("Meta+f");
  await expect(page.getByRole("searchbox", { name: "Search conversation" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(textbox).toBeFocused();

  await page.keyboard.press("Meta+Shift+i");
  await expect(page.getByRole("dialog", { name: "Activity inspector" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Close activity inspector" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(textbox).toBeFocused();
});

test("tab order traverses sidebar, header, transcript, composer, and keyboard-opened inspector with visible focus", async ({ page, authority }) => {
  await page.goto(authority.entryPath);
  await expect.poll(authority.socketConnections).toBe(1);
  authority.emit({ type: "turn_started", turn_id: "keyboard-turn", mode: "base", submission_id: null });
  authority.emit({
    type: "activity_completed", turn_id: "keyboard-turn", activity_id: "keyboard-activity",
    parent_activity_id: null, actor: "tool", name: "read_file", args: { path: "README.md" },
    result: "keyboard fixture", is_error: false, started_at: "2026-08-08T08:00:00Z", duration_ms: 12,
  });
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "New chat" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Search sessions" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: /^Fixture session, Workspace/ })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "More actions for Fixture session" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Settings" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Collapse sidebar" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expectVisibleFocus(page.getByRole("button", { name: "Permission mode: Default" }));
  await page.keyboard.press("Tab");
  await expectVisibleFocus(page.getByRole("button", { name: "Activity", exact: true }));
  await page.keyboard.press("Tab");
  const activity = page.getByRole("button", { name: /Open activity: read file/ });
  await expectVisibleFocus(activity);
  await page.keyboard.press("Tab");
  const textbox = page.getByRole("textbox", { name: "Message" });
  await expectVisibleFocus(textbox);

  await page.keyboard.press("a");
  await page.keyboard.press("d");
  await page.keyboard.press("s");
  expect(authority.outbound()).toHaveLength(0);
  await expect(textbox).toHaveValue("ads");

  await page.keyboard.press("Shift+Tab");
  await expectVisibleFocus(activity);
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog", { name: "Activity inspector" })).toBeVisible();
  await expectVisibleFocus(page.getByRole("button", { name: "Close activity inspector" }));
  await page.keyboard.press("Escape");
  await expectVisibleFocus(activity);
  await page.keyboard.press("Tab");
  await expectVisibleFocus(textbox);
  await page.keyboard.press("Meta+Shift+i");
  await expectVisibleFocus(page.getByRole("button", { name: "Close activity inspector" }));
  await page.keyboard.press("Escape");
  await expectVisibleFocus(textbox);
});
