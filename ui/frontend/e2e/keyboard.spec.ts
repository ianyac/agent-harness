import { expect, test } from "./fixtures";

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

test("tab order enters sidebar before header and composer and destructive keys are not global", async ({ page, authority }) => {
  await page.goto(authority.entryPath);
  await expect.poll(authority.socketConnections).toBe(1);
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
  await expect(page.getByRole("button", { name: "Permission mode: Default" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Activity" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("textbox", { name: "Message" })).toBeFocused();

  await page.keyboard.press("a");
  await page.keyboard.press("d");
  await page.keyboard.press("s");
  expect(authority.outbound()).toHaveLength(0);
  await expect(page.getByRole("textbox", { name: "Message" })).toHaveValue("ads");
});
