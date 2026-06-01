"use client";

import { useState } from "react";
import { freeAlertsSignup, login, signup, type Me } from "@/lib/api";

type Mode = "login" | "signup" | "alerts";

interface Props {
  open: boolean;
  initialMode?: Mode;
  onClose: () => void;
  onAuthed?: (me: Me | null) => void;
}

/**
 * Lightweight email+password modal. Three modes share the same form:
 *  - login    — existing user signs in.
 *  - signup   — paid tier signup (the user will then hit Stripe Checkout).
 *  - alerts   — FREE alerts signup; never touches Stripe.
 *
 * Server-side gating means even if the modal isn't shown, the backend
 * always returns the half-preview for unentitled callers. This is UX, not
 * security.
 */
export function SignInModal({ open, initialMode = "signup", onClose, onAuthed }: Props) {
  const [mode, setMode] = useState<Mode>(initialMode);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open) return null;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === "login") {
        const me = await login(email, password);
        onAuthed?.(me);
      } else if (mode === "signup") {
        const me = await signup(email, password);
        onAuthed?.(me);
      } else {
        await freeAlertsSignup(email, password);
        onAuthed?.(null);
      }
      onClose();
    } catch (e2) {
      setError((e2 as Error).message || "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  const title =
    mode === "alerts"
      ? "Get free tender alerts"
      : mode === "login"
        ? "Sign in"
        : "Create an account";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/70 p-4">
      <div className="w-full max-w-md rounded-xl bg-slate-800 p-6 text-slate-100 shadow-2xl">
        <div className="mb-4 flex items-start justify-between">
          <h2 className="text-lg font-semibold">{title}</h2>
          <button
            type="button"
            className="text-slate-400 hover:text-slate-200"
            onClick={onClose}
            aria-label="Close"
          >
            ×
          </button>
        </div>
        <form onSubmit={submit} className="space-y-3">
          <label className="block text-sm">
            <span className="mb-1 block text-slate-300">Email</span>
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-slate-100 focus:border-amber-400 focus:outline-none"
            />
          </label>
          <label className="block text-sm">
            <span className="mb-1 block text-slate-300">Password</span>
            <input
              type="password"
              required
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-slate-100 focus:border-amber-400 focus:outline-none"
            />
            <span className="mt-1 block text-xs text-slate-500">
              Minimum 8 characters. Never shown to anyone but you.
            </span>
          </label>
          {error && <p className="text-sm text-red-400">{error}</p>}
          <button
            type="submit"
            disabled={busy}
            className="w-full rounded-md bg-amber-400 px-3 py-2 text-sm font-medium text-slate-900 hover:bg-amber-300 disabled:bg-slate-600"
          >
            {busy
              ? "…"
              : mode === "alerts"
                ? "Enable alerts"
                : mode === "login"
                  ? "Sign in"
                  : "Create account"}
          </button>
        </form>
        <div className="mt-4 flex justify-between text-xs text-slate-400">
          <ModeLink current={mode} target="login" onClick={setMode}>
            Sign in
          </ModeLink>
          <ModeLink current={mode} target="signup" onClick={setMode}>
            Create account
          </ModeLink>
          <ModeLink current={mode} target="alerts" onClick={setMode}>
            Free alerts
          </ModeLink>
        </div>
      </div>
    </div>
  );
}

function ModeLink({
  current,
  target,
  onClick,
  children,
}: {
  current: Mode;
  target: Mode;
  onClick: (m: Mode) => void;
  children: React.ReactNode;
}) {
  const active = current === target;
  return (
    <button
      type="button"
      onClick={() => onClick(target)}
      className={
        active
          ? "text-amber-300"
          : "text-slate-500 hover:text-slate-200"
      }
    >
      {children}
    </button>
  );
}
