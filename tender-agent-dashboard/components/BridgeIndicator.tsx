"use client";

import useSWR from "swr";

import { getBridgeHealth } from "@/lib/api";

// One-glance indicator: green when the native browser bridge is running, grey
// when it's down. Polls every 20s.
export default function BridgeIndicator() {
  const { data } = useSWR("bridge-health", () => getBridgeHealth(), {
    refreshInterval: 20_000,
    shouldRetryOnError: false,
  });
  const up = data?.available ?? false;
  return (
    <span
      className="hidden items-center gap-1.5 sm:inline-flex"
      title={
        up
          ? "Browser bridge is running"
          : "Browser bridge is not running — start start-bridge.ps1 to fetch from login portals"
      }
      style={{ fontSize: "12px" }}
    >
      <span
        aria-hidden="true"
        className="inline-block h-2 w-2 rounded-full"
        style={{
          background: up ? "#97d5bc" : "rgba(255,255,255,0.28)",
          boxShadow: up ? "0 0 6px rgba(151,213,188,0.7)" : "none",
        }}
      />
      <span className="text-text-dim">Bridge</span>
    </span>
  );
}
