"""Text-to-embedding abstraction for vault documents.

Anthropic doesn't ship an embeddings API. The vault stores `vector(1536)` —
matching OpenAI's `text-embedding-3-small` (the safe, ubiquitous default for
1536-dim embeddings) and Voyage AI's `voyage-3` family.

This module ships two embedders out of the box:

- `NullEmbedder` — returns an all-zero vector. The matcher's semantic step
  silently degrades to "no candidate" when active. Pick this when no real
  provider is configured; structured filters still do useful work.
- `HashEmbedder` — deterministic pseudo-random 1536-dim vector keyed on the
  SHA-256 of the text. Use in tests and local dev where you want pgvector's
  `<=>` operator to actually rank, but you don't want to burn API credits.

Production wires a real provider behind the same `Embedder` interface (see
`docs/vault.md` "Wiring a real embedder"). Keeping the choice off
`pyproject.toml`'s mandatory dependencies means a fresh checkout always
installs cleanly without an embedding-API SDK.
"""
from __future__ import annotations

import hashlib
import struct
from abc import ABC, abstractmethod

import structlog

logger = structlog.get_logger(__name__)

EMBEDDING_DIM = 1536


class Embedder(ABC):
    """Text -> fixed-size float vector."""

    dim: int = EMBEDDING_DIM

    @abstractmethod
    def embed(self, text: str) -> list[float]: ...


class NullEmbedder(Embedder):
    """Returns all-zero vectors. Production fallback when no provider is wired.

    Cosine similarity against an all-zero vector is undefined, so the matcher
    treats a zero embedding as "no semantic signal" — structured filters
    still work, but the semantic-search step contributes nothing.
    """

    def __init__(self) -> None:
        self._warned = False

    def embed(self, text: str) -> list[float]:
        if not self._warned:
            logger.warning(
                "vault.embeddings.null_embedder_in_use",
                hint="Wire a real Embedder for semantic search to contribute.",
            )
            self._warned = True
        return [0.0] * self.dim


class HashEmbedder(Embedder):
    """Deterministic pseudo-random embedder keyed on the text's SHA-256.

    Same text always produces the same vector, different texts produce
    different vectors, and the magnitude is normalised so cosine similarity
    is in [-1, 1] as expected. Useful for tests and dev — does NOT carry
    semantic meaning, so two semantically-similar texts produce unrelated
    vectors. Don't use in production.
    """

    def embed(self, text: str) -> list[float]:
        # Stream chunks of SHA-256 to produce dim*4 bytes deterministically.
        out: list[float] = []
        counter = 0
        while len(out) < self.dim:
            h = hashlib.sha256(f"{counter}:{text}".encode()).digest()
            for i in range(0, len(h), 4):
                if len(out) >= self.dim:
                    break
                # Unpack 4 bytes as little-endian unsigned int, map to [-1, 1].
                (val,) = struct.unpack("<I", h[i : i + 4])
                out.append((val / 0xFFFFFFFF) * 2 - 1)
            counter += 1

        # Normalise so the vector lies on the unit hypersphere; cosine sim
        # against another normalised vector reduces to a dot product.
        norm = sum(v * v for v in out) ** 0.5
        return [v / norm for v in out] if norm > 0 else out


_DEFAULT: Embedder | None = None


def get_embedder() -> Embedder:
    """Return the configured embedder. Defaults to `NullEmbedder` so a fresh
    install boots without an embedding provider — tests + dev override via
    `set_embedder()`."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = NullEmbedder()
    return _DEFAULT


def set_embedder(embedder: Embedder) -> None:
    """Override the module-level embedder. Used by tests and by production
    bootstrap (where a real provider's client is constructed once and
    injected)."""
    global _DEFAULT
    _DEFAULT = embedder
