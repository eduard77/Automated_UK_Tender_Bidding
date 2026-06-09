"""The three providers sit behind ONE interface, exercised fully offline.

Gmail + Outlook are driven with httpx.MockTransport (the source-adapter
convention). Yahoo is the deferred slot: configured() is False and every call
raises a clear "not yet configured" error. No real mailbox is contacted.
"""
from __future__ import annotations

import base64

import httpx
import pytest

from tender_agent.services.email.providers import (
    build_provider,
    canonical_provider,
    provider_configured,
)
from tender_agent.services.email.providers.base import (
    EmailProvider,
    OAuthTokens,
    ProviderNotConfiguredError,
)
from tender_agent.services.email.providers.gmail import GmailProvider
from tender_agent.services.email.providers.outlook import OutlookProvider
from tender_agent.services.email.providers.yahoo import YahooProvider


def _b64url(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- Gmail ------------------------------------------------------------------

_GMAIL_MSG = {
    "internalDate": "1700000000000",
    "payload": {
        "headers": [
            {"name": "Subject", "value": "DN12345 clarification"},
            {"name": "From", "value": "buyer@council.gov.uk"},
        ],
        "parts": [
            {
                "mimeType": "text/plain",
                "body": {"data": _b64url("Body. See https://example.com/doc")},
            },
            {
                "mimeType": "application/pdf",
                "filename": "spec.pdf",
                "body": {"attachmentId": "att1"},
            },
        ],
    },
}


def _gmail_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.method == "POST" and path.endswith("/token"):
        return httpx.Response(
            200,
            json={
                "access_token": "at",
                "refresh_token": "rt",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )
    if path.endswith("/profile"):
        return httpx.Response(200, json={"emailAddress": "me@gmail.com"})
    if "/attachments/" in path:
        return httpx.Response(200, json={"data": _b64url("PDFBYTES")})
    if path.endswith("/messages/m1"):
        return httpx.Response(200, json=_GMAIL_MSG)
    if path.endswith("/messages"):
        return httpx.Response(200, json={"messages": [{"id": "m1"}]})
    return httpx.Response(404, json={})


def _gmail() -> GmailProvider:
    return GmailProvider(
        client_id="cid",
        client_secret="sec",
        redirect_uri="https://cb/email/oauth/callback",
        client=_client(_gmail_handler),
    )


def test_gmail_authorization_url_has_scope_state_and_client() -> None:
    url = _gmail().authorization_url(state="xyz")
    assert "client_id=cid" in url
    assert "gmail.readonly" in url
    assert "state=xyz" in url


@pytest.mark.asyncio
async def test_gmail_exchange_and_refresh() -> None:
    g = _gmail()
    tokens = await g.exchange_code("code")
    assert tokens.access_token == "at"
    assert tokens.refresh_token == "rt"
    assert tokens.expiry is not None
    # Google omits refresh_token on refresh -> the prior one is preserved.
    g2 = GmailProvider(
        client_id="cid",
        client_secret="sec",
        redirect_uri="https://cb",
        client=_client(
            lambda r: httpx.Response(
                200, json={"access_token": "at2", "expires_in": 3600}
            )
        ),
    )
    refreshed = await g2.refresh(OAuthTokens(access_token="old", refresh_token="rt"))
    assert refreshed.access_token == "at2"
    assert refreshed.refresh_token == "rt"


@pytest.mark.asyncio
async def test_gmail_list_and_fetch_with_attachment_and_links() -> None:
    g = _gmail()
    tokens = OAuthTokens(access_token="at")
    refs = await g.list_recent(tokens, since=None, max_results=10)
    assert [r.id for r in refs] == ["m1"]

    msg = await g.fetch_message(tokens, "m1")
    assert msg.subject == "DN12345 clarification"
    assert msg.sender == "buyer@council.gov.uk"
    assert len(msg.attachments) == 1
    assert msg.attachments[0].filename == "spec.pdf"
    assert msg.attachments[0].data == b"PDFBYTES"
    assert "https://example.com/doc" in msg.links


# --- Outlook ----------------------------------------------------------------

_GRAPH_MSG = {
    "id": "o1",
    "subject": "PROC-2026-0099 documents",
    "from": {"emailAddress": {"address": "buyer@nhs.uk"}},
    "receivedDateTime": "2026-06-09T10:00:00Z",
    "body": {"contentType": "html", "content": "<p>See <a>link</a> here</p>"},
    "hasAttachments": True,
}
_GRAPH_ATTACHMENTS = {
    "value": [
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": "itt.docx",
            "contentType": "application/vnd.openxmlformats",
            "contentBytes": base64.b64encode(b"DOCXBYTES").decode(),
        },
        {
            "@odata.type": "#microsoft.graph.itemAttachment",
            "name": "ignored",
        },
    ]
}


def _graph_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.method == "POST" and path.endswith("/token"):
        return httpx.Response(
            200,
            json={
                "access_token": "gat",
                "refresh_token": "grt",
                "expires_in": 3600,
            },
        )
    if path.endswith("/attachments"):
        return httpx.Response(200, json=_GRAPH_ATTACHMENTS)
    if path.endswith("/messages/o1"):
        return httpx.Response(200, json=_GRAPH_MSG)
    if path.endswith("/messages"):
        return httpx.Response(200, json={"value": [{"id": "o1"}]})
    if path.endswith("/me"):
        return httpx.Response(200, json={"mail": "me@outlook.com"})
    return httpx.Response(404, json={})


def _outlook() -> OutlookProvider:
    return OutlookProvider(
        client_id="cid",
        client_secret="sec",
        redirect_uri="https://cb",
        client=_client(_graph_handler),
    )


def test_outlook_authorization_url_is_readonly_scope() -> None:
    url = _outlook().authorization_url(state="s1")
    assert "Mail.Read" in url
    assert "Mail.Send" not in url
    assert "state=s1" in url


@pytest.mark.asyncio
async def test_outlook_fetch_only_files_file_attachments() -> None:
    o = _outlook()
    tokens = await o.exchange_code("code")
    assert tokens.access_token == "gat"
    addr = await o.get_address(tokens)
    assert addr == "me@outlook.com"

    msg = await o.fetch_message(tokens, "o1")
    assert msg.subject == "PROC-2026-0099 documents"
    # Only the fileAttachment is pulled; the itemAttachment is skipped.
    assert len(msg.attachments) == 1
    assert msg.attachments[0].filename == "itt.docx"
    assert msg.attachments[0].data == b"DOCXBYTES"


# --- Yahoo (deferred) + interface -------------------------------------------


def test_yahoo_is_deferred_not_configured() -> None:
    y = YahooProvider()
    assert y.configured() is False
    with pytest.raises(ProviderNotConfiguredError) as exc:
        y.authorization_url(state="s")
    assert "not yet" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_yahoo_calls_raise_not_configured() -> None:
    y = YahooProvider()
    with pytest.raises(ProviderNotConfiguredError):
        await y.exchange_code("c")
    with pytest.raises(ProviderNotConfiguredError):
        await y.list_recent(OAuthTokens(access_token="x"), since=None, max_results=1)


def test_all_three_share_one_interface() -> None:
    assert canonical_provider("microsoft") == "outlook"
    assert canonical_provider("google") == "gmail"
    for name in ("gmail", "outlook", "yahoo"):
        assert isinstance(build_provider(name), EmailProvider)


def test_provider_configured_reflects_settings(monkeypatch) -> None:
    from tender_agent.config import settings

    monkeypatch.setattr(settings, "gmail_client_id", "")
    assert provider_configured("gmail") is False
    monkeypatch.setattr(settings, "gmail_client_id", "cid")
    monkeypatch.setattr(settings, "gmail_client_secret", "sec")
    monkeypatch.setattr(settings, "email_oauth_redirect_uri", "https://cb")
    assert provider_configured("gmail") is True
    # Yahoo stays unconfigured even with creds present (deferred).
    assert provider_configured("yahoo") is False
