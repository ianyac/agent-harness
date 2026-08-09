import { expect, sessionId, test, type FixtureAuthority } from "./fixtures";

async function openSession(page: Parameters<Parameters<typeof test>[1]>[0]["page"], authority: FixtureAuthority) {
  await page.goto(authority.entryPath);
  await expect(page).toHaveURL(/\/_app\/s{43}\/$/);
  await expect(page.getByRole("button", { name: "Fixture session, Workspace /fixtures/workspace" })).toBeVisible();
  await expect.poll(authority.socketConnections).toBe(1);
}

test("built app opens the active session WebSocket", async ({ page, authority }) => {
  await openSession(page, authority);
});

test("browser project runs the pinned offline Chromium build", async ({ browser }) => {
  expect(browser.version()).toBe("148.0.7778.96");
});

test("offline authority rejects missing and incorrect WebSocket capability protocols", async ({ page, authority }) => {
  const url = `ws://127.0.0.1:4173/ws/sessions/${sessionId}`;
  await page.goto("http://127.0.0.1:4173/");
  for (const protocols of [[], ["harness-ui", "wrong-capability"]]) {
    const outcome = await page.evaluate(({ socketUrl, requestedProtocols }) => new Promise<string>((resolve) => {
      const socket = new WebSocket(socketUrl, requestedProtocols);
      socket.addEventListener("open", () => resolve("opened"), { once: true });
      socket.addEventListener("close", (event) => resolve(`closed:${event.code}`), { once: true });
    }), { socketUrl: url, requestedProtocols: protocols });
    expect(outcome).not.toBe("opened");
    expect(authority.socketConnections()).toBe(0);
  }

  await openSession(page, authority);
});

