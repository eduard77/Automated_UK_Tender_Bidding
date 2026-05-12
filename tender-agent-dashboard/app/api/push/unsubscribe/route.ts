import { NextResponse } from "next/server";

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

// POST /api/push/unsubscribe — body is { endpoint }. Forwards to the backend
// DELETE /push/subscriptions (which takes a JSON body). We use POST here on
// the dashboard side because some browser fetch policies block bodies on DELETE.
export async function POST(req: Request): Promise<Response> {
  const body = await req.text();
  const res = await fetch(`${API_BASE}/push/subscriptions`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body,
    cache: "no-store",
  });
  return new NextResponse(null, { status: res.status });
}
