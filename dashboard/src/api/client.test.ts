import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, get, getPage, send } from "./client";

function asApiErr(e: unknown): ApiError {
  return e as ApiError;
}

function mockFetch(status: number, body: unknown): ReturnType<typeof vi.fn> {
  const fn = vi.fn().mockResolvedValue(
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("client", () => {
  it("returns plain payloads on 2xx", async () => {
    mockFetch(200, { hello: "world" });
    await expect(get("/x")).resolves.toEqual({ hello: "world" });
  });

  it("passes envelopes through untouched", async () => {
    mockFetch(200, { items: [1], total: 1, limit: 50, offset: 0 });
    const page = await getPage<number>("/list");
    expect(page).toEqual({ items: [1], total: 1, limit: 50, offset: 0 });
  });

  it("maps the backend error envelope to ApiError with its code", async () => {
    mockFetch(404, { error: { code: "not_found", message: "no such account" } });
    const err = asApiErr(await get("/missing").catch((e) => e));
    expect(err).toBeInstanceOf(ApiError);
    expect(err.code).toBe("not_found");
    expect(err.status).toBe(404);
    expect(err.message).toBe("no such account");
  });

  it("maps fastapi validation lists", async () => {
    mockFetch(400, { detail: [{ msg: "bad window" }] });
    const err = asApiErr(await send("POST", "/backtests", {}).catch((e) => e));
    expect(err).toBeInstanceOf(ApiError);
    expect(err.code).toBe("validation_error");
    expect(err.message).toContain("bad window");
  });

  it("sends JSON bodies for mutations only", async () => {
    const fetchMock = mockFetch(200, { ok: true });
    await send("PUT", "/trades/t1/journal", { author: "web", text: "note" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/trades/t1/journal");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body)).toEqual({ author: "web", text: "note" });
  });

  it("wraps network failures as network_error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("down")));
    const err = asApiErr(await get("/x").catch((e) => e));
    expect(err).toBeInstanceOf(ApiError);
    expect(err.code).toBe("network_error");
  });
});
