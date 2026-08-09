import type { ServiceConnection } from "../platform/types";
import { normalizeServiceConnection } from "../platform/types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly category: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type JsonRequestInit = Omit<RequestInit, "body" | "method">;

export class ApiClient {
  readonly #connection: ServiceConnection;
  readonly #fetch: typeof fetch;

  constructor(
    connection: ServiceConnection,
    fetchRequest: typeof fetch = globalThis.fetch.bind(globalThis),
  ) {
    this.#connection = normalizeServiceConnection(connection);
    this.#fetch = fetchRequest;
  }

  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const url = new URL(path, `${this.#connection.baseUrl}/`);
    if (
      url.origin !== this.#connection.baseUrl ||
      url.username !== "" ||
      url.password !== "" ||
      url.hash !== ""
    ) {
      throw new Error("API requests must stay on the same service origin.");
    }

    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${this.#connection.token}`);
    const response = await this.#fetch(url, {
      ...init,
      headers,
      credentials: "omit",
      redirect: "error",
      referrerPolicy: "no-referrer",
    });

    if (!response.ok) {
      throw await this.#apiError(response);
    }
    if (response.status === 204) return undefined as T;
    try {
      return (await response.json()) as T;
    } catch {
      throw new ApiError(response.status, "invalid_response", "Invalid service response.");
    }
  }

  get<T>(path: string, init: Omit<RequestInit, "method"> = {}): Promise<T> {
    return this.request<T>(path, { ...init, method: "GET" });
  }

  post<T>(path: string, body?: unknown, init: JsonRequestInit = {}): Promise<T> {
    return this.#jsonRequest<T>("POST", path, body, init);
  }

  patch<T>(path: string, body: unknown, init: JsonRequestInit = {}): Promise<T> {
    return this.#jsonRequest<T>("PATCH", path, body, init);
  }

  delete<T = void>(path: string, init: Omit<RequestInit, "method"> = {}): Promise<T> {
    return this.request<T>(path, { ...init, method: "DELETE" });
  }

  #jsonRequest<T>(
    method: "POST" | "PATCH",
    path: string,
    body: unknown,
    init: JsonRequestInit,
  ): Promise<T> {
    const headers = new Headers(init.headers);
    if (body !== undefined && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    return this.request<T>(path, {
      ...init,
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  }

  async #apiError(response: Response): Promise<ApiError> {
    try {
      const payload = (await response.json()) as unknown;
      if (typeof payload === "object" && payload !== null && !Array.isArray(payload)) {
        const error = (payload as Record<string, unknown>).error;
        if (typeof error === "object" && error !== null && !Array.isArray(error)) {
          const record = error as Record<string, unknown>;
          if (typeof record.type === "string" && typeof record.message === "string") {
            return new ApiError(response.status, record.type, record.message);
          }
        }
      }
    } catch {
      // The stable fallback below deliberately excludes response text.
    }
    return new ApiError(response.status, "request_failed", "The service request failed.");
  }
}
