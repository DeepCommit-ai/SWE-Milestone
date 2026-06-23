"""Tests for the per-repo quarantine policy module."""

import pytest

from harness.e2e.quarantine import (
    cidr_overlaps_any,
    image_for_repo,
    load_quarantine_env,
    quarantine_coverage_errors,
)


def _write_config(root, repo, text):
    d = root / "quarantine_configs"
    d.mkdir(exist_ok=True)
    (d / f"{repo}.yaml").write_text(text)


class TestLoadQuarantineEnv:
    def test_absent_config_returns_empty(self, tmp_path):
        assert load_quarantine_env("norepo", tmp_path) == {}

    def test_deny_fields(self, tmp_path):
        _write_config(tmp_path, "r1", """
deny_domains: [crates.io, static.crates.io]
deny_cidrs: [151.101.0.0/16]
""")
        env = load_quarantine_env("r1", tmp_path)
        assert env["EVOCLAW_DENY_DOMAINS"] == "crates.io,static.crates.io"
        assert env["EVOCLAW_DENY_CIDRS"] == "151.101.0.0/16"

    def test_offline_switches(self, tmp_path):
        _write_config(tmp_path, "r2", """
cargo_offline: true
go_offline: true
maven_offline: true
maven_repo_local: /root/.m2/repository
npm_offline: true
""")
        env = load_quarantine_env("r2", tmp_path)
        assert env["EVOCLAW_CARGO_OFFLINE"] == "1"
        assert env["EVOCLAW_GO_OFFLINE"] == "1"
        assert env["EVOCLAW_MAVEN_OFFLINE"] == "1"
        assert env["EVOCLAW_MAVEN_REPO_LOCAL"] == "/root/.m2/repository"
        assert env["EVOCLAW_NPM_OFFLINE"] == "1"

    def test_audit_lists_joined(self, tmp_path):
        _write_config(tmp_path, "r3", """
cache_forbid_globs:
  - /usr/local/cargo/registry/cache/*/grep-*.crate
  - /usr/local/cargo/registry/src/*/grep-*
verify_fetch_urls:
  - https://static.crates.io/crates/grep-printer/grep-printer-0.3.1.crate
""")
        env = load_quarantine_env("r3", tmp_path)
        assert env["EVOCLAW_CACHE_FORBID_GLOBS"] == (
            "/usr/local/cargo/registry/cache/*/grep-*.crate,"
            "/usr/local/cargo/registry/src/*/grep-*"
        )
        assert env["EVOCLAW_VERIFY_FETCH_URLS"] == (
            "https://static.crates.io/crates/grep-printer/grep-printer-0.3.1.crate"
        )

    def test_malformed_yaml_exits(self, tmp_path):
        _write_config(tmp_path, "r4", ":\n  - not: [valid")
        with pytest.raises(SystemExit):
            load_quarantine_env("r4", tmp_path)


class TestCoverageGate:
    def test_missing_config_is_error(self, tmp_path):
        errs = quarantine_coverage_errors(["repoA"], tmp_path)
        assert len(errs) == 1 and "repoA" in errs[0] and "UNPROTECTED" in errs[0]

    def test_missing_ecosystem_is_error(self, tmp_path):
        _write_config(tmp_path, "repoB", "deny_domains: [crates.io]\n")
        errs = quarantine_coverage_errors(["repoB"], tmp_path)
        assert len(errs) == 1 and "ecosystem" in errs[0]

    def test_unknown_ecosystem_is_error(self, tmp_path):
        _write_config(tmp_path, "repoC", "ecosystem: [conda]\n")
        errs = quarantine_coverage_errors(["repoC"], tmp_path)
        assert len(errs) == 1 and "conda" in errs[0]

    def test_uncovered_registry_is_error(self, tmp_path):
        _write_config(tmp_path, "repoD", """
ecosystem: [cargo]
deny_domains: [crates.io]
""")
        errs = quarantine_coverage_errors(["repoD"], tmp_path)
        assert len(errs) == 1
        assert "static.crates.io" in errs[0] and "index.crates.io" in errs[0]

    def test_full_coverage_passes(self, tmp_path):
        _write_config(tmp_path, "repoE", """
ecosystem: [go, npm]
deny_domains: [proxy.golang.org, sum.golang.org, goproxy.cn, goproxy.io,
               registry.npmjs.org, registry.yarnpkg.com]
""")
        assert quarantine_coverage_errors(["repoE"], tmp_path) == []

    def test_ecosystem_none_passes(self, tmp_path):
        _write_config(tmp_path, "repoF", "ecosystem: [none]\n")
        assert quarantine_coverage_errors(["repoF"], tmp_path) == []


