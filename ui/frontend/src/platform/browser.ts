import type { PlatformAdapter, ServiceConnection } from "./types";
import { isCapabilityToken, normalizeServiceConnection } from "./types";

type BrowserLocation = Pick<
  Location,
  "hash" | "hostname" | "origin" | "pathname" | "protocol" | "search"
>;

type BrowserHistory = Pick<History, "replaceState" | "state">;

export type BrowserEnvironment = {
  readonly location: BrowserLocation;
  readonly history: BrowserHistory;
};

type BrowserCapture =
  | { readonly connection: ServiceConnection; readonly error?: never }
  | { readonly connection?: never; readonly error: Error };

let browserCapture: BrowserCapture | undefined;

export type ServiceConnectionUnavailableKind = "token-lost";

export class ServiceConnectionUnavailableError extends Error {
  readonly kind: ServiceConnectionUnavailableKind;

  constructor(kind: ServiceConnectionUnavailableKind, message: string) {
    super(message);
    this.name = "ServiceConnectionUnavailableError";
    this.kind = kind;
  }
}

function staticCapability(pathname: string): string | null {
  const match = /^\/_app\/([A-Za-z0-9_-]{43})(?:\/.*)?$/.exec(pathname);
  return match?.[1] ?? null;
}

function fragmentCapability(fragment: string): string | null {
  if (!fragment.startsWith("#")) return null;
  const parameters = new URLSearchParams(fragment.slice(1));
  const entries = [...parameters.entries()];
  if (entries.length !== 1 || entries[0][0] !== "token") return null;
  return isCapabilityToken(entries[0][1]) ? entries[0][1] : null;
}

function readBrowserConnection(environment: BrowserEnvironment): ServiceConnection {
  if (browserCapture !== undefined) {
    if (browserCapture.error !== undefined) throw browserCapture.error;
    return browserCapture.connection;
  }

  const fragment = environment.location.hash;
  environment.history.replaceState(
    environment.history.state,
    "",
    `${environment.location.pathname}${environment.location.search}`,
  );

  try {
    const pathCapability = staticCapability(environment.location.pathname);
    const token = fragmentCapability(fragment);
    if (pathCapability === null) {
      throw new Error("Missing or invalid browser static capability.");
    }
    if (token === null && fragment === "") {
      // A valid app path with no fragment at all means the token was consumed
      // by an earlier page lifetime (reload); it cannot be recovered here.
      throw new ServiceConnectionUnavailableError(
        "token-lost",
        "The service access token was lost with the previous page lifetime.",
      );
    }
    if (token === null || token === pathCapability) {
      throw new Error("Missing or invalid browser API capability.");
    }

    const connection = normalizeServiceConnection({
      baseUrl: environment.location.origin,
      token,
    });
    browserCapture = { connection };
    return connection;
  } catch (error) {
    const captureError =
      error instanceof Error ? error : new Error("Invalid browser service connection.");
    browserCapture = { error: captureError };
    throw captureError;
  }
}

export function createBrowserPlatform(
  environment: BrowserEnvironment = window,
): PlatformAdapter {
  let capture:
    | { readonly connection: ServiceConnection; readonly unavailable?: never }
    | { readonly connection?: never; readonly unavailable: ServiceConnectionUnavailableError };
  try {
    capture = { connection: readBrowserConnection(environment) };
  } catch (error) {
    // Token loss is terminal for this page lifetime but must not prevent the
    // platform from loading; it surfaces through getServiceConnection instead.
    if (!(error instanceof ServiceConnectionUnavailableError)) throw error;
    capture = { unavailable: error };
  }
  return Object.freeze({
    kind: "browser" as const,
    async getServiceConnection(): Promise<ServiceConnection> {
      if (capture.unavailable !== undefined) throw capture.unavailable;
      return capture.connection;
    },
    async chooseWorkspace(): Promise<null> {
      return null;
    },
    async notify(): Promise<void> {},
  });
}
