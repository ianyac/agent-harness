export type ServiceConnection = {
  readonly baseUrl: string;
  readonly token: string;
};

export interface PlatformAdapter {
  readonly kind: "browser" | "tauri";
  getServiceConnection(): Promise<ServiceConnection>;
  chooseWorkspace(): Promise<string | null>;
  notify(input: { title: string; body: string }): Promise<void>;
  openLogs?: () => Promise<void>;
  restartService?: () => Promise<void>;
  quit?: () => Promise<void>;
}

const capabilityPattern = /^[A-Za-z0-9_-]{43}$/;
const loopbackHosts = new Set(["127.0.0.1", "localhost", "[::1]"]);

export function isCapabilityToken(value: unknown): value is string {
  return typeof value === "string" && capabilityPattern.test(value);
}

export function normalizeServiceConnection(value: unknown): ServiceConnection {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Invalid service connection.");
  }

  const record = value as Record<string, unknown>;
  if (!isCapabilityToken(record.token)) {
    throw new Error("Invalid service API capability.");
  }
  if (typeof record.baseUrl !== "string") {
    throw new Error("Invalid service origin.");
  }

  let url: URL;
  try {
    url = new URL(record.baseUrl);
  } catch {
    throw new Error("Invalid service origin.");
  }
  if (
    (url.protocol !== "http:" && url.protocol !== "https:") ||
    !loopbackHosts.has(url.hostname) ||
    url.username !== "" ||
    url.password !== "" ||
    url.pathname !== "/" ||
    url.search !== "" ||
    url.hash !== ""
  ) {
    throw new Error("Invalid service origin.");
  }

  return Object.freeze({ baseUrl: url.origin, token: record.token });
}
