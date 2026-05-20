"""Portal adapter framework (Phase 4 chunk 2)."""
from tender_agent.services.portals.base import (
    Credentials,
    PortalAdapter,
    PortalContext,
)
from tender_agent.services.portals.fallback import FallbackAdapter
from tender_agent.services.portals.registry import (
    ADAPTERS,
    get_adapter_for_platform,
    get_fallback_adapter,
)
from tender_agent.services.portals.results import (
    AuthResult,
    AuthStatus,
    DownloadResult,
    DownloadStatus,
    LocateResult,
    LocateStatus,
    RegisterResult,
    RegisterStatus,
    ScreenshotResult,
)

__all__ = [
    "ADAPTERS",
    "AuthResult",
    "AuthStatus",
    "Credentials",
    "DownloadResult",
    "DownloadStatus",
    "FallbackAdapter",
    "LocateResult",
    "LocateStatus",
    "PortalAdapter",
    "PortalContext",
    "RegisterResult",
    "RegisterStatus",
    "ScreenshotResult",
    "get_adapter_for_platform",
    "get_fallback_adapter",
]
