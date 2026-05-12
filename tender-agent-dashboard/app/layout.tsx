import "./globals.css";
import type { Metadata, Viewport } from "next";
import Link from "next/link";

import PushBell from "@/components/PushBell";

export const metadata: Metadata = {
  title: "Tender Agent",
  description: "UK public tender discovery and bid automation",
  manifest: "/manifest.json",
  appleWebApp: { capable: true, statusBarStyle: "black-translucent", title: "Tenders" },
};

export const viewport: Viewport = {
  themeColor: "#0E1116",
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  // PushBell is always rendered. It fetches the VAPID public key from
  // /api/push/vapid-key on mount and hides itself when push isn't supported
  // by the browser or isn't configured on the backend. No env gate here.
  return (
    <html lang="en">
      <body className="min-h-screen">
        <header className="border-b border-bone/10 sticky top-0 z-30 bg-ink/85 backdrop-blur-md">
          <div className="max-w-6xl mx-auto px-5 py-4 flex flex-wrap items-center justify-between gap-3">
            <Link href="/" className="flex items-baseline gap-2">
              <span className="font-display text-2xl tracking-tight text-bone">Tender</span>
              <span className="font-display italic text-rust text-2xl">/agent</span>
            </Link>
            <div className="flex items-center gap-6">
              <nav className="flex items-center gap-6 text-sm" aria-label="Primary">
                <Link href="/" className="text-bone/70 hover:text-bone transition">Tenders</Link>
                <Link href="/filters" className="text-bone/70 hover:text-bone transition">Filters</Link>
              </nav>
              <PushBell />
            </div>
          </div>
        </header>
        <main className="max-w-6xl mx-auto px-5 py-8 pb-24">{children}</main>
        <footer className="fixed bottom-0 inset-x-0 border-t border-bone/10 bg-ink/90 backdrop-blur-md">
          <div className="max-w-6xl mx-auto px-5 py-3 flex items-center justify-between text-xs text-bone/50 font-mono uppercase tracking-wider">
            <span>UK · public · tenders</span>
            <span>v0.2 · pass 2</span>
          </div>
        </footer>
      </body>
    </html>
  );
}
