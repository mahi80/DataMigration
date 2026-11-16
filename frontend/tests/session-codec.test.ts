import { describe, expect, it } from "vitest";
import { openToken, sealToken, sessionKey, SESSION_SECONDS, validToken } from "../src/lib/session-codec";

const key = sessionKey("23".repeat(32));
const token = "synthetic-workspace-token-for-tests-only";

describe("encrypted console sessions", () => {
  it("keeps the bearer token encrypted and recovers it only with the configured key", async () => {
    const cookie = await sealToken(token, key);
    expect(cookie).not.toContain(token);
    expect(cookie.split(".")).toHaveLength(5);
    expect(await openToken(cookie, key)).toBe(token);
    expect(await openToken(cookie, sessionKey("42".repeat(32)))).toBeNull();
  });

  it("rejects modified, missing, oversized and expired sessions", async () => {
    const cookie = await sealToken(token, key);
    const parts = cookie.split(".");
    parts[3] = (parts[3][0] === "A" ? "B" : "A") + parts[3].slice(1);
    expect(await openToken(parts.join("."), key)).toBeNull();
    expect(await openToken(undefined, key)).toBeNull();
    expect(await openToken("x".repeat(4000), key)).toBeNull();
    const expired = await sealToken(token, key, Math.floor(Date.now() / 1000) - SESSION_SECONDS - 30);
    expect(await openToken(expired, key)).toBeNull();
  });

  it("rejects incomplete keys and non-opaque access tokens without echoing their contents", async () => {
    expect(() => sessionKey("private-value")).toThrow("32-byte");
    expect(validToken("short")).toBe(false);
    expect(validToken("x".repeat(1025))).toBe(false);
    expect(validToken("token with whitespace and sufficient length")).toBe(false);
    expect(validToken(123)).toBe(false);
    await expect(sealToken("short", key)).rejects.toThrow("Invalid access token format");
  });
});
