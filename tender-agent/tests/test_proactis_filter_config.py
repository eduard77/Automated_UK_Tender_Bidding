"""Tests for the Proactis discovery filter config (Step 1)."""
from __future__ import annotations

from tender_agent.services.discovery.proactis_filter_config import (
    ProactisFilterConfig,
)


def test_defaults_are_unconstrained_open_only() -> None:
    cfg = ProactisFilterConfig()
    assert cfg.keywords == ""
    assert cfg.regions == []
    assert cfg.categories == []
    assert cfg.portals == []
    assert cfg.organisations == []
    # Critical default: open-only.
    assert cfg.include_closed is False
    # Even an "unconstrained" config is constrained-on-status (open-only),
    # so is_constrained() returns True for the default. is_constrained()
    # exists for log clarity, not for triggering different code paths.
    assert cfg.is_constrained() is True


def test_is_constrained_false_only_when_truly_unconstrained() -> None:
    """If a future operator deliberately asks for everything, no filters and
    closed included, the flag flips to False so the log can warn about a
    full walk."""
    cfg = ProactisFilterConfig(include_closed=True)
    assert cfg.is_constrained() is False


def test_keywords_whitespace_doesnt_count() -> None:
    cfg = ProactisFilterConfig(keywords="   ", include_closed=True)
    assert cfg.is_constrained() is False


def test_lists_populated_count_as_constrained() -> None:
    cfg = ProactisFilterConfig(
        include_closed=True, regions=["North West"]
    )
    assert cfg.is_constrained() is True


def test_pagination_cap_default_is_bounded() -> None:
    """The cap is a safety net, not the expected value. Just confirms a
    sensible bounded default — operators reduce it if they want."""
    cfg = ProactisFilterConfig()
    assert cfg.max_pages > 0
    assert cfg.max_pages <= 100
