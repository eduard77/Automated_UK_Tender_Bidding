"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import { fetchMe, logout as apiLogout, type Me } from "./api";

export interface AuthState {
  me: Me | null;
  loading: boolean;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const next = await fetchMe();
      setMe(next);
    } catch {
      // fetchMe already swallows non-2xx, but be defensive: a thrown
      // network error must not blow up the provider — anonymous is the
      // default state.
      setMe(null);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const next = await fetchMe();
        if (!cancelled) setMe(next);
      } catch {
        if (!cancelled) setMe(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const logout = useCallback(async () => {
    try {
      await apiLogout();
    } finally {
      setMe(null);
    }
  }, []);

  const value = useMemo<AuthState>(
    () => ({ me, loading, refresh, logout }),
    [me, loading, refresh, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    // Permissive fallback for callers rendered outside the provider
    // (e.g. isolated unit tests). The server is the source of truth for
    // what content is actually returned, so a missing client context can
    // never grant access — it just lets the component render its
    // anonymous state without crashing.
    return {
      me: null,
      loading: false,
      refresh: async () => {},
      logout: async () => {},
    };
  }
  return ctx;
}

const PLAN_LABELS: Record<Me["plan"], string> = {
  free: "Free",
  payg: "PAYG",
  plan_100: "Plan 100",
  plan_unlimited: "Unlimited",
};

export function planLabel(plan: Me["plan"]): string {
  return PLAN_LABELS[plan] ?? plan;
}
