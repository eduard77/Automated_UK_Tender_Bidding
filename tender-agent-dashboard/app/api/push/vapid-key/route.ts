import { NextResponse } from "next/server";

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

// Cache the upstream response for 5 minutes — the VAPID public key rarely
// changes, and we want a snappy bell on first paint. Next 15 requires this
// export to be a literal so it can be statically analysed.
export const revalidate = 300;

// GET /api/push/vapid-key — proxies the backend's VAPID public key (and subject)
// to the browser. Backend returns 503 if push isn't configured; we surface that
// status so the bell can hide itself without leaking config detail.
export async function GET(): Promise<Response> {
  try {
    const res = await fetch(`${API_BASE}/push/vapid-public-key`, {
      next: { revalidate: 300 },
    });
    if (!res.ok) {
      // 503 → push not configured. Anything else (502/504/timeout) → also "off".
      return new NextResponse(null, { status: res.status });
    }
    const data = await res.json();
    return NextResponse.json(data, {
      headers: {
        "Cache-Control": "public, max-age=300, stale-while-revalidate=60",
      },
    });
  } catch {
    // Network failure to the backend — treat as "push not configured" rather
    // than a 500 so the bell hides quietly.
    return new NextResponse(null, { status: 503 });
  }
}
