import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PlatformAdapter } from "../../platform/types";
import { emptyTranscript } from "../../protocol/reducer";
import type { TranscriptState } from "../../protocol/types";
import type { SessionRecord } from "../sessions/useSessions";
import { defaultPreferences } from "./preferences";
import { NotificationObserver } from "./NotificationObserver";

function session(id: string): SessionRecord {
  return {
    session_id: id,
    workspace: `/work/${id}`,
    title: `Title ${id}`,
    mode: "default",
    context_mode: "compaction",
    created_at: "2026-08-09T04:00:00Z",
    updated_at: "2026-08-09T04:00:00Z",
    last_opened_at: "2026-08-09T04:00:00Z",
    archived_at: null,
  };
}

function adapter(notify: PlatformAdapter["notify"]): PlatformAdapter {
  return {
    kind: "tauri",
    getServiceConnection: async () => ({ baseUrl: "http://127.0.0.1:4010", token: "t".repeat(43) }),
    chooseWorkspace: async () => null,
    notify,
  };
}

function transcript(changes: Partial<TranscriptState> = {}): TranscriptState {
  return { ...emptyTranscript(), ...changes };
}

afterEach(cleanup);

describe("NotificationObserver", () => {
  it("hydrates silently then notifies only a newly observed background permission id once", async () => {
    const notify = vi.fn().mockResolvedValue(undefined);
    const records = [session("active"), session("background")];
    const initial = {
      active: transcript(),
      background: transcript({
        generation: 4,
        lastSequence: 8,
        permission: { turnId: "turn-old", requestId: "old", action: "read", scope: "secret scope", reason: "secret reason" },
      }),
    };
    const props = {
      clientKey: "client-a",
      platform: adapter(notify),
      sessions: records,
      activeSessionId: "active",
      preferences: { ...defaultPreferences, notifyPermissions: true },
    };
    const { rerender } = render(<NotificationObserver {...props} transcriptBySession={initial} />);
    expect(notify).not.toHaveBeenCalled();

    const next = {
      ...initial,
      background: transcript({
        generation: 4,
        lastSequence: 9,
        permission: { turnId: "turn-new", requestId: "new-request", action: "write", scope: "credential text", reason: "tool result" },
      }),
    };
    rerender(<NotificationObserver {...props} transcriptBySession={next} />);
    await waitFor(() => expect(notify).toHaveBeenCalledTimes(1));
    expect(notify).toHaveBeenCalledWith({
      title: "Permission needed · Title background",
      body: "background · /work/background",
    });
    rerender(<NotificationObserver {...props} transcriptBySession={{ ...next, background: { ...next.background, lastSequence: 10 } }} />);
    expect(notify).toHaveBeenCalledTimes(1);
  });

  it("notifies only a background running-to-terminal transition and ignores active or disabled transitions", async () => {
    const notify = vi.fn().mockResolvedValue(undefined);
    const records = [session("active"), session("background")];
    const initial = { active: transcript({ running: true }), background: transcript({ running: true }) };
    const props = {
      clientKey: "client-a",
      platform: adapter(notify),
      sessions: records,
      activeSessionId: "active",
      preferences: { ...defaultPreferences, notifyCompletions: true },
    };
    const { rerender } = render(<NotificationObserver {...props} transcriptBySession={initial} />);
    rerender(<NotificationObserver {...props} transcriptBySession={{
      active: transcript({ lastSequence: 2, running: false }),
      background: transcript({ lastSequence: 2, running: false }),
    }} />);
    await waitFor(() => expect(notify).toHaveBeenCalledTimes(1));
    expect(notify).toHaveBeenCalledWith({
      title: "Work complete · Title background",
      body: "background · /work/background",
    });

    rerender(<NotificationObserver {...props} preferences={defaultPreferences} transcriptBySession={{
      active: transcript({ lastSequence: 3, running: true }),
      background: transcript({ lastSequence: 3, running: true }),
    }} />);
    rerender(<NotificationObserver {...props} preferences={defaultPreferences} transcriptBySession={{
      active: transcript({ lastSequence: 4, running: false }),
      background: transcript({ lastSequence: 4, running: false }),
    }} />);
    expect(notify).toHaveBeenCalledTimes(1);
  });

  it("invalidates bookkeeping on client replacement, ignores stale generations, and contains notification failure", async () => {
    const notify = vi.fn().mockRejectedValue(new Error("Notification denied"));
    const records = [session("active"), session("background")];
    const props = {
      platform: adapter(notify),
      sessions: records,
      activeSessionId: "active",
      preferences: { ...defaultPreferences, notifyPermissions: true },
    };
    const initial = { active: transcript(), background: transcript({ generation: 7, lastSequence: 4 }) };
    const { rerender } = render(<NotificationObserver {...props} clientKey="a" transcriptBySession={initial} />);
    rerender(<NotificationObserver {...props} clientKey="a" transcriptBySession={{ active: initial.active, background: transcript({
      generation: 6,
      lastSequence: 99,
      permission: { turnId: "stale", requestId: "stale", action: "read", scope: "x", reason: "x" },
    }) }} />);
    expect(notify).not.toHaveBeenCalled();

    const replaced = { active: transcript(), background: transcript({
      generation: 9,
      lastSequence: 1,
      permission: { turnId: "hydrated", requestId: "hydrated", action: "read", scope: "x", reason: "x" },
    }) };
    rerender(<NotificationObserver {...props} clientKey="b" transcriptBySession={replaced} />);
    expect(notify).not.toHaveBeenCalled();
    rerender(<NotificationObserver {...props} clientKey="b" transcriptBySession={{ ...replaced, background: transcript({
      generation: 9,
      lastSequence: 2,
      permission: { turnId: "new", requestId: "new", action: "read", scope: "x", reason: "x" },
    }) }} />);
    await waitFor(() => expect(notify).toHaveBeenCalledTimes(1));
  });
});
