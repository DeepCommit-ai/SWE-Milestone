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


class TestQuarantineEnvFromImage:
    """F2: recover the full quarantine env from the image's repo policy when the
    process env lacks it (env-less direct resume / run_milestone), so mirror
    poison + deny survive env loss. Derived from a disk fact, not a signal."""

    def test_recovers_env_case_insensitively(self, tmp_path):
        from harness.e2e.container_setup import _quarantine_env_from_image

        # config filename has uppercase (BurntSushi) but the docker image repo is
        # lowercase — must match case-insensitively or the recovery misses it.
        d = tmp_path / "quarantine_configs"
        d.mkdir()
        (d / "BurntSushi_ripgrep_1_2.yaml").write_text(
            "ecosystem: [cargo]\ncargo_offline: true\n"
            "deny_domains: [crates.io]\ndeny_cidrs: [151.101.0.0/16]\n"
        )
        env = _quarantine_env_from_image(
            "burntsushi_ripgrep_1_2/base-offline:v0.9", tmp_path
        )
        assert env["EVOCLAW_QUARANTINE"] == "1"
        assert env["EVOCLAW_DENY_DOMAINS"] == "crates.io"

    def test_empty_for_no_config_repo(self, tmp_path):
        from harness.e2e.container_setup import _quarantine_env_from_image

        (tmp_path / "quarantine_configs").mkdir()
        assert _quarantine_env_from_image("norepo/base:latest", tmp_path) == {}

    def test_empty_for_unparseable_image(self, tmp_path):
        from harness.e2e.container_setup import _quarantine_env_from_image

        (tmp_path / "quarantine_configs").mkdir()
        assert _quarantine_env_from_image("", tmp_path) == {}


class TestRecoverQuarantineEnv:
    """F2 (v2): recovery prefers the authoritative repo_name (a known fact passed
    by the caller) over parsing the image (a fragile signal), so a
    registry-prefixed image can't make a policy'd repo silently run unprotected.
    Image parsing is only a fallback when no repo_name is given."""

    def _cfg(self, tmp_path):
        d = tmp_path / "quarantine_configs"
        d.mkdir()
        (d / "BurntSushi_ripgrep_1_2.yaml").write_text(
            "ecosystem: [cargo]\ncargo_offline: true\n"
            "deny_domains: [crates.io]\ndeny_cidrs: [151.101.0.0/16]\n"
        )

    def test_prefers_repo_name_over_image(self, tmp_path):
        from harness.e2e.container_setup import _recover_quarantine_env

        self._cfg(tmp_path)
        # registry-prefixed image would misparse to 'registry.io' -> {} (fail
        # open); the authoritative repo_name must win.
        env = _recover_quarantine_env(
            "BurntSushi_ripgrep_1_2",
            "registry.io/burntsushi_ripgrep_1_2/base-offline:v0.9",
            tmp_path,
        )
        assert env["EVOCLAW_QUARANTINE"] == "1"
        assert env["EVOCLAW_CARGO_OFFLINE"] == "1"

    def test_repo_name_case_insensitive(self, tmp_path):
        from harness.e2e.container_setup import _recover_quarantine_env

        self._cfg(tmp_path)
        # run_milestone lowercases repo_name; config file is BurntSushi_...
        env = _recover_quarantine_env("burntsushi_ripgrep_1_2", "x/base:latest", tmp_path)
        assert env["EVOCLAW_QUARANTINE"] == "1"

    def test_falls_back_to_image_when_no_repo_name(self, tmp_path):
        from harness.e2e.container_setup import _recover_quarantine_env

        self._cfg(tmp_path)
        env = _recover_quarantine_env(
            None, "burntsushi_ripgrep_1_2/base-offline:v0.9", tmp_path
        )
        assert env["EVOCLAW_QUARANTINE"] == "1"

    def test_empty_for_no_policy(self, tmp_path):
        from harness.e2e.container_setup import _recover_quarantine_env

        (tmp_path / "quarantine_configs").mkdir()
        assert _recover_quarantine_env("norepo", "norepo/base:latest", tmp_path) == {}


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
