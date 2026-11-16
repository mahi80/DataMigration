import { NextResponse } from "next/server";
import { configuration, errorResponse, PRIVATE_HEADERS } from "@/lib/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export function GET() {
  try {
    configuration();
    return NextResponse.json({ status: "ok", service: "migration-console" }, { headers: PRIVATE_HEADERS });
  } catch (error) { return errorResponse(error); }
}
