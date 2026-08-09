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
        {...{ sessionId: "session-1" }}
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
        {...{ sessionId: "session-1" }}
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

  it("stages confirmation focus and returns it to the permission-mode control on Escape", async () => {
    const user = userEvent.setup();
    render(
      <ConversationHeader
        {...{ sessionId: "session-1" }}
        workspace="/work/agent-harness"
        branch={null}
        mode="default"
        onSetSessionMode={() => {}}
        onToggleActivity={() => {}}
      />,
    );
    const modeButton = screen.getByRole("button", { name: "Permission mode: Default" });
    await user.click(modeButton);
    await user.click(screen.getByRole("menuitemradio", { name: "Accept all" }));

    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Enable accept all?" })).not.toBeInTheDocument();
    expect(modeButton).toHaveFocus();
  });

  it("cancels the confirmation instead of retargeting it when the session prop changes", async () => {
    const user = userEvent.setup();
    const onSetSessionMode = vi.fn();
    const { rerender } = render(
      <ConversationHeader
        {...{ sessionId: "session-1" }}
        workspace="/work/one"
        branch={null}
        mode="default"
        onSetSessionMode={onSetSessionMode}
        onToggleActivity={() => {}}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Permission mode: Default" }));
    await user.click(screen.getByRole("menuitemradio", { name: "Accept all" }));

    rerender(
      <ConversationHeader
        {...{ sessionId: "session-2" }}
        workspace="/work/two"
        branch={null}
        mode="default"
        onSetSessionMode={onSetSessionMode}
        onToggleActivity={() => {}}
      />,
    );

    expect(screen.queryByRole("dialog", { name: "Enable accept all?" })).not.toBeInTheDocument();
    expect(onSetSessionMode).not.toHaveBeenCalled();
  });
});