test("sends, streams, groups activity, completes, and opens inspector detail", async ({ page, authority }) => {
  await openSession(page, authority);
  await page.getByRole("textbox", { name: "Message" }).fill("Inspect the workspace");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect.poll(() => authority.outbound().length).toBe(1);
  const submissionId = authority.outbound()[0]?.submission_id;
  expect(submissionId).toMatch(/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/);

  authority.emit({ type: "turn_started", turn_id: "turn-1", mode: "base", submission_id: submissionId });
  authority.emit({ type: "assistant_delta", turn_id: "turn-1", text: "Checking " });
  authority.emit({
    type: "activity_started", turn_id: "turn-1", activity_id: "activity-1",
    parent_activity_id: null, actor: "tool", name: "read_file", args: { path: "README.md" },
    started_at: "2026-08-08T08:00:00Z",
  });
  authority.emit({
    type: "activity_completed", turn_id: "turn-1", activity_id: "activity-1",
    parent_activity_id: null, actor: "tool", name: "read_file", args: { path: "README.md" },
    result: "fixture result", is_error: false, started_at: "2026-08-08T08:00:00Z", duration_ms: 24,
  });
  await expect(page.getByText("Checking", { exact: true })).toBeVisible();
  const activity = page.getByRole("button", { name: /Open activity: read file.*Complete.*1 action/ });
  await expect(activity).toBeVisible();
  await activity.click();
  await expect(page.getByRole("dialog", { name: "Activity inspector" })).toBeVisible();
  await expect(page.getByLabel("Selected activity detail").getByText("fixture result", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Close activity inspector" }).click();
  await expect(activity).toBeFocused();

  await page.evaluate(() => {
    const liveRegion = document.querySelector('[aria-label="Conversation update"]');
    if (liveRegion === null) throw new Error("Missing conversation update fixture region");
    const scope = globalThis as typeof globalThis & { __completionAnnouncements: string[] };
    scope.__completionAnnouncements = [];
    new MutationObserver(() => {
      const value = liveRegion.textContent?.trim() ?? "";
      if (value !== "") scope.__completionAnnouncements.push(value);
    }).observe(liveRegion, { childList: true, characterData: true, subtree: true });
  });

  authority.emit({
    type: "turn_completed", turn_id: "turn-1", final_text: "Workspace inspected.",
    messages: [
      { role: "user", content: "Inspect the workspace" },
      { role: "assistant", content: "Workspace inspected." },
    ],
  });
  await expect(page.getByText("Workspace inspected.", { exact: true })).toBeVisible();
  await expect(page.getByRole("status", { name: "Conversation update" })).toHaveText("Response complete");
  await expect.poll(() => page.evaluate(() => (
    globalThis as typeof globalThis & { __completionAnnouncements: string[] }
  ).__completionAnnouncements)).toEqual(["Response complete"]);
  authority.emit({ type: "safety_updated", turn_id: null, safety: { mode: "default" } });
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
  expect(await page.evaluate(() => (
    globalThis as typeof globalThis & { __completionAnnouncements: string[] }
  ).__completionAnnouncements)).toEqual(["Response complete"]);
});

test("queues, edits, clears, and stops without a false completion announcement", async ({ page, authority }) => {
  await openSession(page, authority);
  await page.getByRole("textbox", { name: "Message" }).fill("Long running task");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect.poll(() => authority.outbound().length).toBe(1);
  const submissionId = authority.outbound()[0]?.submission_id;
  authority.emit({ type: "turn_started", turn_id: "turn-stop", mode: "base", submission_id: submissionId });

  await page.getByRole("textbox", { name: "Message" }).fill("Clear without editing");
  await page.getByRole("button", { name: "Queue message" }).click();
  await expect(page.getByRole("status", { name: "Queued follow-up" })).toContainText("Clear without editing");
  await page.getByRole("button", { name: "Clear queued follow-up" }).click();
  expect(authority.outbound().at(-1)).toEqual({ type: "clear_queued_message" });
  await expect(page.getByRole("textbox", { name: "Message" })).toHaveValue("");

  await page.getByRole("textbox", { name: "Message" }).fill("Follow up safely");
  await page.getByRole("button", { name: "Queue message" }).click();
  await expect(page.getByRole("status", { name: "Queued follow-up" })).toContainText("Follow up safely");
  expect(authority.outbound().at(-1)).toMatchObject({ type: "queue_message", text: "Follow up safely" });
  await page.getByRole("button", { name: "Edit queued follow-up" }).click();
  await expect(page.getByRole("textbox", { name: "Message" })).toHaveValue("Follow up safely");
  expect(authority.outbound().at(-1)).toEqual({ type: "clear_queued_message" });

  await page.getByRole("button", { name: "Stop turn" }).click();
  expect(authority.outbound().at(-1)).toEqual({ type: "cancel_turn", turn_id: "turn-stop" });
  authority.emit({ type: "turn_stopping", turn_id: "turn-stop" });
  authority.emit({ type: "turn_cancelled", turn_id: "turn-stop" });
  await expect(page.getByRole("button", { name: "Send message" })).toBeVisible();
  await expect(page.getByRole("status", { name: "Conversation update" })).not.toHaveText("Response complete");
});

test("retries the exact failed turn with a fresh submission id", async ({ page, authority }) => {
  await openSession(page, authority);
  await page.getByRole("textbox", { name: "Message" }).fill("Retry this exact request");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect.poll(() => authority.outbound().length).toBe(1);
  const first = authority.outbound()[0];
  authority.emit({ type: "turn_started", turn_id: "turn-failed", mode: "base", submission_id: first?.submission_id });
  authority.emit({ type: "turn_failed", turn_id: "turn-failed", error_category: "turn_failure", message: "fixture failure" });
  await page.getByRole("button", { name: "Retry" }).click();
  await expect.poll(() => authority.outbound().length).toBe(2);
  expect(authority.outbound()[1]).toMatchObject({ type: "send_message", text: "Retry this exact request", mode: "base" });
  expect(authority.outbound()[1]?.submission_id).not.toBe(first?.submission_id);
  expect(sessionId).toBe("11111111-1111-4111-8111-111111111111");
});
