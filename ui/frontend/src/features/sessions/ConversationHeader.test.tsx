import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConversationHeader } from "./ConversationHeader";

afterEach(cleanup);

describe("ConversationHeader", () => {
  it("shows workspace, branch, and constructible base modes without plan mode", async () => {
    const user = userEvent.setup();
    const onSetSessionMode = vi.fn();
    render(
      <ConversationHeader
        workspace="/work/agent-harness"
        branch="ui/navigation"
        mode="default"
        onSetSessionMode={onSetSessionMode}
        onToggleActivity={() => {}}
      />,
    );

    expect(screen.getByText("agent-harness")).toBeVisible();
    expect(screen.getByText("ui/navigation")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Permission mode: Default" }));
    expect(screen.getByRole("menuitemradio", { name: "Default" })).toBeVisible();
    expect(screen.getByRole("menuitemradio", { name: "Accept all" })).toBeVisible();
    expect(screen.getByRole("menuitemradio", { name: "Read only" })).toBeVisible();
    expect(screen.queryByRole("menuitemradio", { name: /plan/i })).not.toBeInTheDocument();

    await user.click(screen.getByRole("menuitemradio", { name: "Read only" }));
    expect(onSetSessionMode).toHaveBeenCalledWith({
      type: "set_session_mode",
      mode: "readOnly",
    });
  });

  it("requires explicit confirmation before accept-all mode", async () => {
    const user = userEvent.setup();
    const onSetSessionMode = vi.fn();
    render(
      <ConversationHeader
        workspace="/work/agent-harness"
        branch={null}
        mode="default"
        onSetSessionMode={onSetSessionMode}
        onToggleActivity={() => {}}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Permission mode: Default" }));
    await user.click(screen.getByRole("menuitemradio", { name: "Accept all" }));

    expect(onSetSessionMode).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Enable accept all?" })).toHaveTextContent(
      "Mutating tools will run without per-call prompts for this session.",
    );
    await user.click(screen.getByRole("button", { name: "Enable accept all" }));
    expect(onSetSessionMode).toHaveBeenCalledWith({
      type: "set_session_mode",
      mode: "acceptAll",
    });
  });
});
