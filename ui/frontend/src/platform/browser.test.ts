import { beforeEach, describe, expect, it, vi } from "vitest";

import type { BrowserEnvironment } from "./browser";

const staticToken = "s".repeat(43);
const apiToken = "a".repeat(43);

function browserEnvironment(url: string): BrowserEnvironment {
  const current = new URL(url);
  return {
    location: current,
    history: {
      state: null,
      replaceState() {
        current.hash = "";
      },
    },
  };
}

describe("browser platform token loss", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it("defers a typed token-lost failure to getServiceConnection after a reload", async () => {
    const { createBrowserPlatform, ServiceConnectionUnavailableError } = await import("./browser");
    const environment = browserEnvironment(`http://127.0.0.1:49152/_app/${staticToken}/`);

    const platform = createBrowserPlatform(environment);
    const failure = await platform.getServiceConnection().then(
      () => null,
      (error: unknown) => error,
    );
    expect(failure).toBeInstanceOf(ServiceConnectionUnavailableError);
    expect((failure as InstanceType<typeof ServiceConnectionUnavailableError>).kind)
      .toBe("token-lost");
  });

  it("keeps deferring the same token-lost failure for later platform creations", async () => {
    const { createBrowserPlatform, ServiceConnectionUnavailableError } = await import("./browser");
    const environment = browserEnvironment(`http://127.0.0.1:49152/_app/${staticToken}/`);

    createBrowserPlatform(environment);
    const second = createBrowserPlatform(environment);
    await expect(second.getServiceConnection())
      .rejects.toBeInstanceOf(ServiceConnectionUnavailableError);
  });

  it("still fails platform creation synchronously for a present but invalid fragment", async () => {
    const { createBrowserPlatform } = await import("./browser");
    const environment = browserEnvironment(
      `http://127.0.0.1:49152/_app/${staticToken}/#token=not-a-capability`,
    );

    expect(() => createBrowserPlatform(environment)).toThrow("API capability");
  });

  it("does not classify an invalid app path without a fragment as token loss", async () => {
    const { createBrowserPlatform } = await import("./browser");
    const environment = browserEnvironment("http://127.0.0.1:49152/other");

    expect(() => createBrowserPlatform(environment)).toThrow("static capability");
  });

  it("resolves a fresh link with an intact fragment token", async () => {
    const { createBrowserPlatform } = await import("./browser");
    const environment = browserEnvironment(
      `http://127.0.0.1:49152/_app/${staticToken}/#token=${apiToken}`,
    );

    await expect(createBrowserPlatform(environment).getServiceConnection()).resolves.toEqual({
      baseUrl: "http://127.0.0.1:49152",
      token: apiToken,
    });
  });
});
