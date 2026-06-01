"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

import { AuthProvider, planLabel, useAuth } from "@/lib/auth";

import BridgeIndicator from "./BridgeIndicator";
import PushBell from "./PushBell";

type NavLink = { href: string; label: string; matches?: (p: string) => boolean };

const NAV_LINKS: NavLink[] = [
  { href: "/", label: "Today", matches: (p) => p === "/" || p.startsWith("/tenders") },
  { href: "/search", label: "Search", matches: (p) => p.startsWith("/search") },
  { href: "/filters", label: "Filters" },
  { href: "/vault", label: "Vault", matches: (p) => p.startsWith("/vault") },
  { href: "/portals", label: "Portals", matches: (p) => p.startsWith("/portals") },
  { href: "/platforms", label: "Platforms", matches: (p) => p.startsWith("/platforms") },
  // Placeholders for routes that will exist later — keep the nav identical to
  // the v2 mockup but make them inert so users can see the surface area.
  { href: "/tenders", label: "Tenders", matches: (p) => p.startsWith("/tenders") && p !== "/" },
];

const SOFT_LINKS: NavLink[] = [
  { href: "/briefs", label: "Brief drafts" },
  { href: "/audit", label: "Audit" },
];

export default function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <AuthProvider>
      <AppShellInner>{children}</AppShellInner>
    </AuthProvider>
  );
}

function AppShellInner({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() ?? "/";
  const [mobileOpen, setMobileOpen] = useState(false);
  // SignInModal is mounted in the next commit; this state opens it.
  const [, setSignInOpen] = useState(false);

  const isActive = (link: NavLink) =>
    link.matches ? link.matches(pathname) : pathname === link.href;

  return (
    <div className="relative z-[2]">
      <nav
        className="sticky top-0 z-[100] flex items-center justify-between border-b border-border bg-bg/70 px-6 py-5 backdrop-blur-xl md:px-12"
        aria-label="Primary"
      >
        <Link
          href="/"
          className="flex items-center gap-3 font-display text-[22px] font-normal tracking-tight text-text outline-none focus-visible:ring-2 focus-visible:ring-mint focus-visible:ring-offset-2 focus-visible:ring-offset-bg"
        >
          <span>genera</span>
          <span className="text-mint font-light -mx-1">/</span>
          <span className="italic text-text-muted">tenders</span>
        </Link>

        <div className="hidden items-center gap-8 md:flex">
          {[...NAV_LINKS, ...SOFT_LINKS].map((link) => {
            const active = isActive(link);
            const soft = SOFT_LINKS.includes(link);
            return (
              <Link
                key={link.href}
                href={link.href}
                aria-current={active ? "page" : undefined}
                className={[
                  "border-b-2 py-2 text-sm transition-colors",
                  active
                    ? "border-mint text-text"
                    : "border-transparent text-text-muted hover:text-text",
                  soft ? "opacity-60 hover:opacity-100" : "",
                ].join(" ")}
                style={{ fontWeight: 450 }}
              >
                {link.label}
              </Link>
            );
          })}
        </div>

        <div className="flex items-center gap-4">
          <BridgeIndicator />
          <PushBell />
          <AccountSlot onOpenSignIn={() => setSignInOpen(true)} />
          {/* Mobile menu trigger */}
          <button
            type="button"
            className="md:hidden flex h-10 w-10 items-center justify-center rounded-lg border border-border bg-bg-elevated text-text-muted"
            aria-label="Toggle navigation"
            aria-expanded={mobileOpen}
            onClick={() => setMobileOpen((v) => !v)}
          >
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              {mobileOpen ? (
                <>
                  <line x1="18" y1="6" x2="6" y2="18" />
                  <line x1="6" y1="6" x2="18" y2="18" />
                </>
              ) : (
                <>
                  <line x1="3" y1="6" x2="21" y2="6" />
                  <line x1="3" y1="12" x2="21" y2="12" />
                  <line x1="3" y1="18" x2="21" y2="18" />
                </>
              )}
            </svg>
          </button>
        </div>
      </nav>

      {/* Mobile drawer */}
      {mobileOpen && (
        <div className="md:hidden border-b border-border bg-bg/95 backdrop-blur-xl">
          <ul className="flex flex-col px-6 py-4">
            {[...NAV_LINKS, ...SOFT_LINKS].map((link) => {
              const active = isActive(link);
              return (
                <li key={link.href}>
                  <Link
                    href={link.href}
                    onClick={() => setMobileOpen(false)}
                    className={[
                      "block py-3 text-sm transition-colors",
                      active ? "text-mint" : "text-text-muted hover:text-text",
                    ].join(" ")}
                  >
                    {link.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      <main className="mx-auto max-w-[1440px] px-6 md:px-12">{children}</main>

      <footer className="relative z-[3] mx-auto max-w-[1440px] border-t border-border px-6 py-12 md:px-12">
        <div className="flex flex-wrap items-center justify-between gap-6">
          <div className="flex flex-col gap-2">
            <span className="font-display text-[15px] text-text">
              Genera<span className="text-mint">/</span>Tenders
            </span>
            <span className="text-[13px] italic text-text-dim">
              Part of Genera Systems · governance built into the build.
            </span>
          </div>
          <div className="flex flex-wrap gap-7 text-[13px] text-text-muted">
            <a href="#methodology" className="transition-colors hover:text-text">
              Methodology
            </a>
            <a href="#sources" className="transition-colors hover:text-text">
              Sources
            </a>
            <a href="#privacy" className="transition-colors hover:text-text">
              Privacy
            </a>
          </div>
        </div>
      </footer>
    </div>
  );
}

/**
 * Right-hand header slot: shows a "Sign in" button when anonymous, or the
 * caller's email + plan chip + Sign out when logged in. The full SignInModal
 * is mounted by AppShellInner in the next commit; this component only
 * triggers `onOpenSignIn`.
 */
function AccountSlot({ onOpenSignIn }: { onOpenSignIn: () => void }) {
  const { me, loading, logout } = useAuth();

  if (loading) {
    return (
      <div
        className="h-10 w-24 rounded-full border border-border bg-bg-elevated"
        aria-hidden="true"
      />
    );
  }

  if (!me) {
    return (
      <button
        type="button"
        onClick={onOpenSignIn}
        className="rounded-full border border-border-strong bg-bg-elevated px-4 py-2 text-sm text-text-muted transition-colors hover:text-text"
      >
        Sign in
      </button>
    );
  }

  return (
    <div className="flex items-center gap-3">
      <div className="hidden flex-col items-end leading-tight md:flex">
        <span
          className="font-mono text-text"
          style={{ fontSize: "12px", letterSpacing: "0.04em" }}
          title={me.email}
        >
          {me.email}
        </span>
        <span
          className="text-mint-pale"
          style={{
            fontSize: "10px",
            letterSpacing: "0.1em",
            textTransform: "uppercase",
          }}
        >
          {planLabel(me.plan)}
        </span>
      </div>
      <button
        type="button"
        onClick={() => {
          void logout();
        }}
        className="rounded-full border border-border bg-bg-elevated px-3 py-2 text-xs text-text-muted transition-colors hover:text-text"
      >
        Sign out
      </button>
    </div>
  );
}

