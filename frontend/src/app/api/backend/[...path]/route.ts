import { NextResponse } from "next/server";
import { backendTarget, readJsonBody, requireSameOrigin } from "@/lib/request-policy";
import { backendRequest, configuration, errorResponse, PRIVATE_HEADERS, sessionToken } from "@/lib/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
type Context = { params: Promise<{ path: string[] }> };

async function handle(request: Request, context: Context) {
  try {
    const { path } = await context.params;
    const target = backendTarget(request.method, path, new URL(request.url).searchParams);
    if (request.method !== "GET") requireSameOrigin(request, configuration().origin.origin);
    const token = await sessionToken();
    const body = request.method === "GET" ? undefined : await readJsonBody(request);
    const data = await backendRequest(target, token, request.method, body);
    // Reconstruct JSON; never forward upstream framing, cookies or cache headers.
    return NextResponse.json(data, { headers: PRIVATE_HEADERS });
  } catch (error) { return errorResponse(error); }
}

export const GET = handle;
export const POST = handle;
