import { NextResponse } from "next/server";

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

// POST /api/push/subscribe — forwards the browser's PushSubscription to the
// backend. Body shape comes from PushSubscription.toJSON(), optionally extended
// with `filter_profile_id` if the dashboard ever wires per-filter subscribes.
export async function POST(req: Request): Promise<Response> {
  const body = await req.text();
  const res = await fetch(`${API_BASE}/push/subscriptions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      // Pass the browser UA through so the backend can store it.
      "User-Agent": req.headers.get("user-agent") ?? "unknown",
    },
    body,
    cache: "no-store",
  });
  const text = await res.text();
  return new NextResponse(text, {
    status: res.status,
    headers: { "Content-Type": res.headers.get("content-type") ?? "application/json" },
  });
}
