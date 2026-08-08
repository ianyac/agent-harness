import { useState } from "react";
import { cleanup, render, screen } from "@testing-library/react";
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

describe("CommandPalette", () => {
  it("opens with Command+K and exposes stable commands with visible shortcuts", async () => {
    const user = userEvent.setup();
    render(<Harness onNewChat={() => {}} />);

    await user.keyboard("{Meta>}k{/Meta}");

    expect(screen.getByRole("dialog", { name: "Command palette" })).toBeVisible();
    expect(screen.getByRole("button", { name: /New chat.*⌘N/ })).toHaveAttribute(
      "data-command-id",
      "action:new-chat",
    );
    expect(screen.getByRole("button", { name: /Open settings/ })).toHaveAttribute(
      "data-command-id",
      "action:open-settings",
    );
    expect(screen.getByRole("button", { name: /Toggle activity.*⌘⇧I/ })).toHaveAttribute(
      "data-command-id",
      "action:toggle-activity",
    );
    expect(screen.getByRole("button", { name: /Trace retries.*payments-api/ })).toHaveAttribute(
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
    expect(screen.getByRole("button", { name: /Trace retries.*payments-api/ })).toBeVisible();
    expect(screen.queryByRole("button", { name: /Polish navigation/ })).not.toBeInTheDocument();
    await user.clear(search);
    await user.type(search, "dashboard-ui");

    expect(screen.getByRole("button", { name: /Polish navigation.*dashboard-ui/ })).toBeVisible();
    expect(screen.queryByRole("button", { name: /Trace retries/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Polish navigation.*dashboard-ui/ }));
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
    await user.click(screen.getByRole("button", { name: "Open settings" }));
    expect(onOpenSettings).toHaveBeenCalledOnce();

    await user.keyboard("{Meta>}k{/Meta}");
    await user.click(screen.getByRole("button", { name: /Toggle activity.*⌘⇧I/ }));
    expect(onToggleActivity).toHaveBeenCalledOnce();
  });
});
