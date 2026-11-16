import { describe, expect, it } from "vitest";
import { backendTarget, configuredBackend, configuredOrigin, readJsonBody, requireSameOrigin } from "../src/lib/request-policy";

describe("console boundary", () => {
  it("requires exact configured origins for mutations, including login", () => {
    const request = (origin?: string, site?: string) => new Request("https://console.example/api/session", {
      method: "POST", headers: { ...(origin ? { origin } : {}), ...(site ? { "sec-fetch-site": site } : {}) },
    });
    expect(() => requireSameOrigin(request("https://console.example", "same-origin"), "https://console.example")).not.toThrow();
    for (const origin of [undefined, "https://console.example.attacker.invalid", "http://console.example", "https://console.example:444"]) {
      expect(() => requireSameOrigin(request(origin), "https://console.example")).toThrow();
    }
    expect(() => requireSameOrigin(request("https://console.example", "cross-site"), "https://console.example")).toThrow();
  });

  it("permits only local HTTP console origins and service URLs without embedded credentials", () => {
    expect(configuredOrigin("http://127.0.0.1:3000").origin).toBe("http://127.0.0.1:3000");
    expect(configuredOrigin("https://console.example").origin).toBe("https://console.example");
    for (const value of ["bad-url", "http://public.example", "https://console.example/path", "https://console.example?x=1", "https://user:secret@console.example"]) {
      expect(() => configuredOrigin(value)).toThrow();
    }
    expect(configuredBackend("http://api:8000").hostname).toBe("api");
    for (const value of ["not-a-url", "file:///private", "https://user:secret@api.example", "https://api.example?destination=other"]) {
      expect(() => configuredBackend(value)).toThrow();
    }
  });

  it("allows specific operations and rejects traversal, method overrides and arbitrary proxy targets", () => {
    expect(backendTarget("POST", ["runs", "review_001", "approval"], new URLSearchParams())).toBe("runs/review_001/approval");
    expect(backendTarget("POST", ["assistant", "chat"], new URLSearchParams())).toBe("assistant/chat");
    expect(backendTarget("GET", ["runs"], new URLSearchParams("limit=10&status=COMPLETED"))).toBe("runs?limit=10&status=COMPLETED");
    for (const [method, path] of [["POST", ["a2a"]], ["GET", ["..", "health"]], ["DELETE", ["runs", "id"]], ["GET", ["https:", "evil.invalid"]], ["GET", ["runs", "a/b"]]] as [string, string[]][]) {
      expect(() => backendTarget(method, path, new URLSearchParams())).toThrow();
    }
    expect(() => backendTarget("GET", ["me"], new URLSearchParams("role=admin"))).toThrow();
    expect(() => backendTarget("GET", ["runs"], new URLSearchParams("limit=1&limit=100"))).toThrow();
    expect(() => backendTarget("GET", ["assistant", "chat"], new URLSearchParams())).toThrow();
    expect(() => backendTarget("POST", ["assistant", "execute"], new URLSearchParams())).toThrow();
  });

  it("bounds JSON by streamed bytes and rejects malformed or non-JSON content", async () => {
    const response = (text: string, type = "application/json") => new Response(text, { headers: { "content-type": type } });
    expect(await readJsonBody(response('{"approved":false}'))).toEqual({ approved: false });
    await expect(readJsonBody(response('{"x":"' + "a".repeat(100) + '"}'), 20)).rejects.toMatchObject({ status: 413 });
    await expect(readJsonBody(response("invalid"))).rejects.toMatchObject({ status: 400 });
    await expect(readJsonBody(response("{}", "text/plain"))).rejects.toMatchObject({ status: 415 });
    await expect(readJsonBody(new Response(null, { headers: { "content-type": "application/json" } }))).rejects.toMatchObject({ status: 400 });
  });
});
