"""Tests for the per-repo quarantine policy module."""

import json

import pytest

from harness.e2e.quarantine import (
    cidr_overlaps_any,
    image_for_repo,
    load_quarantine_env,
    normalize_maven_plugin_probes,
    quarantine_coverage_errors,
    quarantine_coverage_errors_from_config,
    quarantine_guard_error_from_config,
)


def _write_config(root, repo, text):
    d = root / "quarantine_configs"
    d.mkdir(exist_ok=True)
    (d / f"{repo}.yaml").write_text(text)


class TestMirrorDomainsAndGoproxy:
    """#4: go-proxy mirror domains are a cross-ecosystem answer channel poisoned
    only in quarantine containers; GOPROXY must agree with that poisoning."""

    def test_mirror_domains_cover_all_go_proxies(self):
        from harness.e2e.quarantine import QUARANTINE_MIRROR_DOMAINS

        assert set(QUARANTINE_MIRROR_DOMAINS) >= {
            "proxy.golang.org",
            "sum.golang.org",
            "index.golang.org",
            "goproxy.cn",
            "goproxy.io",
        }

    def test_goproxy_uses_local_file_proxy_under_go_offline(self):
        from harness.e2e.quarantine import GO_OFFLINE_FILE_PROXY, goproxy_value

        assert (
            goproxy_value(go_offline=True, quarantine_active=False)
            == GO_OFFLINE_FILE_PROXY
        )

    def test_goproxy_off_under_quarantine_even_without_go_offline(self):
        # A quarantine container poisons the go-proxy mirror domains, so a bare
        # GOPROXY=proxy.golang.org would resolve to 0.0.0.0 and every fetch would
        # fail. A non-Go quarantine therefore keeps GOPROXY fully off.
        from harness.e2e.quarantine import goproxy_value

        assert goproxy_value(go_offline=False, quarantine_active=True) == "off"

    def test_goproxy_direct_when_unprotected(self):
        # Non-quarantine container: mirror domains are NOT poisoned, so the
        # sanctioned proxy must stay configured (pre-PR baseline parity).
        from harness.e2e.quarantine import goproxy_value

        assert (
            goproxy_value(go_offline=False, quarantine_active=False)
            == "https://proxy.golang.org,direct"
        )

    def test_load_quarantine_env_sets_quarantine_flag(self, tmp_path):
        _write_config(
            tmp_path, "r", "ecosystem: [go]\ndeny_domains: [proxy.golang.org]\n"
        )
        env = load_quarantine_env("r", tmp_path)
        assert env["SWE_MILESTONE_QUARANTINE"] == "1"


class TestGateHardening:
    """#1: fail-closed gate must reject a policy whose registry stays reachable
    — a missing deny_cidrs (CDN accept survives) or a missing offline switch."""

    def _errs(self, tmp_path, name, body):
        _write_config(tmp_path, name, body)
        return quarantine_coverage_errors([name], tmp_path)

    def test_missing_deny_cidrs_rejected(self, tmp_path):
        # pypi is Fastly-fronted; with no deny_cidrs the CDN accept range stays
        # and pypi is reachable — deny_domains alone doesn't drop it.
        errs = self._errs(
            tmp_path, "sk",
            "ecosystem: [pip]\ndeny_domains: [pypi.org, files.pythonhosted.org]\n",
        )
        assert errs
        assert any("cidr" in e.lower() or "exempt" in e.lower() for e in errs)

    def test_deny_cidrs_present_passes(self, tmp_path):
        errs = self._errs(
            tmp_path, "sk",
            "ecosystem: [pip]\n"
            "deny_domains: [pypi.org, files.pythonhosted.org]\n"
            "deny_cidrs: [151.101.0.0/16]\n",
        )
        assert errs == []

    def test_missing_offline_switch_rejected(self, tmp_path):
        # cargo ecosystem without cargo_offline: the package manager runs online
        # against crates.io even with the firewall up (legitimate fetch path).
        errs = self._errs(
            tmp_path, "rg",
            "ecosystem: [cargo]\n"
            "deny_domains: [crates.io, static.crates.io, index.crates.io]\n"
            "deny_cidrs: [151.101.0.0/16]\n",
        )
        assert errs
        assert any("offline" in e.lower() for e in errs)

    def test_cargo_with_offline_switch_passes(self, tmp_path):
        errs = self._errs(
            tmp_path, "rg",
            "ecosystem: [cargo]\ncargo_offline: true\n"
            "deny_domains: [crates.io, static.crates.io, index.crates.io]\n"
            "deny_cidrs: [151.101.0.0/16]\n",
        )
        assert errs == []

    def test_firewall_exempt_domain_needs_no_cidr(self, tmp_path):
        # proxy/sum.golang.org ride Google's Vertex range (un-CIDR-blockable),
        # so they're exempt (poison + local-only GOPROXY defense); goproxy.cn/io still
        # require deny_cidrs.
        errs = self._errs(
            tmp_path, "gz",
            "ecosystem: [go]\ngo_offline: true\n"
            "deny_domains: [proxy.golang.org, sum.golang.org, goproxy.cn, goproxy.io]\n"
            "deny_cidrs: [104.16.0.0/12, 155.102.0.0/16]\n"
            "firewall_exempt_domains: [proxy.golang.org, sum.golang.org]\n",
        )
        assert errs == []

    def test_none_ecosystem_needs_no_switches(self, tmp_path):
        errs = self._errs(tmp_path, "n", "ecosystem: [none]\n")
        assert errs == []


