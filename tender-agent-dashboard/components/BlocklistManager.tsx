"use client";

import Link from "next/link";
import { useState } from "react";
import useSWR from "swr";

import BlocklistRow from "./BlocklistRow";
import PillKicker from "./PillKicker";
import {
  type BlocklistEntry,
  addBlocklistDomain,
  fetcher,
  removeBlocklistDomain,
} from "@/lib/api";

export default function BlocklistManager() {
  const { data, error, isLoading, mutate } = useSWR<BlocklistEntry[]>(
    "/portal-blocklist",
    fetcher,
  );

  const [domain, setDomain] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!domain.trim()) return;
    setBusy(true);
    setErrorMsg(null);
    try {
      await addBlocklistDomain({
        domain: domain.trim(),
        reason: reason.trim() || undefined,
      });
      setDomain("");
      setReason("");
      await mutate();
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? err.message : "Failed to add domain.",
      );
    } finally {
      setBusy(false);
    }
  };

  const remove = async (entry: BlocklistEntry) => {
    if (
      !window.confirm(
        `Remove ${entry.domain} from the blocklist? Existing portals for this domain are NOT removed; new sightings will start being created again.`,
      )
    ) {
      return;
    }
    setBusy(true);
    setErrorMsg(null);
    try {
      await removeBlocklistDomain(entry.id);
      await mutate();
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? err.message : "Failed to remove domain.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-10 py-14">
      <header className="space-y-5">
        <PillKicker withDot={false}>Portal blocklist · noise filter</PillKicker>
        <h1
          className="font-display text-text text-balance"
          style={{
            fontSize: "clamp(32px, 4vw, 52px)",
            fontWeight: 400,
            letterSpacing: "-0.03em",
            lineHeight: 1.05,
            fontVariationSettings: '"opsz" 96',
          }}
        >
          Domains we ignore.
        </h1>
        <p
          className="text-text-muted"
          style={{ fontSize: "16px", lineHeight: 1.6, maxWidth: "720px" }}
        >
          Add a domain here to stop the URL extractor from clustering it as a
          portal. Source domains we already poll, social media, and the gov.uk
          apex are seeded; add anything else that turns out to be noise.
        </p>
        <div>
          <Link
            href="/portals"
            className="text-text-muted hover:text-text"
            style={{ fontSize: "13px" }}
          >
            ← Portal registry
          </Link>
        </div>
      </header>

      <form
        onSubmit={add}
        className="rounded-2xl border border-border bg-bg-elevated p-7 backdrop-blur-md flex flex-wrap items-end gap-4"
        style={{ borderRadius: "18px" }}
      >
        <div className="flex flex-col gap-1.5 flex-1 min-w-[220px]">
          <label
            htmlFor="bl-domain"
            className="text-text-dim"
            style={{ fontSize: "11px", letterSpacing: "0.1em", textTransform: "uppercase" }}
          >
            Domain
          </label>
          <input
            id="bl-domain"
            type="text"
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
            placeholder="example.com"
            required
            className="bg-bg border border-border rounded-lg px-3 py-2 text-sm text-text font-mono outline-none focus:border-mint-pale/60"
          />
        </div>
        <div className="flex flex-col gap-1.5 flex-[2] min-w-[220px]">
          <label
            htmlFor="bl-reason"
            className="text-text-dim"
            style={{ fontSize: "11px", letterSpacing: "0.1em", textTransform: "uppercase" }}
          >
            Reason
          </label>
          <input
            id="bl-reason"
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="optional — why this is noise"
            className="bg-bg border border-border rounded-lg px-3 py-2 text-sm text-text outline-none focus:border-mint-pale/60"
          />
        </div>
        <button
          type="submit"
          disabled={busy || !domain.trim()}
          className="btn-primary disabled:opacity-50"
        >
          Add domain
        </button>
      </form>

      {errorMsg && (
        <div
          role="alert"
          className="rounded-xl border border-danger/40 bg-danger/5 p-5 text-text"
          style={{ borderRadius: "18px", fontSize: "14px" }}
        >
          {errorMsg}
        </div>
      )}

      {error ? (
        <div
          role="alert"
          className="rounded-xl border border-danger/40 bg-danger/5 p-5 text-text"
          style={{ borderRadius: "18px", fontSize: "14px" }}
        >
          Failed to load blocklist.
        </div>
      ) : isLoading && !data ? (
        <div className="text-text-dim" style={{ fontSize: "14px" }}>
          Loading…
        </div>
      ) : (
        <div
          className="rounded-2xl border border-border bg-bg-elevated p-6 backdrop-blur-md overflow-x-auto"
          style={{ borderRadius: "18px" }}
        >
          <table className="w-full text-left">
            <thead
              className="text-text-dim"
              style={{ fontSize: "11px", letterSpacing: "0.1em", textTransform: "uppercase" }}
            >
              <tr>
                <th className="pb-3 pr-4 font-normal">Domain</th>
                <th className="pb-3 pr-4 font-normal">Reason</th>
                <th className="pb-3 pr-4 font-normal">Added by</th>
                <th className="pb-3 pr-4 font-normal">When</th>
                <th className="pb-3 pl-4 text-right font-normal" />
              </tr>
            </thead>
            <tbody>
              {(data ?? []).map((e) => (
                <BlocklistRow
                  key={e.id}
                  entry={e}
                  busy={busy}
                  onRemove={() => remove(e)}
                />
              ))}
            </tbody>
          </table>
          {data && data.length === 0 && (
            <p className="text-text-dim py-6" style={{ fontSize: "14px" }}>
              No domains in the blocklist.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
