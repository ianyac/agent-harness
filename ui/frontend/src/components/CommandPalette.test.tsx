import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CommandPalette } from "./CommandPalette";
import type { SessionRecord } from "../features/sessions/useSessions";

afterEach(cleanup);

const sessions: SessionRecord[] = [
  {
    session_id: "session-api",
    workspace: "/work/payments-api",
    title: "Trace retries",
    mode: "default",
    context_mode: "compaction",
    created_at: "2026-08-09T04:00:00.000000+00:00",
    updated_at: "2026-08-09T04:00:00.000000+00:00",
    last_opened_at: "2026-08-09T04:00:00.000000+00:00",
    archived_at: null,
  },
  {
    session_id: "session-ui",
    workspace: "/work/dashboard-ui",
    title: "Polish navigation",
    mode: "readOnly",
    context_mode: "folding",
    created_at: "2026-08-08T04:00:00.000000+00:00",
    updated_at: "2026-08-08T04:00:00.000000+00:00",
    last_opened_at: "2026-08-08T04:00:00.000000+00:00",
    archived_at: null,
  },
];

function Harness(props: {
  onNewChat: () => void;
  onSelectSession?: (id: string) => void;
  onOpenSettings?: () => void;
  onToggleActivity?: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <CommandPalette
      open={open}
      onOpenChange={setOpen}
      sessions={sessions}
      onNewChat={props.onNewChat}
      onOpenSettings={props.onOpenSettings ?? (() => {})}
      onToggleActivity={props.onToggleActivity ?? (() => {})}
      onSelectSession={props.onSelectSession ?? (() => {})}
    />
  );
}

function TriggerHarness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>Search sessions</button>
      <CommandPalette
        open={open}
        onOpenChange={setOpen}
        sessions={sessions}
        onNewChat={() => {}}
        onOpenSettings={() => {}}
        onToggleActivity={() => {}}
        onSelectSession={() => {}}
      />
    </>
  );
}

