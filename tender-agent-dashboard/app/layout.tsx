import "./globals.css";
import type { Metadata, Viewport } from "next";

import AppShell from "@/components/AppShell";
import AtmosphereBackground from "@/components/AtmosphereBackground";
import { brand } from "@/lib/theme";

export const metadata: Metadata = {
  title: "Genera / Tenders",
  description:
    "UK public tender discovery and bid automation — live construction signal from Find a Tender, Contracts Finder, and Public Contracts Scotland.",
  manifest: "/manifest.json",
  appleWebApp: { capable: true, statusBarStyle: "black-translucent", title: "Tenders" },
};

export const viewport: Viewport = {
  themeColor: brand.themeColor,
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen">
        <AtmosphereBackground />
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
