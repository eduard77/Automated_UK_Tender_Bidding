/** @type {import('next').NextConfig} */

// Local-dev backend URL. The Tender Agent service runs at :8000 outside the
// dashboard process. Production deployments serve dashboard + API on the
// same origin and can leave this unset.
const BACKEND_ORIGIN =
  process.env.TENDER_AGENT_API_ORIGIN ?? "http://localhost:8000";

const nextConfig = {
  reactStrictMode: true,
  async headers() {
    return [
      {
        source: "/sw.js",
        headers: [
          { key: "Content-Type", value: "application/javascript; charset=utf-8" },
          { key: "Cache-Control", value: "no-cache, no-store, must-revalidate" },
          { key: "Service-Worker-Allowed", value: "/" },
        ],
      },
    ];
  },
  // Browser-side calls go through `/__api/*` so they're same-origin and the
  // backend doesn't need CORS middleware. Next.js forwards each call to the
  // configured backend at request time.
  async rewrites() {
    return [
      {
        source: "/__api/:path*",
        destination: `${BACKEND_ORIGIN}/:path*`,
      },
    ];
  },
};

export default nextConfig;
