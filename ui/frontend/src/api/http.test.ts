import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "./http";

const connection = {
  baseUrl: "http://127.0.0.1:4010",
  token: "t".repeat(43),
};

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe("ApiClient", () => {
  it("invokes the default browser fetch with the global receiver", async () => {
    let receiver: unknown;
    globalThis.fetch = vi.fn(function (this: unknown) {
      receiver = this;
      return Promise.resolve(Response.json({ ok: true }));
    }) as typeof fetch;
    const client = new ApiClient(connection);

    await expect(client.get("/api/config")).resolves.toEqual({ ok: true });

    expect(receiver).toBe(globalThis);
  });
});