describe("CommandPalette", () => {
  it("opens with Command+K and exposes stable commands with visible shortcuts", async () => {
    const user = userEvent.setup();
    render(<Harness onNewChat={() => {}} />);

    await user.keyboard("{Meta>}k{/Meta}");

    expect(screen.getByRole("dialog", { name: "Command palette" })).toBeVisible();
    expect(screen.getByRole("option", { name: /New chat.*⌘N/ })).toHaveAttribute(
      "data-command-id",
      "action:new-chat",
    );
    expect(screen.getByRole("option", { name: /Open settings/ })).toHaveAttribute(
      "data-command-id",
      "action:open-settings",
    );
    expect(screen.getByRole("option", { name: /Toggle activity.*⌘⇧I/ })).toHaveAttribute(
      "data-command-id",
      "action:toggle-activity",
    );
    expect(screen.getByRole("option", { name: /Trace retries.*payments-api/ })).toHaveAttribute(
      "data-command-id",
      "session:session-api",
    );
  });

  it("searches both title and workspace and navigates to the chosen session", async () => {
    const user = userEvent.setup();
    const onSelectSession = vi.fn();
    render(<Harness onNewChat={() => {}} onSelectSession={onSelectSession} />);
    await user.keyboard("{Meta>}k{/Meta}");

    const search = screen.getByRole("searchbox", { name: "Search commands and sessions" });
    await user.type(search, "Trace retries");
    expect(screen.getByRole("option", { name: /Trace retries.*payments-api/ })).toBeVisible();
    expect(screen.queryByRole("option", { name: /Polish navigation/ })).not.toBeInTheDocument();
    await user.clear(search);
    await user.type(search, "dashboard-ui");

    expect(screen.getByRole("option", { name: /Polish navigation.*dashboard-ui/ })).toBeVisible();
    expect(screen.queryByRole("option", { name: /Trace retries/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: /Polish navigation.*dashboard-ui/ }));
    expect(onSelectSession).toHaveBeenCalledWith("session-ui");
    expect(screen.queryByRole("dialog", { name: "Command palette" })).not.toBeInTheDocument();
  });

  it("creates a session with Command+N without opening the palette", async () => {
    const user = userEvent.setup();
    const onNewChat = vi.fn();
    render(<Harness onNewChat={onNewChat} />);

    await user.keyboard("{Meta>}n{/Meta}");

    expect(onNewChat).toHaveBeenCalledOnce();
    expect(screen.queryByRole("dialog", { name: "Command palette" })).not.toBeInTheDocument();
  });

  it("runs settings and activity commands from their stable results", async () => {
    const user = userEvent.setup();
    const onOpenSettings = vi.fn();
    const onToggleActivity = vi.fn();
    render(
      <Harness
        onNewChat={() => {}}
        onOpenSettings={onOpenSettings}
        onToggleActivity={onToggleActivity}
      />,
    );

    await user.keyboard("{Meta>}k{/Meta}");
    await user.click(screen.getByRole("option", { name: "Open settings" }));
    expect(onOpenSettings).toHaveBeenCalledOnce();

    await user.keyboard("{Meta>}k{/Meta}");
    await user.click(screen.getByRole("option", { name: /Toggle activity.*⌘⇧I/ }));
    expect(onToggleActivity).toHaveBeenCalledOnce();
  });

  it("supports listbox arrow navigation with wraparound and Home/End jumps", async () => {
    const user = userEvent.setup();
    render(<Harness onNewChat={() => {}} />);
    await user.keyboard("{Meta>}k{/Meta}");

    const input = screen.getByRole("searchbox", { name: "Search commands and sessions" });
    const options = screen.getAllByRole("option");
    expect(options).toHaveLength(5);
    expect(screen.getByRole("listbox", { name: "Commands and sessions" })).toBeVisible();
    expect(options[0]).toHaveAttribute("aria-selected", "true");
    expect(input).toHaveAttribute("aria-activedescendant", options[0].id);
    expect(input).toHaveAttribute("aria-controls", screen.getByRole("listbox", { name: "Commands and sessions" }).id);

    await user.keyboard("{ArrowDown}");
    expect(options[1]).toHaveAttribute("aria-selected", "true");
    expect(options[0]).toHaveAttribute("aria-selected", "false");
    expect(input).toHaveAttribute("aria-activedescendant", options[1].id);

    await user.keyboard("{ArrowUp}{ArrowUp}");
    expect(options[4]).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowDown}");
    expect(options[0]).toHaveAttribute("aria-selected", "true");

    await user.keyboard("{End}");
    expect(options[4]).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{Home}");
    expect(options[0]).toHaveAttribute("aria-selected", "true");
  });

  it("activates the active option with Enter", async () => {
    const user = userEvent.setup();
    const onSelectSession = vi.fn();
    render(<Harness onNewChat={() => {}} onSelectSession={onSelectSession} />);
    await user.keyboard("{Meta>}k{/Meta}");

    await user.keyboard("{ArrowDown}{ArrowDown}{ArrowDown}");
    expect(screen.getByRole("option", { name: /Trace retries.*payments-api/ }))
      .toHaveAttribute("aria-selected", "true");
    await user.keyboard("{Enter}");

    expect(onSelectSession).toHaveBeenCalledWith("session-api");
    expect(screen.queryByRole("dialog", { name: "Command palette" })).not.toBeInTheDocument();
  });

  it("defaults the active option to the first result whenever the query changes", async () => {
    const user = userEvent.setup();
    const onNewChat = vi.fn();
    render(<Harness onNewChat={onNewChat} />);
    await user.keyboard("{Meta>}k{/Meta}");

    await user.keyboard("{ArrowDown}{ArrowDown}");
    const input = screen.getByRole("searchbox", { name: "Search commands and sessions" });
    await user.type(input, "chat");

    const option = screen.getByRole("option", { name: /New chat/ });
    expect(screen.getAllByRole("option")).toHaveLength(1);
    expect(option).toHaveAttribute("aria-selected", "true");
    expect(input).toHaveAttribute("aria-activedescendant", option.id);
    await user.keyboard("{Enter}");
    expect(onNewChat).toHaveBeenCalledOnce();
  });

  it("stages search focus and restores the invoking control after Escape", async () => {
    const user = userEvent.setup();
    render(<TriggerHarness />);
    const trigger = screen.getByRole("button", { name: "Search sessions" });
    await user.click(trigger);

    expect(screen.getByRole("searchbox", { name: "Search commands and sessions" })).toHaveFocus();
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog", { name: "Command palette" })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("restores the Command+K focus origin and removes its shortcut listener on unmount", async () => {
    const user = userEvent.setup();
    const onNewChat = vi.fn();
    const { unmount } = render(<Harness onNewChat={onNewChat} />);
    const outside = document.createElement("button");
    outside.textContent = "Outside";
    document.body.append(outside);
    outside.focus();

    await user.keyboard("{Meta>}k{/Meta}");
    await user.keyboard("{Escape}");
    expect(outside).toHaveFocus();

    unmount();
    fireEvent.keyDown(window, { key: "n", metaKey: true });
    expect(onNewChat).not.toHaveBeenCalled();
    outside.remove();
  });

  it("ignores repeated global shortcut keydown events", () => {
    const onNewChat = vi.fn();
    render(<Harness onNewChat={onNewChat} />);

    fireEvent.keyDown(window, { key: "n", metaKey: true, repeat: true });

    expect(onNewChat).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog", { name: "Command palette" })).not.toBeInTheDocument();
  });
});
