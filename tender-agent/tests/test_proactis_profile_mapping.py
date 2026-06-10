"""Step 1: profile → ProactisFilterConfig translation.

Pure data, no network, no DB. The discovery service is driven from the
operator's existing FilterProfile; this test pins down what each field
becomes on the Proactis side so a Step-2 dashboard form mapping can rely on
the same translation.
"""
from __future__ import annotations

import pytest

from tender_agent.services.discovery.proactis_filter_config import (
    ProactisFilterConfig,
)


class _FP:
    """Tiny stand-in for FilterProfile — the mapper only reads attributes
    via getattr, so a plain duck-typed object is enough. Avoids dragging
    SQLAlchemy into the unit test."""

    def __init__(
        self,
        *,
        cpv_codes=None,
        cpv_prefixes=None,
        regions=None,
        keywords_any=None,
    ):
        self.cpv_codes = cpv_codes
        self.cpv_prefixes = cpv_prefixes
        self.regions = regions
        self.keywords_any = keywords_any


def test_empty_profile_yields_empty_config():
    cfg = ProactisFilterConfig.from_filter_profile(_FP())
    assert cfg.keywords == ""
    assert cfg.regions == []
    assert cfg.categories == []
    assert cfg.include_closed is False


def test_cpv_codes_translate_to_categories_in_order():
    cfg = ProactisFilterConfig.from_filter_profile(
        _FP(cpv_codes=["72000000", "45000000"])
    )
    assert cfg.categories == ["72000000", "45000000"]


def test_cpv_codes_and_prefixes_concatenated_codes_first():
    cfg = ProactisFilterConfig.from_filter_profile(
        _FP(cpv_codes=["72000000"], cpv_prefixes=["45"])
    )
    assert cfg.categories == ["72000000", "45"]


def test_duplicates_in_codes_and_prefixes_are_deduped():
    cfg = ProactisFilterConfig.from_filter_profile(
        _FP(cpv_codes=["72000000", "72000000"], cpv_prefixes=["72000000"])
    )
    assert cfg.categories == ["72000000"]


def test_blank_codes_and_whitespace_are_skipped():
    cfg = ProactisFilterConfig.from_filter_profile(
        _FP(cpv_codes=["", "  ", "72000000"], cpv_prefixes=[None, "45"])
    )
    assert cfg.categories == ["72000000", "45"]


def test_keywords_any_joined_into_single_query():
    cfg = ProactisFilterConfig.from_filter_profile(
        _FP(keywords_any=["security", "soc"])
    )
    assert cfg.keywords == "security soc"


def test_keywords_skipped_when_blank():
    cfg = ProactisFilterConfig.from_filter_profile(
        _FP(keywords_any=["", None, "  "])
    )
    assert cfg.keywords == ""


def test_regions_passed_through_unchanged():
    cfg = ProactisFilterConfig.from_filter_profile(
        _FP(regions=["North West", "Merseyside"])
    )
    assert cfg.regions == ["North West", "Merseyside"]


def test_regions_blank_entries_filtered():
    cfg = ProactisFilterConfig.from_filter_profile(
        _FP(regions=["", None, "London", "   "])
    )
    assert cfg.regions == ["London"]


def test_is_constrained_when_profile_has_any_signal():
    cfg = ProactisFilterConfig.from_filter_profile(
        _FP(cpv_codes=["72000000"])
    )
    assert cfg.is_constrained() is True


@pytest.mark.parametrize(
    "profile,expected_cats,expected_regs,expected_kw",
    [
        # Realistic operator profile: SOC retrofit work in NW England.
        (
            _FP(
                cpv_codes=["72200000", "48700000"],
                cpv_prefixes=["72"],
                regions=["North West", "Merseyside"],
                keywords_any=["SOC", "cyber"],
            ),
            ["72200000", "48700000", "72"],
            ["North West", "Merseyside"],
            "SOC cyber",
        ),
        # Regions-only profile.
        (
            _FP(regions=["London"]),
            [],
            ["London"],
            "",
        ),
    ],
)
def test_representative_profiles_round_trip(
    profile, expected_cats, expected_regs, expected_kw
):
    cfg = ProactisFilterConfig.from_filter_profile(profile)
    assert cfg.categories == expected_cats
    assert cfg.regions == expected_regs
    assert cfg.keywords == expected_kw
    assert cfg.include_closed is False


def test_portals_kwarg_passes_through_trimmed():
    """Deployment-level portal scope (PROACTIS_DISCOVERY_PORTALS) rides in via
    the kwarg — the FilterProfile has no portals dimension."""
    cfg = ProactisFilterConfig.from_filter_profile(
        _FP(cpv_codes=["72000000"]),
        portals=["YPO", "  The Chest  ", "", None],
    )
    assert cfg.portals == ["YPO", "The Chest"]


def test_portals_default_empty_leaves_control_alone():
    cfg = ProactisFilterConfig.from_filter_profile(_FP())
    assert cfg.portals == []
