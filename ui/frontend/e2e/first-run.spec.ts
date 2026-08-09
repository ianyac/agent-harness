import { expect, test } from "./fixtures";

test("recovers the service check after the quiet ten-second window", async ({ page, authority }) => {
  await page.clock.install();
  authority.failNextHealthCheck();
  authority.setSessions([]);
  await page.goto(authority.entryPath);
  await expect(page.getByRole("status", { name: "Local service reconnecting" })).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page.clock.fastForward(10_000);
  await expect(page.getByRole("alert")).toContainText("Still reconnecting to the local service");
  await page.getByRole("button", { name: "Retry connection" }).click();
  await expect(page.getByRole("heading", { name: "Start locally" })).toBeVisible();
});

test("validates an absolute workspace then retries the exact credential prerequisite", async ({ page, authority }) => {
  authority.setSessions([]);
  authority.failNextCreateWithCredentialPrerequisite();
  await page.goto(authority.entryPath);
  const workspace = page.getByRole("textbox", { name: "Workspace path" });
  await expect(page.getByRole("heading", { name: "Start locally" })).toBeVisible();
  await workspace.fill("relative/workspace");
  await page.getByRole("button", { name: "Start local session" }).click();
  await expect(page.getByRole("alert")).toHaveText("Enter an absolute workspace path without surrounding whitespace.");
  expect(authority.createRequests()).toHaveLength(0);

  await workspace.fill("/fixtures/workspace");
  await page.getByRole("button", { name: "Start local session" }).click();
  await expect(page.getByRole("heading", { name: "Sign in required" })).toBeVisible();
  await expect(page.getByText("codex login", { exact: true })).toBeVisible();
  expect(authority.createRequests()).toEqual([{
    workspace: "/fixtures/workspace", mode: "default", context_mode: "compaction", title: "New chat",
  }]);
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByRole("button", { name: "Fixture session, Workspace /fixtures/workspace" })).toBeVisible();
  expect(authority.createRequests()).toHaveLength(2);
  expect(authority.createRequests()[1]).toEqual(authority.createRequests()[0]);
  await expect.poll(authority.socketConnections).toBe(1);
});