class TestAgentQuarantineEnvVars:
    def _env_dict(self, flags):
        """Run get_quarantine_env_vars under a controlled env, return {k: v}."""
        import os

        from harness.e2e.agents.base import AgentFramework

        class _F(AgentFramework):
            FRAMEWORK_NAME = "test"

            def get_container_mounts(self):
                return []

            def get_container_init_script(self, agent_name):
                return ""

            def build_run_command(self, model, session_id, prompt_path):
                return ""

            def build_resume_command(self, model, session_id, message_path):
                return ""

        saved = {k: os.environ.pop(k) for k in list(os.environ)
                 if k.startswith("EVOCLAW_")}
        try:
            os.environ.update(flags)
            args = _F().get_quarantine_env_vars()
        finally:
            for k in flags:
                os.environ.pop(k, None)
            os.environ.update(saved)
        assert all(args[i] == "-e" for i in range(0, len(args), 2))
        pairs = [args[i + 1] for i in range(0, len(args), 2)]
        return dict(p.split("=", 1) for p in pairs)

    def test_no_flags_no_vars(self):
        assert self._env_dict({}) == {}

    def test_cargo_offline(self):
        assert self._env_dict({"EVOCLAW_CARGO_OFFLINE": "1"}) == {
            "CARGO_NET_OFFLINE": "true"}

    def test_go_offline(self):
        assert self._env_dict({"EVOCLAW_GO_OFFLINE": "1"}) == {"GOPROXY": "off"}

    def test_maven_offline_with_repo_local(self):
        env = self._env_dict({"EVOCLAW_MAVEN_OFFLINE": "1",
                              "EVOCLAW_MAVEN_REPO_LOCAL": "/root/.m2/repository"})
        assert env == {"MAVEN_ARGS": "-o -Dmaven.repo.local=/root/.m2/repository"}

    def test_maven_offline_without_repo_local(self):
        assert self._env_dict({"EVOCLAW_MAVEN_OFFLINE": "1"}) == {"MAVEN_ARGS": "-o"}

    def test_npm_offline(self):
        assert self._env_dict({"EVOCLAW_NPM_OFFLINE": "1"}) == {
            "npm_config_offline": "true"}

    def test_pip_wheelhouse_alone_no_longer_triggers(self):
        # EVOCLAW_PIP_WHEELHOUSE is the old trigger; it must no longer set pip env.
        env = self._env_dict({"EVOCLAW_PIP_WHEELHOUSE": "/wh"})
        assert env == {}

    def test_pip_offline_uses_in_image_wheelhouse(self):
        # New trigger: EVOCLAW_PIP_OFFLINE=1 → pip reads in-image /wheelhouse.
        env = self._env_dict({"EVOCLAW_PIP_OFFLINE": "1"})
        assert env == {"PIP_NO_INDEX": "1", "PIP_FIND_LINKS": "/wheelhouse"}

    def test_pip_offline_mounts_returns_empty(self):
        # get_quarantine_mounts must return [] — wheelhouse is baked into the image.
        import os

        from harness.e2e.agents.base import AgentFramework

        class _F(AgentFramework):
            FRAMEWORK_NAME = "test"

            def get_container_mounts(self):
                return []

            def get_container_init_script(self, agent_name):
                return ""

            def build_run_command(self, model, session_id, prompt_path):
                return ""

            def build_resume_command(self, model, session_id, message_path):
                return ""

        saved = {k: os.environ.pop(k) for k in list(os.environ)
                 if k.startswith("EVOCLAW_")}
        try:
            os.environ["EVOCLAW_PIP_OFFLINE"] = "1"
            os.environ["EVOCLAW_PIP_WHEELHOUSE"] = "/any/host/path"
            mounts = _F().get_quarantine_mounts()
        finally:
            for k in ("EVOCLAW_PIP_OFFLINE", "EVOCLAW_PIP_WHEELHOUSE"):
                os.environ.pop(k, None)
            os.environ.update(saved)
        assert mounts == []


class TestImageForRepo:
    def test_no_config_uses_base(self, tmp_path):
        assert image_for_repo("Foo_Bar", tmp_path) == "foo_bar/base:latest"

    def test_cargo_quarantine_uses_offline(self, tmp_path):
        _write_config(tmp_path, "rg", "ecosystem: [cargo]\ncargo_offline: true\n")
        assert image_for_repo("rg", tmp_path) == "rg/base-offline:latest"

    def test_go_quarantine_uses_offline(self, tmp_path):
        _write_config(tmp_path, "gz", "ecosystem: [go]\ngo_offline: true\n")
        assert image_for_repo("gz", tmp_path) == "gz/base-offline:latest"

    def test_pip_only_uses_offline(self, tmp_path):
        # pip closure is now baked into base-offline:latest (no longer host-mounted).
        _write_config(tmp_path, "sk", "ecosystem: [pip]\npip_wheelhouse: /wh\n")
        assert image_for_repo("sk", tmp_path) == "sk/base-offline:latest"

    def test_image_for_repo_pip_uses_base_offline(self, tmp_path):
        _write_config(tmp_path, "scikit-learn_x", "ecosystem: [pip]\npip_wheelhouse: /x\n")
        assert image_for_repo("scikit-learn_x", tmp_path).endswith("/base-offline:latest")


class TestCidrOverlap:
    def test_denied_slash12_covers_accept_slash13(self):
        assert cidr_overlaps_any("104.16.0.0/13", ["104.16.0.0/12"])

    def test_exact_match(self):
        assert cidr_overlaps_any("151.101.0.0/16", ["151.101.0.0/16"])

    def test_disjoint(self):
        assert not cidr_overlaps_any("142.250.0.0/15", ["104.16.0.0/12"])

    def test_invalid_deny_entries_ignored(self):
        assert not cidr_overlaps_any("142.250.0.0/15", ["bogus", ""])
