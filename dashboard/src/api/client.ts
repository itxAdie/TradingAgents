import type { ApiErrorBody, Envelope } from "./types";

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

function extractError(status: number, body: ApiErrorBody): ApiError {
  if (body?.error) {
    return new ApiError(status, body.error.code, body.error.message);
  }
  if (typeof body?.detail === "string") {
    return new ApiError(status, "http_error", body.detail);
  }
  if (Array.isArray(body?.detail)) {
    const msg = body.detail.map((d) => d.msg).join("; ");
    return new ApiError(status, "validation_error", msg);
  }
  return new ApiError(status, "http_error", `HTTP ${status}`);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      headers: { Accept: "application/json" },
      ...init,
    });
  } catch (err) {
    throw new ApiError(0, "network_error", String(err));
  }
  const text = await response.text();
  const body: unknown = text ? JSON.parse(text) : {};
  if (!response.ok) {
    throw extractError(response.status, body as ApiErrorBody);
  }
  return body as T;
}

/** GET a plain resource. */
export function get<T>(path: string): Promise<T> {
  return request<T>(path);
}

/** GET an enveloped list; returns the envelope as-is for pagination. */
export function getPage<T>(path: string): Promise<Envelope<T>> {
  return request<Envelope<T>>(path);
}

/** POST/PUT JSON bodies (the only mutations the terminal may perform). */
export function send<T>(
  method: "POST" | "PUT",
  path: string,
  payload?: unknown,
): Promise<T> {
  return request<T>(path, {
    method,
    headers: payload === undefined ? undefined : { "Content-Type": "application/json" },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
}
