import "server-only";
import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { configuredBackend, configuredOrigin, ConsoleError, readJsonBody } from "./request-policy";
import { openToken, sessionKey } from "./session-codec";

export const COOKIE_NAME = "migration_console_session";
export const PRIVATE_HEADERS = { "Cache-Control": "no-store, private", "Vary": "Cookie" };
export type Principal = { subject: string; role: string; actions: string[] };

export function configuration() {
  try {
    return {
      origin: configuredOrigin(process.env.MIGRATION_UI_ORIGIN || "http://localhost:3000"),
      api: configuredBackend(process.env.MIGRATION_API_URL || "http://127.0.0.1:8000"),
      key: sessionKey(process.env.MIGRATION_UI_SESSION_SECRET || ""),
    };
  } catch {
    throw new ConsoleError(503, "Workspace sign-in is not configured. Ask your administrator to check the console settings.");
  }
}

export async function sessionToken(): Promise<string> {
  const config = configuration();
  const token = await openToken((await cookies()).get(COOKIE_NAME)?.value, config.key);
  if (!token) throw new ConsoleError(401, "Sign in to your workspace to continue.");
  return token;
}

export async function backendRequest(target: string, token: string, method = "GET", body?: unknown) {
  const config = configuration();
  const base = config.api.toString().replace(/\/$/, "");
  let response: Response;
  try {
    response = await fetch(`${base}/${target}`, {
      method,
      cache: "no-store",
      redirect: "error",
      headers: { Authorization: `Bearer ${token}`, Accept: "application/json", ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(90_000),
    });
  } catch {
    throw new ConsoleError(502, method === "GET"
      ? "The migration service could not be reached. Try refreshing shortly."
      : "The service response could not be confirmed. Check this run's status before retrying the operation.");
  }
  let data: unknown;
  try { data = await readJsonBody(response, 8_000_000); }
  catch { throw new ConsoleError(502, "The migration service returned an unexpected response. Check the run status before retrying."); }
  if (!response.ok) {
    const record = data && typeof data === "object" ? data as Record<string, unknown> : {};
    const detail = typeof record.detail === "string" ? record.detail : undefined;
    const message = response.status === 401 ? "Your access token is invalid or has been revoked. Sign in again."
      : response.status === 403 ? "Your workspace role does not permit this operation or resource."
      : detail?.slice(0, 1000) || "The migration service could not complete this request. Review the selected resources and mappings.";
    throw new ConsoleError(response.status >= 400 && response.status < 500 ? response.status : 502, message);
  }
  return data;
}

export function principalFrom(value: unknown): Principal {
  if (!value || typeof value !== "object") throw new ConsoleError(502, "The workspace identity could not be verified.");
  const principal = value as Record<string, unknown>;
  if (typeof principal.subject !== "string" || typeof principal.role !== "string" ||
      !Array.isArray(principal.actions) || !principal.actions.every(action => typeof action === "string")) {
    throw new ConsoleError(502, "The workspace identity could not be verified.");
  }
  return { subject: principal.subject, role: principal.role, actions: principal.actions };
}

export function errorResponse(error: unknown) {
  const safe = error instanceof ConsoleError ? error : new ConsoleError(500, "The console could not complete this request.");
  const response = NextResponse.json({ detail: safe.message, error: safe.message }, { status: safe.status, headers: PRIVATE_HEADERS });
  if (safe.status === 401) response.cookies.delete(COOKIE_NAME);
  return response;
}