class TestLoadQuarantineEnv:
    def test_absent_config_returns_empty(self, tmp_path):
        assert load_quarantine_env("norepo", tmp_path) == {}

    def test_load_quarantine_env_pip_sets_offline_flag(self, tmp_path):
        (tmp_path / "quarantine_configs").mkdir()
        (tmp_path / "quarantine_configs" / "sk.yaml").write_text(
            "ecosystem: [pip]\nclosure: {ecosystem: pip}\n"
        )
        env = load_quarantine_env("sk", tmp_path)
        assert env.get("SWE_MILESTONE_PIP_OFFLINE") == "1"
        assert "SWE_MILESTONE_PIP_WHEELHOUSE" not in env   # no longer a host path

    def test_deny_fields(self, tmp_path):
        _write_config(tmp_path, "r1", """
deny_domains: [crates.io, static.crates.io]
deny_cidrs: [151.101.0.0/16]
""")
        env = load_quarantine_env("r1", tmp_path)
        assert env["SWE_MILESTONE_DENY_DOMAINS"] == "crates.io,static.crates.io"
        assert env["SWE_MILESTONE_DENY_CIDRS"] == "151.101.0.0/16"

    def test_firewall_exempt_domains_exported(self, tmp_path):
        # #2b: verify_network_lockdown must only exempt domains the policy
        # DECLARES un-CIDR-blockable, not infer it at runtime (fail-open).
        _write_config(tmp_path, "gz", """
firewall_exempt_domains: [proxy.golang.org, sum.golang.org]
""")
        env = load_quarantine_env("gz", tmp_path)
        assert env["SWE_MILESTONE_FIREWALL_EXEMPT"] == "proxy.golang.org,sum.golang.org"

    def test_offline_switches(self, tmp_path):
        _write_config(tmp_path, "r2", """
cargo_offline: true
go_offline: true
maven_offline: true
maven_repo_local: /root/.m2/repository
npm_offline: true
""")
        env = load_quarantine_env("r2", tmp_path)
        assert env["SWE_MILESTONE_CARGO_OFFLINE"] == "1"
        assert env["SWE_MILESTONE_GO_OFFLINE"] == "1"
        assert env["SWE_MILESTONE_MAVEN_OFFLINE"] == "1"
        assert env["SWE_MILESTONE_MAVEN_REPO_LOCAL"] == "/root/.m2/repository"
        assert env["SWE_MILESTONE_NPM_OFFLINE"] == "1"

    def test_closure_cache_paths_exported_as_json(self, tmp_path):
        _write_config(tmp_path, "cache-repo", """
ecosystem: [go, npm]
closure:
  cache_paths:
    - /go/pkg/mod/cache/download
    - /root/.npm/_cacache
  offline_build: npm ci --offline
""")

        env = load_quarantine_env("cache-repo", tmp_path)

        assert json.loads(env["SWE_MILESTONE_CACHE_PATHS"]) == [
            "/go/pkg/mod/cache/download",
            "/root/.npm/_cacache",
        ]

    def test_maven_plugin_probes_exported_as_validated_json(self, tmp_path):
        _write_config(tmp_path, "dubbo", """
ecosystem: [maven]
closure:
  cache_paths: [/root/.m2/repository]
  offline_build: mvn -o test-compile
  maven_plugin_probes:
    - pom: pom.xml
      goal: spotless:check
      timeout_seconds: 90
    - pom: dubbo-dependencies-bom/pom.xml
      goal: spotless:check
""")
        env = load_quarantine_env("dubbo", tmp_path)
        assert json.loads(env["SWE_MILESTONE_MAVEN_PLUGIN_PROBES"]) == [
            {"pom": "pom.xml", "goal": "spotless:check", "timeout_seconds": 90},
            {
                "pom": "dubbo-dependencies-bom/pom.xml",
                "goal": "spotless:check",
                "timeout_seconds": 120,
            },
        ]

    @pytest.mark.parametrize(
        "value",
        [
            "not-a-list",
            [{"pom": "../pom.xml", "goal": "spotless:check"}],
            [{"pom": "pom.xml", "goal": "spotless:check;curl bad"}],
            [{"pom": "pom.xml", "goal": "spotless:check", "timeout_seconds": 0}],
        ],
    )
    def test_maven_plugin_probe_validation_rejects_unsafe_values(self, value):
        with pytest.raises(ValueError):
            normalize_maven_plugin_probes(value)

    def test_pip_wheelhouse_exported_when_native_cache_paths_empty(self, tmp_path):
        _write_config(tmp_path, "wheelhouse-repo", """
ecosystem: [pip]
closure:
  cache_paths: []
  offline_build: pip install --no-index -f /wheelhouse
""")

        env = load_quarantine_env("wheelhouse-repo", tmp_path)

        assert json.loads(env["SWE_MILESTONE_CACHE_PATHS"]) == ["/wheelhouse"]

    def test_audit_lists_joined(self, tmp_path):
        _write_config(tmp_path, "r3", """
cache_forbid_globs:
  - /usr/local/cargo/registry/cache/*/grep-*.crate
  - /usr/local/cargo/registry/src/*/grep-*
verify_fetch_urls:
  - https://static.crates.io/crates/grep-printer/grep-printer-0.3.1.crate
""")
        env = load_quarantine_env("r3", tmp_path)
        assert env["SWE_MILESTONE_CACHE_FORBID_GLOBS"] == (
            "/usr/local/cargo/registry/cache/*/grep-*.crate,"
            "/usr/local/cargo/registry/src/*/grep-*"
        )
        assert env["SWE_MILESTONE_VERIFY_FETCH_URLS"] == (
            "https://static.crates.io/crates/grep-printer/grep-printer-0.3.1.crate"
        )

    def test_malformed_yaml_exits(self, tmp_path):
        _write_config(tmp_path, "r4", ":\n  - not: [valid")
        with pytest.raises(SystemExit):
            load_quarantine_env("r4", tmp_path)


