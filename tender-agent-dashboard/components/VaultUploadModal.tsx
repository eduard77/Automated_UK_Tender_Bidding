"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import {
  ApiError,
  uploadVaultDocument,
  vaultCategoryLabel,
  type VaultCategory,
  type VaultDocument,
} from "@/lib/api";

const ACCEPTED_TYPES =
  "application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/msword,text/plain";

const CATEGORY_OPTIONS: VaultCategory[] = [
  "insurance",
  "accreditation",
  "policy",
  "financial",
  "capability",
  "people",
  "technical",
  "past_bid",
  "corporate",
  "uncategorised",
];

type State =
  | { kind: "idle" }
  | { kind: "uploading" }
  | { kind: "success"; document: VaultDocument }
  | { kind: "error"; message: string };

export default function VaultUploadModal({
  open,
  onClose,
  onUploaded,
}: {
  open: boolean;
  onClose: () => void;
  onUploaded?: (doc: VaultDocument) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [description, setDescription] = useState("");
  const [title, setTitle] = useState("");
  const [category, setCategory] = useState<VaultCategory>("uncategorised");
  const [dragActive, setDragActive] = useState(false);
  const [state, setState] = useState<State>({ kind: "idle" });
  const inputRef = useRef<HTMLInputElement | null>(null);
  const firstFocusRef = useRef<HTMLButtonElement | null>(null);

  // ESC closes modal (when idle).
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape" && state.kind !== "uploading") {
        onClose();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onClose, state.kind]);

  // Reset state every time the modal opens.
  useEffect(() => {
    if (open) {
      setState({ kind: "idle" });
      setFile(null);
      setDescription("");
      setTitle("");
      setCategory("uncategorised");
      // Move focus into the dialog.
      setTimeout(() => firstFocusRef.current?.focus(), 0);
    }
  }, [open]);

  if (!open) return null;

  const handleFile = (incoming: File | null) => {
    setFile(incoming);
    if (incoming && !title) {
      // Auto-fill the title from filename minus extension.
      const noExt = incoming.name.replace(/\.[^.]+$/, "");
      setTitle(noExt);
    }
  };

  const onDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragActive(false);
    const f = e.dataTransfer.files[0];
    if (f) handleFile(f);
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file || !description.trim()) return;
    setState({ kind: "uploading" });
    const fd = new FormData();
    fd.append("file", file);
    fd.append("description", description.trim());
    if (title.trim()) fd.append("title", title.trim());
    fd.append("category", category);
    try {
      const doc = await uploadVaultDocument(fd);
      setState({ kind: "success", document: doc });
      onUploaded?.(doc);
    } catch (err) {
      let msg = "Upload failed";
      if (err instanceof ApiError) {
        const d = err.detail;
        if (d && typeof d === "object" && "detail" in d) {
          msg = String((d as { detail: unknown }).detail);
        } else {
          msg = `${err.status}: ${err.message}`;
        }
      } else if (err instanceof Error) {
        msg = err.message;
      }
      setState({ kind: "error", message: msg });
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="vault-upload-heading"
      className="fixed inset-0 z-[200] flex items-center justify-center px-4 py-8"
    >
      <div
        className="absolute inset-0 bg-bg/70 backdrop-blur-md"
        onClick={() => {
          if (state.kind !== "uploading") onClose();
        }}
      />
      <div
        className="relative max-h-[90vh] w-full max-w-[640px] overflow-y-auto rounded-2xl border border-border-strong bg-bg-elevated-strong p-10 backdrop-blur-xl"
        style={{ borderRadius: "24px" }}
      >
        {state.kind === "success" ? (
          <SuccessPanel
            doc={state.document}
            onAddAnother={() => {
              setState({ kind: "idle" });
              setFile(null);
              setDescription("");
              setTitle("");
              setCategory("uncategorised");
              setTimeout(() => firstFocusRef.current?.focus(), 0);
            }}
            onClose={onClose}
          />
        ) : (
          <form onSubmit={submit} className="space-y-7">
            <header className="flex items-start justify-between gap-6">
              <div>
                <span className="pill-kicker">Vault upload</span>
                <h2
                  id="vault-upload-heading"
                  className="font-display text-text mt-4"
                  style={{
                    fontSize: "32px",
                    fontWeight: 400,
                    letterSpacing: "-0.025em",
                    lineHeight: 1.1,
                  }}
                >
                  Add evidence.
                </h2>
              </div>
              <button
                ref={firstFocusRef}
                type="button"
                aria-label="Close"
                onClick={onClose}
                disabled={state.kind === "uploading"}
                className="flex h-9 w-9 items-center justify-center rounded-lg border border-border text-text-muted transition-colors hover:border-border-strong hover:text-text disabled:opacity-50"
              >
                ×
              </button>
            </header>

            <Dropzone
              file={file}
              dragActive={dragActive}
              onPick={() => inputRef.current?.click()}
              onDrop={onDrop}
              onDragOver={(e) => {
                e.preventDefault();
                setDragActive(true);
              }}
              onDragLeave={() => setDragActive(false)}
              disabled={state.kind === "uploading"}
            />
            <input
              ref={inputRef}
              type="file"
              accept={ACCEPTED_TYPES}
              hidden
              onChange={(e) => handleFile(e.target.files?.[0] ?? null)}
              disabled={state.kind === "uploading"}
            />

            <FormField
              label="What is this document?"
              required
              help="A line in your own words — gets stored with the document and helps the (forthcoming) classifier."
            >
              <textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="e.g. 2026 PI insurance renewal certificate from Hiscox, 10m cover."
                rows={3}
                required
                disabled={state.kind === "uploading"}
                className="w-full resize-none rounded-md border border-border-strong bg-bg-elevated px-3.5 py-2.5 text-[14px] text-text placeholder:text-text-dim transition-colors focus:border-mint focus:outline-none"
              />
            </FormField>

            <div className="grid grid-cols-1 gap-5 md:grid-cols-[1fr_220px]">
              <FormField
                label="Title (auto-filled from filename)"
                help="Leave blank to use the filename."
              >
                <input
                  type="text"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  placeholder="Optional"
                  disabled={state.kind === "uploading"}
                  className="w-full rounded-md border border-border-strong bg-bg-elevated px-3.5 py-2.5 text-[14px] text-text placeholder:text-text-dim transition-colors focus:border-mint focus:outline-none"
                />
              </FormField>
              <FormField label="Category">
                <select
                  value={category}
                  onChange={(e) => setCategory(e.target.value as VaultCategory)}
                  disabled={state.kind === "uploading"}
                  className="w-full cursor-pointer rounded-md border border-border-strong bg-bg-elevated px-3.5 py-2.5 text-[14px] text-text transition-colors focus:border-mint focus:outline-none"
                >
                  {CATEGORY_OPTIONS.map((c) => (
                    <option key={c} value={c}>
                      {vaultCategoryLabel(c)}
                    </option>
                  ))}
                </select>
              </FormField>
            </div>

            {state.kind === "error" && (
              <div
                role="alert"
                className="rounded-xl border border-danger/40 bg-danger/5 p-4 text-text"
                style={{ borderRadius: "12px", fontSize: "13px", lineHeight: 1.55 }}
              >
                {state.message}
              </div>
            )}

            <div className="flex flex-wrap gap-3 border-t border-border pt-6">
              <button
                type="submit"
                disabled={!file || !description.trim() || state.kind === "uploading"}
                className="btn-primary"
              >
                {state.kind === "uploading" ? "Storing…" : "Upload to vault"}
                {state.kind !== "uploading" && <span className="arrow">→</span>}
              </button>
              <button
                type="button"
                onClick={onClose}
                disabled={state.kind === "uploading"}
                className="btn-secondary"
              >
                Cancel
              </button>
              {state.kind === "uploading" && (
                <span
                  className="text-text-muted ml-auto self-center"
                  style={{ fontSize: "13px" }}
                >
                  This may take a moment if the document is large.
                </span>
              )}
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

function Dropzone({
  file,
  dragActive,
  onPick,
  onDrop,
  onDragOver,
  onDragLeave,
  disabled,
}: {
  file: File | null;
  dragActive: boolean;
  onPick: () => void;
  onDrop: (e: React.DragEvent<HTMLDivElement>) => void;
  onDragOver: (e: React.DragEvent<HTMLDivElement>) => void;
  onDragLeave: () => void;
  disabled?: boolean;
}) {
  return (
    <div
      onClick={!disabled ? onPick : undefined}
      onDrop={!disabled ? onDrop : undefined}
      onDragOver={!disabled ? onDragOver : undefined}
      onDragLeave={!disabled ? onDragLeave : undefined}
      role="button"
      tabIndex={disabled ? -1 : 0}
      onKeyDown={(e) => {
        if (!disabled && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          onPick();
        }
      }}
      className={[
        "rounded-2xl border border-dashed p-8 text-center transition-colors outline-none",
        dragActive ? "border-mint bg-mint/5" : "border-border-strong bg-bg-elevated",
        disabled
          ? "cursor-not-allowed opacity-60"
          : "cursor-pointer hover:border-mint-pale focus-visible:border-mint",
      ].join(" ")}
      style={{ borderRadius: "18px" }}
      aria-label="Choose or drop a file"
    >
      {file ? (
        <div>
          <p
            className="display-num text-mint-pale"
            style={{ fontSize: "20px", letterSpacing: "-0.015em" }}
          >
            {file.name}
          </p>
          <p
            className="text-text-dim mt-2 font-mono"
            style={{ fontSize: "12px" }}
          >
            {(file.size / 1024).toFixed(1)} KB · {file.type || "unknown type"}
          </p>
          <p
            className="text-text-muted mt-3"
            style={{ fontSize: "13px" }}
          >
            Click or drop to replace
          </p>
        </div>
      ) : (
        <div>
          <svg
            width="40"
            height="40"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="mx-auto mb-4 text-text-dim"
            aria-hidden="true"
          >
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
            <polyline points="17 8 12 3 7 8" />
            <line x1="12" y1="3" x2="12" y2="15" />
          </svg>
          <p
            className="font-display text-text"
            style={{ fontSize: "18px", letterSpacing: "-0.01em" }}
          >
            Drop a PDF or DOCX here.
          </p>
          <p
            className="text-text-muted mt-2"
            style={{ fontSize: "13px" }}
          >
            Or click to browse.
          </p>
        </div>
      )}
    </div>
  );
}

function FormField({
  label,
  required,
  help,
  children,
}: {
  label: string;
  required?: boolean;
  help?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <span
        className="block text-text-dim"
        style={{
          fontSize: "11px",
          fontWeight: 500,
          letterSpacing: "0.12em",
          textTransform: "uppercase",
        }}
      >
        {label}
        {required && <span className="text-mint"> *</span>}
      </span>
      <div className="mt-2">{children}</div>
      {help && (
        <span
          className="mt-2 block text-text-dim"
          style={{
            fontSize: "11px",
            lineHeight: 1.5,
            letterSpacing: "0.04em",
          }}
        >
          {help}
        </span>
      )}
    </label>
  );
}

function SuccessPanel({
  doc,
  onAddAnother,
  onClose,
}: {
  doc: VaultDocument;
  onAddAnother: () => void;
  onClose: () => void;
}) {
  return (
    <div className="space-y-7">
      <header>
        <span
          className="inline-flex items-center gap-2 pill-kicker"
          style={{ color: "#9ff2b2" }}
        >
          <span
            aria-hidden="true"
            className="inline-block h-1.5 w-1.5 rounded-full bg-mint"
          />
          Stored
        </span>
        <h2
          className="font-display text-text mt-4"
          style={{
            fontSize: "32px",
            fontWeight: 400,
            letterSpacing: "-0.025em",
            lineHeight: 1.1,
          }}
        >
          Saved to the vault.
        </h2>
      </header>
      <p
        className="text-text-muted"
        style={{ fontSize: "15px", lineHeight: 1.6 }}
      >
        Stored as <strong className="text-text">{doc.title}</strong> in{" "}
        <strong className="text-text">{vaultCategoryLabel(doc.category)}</strong>.
        Open the document detail to fill in the structured claim values
        (cover amount, expiry, certifier, etc.) so the matcher can reason over them.
      </p>
      <div className="rounded-xl border border-border bg-bg-elevated p-5" style={{ borderRadius: "12px" }}>
        <p
          className="text-text-dim"
          style={{
            fontSize: "11px",
            letterSpacing: "0.12em",
            textTransform: "uppercase",
            fontWeight: 500,
          }}
        >
          Claims status
        </p>
        <p
          className="text-text-muted mt-2"
          style={{ fontSize: "13px", lineHeight: 1.55 }}
        >
          The blob was stored. Auto-extraction of structured claims is a
          follow-up — for now, edit claims by hand on the detail page.
        </p>
      </div>
      <div className="flex flex-wrap gap-3 border-t border-border pt-6">
        <Link
          href={`/vault/${doc.id}`}
          onClick={onClose}
          className="btn-primary"
        >
          Review and fill claims <span className="arrow">→</span>
        </Link>
        <button type="button" onClick={onAddAnother} className="btn-secondary">
          Add another
        </button>
      </div>
    </div>
  );
}
