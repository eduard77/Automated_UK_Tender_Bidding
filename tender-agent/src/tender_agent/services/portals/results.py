"""Structured result types + status enums for portal adapters.

Every adapter method returns one of these — never raw data, never a bare bool.
The orchestrator branches on the status enum, so adding a new terminal state
is a matter of adding an enum member and a branch.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class AuthStatus(StrEnum):
    success = "success"
    needs_registration = "needs_registration"
    invalid_credentials = "invalid_credentials"
    requires_2fa = "requires_2fa"
    blocked = "blocked"
    error = "error"


class LocateStatus(StrEnum):
    found = "found"
    not_found = "not_found"
    requires_interest_first = "requires_interest_first"
    access_denied = "access_denied"
    error = "error"


class RegisterStatus(StrEnum):
    success = "success"
    already_registered = "already_registered"
    requires_manual = "requires_manual"
    error = "error"


class DownloadStatus(StrEnum):
    complete = "complete"
    partial = "partial"
    nothing_available = "nothing_available"
    error = "error"


@dataclass
class DownloadedFile:
    url: str
    path: str
    filename: str
    bytes: int
    content_type: str | None = None


@dataclass
class AuthResult:
    status: AuthStatus
    detail: str | None = None


@dataclass
class LocateResult:
    status: LocateStatus
    tender_url: str | None = None
    detail: str | None = None


@dataclass
class RegisterResult:
    status: RegisterStatus
    detail: str | None = None


@dataclass
class DownloadResult:
    status: DownloadStatus
    files: list[DownloadedFile] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    detail: str | None = None


@dataclass
class ScreenshotResult:
    path: str | None = None
    captured: bool = False
    detail: str | None = None