class TestFirewallExemptWhitelist:
    """F1: firewall_exempt_domains must be restricted to genuinely un-CIDR-blockable
    (Google/Vertex-shared) domains. Exempting a CIDR-blockable registry would
    bypass BOTH the gate's cidr requirement and verify's reachability assertion —
    a declaration-driven fail-open. The gate must reject it."""

    def test_cidr_blockable_registry_in_exempt_rejected(self, tmp_path):
        # crates.io IS Fastly-CIDR-blockable; exempting it would silently reopen
        # the answer channel. Even WITH a deny_cidr present, listing it as exempt
        # is illegal (verify would still skip it).
        _write_config(tmp_path, "evil", """
ecosystem: [cargo]
cargo_offline: true
deny_domains: [crates.io, static.crates.io, index.crates.io]
deny_cidrs: [151.101.0.0/16]
firewall_exempt_domains: [crates.io]
""")
        errs = quarantine_coverage_errors(["evil"], tmp_path)
        assert errs
        assert any("crates.io" in e and "exempt" in e.lower() for e in errs)

    def test_google_shared_domains_in_exempt_allowed(self, tmp_path):
        _write_config(tmp_path, "gz", """
ecosystem: [go]
go_offline: true
deny_domains: [proxy.golang.org, sum.golang.org, goproxy.cn, goproxy.io]
deny_cidrs: [104.16.0.0/12, 155.102.0.0/16]
firewall_exempt_domains: [proxy.golang.org, sum.golang.org, golang.org, go.dev, pkg.go.dev]
""")
        assert quarantine_coverage_errors(["gz"], tmp_path) == []


