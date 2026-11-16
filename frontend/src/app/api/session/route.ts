import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { backendRequest, configuration, COOKIE_NAME, errorResponse, principalFrom, PRIVATE_HEADERS, sessionToken } from "@/lib/server";
import { ConsoleError, readJsonBody, requireSameOrigin } from "@/lib/request-policy";
import { sealToken, SESSION_SECONDS, validToken } from "@/lib/session-codec";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  try {
    if (!(await cookies()).has(COOKIE_NAME)) {
      return NextResponse.json({ authenticated: false }, { status: 401, headers: PRIVATE_HEADERS });
    }
    const principal = principalFrom(await backendRequest("me", await sessionToken()));
    return NextResponse.json({ authenticated: true, principal }, { headers: PRIVATE_HEADERS });
  } catch (error) { return errorResponse(error); }
}

export async function POST(request: Request) {
  try {
    const config = configuration();
    requireSameOrigin(request, config.origin.origin);
    const value = await readJsonBody(request, 4096);
    if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).length !== 1 ||
        !("token" in value) || !validToken(value.token)) {
      throw new ConsoleError(400, "Enter your workspace access token (24–1024 characters).");
    }
    const principal = principalFrom(await backendRequest("me", value.token));
    const response = NextResponse.json({ authenticated: true, principal }, { headers: PRIVATE_HEADERS });
    response.cookies.set(COOKIE_NAME, await sealToken(value.token, config.key), {
      httpOnly: true,
      secure: config.origin.protocol === "https:",
      sameSite: "lax",
      maxAge: SESSION_SECONDS,
      path: "/",
    });
    return response;
  } catch (error) { return errorResponse(error); }
}

export async function DELETE(request: Request) {
  try {
    requireSameOrigin(request, configuration().origin.origin);
    const response = NextResponse.json({ authenticated: false }, { headers: PRIVATE_HEADERS });
    response.cookies.delete(COOKIE_NAME);
    return response;
  } catch (error) { return errorResponse(error); }
}
