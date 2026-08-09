import { expect, test } from "./fixtures";

test("projects quiet socket reconnect, escalates at ten seconds, and self-heals on retry", async ({ page, authority }) => {
  await page.clock.install();
  await page.goto(authority.entryPath);
  await expect.poll(authority.socketConnections).toBe(1);
  authority.withholdNextSnapshots();
  authority.closeSocket();
  await expect(page.getByRole("status", { name: "Local service reconnecting" })).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page.clock.fastForward(10_000);
  await expect(page.getByRole("alert")).toContainText("Still reconnecting to the local service");
  await page.getByRole("button", { name: "Retry connection" }).click();
  await expect.poll(authority.socketConnections).toBe(3);
  await expect(page.getByRole("status", { name: "Local service reconnecting" })).toHaveCount(0);
});

test("new-generation snapshot removes stale stream and old generation events stay ignored", async ({ page, authority }) => {
  await page.clock.install();
  await page.goto(authority.entryPath);
  await expect.poll(authority.socketConnections).toBe(1);
  authority.emit({ type: "turn_started", turn_id: "stale-turn", mode: "base", submission_id: null });
  authority.emit({ type: "assistant_delta", turn_id: "stale-turn", text: "stale stream" });
  await expect(page.getByText("stale stream", { exact: true })).toBeVisible();
  authority.closeSocket();
  await page.clock.fastForward(1_000);
  await expect.poll(authority.socketConnections).toBe(2);
  await expect(page.getByText("stale stream", { exact: true })).toHaveCount(0);
  authority.sendRaw({
    type: "assistant_delta", session_id: "11111111-1111-4111-8111-111111111111",
    generation: 1, sequence: 99, turn_id: "stale-turn", text: "superseded stream",
  });
  await expect(page.getByText("superseded stream", { exact: true })).toHaveCount(0);
});

test("missing workspace preserves its category and offers honest archive recovery", async ({ page, authority }) => {
  await page.goto(authority.entryPath);
  await expect.poll(authority.socketConnections).toBe(1);
  authority.emit({ type: "turn_started", turn_id: "missing-turn", mode: "base", submission_id: null });
  authority.emit({
    type: "turn_failed", turn_id: "missing-turn", error_category: "missing_workspace",
    message: "fixture detail that must not render",
  });
  await expect(page.getByRole("heading", { name: "Workspace unavailable" })).toBeVisible();
  await expect(page.getByRole("alert")).not.toContainText("fixture detail");
  await page.getByRole("button", { name: "Archive session" }).click();
  await expect(page.getByRole("button", { name: /Fixture session/ })).toHaveCount(0);
});

test("retained superseded-session cleanup can be retried", async ({ page, authority }) => {
  authority.failNextDelete();
  await page.addInitScript(() => {
    localStorage.setItem(
      "agent-harness:superseded-session-cleanup:v1:22222222-2222-4222-8222-222222222222",
      JSON.stringify({ version: 1, sessionIds: ["stale-cleanup"] }),
    );
  });
  await page.goto(authority.entryPath);
  await expect(page.getByRole("heading", { name: "Cleanup needs another try" })).toBeVisible();
  await page.getByRole("button", { name: "Retry cleanup" }).click();
  await expect(page.getByRole("heading", { name: "Cleanup needs another try" })).toBeVisible();
  await page.getByRole("button", { name: "Retry cleanup" }).click();
  await expect(page.getByRole("heading", { name: "Cleanup needs another try" })).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "Message" })).toBeVisible();
});
