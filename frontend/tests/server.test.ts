import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { backendRequest, configuration, errorResponse, principalFrom } from "../src/lib/server";

beforeEach(() => {
  vi.stubEnv("MIGRATION_UI_SESSION_SECRET", "23".repeat(32));
  vi.stubEnv("MIGRATION_UI_ORIGIN", "http://127.0.0.1:3000");
  vi.stubEnv("MIGRATION_API_URL", "http://127.0.0.1:8000");
});
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); });

describe("server-side API transport", () => {
  it("sends the user's token only to the fixed backend with caching and redirects disabled", async () => {
    const fetcher = vi.fn().mockResolvedValue(Response.json({ status: "COMPLETED" }));
    vi.stubGlobal("fetch", fetcher);
    expect(await backendRequest("runs/example", "opaque-test-token")).toEqual({ status: "COMPLETED" });
    expect(fetcher).toHaveBeenCalledWith("http://127.0.0.1:8000/runs/example", expect.objectContaining({
      cache: "no-store", redirect: "error", headers: expect.objectContaining({ Authorization: "Bearer opaque-test-token" }),
    }));
  });

  it("preserves backend role denial and hides raw infrastructure errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ detail: "private backend diagnostic" }, { status: 403 })));
    await expect(backendRequest("connections", "token")).rejects.toMatchObject({ status: 403, message: "Your workspace role does not permit this operation or resource." });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("PRIVATE-URI-AND-TOKEN")));
    await expect(backendRequest("runs", "token", "POST", {})).rejects.toMatchObject({ status: 502 });
    const response = errorResponse(new Error("PRIVATE-URI-AND-TOKEN"));
    expect(await response.text()).not.toContain("PRIVATE");
  });

  it("rejects malformed upstream responses and invalid identity shapes", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("<html>proxy error</html>", { headers: { "content-type": "text/html" } })));
    await expect(backendRequest("me", "token")).rejects.toMatchObject({ status: 502 });
    expect(() => principalFrom({ subject: "caller", role: "admin", actions: "all" })).toThrow();
    expect(principalFrom({ subject: "caller", role: "planner", actions: ["plan"], secret: "not-returned" })).toEqual({ subject: "caller", role: "planner", actions: ["plan"] });
  });

  it("fails closed on incomplete console configuration", () => {
    vi.stubEnv("MIGRATION_UI_SESSION_SECRET", "DO-NOT-ECHO-THIS");
    expect(() => configuration()).toThrow("Workspace sign-in is not configured");
  });
});
