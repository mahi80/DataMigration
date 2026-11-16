import { EncryptJWT, jwtDecrypt } from "jose";

const ISSUER = "datamigration-console";
const AUDIENCE = "datamigration-api";
export const SESSION_SECONDS = 8 * 60 * 60;

export function sessionKey(secret: string): Uint8Array {
  if (!/^[a-fA-F0-9]{64}$/.test(secret)) {
    throw new Error("Session configuration requires a 32-byte hexadecimal key");
  }
  return new Uint8Array(Buffer.from(secret, "hex"));
}

export function validToken(token: unknown): token is string {
  return typeof token === "string" && token.length >= 24 && token.length <= 1024 && !/[\s\x00-\x1f\x7f]/.test(token);
}

export async function sealToken(token: string, key: Uint8Array, now = Math.floor(Date.now() / 1000)): Promise<string> {
  if (!validToken(token)) throw new Error("Invalid access token format");
  return new EncryptJWT({ token })
    .setProtectedHeader({ alg: "dir", enc: "A256GCM" })
    .setIssuer(ISSUER)
    .setAudience(AUDIENCE)
    .setIssuedAt(now)
    .setExpirationTime(now + SESSION_SECONDS)
    .encrypt(key);
}

export async function openToken(value: string | undefined, key: Uint8Array): Promise<string | null> {
  if (!value || value.length > 3500) return null;
  try {
    const { payload } = await jwtDecrypt(value, key, {
      issuer: ISSUER,
      audience: AUDIENCE,
      keyManagementAlgorithms: ["dir"],
      contentEncryptionAlgorithms: ["A256GCM"],
      clockTolerance: 5,
      maxTokenAge: SESSION_SECONDS,
    });
    return validToken(payload.token) ? payload.token : null;
  } catch {
    return null;
  }
}
