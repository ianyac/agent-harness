import { expect, test as base, type Page, type WebSocketRoute } from "@playwright/test";

const staticCapability = "s".repeat(43);
const apiCapability = "a".repeat(43);

export const sessionId = "11111111-1111-4111-8111-111111111111";

const session = {
  session_id: sessionId,
  workspace: "/fixtures/workspace",
  title: "Fixture session",
  mode: "default",
  context_mode: "compaction",
  created_at: "2026-08-08T08:00:00Z",
  updated_at: "2026-08-08T08:00:00Z",
  last_opened_at: "2026-08-08T08:00:00Z",
  archived_at: null,
} as const;

type FixtureEvent = Record<string, unknown> & { readonly type: string };

export type FixtureAuthority = {
  readonly entryPath: string;
  readonly socketConnections: () => number;
  readonly outbound: () => readonly FixtureEvent[];
  readonly setSessions: (records: readonly typeof session[]) => void;
  readonly failNextCreateWithCredentialPrerequisite: () => void;
  readonly failNextHealthCheck: () => void;
  readonly createRequests: () => readonly Record<string, unknown>[];
  readonly emit: (event: FixtureEvent) => void;
  readonly sendRaw: (event: FixtureEvent) => void;
  readonly closeSocket: () => void;
  readonly withholdNextSnapshots: (count?: number) => void;
  readonly failNextDelete: () => void;
};

async function installOfflineAuthority(page: Page): Promise<FixtureAuthority> {
  let socketConnections = 0;
  let records: readonly typeof session[] = [session];
  let credentialFailures = 0;
  let healthFailures = 0;
  let activeSocket: WebSocketRoute | null = null;
  let generation = 0;
  let sequence = 0;
  let withheldSnapshots = 0;
  let deleteFailures = 0;
  const outboundEvents: FixtureEvent[] = [];
  const createBodies: Record<string, unknown>[] = [];

  await page.route("**/_app/*/assets/**", async (route) => {
    const source = new URL(route.request().url());
    const asset = source.pathname.slice(source.pathname.indexOf("/assets/"));
    const response = await page.request.get(`http://127.0.0.1:4173${asset}`);
    await route.fulfill({ response });
  });

  await page.routeWebSocket("**/ws/sessions/**", (socket: WebSocketRoute) => {
    const protocols = socket.protocols();
    if (protocols.length !== 2 || protocols[0] !== "harness-ui" || protocols[1] !== apiCapability) {
      void socket.close({ code: 1008, reason: "Fixture capability required" });
      return;
    }
    socketConnections += 1;
    generation += 1;
    sequence = 1;
    activeSocket = socket;
    socket.onMessage((message) => {
      if (typeof message !== "string") return;
      const value = JSON.parse(message) as unknown;
      if (typeof value === "object" && value !== null && !Array.isArray(value)) {
        outboundEvents.push(value as FixtureEvent);
      }
    });
    if (withheldSnapshots > 0) {
      withheldSnapshots -= 1;
      return;
    }
    socket.send(JSON.stringify({
      type: "session_snapshot",
      session_id: sessionId,
      generation,
      sequence: 1,
      turn_id: null,
      messages: [],
      running: false,
      queued_message: null,
      safety: {},
    }));
  });

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    expect(request.headers().authorization).toBe(`Bearer ${apiCapability}`);
    const path = new URL(request.url()).pathname;
    const method = request.method();
    if (path === "/api/health" && healthFailures > 0) {
      healthFailures -= 1;
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ error: { type: "service_unavailable", message: "Local fixture unavailable." } }),
      });
      return;
    }
    if (path === "/api/sessions" && method === "POST") {
      createBodies.push(request.postDataJSON() as Record<string, unknown>);
      if (credentialFailures > 0) {
        credentialFailures -= 1;
        await route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({ error: { type: "credential_prerequisite", message: "Run codex login, then retry." } }),
        });
        return;
      }
      records = [session];
      await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify(session) });
      return;
    }
    if (path.startsWith("/api/sessions/") && method === "DELETE") {
      if (deleteFailures > 0) {
        deleteFailures -= 1;
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({ error: { type: "cleanup_failed", message: "Fixture cleanup failed." } }),
        });
        return;
      }
      records = records.filter((record) => `/api/sessions/${record.session_id}` !== path);
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    const body = path === "/api/health"
      ? { status: "ok", service_id: "22222222-2222-4222-8222-222222222222" }
      : path === "/api/config"
        ? {
            base_workspace: "/fixtures/workspace",
            default_mode: "default",
            default_context_mode: "compaction",
            modes: ["default", "acceptAll", "readOnly"],
            context_modes: ["compaction", "folding"],
          }
        : path === "/api/sessions" && method === "GET"
          ? records
          : null;
    if (body === null) {
      await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ error: { type: "not_found", message: "Not found" } }) });
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });

  await page.context().route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname !== "127.0.0.1" || url.port !== "4173") {
      await route.abort("blockedbyclient");
      return;
    }
    await route.continue();
  });

  return {
    entryPath: `/_app/${staticCapability}/#token=${apiCapability}`,
    socketConnections: () => socketConnections,
    outbound: () => outboundEvents,
    setSessions: (next) => { records = next; },
    failNextCreateWithCredentialPrerequisite: () => { credentialFailures += 1; },
    failNextHealthCheck: () => { healthFailures += 1; },
    createRequests: () => createBodies,
    emit: (event) => {
      if (activeSocket === null) throw new Error("No active fixture socket.");
      sequence += 1;
      activeSocket.send(JSON.stringify({
        session_id: sessionId,
        generation,
        sequence,
        turn_id: null,
        ...event,
      }));
    },
    sendRaw: (event) => {
      if (activeSocket === null) throw new Error("No active fixture socket.");
      activeSocket.send(JSON.stringify(event));
    },
    closeSocket: () => {
      if (activeSocket === null) throw new Error("No active fixture socket.");
      activeSocket.close();
      activeSocket = null;
    },
    withholdNextSnapshots: (count = 1) => { withheldSnapshots += count; },
    failNextDelete: () => { deleteFailures += 1; },
  };
}

export const test = base.extend<{ authority: FixtureAuthority }>({
  authority: async ({ page }, use) => {
    await use(await installOfflineAuthority(page));
  },
});

export { expect };
