import { expect, test, type FixtureAuthority } from "./fixtures";

async function startTurn(page: Parameters<Parameters<typeof test>[1]>[0]["page"], authority: FixtureAuthority) {
  await page.goto(authority.entryPath);
  await expect.poll(authority.socketConnections).toBe(1);
  const textbox = page.getByRole("textbox", { name: "Message" });
  await textbox.fill("Needs a decision");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect.poll(() => authority.outbound().length).toBe(1);
  authority.emit({
    type: "turn_started",
    turn_id: "decision-turn",
    mode: "base",
    submission_id: authority.outbound()[0]?.submission_id,
  });
  await textbox.focus();
  return textbox;
}

test("permission uses the exact request id and restores focus only after authoritative resolution", async ({ page, authority }) => {
  const textbox = await startTurn(page, authority);
  authority.emit({
    type: "permission_requested", turn_id: "decision-turn", request_id: "permission-exact",
    action: "write_file", scope: "{\"path\":\"README.md\"}", reason: "Update the local fixture file",
  });
  const card = page.getByRole("group", { name: "Permission decision" });
  await expect(card).toBeFocused();
  await page.getByRole("button", { name: /Allow once/ }).click();
  await expect.poll(() => authority.outbound().length).toBe(2);
  expect(authority.outbound()[1]).toEqual({
    type: "answer_permission", request_id: "permission-exact", answer: "yes",
  });
  await expect(card).toBeFocused();
  authority.emit({
    type: "permission_resolved", turn_id: "decision-turn", request_id: "permission-exact", answer: "yes",
  });
  await expect(textbox).toBeFocused();
});

test("plan approval waits for authority and revision sends scoped feedback", async ({ page, authority }) => {
  const textbox = await startTurn(page, authority);
  authority.emit({
    type: "plan_approval_requested", turn_id: "decision-turn", request_id: "plan-exact",
    plan: "1. Inspect\n2. Update",
  });
  const card = page.getByRole("group", { name: "Plan review" });
  await expect(card).toBeFocused();
  await page.getByRole("button", { name: "Approve plan" }).click();
  await expect.poll(() => authority.outbound().length).toBe(2);
  expect(authority.outbound()[1]).toEqual({ type: "answer_plan", request_id: "plan-exact", approved: true });
  await expect(card).toBeFocused();
  authority.emit({
    type: "plan_approval_resolved", turn_id: "decision-turn", request_id: "plan-exact",
    approved: true, feedback: "",
  });
  await expect(textbox).toBeFocused();

  authority.emit({
    type: "plan_approval_requested", turn_id: "decision-turn", request_id: "plan-revise",
    plan: "1. Risky update",
  });
  await page.getByRole("button", { name: "Revise plan" }).click();
  await page.getByRole("textbox", { name: "Revision feedback (optional)" }).fill("Keep the local boundary");
  await page.getByRole("button", { name: "Send revision" }).click();
  await expect.poll(() => authority.outbound().length).toBe(3);
  expect(authority.outbound()[2]).toEqual({
    type: "answer_plan", request_id: "plan-revise", approved: false, feedback: "Keep the local boundary",
  });
});