class TestResumeUnprotected:
    """Resuming an --unprotected baseline must stay OPEN, not silently re-harden.
    The flag is persisted in trial_metadata and read back on resume (it isn't a
    CLI arg the second time)."""

    def test_metadata_wants_unprotected(self):
        from harness.e2e.quarantine import metadata_wants_unprotected

        assert metadata_wants_unprotected({"unprotected": True}) is True
        assert metadata_wants_unprotected({"unprotected": False}) is False
        assert metadata_wants_unprotected({"model": "m"}) is False
        assert metadata_wants_unprotected({}) is False
        assert metadata_wants_unprotected(None) is False


class TestQuarantineGuard:
    """#3: a direct run_e2e launch of a repo that HAS a policy but wasn't given
    the quarantine env must refuse (the 'silently ran unprotected' condition)."""

    def test_flags_unapplied_policy(self, tmp_path):
        from harness.e2e.quarantine import quarantine_guard_error

        _write_config(tmp_path, "sk", "ecosystem: [pip]\ndeny_domains: [pypi.org]\n")
        err = quarantine_guard_error(
            "sk", tmp_path, quarantine_active=False, unprotected=False
        )
        assert err and "sk" in err

    def test_ok_when_quarantine_active(self, tmp_path):
        from harness.e2e.quarantine import quarantine_guard_error

        _write_config(tmp_path, "sk", "ecosystem: [pip]\n")
        assert (
            quarantine_guard_error(
                "sk", tmp_path, quarantine_active=True, unprotected=False
            )
            is None
        )

    def test_ok_when_unprotected(self, tmp_path):
        from harness.e2e.quarantine import quarantine_guard_error

        _write_config(tmp_path, "sk", "ecosystem: [pip]\n")
        assert (
            quarantine_guard_error(
                "sk", tmp_path, quarantine_active=False, unprotected=True
            )
            is None
        )

    def test_ok_when_no_policy(self, tmp_path):
        from harness.e2e.quarantine import quarantine_guard_error

        assert (
            quarantine_guard_error(
                "norepo", tmp_path, quarantine_active=False, unprotected=False
            )
            is None
        )

    def test_pure_guard_uses_supplied_mapping(self):
        assert quarantine_guard_error_from_config(
            "sk", {"ecosystem": ["pip"]}, False, False
        )
        assert (
            quarantine_guard_error_from_config("sk", None, False, False)
            is None
        )


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
        # offline switch + deny_cidrs present so the ONLY gap is the two
        # un-denied registry domains (isolates this assertion).
        _write_config(tmp_path, "repoD", """
ecosystem: [cargo]
cargo_offline: true
deny_domains: [crates.io]
deny_cidrs: [151.101.0.0/16]
""")
        errs = quarantine_coverage_errors(["repoD"], tmp_path)
        assert len(errs) == 1
        assert "static.crates.io" in errs[0] and "index.crates.io" in errs[0]

    def test_full_coverage_passes(self, tmp_path):
        _write_config(tmp_path, "repoE", """
ecosystem: [go, npm]
go_offline: true
npm_offline: true
deny_domains: [proxy.golang.org, sum.golang.org, goproxy.cn, goproxy.io,
               registry.npmjs.org, registry.yarnpkg.com]
deny_cidrs: [104.16.0.0/12, 155.102.0.0/16]
firewall_exempt_domains: [proxy.golang.org, sum.golang.org]
""")
        assert quarantine_coverage_errors(["repoE"], tmp_path) == []

    def test_ecosystem_none_passes(self, tmp_path):
        _write_config(tmp_path, "repoF", "ecosystem: [none]\n")
        assert quarantine_coverage_errors(["repoF"], tmp_path) == []

    def test_pure_coverage_never_needs_a_project_root(self):
        assert quarantine_coverage_errors_from_config(
            "repoF", {"ecosystem": ["none"]}
        ) == []


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
                 if k.startswith("SWE_MILESTONE_")}
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
        assert self._env_dict({"SWE_MILESTONE_CARGO_OFFLINE": "1"}) == {
            "CARGO_NET_OFFLINE": "true"}

    def test_go_offline(self):
        env = self._env_dict({
            "SWE_MILESTONE_GO_OFFLINE": "1",
            "SWE_MILESTONE_GO_TOOLCHAIN": "1.21.13",
        })
        assert env == {
            "GOPROXY": "file:///go/pkg/mod/cache/download",
            "GONOPROXY": "none",
            "GOSUMDB": "off",
            "GOTOOLCHAIN": "local",
            "GOFLAGS": "-buildvcs=false",
            "GOENV": "/home/fakeroot/.cache/evoclaw-goenv/env",
            "BASH_ENV": "/etc/evoclaw/go-runtime.sh",
            "GOMODCACHE": "/home/fakeroot/.cache/evoclaw-gomodcache",
            "GOCACHE": "/home/fakeroot/.cache/go-build",
            "GOBIN": "/home/fakeroot/go/bin",
            "PATH": (
                "/home/fakeroot/go/bin:/usr/local/go/bin:/go/bin:"
                "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            ),
            "GOLANG_VERSION": "1.21.13",
        }

    @pytest.mark.parametrize(
        "framework_name",
        ["claude-code", "codex", "gemini-cli", "openhands"],
    )
    def test_every_framework_inherits_the_same_go_runtime_contract(
        self,
        framework_name,
        monkeypatch,
    ):
        from harness.e2e.agents import get_agent_framework

        monkeypatch.setenv("SWE_MILESTONE_GO_OFFLINE", "1")
        monkeypatch.setenv("SWE_MILESTONE_GO_TOOLCHAIN", "1.21.13")
        monkeypatch.setenv("UNIFIED_API_KEY", "test-key")
        monkeypatch.setenv("UNIFIED_BASE_URL", "https://example.invalid")

        args = get_agent_framework(framework_name).get_effective_container_env_vars()
        assert all(args[index] == "-e" for index in range(0, len(args), 2))
        pairs = [args[index] for index in range(1, len(args), 2)]
        env = dict(
            value.split("=", 1)
            for value in pairs
        )
        go_keys = {
            "GOPROXY",
            "GONOPROXY",
            "GOSUMDB",
            "GOTOOLCHAIN",
            "GOFLAGS",
            "GOENV",
            "BASH_ENV",
            "GOMODCACHE",
            "GOCACHE",
            "GOBIN",
            "PATH",
            "GOLANG_VERSION",
        }
        assert all(
            sum(value.startswith(f"{key}=") for value in pairs) == 1
            for key in go_keys
        )

        assert {
            "GOPROXY": env.get("GOPROXY"),
            "GONOPROXY": env.get("GONOPROXY"),
            "GOSUMDB": env.get("GOSUMDB"),
            "GOTOOLCHAIN": env.get("GOTOOLCHAIN"),
            "GOFLAGS": env.get("GOFLAGS"),
            "GOENV": env.get("GOENV"),
            "BASH_ENV": env.get("BASH_ENV"),
            "GOMODCACHE": env.get("GOMODCACHE"),
            "GOCACHE": env.get("GOCACHE"),
            "GOBIN": env.get("GOBIN"),
            "PATH": env.get("PATH"),
            "GOLANG_VERSION": env.get("GOLANG_VERSION"),
        } == {
            "GOPROXY": "file:///go/pkg/mod/cache/download",
            "GONOPROXY": "none",
            "GOSUMDB": "off",
            "GOTOOLCHAIN": "local",
            "GOFLAGS": "-buildvcs=false",
            "GOENV": "/home/fakeroot/.cache/evoclaw-goenv/env",
            "BASH_ENV": "/etc/evoclaw/go-runtime.sh",
            "GOMODCACHE": "/home/fakeroot/.cache/evoclaw-gomodcache",
            "GOCACHE": "/home/fakeroot/.cache/go-build",
            "GOBIN": "/home/fakeroot/go/bin",
            "PATH": (
                "/home/fakeroot/go/bin:/usr/local/go/bin:/go/bin:"
                "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            ),
            "GOLANG_VERSION": "1.21.13",
        }

    def test_core_env_merge_protects_a_future_adapter_that_omits_quarantine(
        self,
        monkeypatch,
    ):
        from harness.e2e.agents.base import AgentFramework

        class FutureFramework(AgentFramework):
            FRAMEWORK_NAME = "future"

            def get_container_mounts(self):
                return []

            def get_container_env_vars(self):
                return [
                    "-e", "FUTURE_API_KEY=dummy",
                    "-e", "GOPROXY=https://wrong.invalid",
                ]

            def get_container_init_script(self, agent_name):
                return ""

            def build_run_command(self, model, session_id, prompt_path):
                return ""

            def build_resume_command(self, model, session_id, message_path):
                return ""

        monkeypatch.setenv("SWE_MILESTONE_GO_OFFLINE", "1")
        monkeypatch.setenv("SWE_MILESTONE_GO_TOOLCHAIN", "1.21.13")
        args = FutureFramework().get_effective_container_env_vars()
        values = [args[index] for index in range(1, len(args), 2)]
        env = dict(value.split("=", 1) for value in values)

        assert env["FUTURE_API_KEY"] == "dummy"
        assert env["GOPROXY"] == "file:///go/pkg/mod/cache/download"
        assert env["GOENV"] == "/home/fakeroot/.cache/evoclaw-goenv/env"
        assert env["BASH_ENV"] == "/etc/evoclaw/go-runtime.sh"
        assert sum(value.startswith("GOPROXY=") for value in values) == 1

    def test_maven_offline_with_repo_local(self):
        env = self._env_dict({"SWE_MILESTONE_MAVEN_OFFLINE": "1",
                              "SWE_MILESTONE_MAVEN_REPO_LOCAL": "/root/.m2/repository"})
        assert env == {"MAVEN_ARGS": "-o -Dmaven.repo.local=/root/.m2/repository"}

    def test_maven_offline_without_repo_local(self):
        assert self._env_dict({"SWE_MILESTONE_MAVEN_OFFLINE": "1"}) == {"MAVEN_ARGS": "-o"}

    def test_npm_offline(self):
        assert self._env_dict({"SWE_MILESTONE_NPM_OFFLINE": "1"}) == {
            "npm_config_offline": "true"}

    def test_pip_wheelhouse_alone_no_longer_triggers(self):
        # SWE_MILESTONE_PIP_WHEELHOUSE is the old trigger; it must no longer set pip env.
        env = self._env_dict({"SWE_MILESTONE_PIP_WHEELHOUSE": "/wh"})
        assert env == {}

    def test_pip_offline_uses_in_image_wheelhouse(self):
        # New trigger: SWE_MILESTONE_PIP_OFFLINE=1 → pip reads in-image /wheelhouse.
        env = self._env_dict({"SWE_MILESTONE_PIP_OFFLINE": "1"})
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
                 if k.startswith("SWE_MILESTONE_")}
        try:
            os.environ["SWE_MILESTONE_PIP_OFFLINE"] = "1"
            os.environ["SWE_MILESTONE_PIP_WHEELHOUSE"] = "/any/host/path"
            mounts = _F().get_quarantine_mounts()
        finally:
            for k in ("SWE_MILESTONE_PIP_OFFLINE", "SWE_MILESTONE_PIP_WHEELHOUSE"):
                os.environ.pop(k, None)
            os.environ.update(saved)
        assert mounts == []


class TestImageForRepo:
    """image_for_repo selects base-offline for a quarantine repo and base
    otherwise, and delegates TAG resolution to resolve_image so SWE_MILESTONE_IMAGE_TAG
    pinning is honored instead of hardcoding :latest (#5)."""

    def _patch_resolver(self, monkeypatch, seen):
        monkeypatch.setattr(
            "harness.e2e.quarantine.resolve_image",
            lambda base: seen.append(base) or f"{base}:PIN",
        )

    def test_no_config_uses_base_via_resolver(self, tmp_path, monkeypatch):
        seen = []
        self._patch_resolver(monkeypatch, seen)
        assert image_for_repo("Foo_Bar", tmp_path) == "swe-milestone/foo_bar__base:PIN"
        assert seen == ["swe-milestone/foo_bar__base"]

    def test_go_quarantine_uses_offline_base_via_resolver(self, tmp_path, monkeypatch):
        _write_config(tmp_path, "gz", "ecosystem: [go]\ngo_offline: true\n")
        seen = []
        self._patch_resolver(monkeypatch, seen)
        assert image_for_repo("gz", tmp_path) == "swe-milestone/gz__base-offline:PIN"
        assert seen == ["swe-milestone/gz__base-offline"]

    def test_pip_uses_offline_base_via_resolver(self, tmp_path, monkeypatch):
        _write_config(tmp_path, "sk", "ecosystem: [pip]\n")
        seen = []
        self._patch_resolver(monkeypatch, seen)
        assert image_for_repo("sk", tmp_path) == "swe-milestone/sk__base-offline:PIN"
        assert seen == ["swe-milestone/sk__base-offline"]


class TestCidrOverlap:
    def test_denied_slash12_covers_accept_slash13(self):
        assert cidr_overlaps_any("104.16.0.0/13", ["104.16.0.0/12"])

    def test_exact_match(self):
        assert cidr_overlaps_any("151.101.0.0/16", ["151.101.0.0/16"])

    def test_disjoint(self):
        assert not cidr_overlaps_any("142.250.0.0/15", ["104.16.0.0/12"])

    def test_invalid_deny_entries_ignored(self):
        assert not cidr_overlaps_any("142.250.0.0/15", ["bogus", ""])
