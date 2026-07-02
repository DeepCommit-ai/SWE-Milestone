"""Unit tests for pure decision helpers in container_setup.

These cover the quarantine-scoped /etc/hosts poison list and the network-probe
result interpreter — logic that must be correct regardless of any running
container, so it is tested without Docker.
"""

from harness.e2e.container_setup import (
    CODE_HOSTING_DOMAINS,
    _interpret_probe,
    _poison_domain_list,
)
from harness.e2e.quarantine import QUARANTINE_MIRROR_DOMAINS


class TestPoisonDomainList:
    """#4: mirror domains are poisoned only in quarantine containers; code
    hosting is always poisoned."""

    def test_excludes_mirrors_when_not_quarantined(self):
        domains = _poison_domain_list(quarantine_active=False)
        for d in QUARANTINE_MIRROR_DOMAINS:
            assert d not in domains
        assert "github.com" in domains

    def test_includes_mirrors_when_quarantined(self):
        domains = _poison_domain_list(quarantine_active=True)
        for d in QUARANTINE_MIRROR_DOMAINS:
            assert d in domains
        assert "github.com" in domains

    def test_mirror_domains_not_in_base_code_hosting(self):
        # The mirror domains must live in QUARANTINE_MIRROR_DOMAINS (conditional),
        # not baked into the always-on CODE_HOSTING_DOMAINS.
        for d in QUARANTINE_MIRROR_DOMAINS:
            assert d not in CODE_HOSTING_DOMAINS


class TestInterpretProbe:
    """#11: a probe result is reachable/blocked/indeterminate — infrastructure
    failure (no REACH/BLOCK marker) must raise, never read as 'blocked'."""

    def test_reach_marker_is_reachable(self):
        assert _interpret_probe(0, "REACH\n") is True

    def test_block_marker_is_blocked(self):
        assert _interpret_probe(0, "BLOCK\n") is False

    def test_no_marker_raises(self):
        import pytest

        with pytest.raises(RuntimeError):
            _interpret_probe(127, "")
