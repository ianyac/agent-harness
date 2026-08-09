import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { ApiClient } from "../../api/http";
import { SessionSidebar } from "./SessionSidebar";
import type { SessionRecord, SessionRuntimeState } from "./useSessions";
import { useSessions } from "./useSessions";

const connection = {
  baseUrl: "http://127.0.0.1:4010",
  token: "t".repeat(43),
};

const storedPreferences = new Map<string, string>();
const sidebarStorage = {
  getItem: (key: string) => storedPreferences.get(key) ?? null,
  setItem: (key: string, value: string) => storedPreferences.set(key, value),
};

function session(changes: Partial<SessionRecord> = {}): SessionRecord {
  return {
    session_id: "session-1",
    workspace: "/work/acme",
    title: "Ship navigation",
    mode: "default",
    context_mode: "compaction",
    created_at: "2026-08-09T04:00:00.000000+00:00",
    updated_at: "2026-08-09T04:00:00.000000+00:00",
    last_opened_at: "2026-08-09T04:00:00.000000+00:00",
    archived_at: null,
    ...changes,
  };
}

afterEach(() => {
  cleanup();
  storedPreferences.clear();
});

describe("SessionSidebar", () => {
  it("groups unarchived sessions by local recency and keeps workspace in each row name", () => {
    const sessions = [
      session(),
      session({
        session_id: "session-2",
        workspace: "/work/other",
        title: "Review release",
        last_opened_at: "2026-08-08T04:00:00.000000+00:00",
      }),
      session({
        session_id: "session-3",
        title: "Old notes",
        last_opened_at: "2026-08-01T04:00:00.000000+00:00",
      }),
      session({
        session_id: "archived",
        title: "Archived work",
        archived_at: "2026-08-09T05:00:00.000000+00:00",
      }),
    ];
    const runtimeBySession: Readonly<Record<string, SessionRuntimeState>> = {
      "session-1": { status: "waiting_permission" },
      "session-2": { status: "running" },
    };

    render(
      <SessionSidebar
        sessions={sessions}
        activeSessionId="session-1"
        runtimeBySession={runtimeBySession}
        now={new Date("2026-08-09T12:00:00+08:00")}
        storage={sidebarStorage}
        onCreate={() => {}}
        onSelect={() => {}}
        onRename={() => {}}
        onArchive={() => {}}
        onSearch={() => {}}
        onOpenSettings={() => {}}
      />,
    );

    const navigation = screen.getByRole("navigation", { name: "Sessions" });
    expect(within(navigation).getByRole("heading", { name: "Today" })).toBeVisible();
    expect(within(navigation).getByRole("heading", { name: "Yesterday" })).toBeVisible();
    expect(within(navigation).getByRole("heading", { name: "Earlier" })).toBeVisible();
    expect(
      within(navigation).getByRole("button", {
        name: /Ship navigation.*\/work\/acme.*waiting for permission/i,
      }),
    ).toHaveAttribute("aria-current", "page");
    expect(
      within(navigation).getByRole("button", {
        name: /Review release.*other.*running in background/i,
      }),
    ).toBeVisible();
    expect(within(navigation).queryByText("Archived work")).not.toBeInTheDocument();
  });

  it("manual collapse persists while retaining named new-chat, search, and session controls", async () => {
    const user = userEvent.setup();
    render(
      <SessionSidebar
        {...{ connectionStatus: "disconnected" }}
        sessions={[
          session(),
          session({
            session_id: "session-2",
            workspace: "/work/other",
            title: "Review release",
          }),
        ]}
        activeSessionId="session-1"
        runtimeBySession={{}}
        now={new Date("2026-08-09T12:00:00+08:00")}
        storage={sidebarStorage}
        onCreate={() => {}}
        onSelect={() => {}}
        onRename={() => {}}
        onArchive={() => {}}
        onSearch={() => {}}
        onOpenSettings={() => {}}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Collapse sidebar" }));

    const navigation = screen.getByRole("navigation", { name: "Sessions" });
    expect(navigation).toHaveAttribute("data-collapsed", "true");
    expect(sidebarStorage.getItem("harness.sidebar.collapsed")).toBe("true");
    expect(screen.getByRole("button", { name: "New chat" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Search sessions" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Settings" })).toBeVisible();
    expect(screen.getByRole("status", { name: "Local service disconnected" })).toBeVisible();
    const shipSession = screen.getByRole("button", { name: /Ship navigation.*\/work\/acme/i });
    await user.hover(shipSession);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Ship navigation");
    expect(screen.getByRole("tooltip")).toHaveTextContent("/work/acme");
    await user.unhover(shipSession);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("tooltip")).not.toBeInTheDocument());

    const reviewSession = screen.getByRole("button", { name: /Review release.*\/work\/other/i });
    act(() => reviewSession.focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Review release");
    expect(screen.getByRole("tooltip")).toHaveTextContent("/work/other");
    expect(screen.getByRole("button", { name: /More actions for Ship navigation/i })).toBeVisible();

    await user.click(screen.getByRole("button", { name: /More actions for Ship navigation/i }));
    await user.click(screen.getByRole("menuitem", { name: "Rename" }));
    const rename = screen.getByRole("textbox", { name: "Rename Ship navigation" });
    expect(rename).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(rename).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /More actions for Ship navigation/i })).toHaveFocus();
  });

  it("uses distinct visible marks for every connection state in the rail", async () => {
    const user = userEvent.setup();
    const props = {
      sessions: [session()],
      activeSessionId: "session-1",
      runtimeBySession: {},
      now: new Date("2026-08-09T12:00:00+08:00"),
      storage: sidebarStorage,
      onCreate: () => {},
      onSelect: () => {},
      onRename: () => {},
      onArchive: () => {},
      onSearch: () => {},
      onOpenSettings: () => {},
    } as const;
    const { rerender } = render(
      <SessionSidebar {...props} connectionStatus="connecting" />,
    );
    await user.click(screen.getByRole("button", { name: "Collapse sidebar" }));

    expect(
      within(screen.getByRole("status", { name: "Local service connecting" })).getByText("…"),
    ).toBeVisible();
    rerender(<SessionSidebar {...props} connectionStatus="connected" />);
    expect(
      within(screen.getByRole("status", { name: "Local service connected" })).getByText("✓"),
    ).toBeVisible();
    rerender(<SessionSidebar {...props} connectionStatus="disconnected" />);
    expect(
      within(screen.getByRole("status", { name: "Local service disconnected" })).getByText("!"),
    ).toBeVisible();
  });

  it("uses local midnight boundaries for Today and Yesterday", () => {
    const now = new Date(2026, 7, 9, 0, 1);
    const justBeforeMidnight = new Date(2026, 7, 8, 23, 59).toISOString();
    render(
      <SessionSidebar
        sessions={[
          session({ session_id: "today", title: "After midnight", last_opened_at: now.toISOString() }),
          session({
            session_id: "yesterday",
            title: "Before midnight",
            last_opened_at: justBeforeMidnight,
          }),
        ]}
        activeSessionId="today"
        runtimeBySession={{}}
        now={now}
        storage={sidebarStorage}
        onCreate={() => {}}
        onSelect={() => {}}
        onRename={() => {}}
        onArchive={() => {}}
        onSearch={() => {}}
        onOpenSettings={() => {}}
      />,
    );

    const today = screen.getByRole("region", { name: "Today" });
    const yesterday = screen.getByRole("region", { name: "Yesterday" });
    expect(within(today).getByRole("button", { name: /^After midnight,/ })).toBeVisible();
    expect(within(yesterday).getByRole("button", { name: /^Before midnight,/ })).toBeVisible();
  });

  it("creates, selects, renames, and archives through the real session hook", async () => {
    const user = userEvent.setup();
    let records = [
      session(),
      session({ session_id: "session-existing-2", title: "Second session" }),
    ];
    let created = 1;
    const fetchRequest: typeof fetch = async (input, init) => {
      const url = new URL(input instanceof Request ? input.url : input.toString());
      if (url.pathname === "/api/config") {
        return Response.json({
          base_workspace: "/work/acme",
          default_mode: "default",
          default_context_mode: "compaction",
          modes: ["default", "acceptAll", "readOnly"],
          context_modes: ["compaction", "folding"],
        });
      }
      if (url.pathname === "/api/sessions" && init?.method === "GET") {
        return Response.json(records);
      }
      if (url.pathname === "/api/sessions" && init?.method === "POST") {
        created += 1;
        const next = session({ session_id: `session-${created}`, title: "New chat" });
        records = [next, ...records];
        return Response.json(next, { status: 201 });
      }
      const id = decodeURIComponent(url.pathname.split("/").at(-1) ?? "");
      if (init?.method === "PATCH") {
        const body = JSON.parse(String(init.body)) as { title: string };
        records = records.map((record) =>
          record.session_id === id ? { ...record, title: body.title } : record,
        );
        return Response.json(records.find((record) => record.session_id === id));
      }
      if (init?.method === "DELETE") {
        records = records.filter((record) => record.session_id !== id);
        return new Response(null, { status: 204 });
      }
      return new Response(null, { status: 404 });
    };

    const client = new ApiClient(connection, fetchRequest);

    function Harness() {
      const model = useSessions(client);
      return (
        <SessionSidebar
          sessions={model.sessions}
          activeSessionId={model.activeSessionId}
          runtimeBySession={{}}
          now={new Date("2026-08-09T12:00:00+08:00")}
          storage={sidebarStorage}
          onCreate={model.createSession}
          onSelect={model.selectSession}
          onRename={model.renameSession}
          onArchive={model.archiveSession}
          onSearch={() => {}}
          onOpenSettings={() => {}}
        />
      );
    }

    render(<Harness />);
    const first = await screen.findByRole("button", { name: /Ship navigation.*acme/i });
    expect(first).toHaveAttribute("aria-current", "page");

    await user.click(screen.getByRole("button", { name: "New chat" }));
    const createdRow = await screen.findByRole("button", { name: /New chat.*acme/i });
    expect(createdRow).toHaveAttribute("aria-current", "page");

    await user.click(screen.getByRole("button", { name: /Ship navigation.*\/work\/acme/i }));
    expect(screen.getByRole("button", { name: /Ship navigation.*\/work\/acme/i })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(createdRow).not.toHaveAttribute("aria-current");

    await user.click(createdRow);

    await user.click(screen.getByRole("button", { name: /More actions for New chat/i }));
    await user.click(await screen.findByRole("menuitem", { name: "Rename" }));
    const renameInput = screen.getByRole("textbox", { name: /Rename New chat/i });
    await user.clear(renameInput);
    await user.type(renameInput, "Fresh title");
    await user.click(screen.getByRole("button", { name: "Save name" }));
    expect(await screen.findByRole("button", { name: /Fresh title.*acme/i })).toBeVisible();
    expect(screen.getByRole("button", { name: /More actions for Fresh title/i })).toHaveFocus();

    await user.click(screen.getByRole("button", { name: /More actions for Fresh title/i }));
    await user.click(await screen.findByRole("menuitem", { name: "Archive" }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /Fresh title.*acme/i })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /Ship navigation.*acme/i })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });
});
