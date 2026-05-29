"""Registry mapping a platform slug to its adapter class.

Empty of real platform adapters in chunk 2 — they arrive in later prompts.
`get_adapter_for_platform` returns the registered adapter class for a slug, or
None so the orchestrator can fall back to FallbackAdapter.
"""
from __future__ import annotations

from tender_agent.services.portals.adapters.delta_esourcing import (
    DeltaEsourcingAdapter,
)
from tender_agent.services.portals.adapters.proactis import ProactisAdapter
from tender_agent.services.portals.base import PortalAdapter
from tender_agent.services.portals.contracts_finder import (
    ContractsFinderDirectAdapter,
)
from tender_agent.services.portals.fallback import FallbackAdapter

# slug -> adapter class. Populated as real adapters land (prompts 3-9).
# `procontract` is the seeded platform slug for procontract.due-north.com
# (migration 0006). The separate `proactis` platform (proactisp2p.com) is a
# DIFFERENT product (Proactis P2P), so it is intentionally NOT mapped here.
ADAPTERS: dict[str, type[PortalAdapter]] = {
    "contracts_finder_direct": ContractsFinderDirectAdapter,
    "delta_esourcing": DeltaEsourcingAdapter,
    "procontract": ProactisAdapter,
}


def get_adapter_for_platform(slug: str | None) -> type[PortalAdapter] | None:
    """Return the adapter class for a platform slug, or None if unregistered."""
    if slug is None:
        return None
    return ADAPTERS.get(slug)


def get_fallback_adapter() -> type[PortalAdapter]:
    return FallbackAdapter
