"""Unit tests for pure decision helpers in container_setup.

These cover the quarantine-scoped /etc/hosts poison list and the network-probe
result interpreter — logic that must be correct regardless of any running
container, so it is tested without Docker.
"""

import json
from types import SimpleNamespace

import pytest

from harness.e2e.container_setup import (
    CODE_HOSTING_DOMAINS,
    ContainerSetup,
    _configured_cache_paths,
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
        assert env["SWE_MILESTONE_QUARANTINE"] == "1"
        assert env["SWE_MILESTONE_DENY_DOMAINS"] == "crates.io"

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
        assert env["SWE_MILESTONE_QUARANTINE"] == "1"
        assert env["SWE_MILESTONE_CARGO_OFFLINE"] == "1"

    def test_repo_name_case_insensitive(self, tmp_path):
        from harness.e2e.container_setup import _recover_quarantine_env

        self._cfg(tmp_path)
        # run_milestone lowercases repo_name; config file is BurntSushi_...
        env = _recover_quarantine_env("burntsushi_ripgrep_1_2", "x/base:latest", tmp_path)
        assert env["SWE_MILESTONE_QUARANTINE"] == "1"

    def test_falls_back_to_image_when_no_repo_name(self, tmp_path):
        from harness.e2e.container_setup import _recover_quarantine_env

        self._cfg(tmp_path)
        env = _recover_quarantine_env(
            None, "burntsushi_ripgrep_1_2/base-offline:v0.9", tmp_path
        )
        assert env["SWE_MILESTONE_QUARANTINE"] == "1"

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
        with pytest.raises(RuntimeError):
            _interpret_probe(127, "")


class TestOfflineCacheAccess:
    """The fakeroot agent must be able to use every quarantine cache."""

    @staticmethod
    def _setup():
        setup = object.__new__(ContainerSetup)
        setup.container_name = "trial-container"
        setup.agent_name = "claude-code"
        setup.workdir = "/testbed"
        return setup

    @staticmethod
    def _clear_cache_env(monkeypatch):
        monkeypatch.delenv("SWE_MILESTONE_CACHE_PATHS", raising=False)
        monkeypatch.delenv("SWE_MILESTONE_MAVEN_REPO_LOCAL", raising=False)
        monkeypatch.delenv("SWE_MILESTONE_MAVEN_PLUGIN_PROBES", raising=False)

    def test_configured_paths_parse_json_and_deduplicate_maven_fallback(self, monkeypatch):
        monkeypatch.setenv(
            "SWE_MILESTONE_CACHE_PATHS",
            '["/go/pkg/mod/cache/download","/root/.m2/repository"]',
        )
        monkeypatch.setenv("SWE_MILESTONE_MAVEN_REPO_LOCAL", "/root/.m2/repository")

        assert _configured_cache_paths() == [
            "/go/pkg/mod/cache/download",
            "/root/.m2/repository",
        ]

    @pytest.mark.parametrize(
        "value",
        ["not-json", "{}", '["relative/cache"]', '["/"]', '[1]'],
    )
    def test_invalid_cache_path_env_fails_closed(self, monkeypatch, value):
        monkeypatch.setenv("SWE_MILESTONE_CACHE_PATHS", value)
        monkeypatch.delenv("SWE_MILESTONE_MAVEN_REPO_LOCAL", raising=False)

        with pytest.raises(RuntimeError, match="JSON list|Invalid quarantine cache path"):
            _configured_cache_paths()

    def test_init_script_grants_minimum_applicable_traverse(self, monkeypatch):
        monkeypatch.setenv(
            "SWE_MILESTONE_CACHE_PATHS", '["/root/.m2/repository"]'
        )
        monkeypatch.setenv("SWE_MILESTONE_MAVEN_REPO_LOCAL", "/root/.m2/repository")
        script = self._setup()._get_base_init_script()

        assert "configured_cache_paths = ['/root/.m2/repository']" in script
        assert "fake_groups = set(os.getgrouplist(fake_user.pw_name, gid))" in script
        assert "if not path.exists():" in script
        assert "required = stat.S_IXGRP" in script
        assert "required |= stat.S_IRGRP" in script
        assert "os.chmod(candidate, mode | required)" in script
        assert "mode | stat.S_IWGRP" not in script

    def test_go_offline_init_seals_inputs_and_uses_disposable_caches(self, monkeypatch):
        monkeypatch.setenv("SWE_MILESTONE_GO_OFFLINE", "1")
        monkeypatch.setenv(
            "SWE_MILESTONE_CACHE_PATHS", '["/go/pkg/mod/cache/download"]'
        )
        script = self._setup()._get_base_init_script()

        assert "toolchain_dirs.extend(['/usr/local/go', '/go', '/root/go'])" in script
        assert "if not go_offline:" in script
        assert "Path('/go/pkg/mod/cache/download')" in script
        assert "(root_mode | 0o555) & ~0o222" in script
        assert "(item_mode | 0o444) & ~0o222" in script
        assert "Path('/home/fakeroot/.cache/evoclaw-gomodcache')" in script

    def test_no_configured_cache_skips_probe(self, monkeypatch):
        self._clear_cache_env(monkeypatch)

        def unexpected_run(*args, **kwargs):
            raise AssertionError("subprocess should not run without a configured repo")

        monkeypatch.setattr("harness.e2e.container_setup.subprocess.run", unexpected_run)
        self._setup()._verify_quarantine_cache_access()

    def test_no_configured_cache_skips_existing_container_repair(self, monkeypatch):
        self._clear_cache_env(monkeypatch)

        def unexpected_run(*args, **kwargs):
            raise AssertionError("subprocess should not run without configured caches")

        monkeypatch.setattr("harness.e2e.container_setup.subprocess.run", unexpected_run)
        self._setup()._repair_existing_quarantine_cache_access()

    def test_existing_container_repair_is_scoped_to_cache_and_maven_write(self, monkeypatch):
        repo = "/root/.m2/repository"
        monkeypatch.setenv("SWE_MILESTONE_CACHE_PATHS", f'["{repo}"]')
        monkeypatch.setenv("SWE_MILESTONE_MAVEN_REPO_LOCAL", repo)
        calls = []

        def successful_run(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout="repaired", stderr="")

        monkeypatch.setattr("harness.e2e.container_setup.subprocess.run", successful_run)
        self._setup()._repair_existing_quarantine_cache_access()

        assert len(calls) == 1
        command, kwargs = calls[0]
        assert command[:4] == ["docker", "exec", "trial-container", "python3"]
        script = command[-1]
        assert f"cache_paths = ['{repo}']" in script
        assert f'maven_repo = "{repo}"' in script or f"maven_repo = '{repo}'" in script
        assert '["chown", "-R"' in script
        assert '["chmod", "-R", "u+rwX"' in script
        assert "required |= stat.S_IRGRP" in script
        assert kwargs == {"capture_output": True, "text": True, "timeout": 180}

    def test_existing_container_repair_failure_is_fatal(self, monkeypatch):
        monkeypatch.setenv("SWE_MILESTONE_CACHE_PATHS", '["/cache"]')
        monkeypatch.delenv("SWE_MILESTONE_MAVEN_REPO_LOCAL", raising=False)
        monkeypatch.setattr(
            "harness.e2e.container_setup.subprocess.run",
            lambda *args, **kwargs: SimpleNamespace(
                returncode=1, stdout="", stderr="chmod failed"
            ),
        )

        with pytest.raises(RuntimeError, match="repair.*chmod failed"):
            self._setup()._repair_existing_quarantine_cache_access()

    def test_configured_cache_is_probed_as_fakeroot(self, monkeypatch):
        cache = "/go/pkg/mod/cache/download"
        monkeypatch.setenv("SWE_MILESTONE_CACHE_PATHS", f'["{cache}"]')
        monkeypatch.delenv("SWE_MILESTONE_MAVEN_REPO_LOCAL", raising=False)
        calls = []

        def successful_run(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr("harness.e2e.container_setup.subprocess.run", successful_run)
        self._setup()._verify_quarantine_cache_access()

        assert len(calls) == 1
        command, kwargs = calls[0]
        assert command[:5] == ["docker", "exec", "--user", "fakeroot", "-e"]
        assert command[-1] == cache
        probe = command[-3]
        assert 'test -d "$1" && test -r "$1" && test -x "$1"' in probe
        assert 'test -w "$1"' not in probe
        assert 'find "$1" -type f -print -quit' in probe
        assert kwargs == {"capture_output": True, "text": True, "timeout": 60}

    def test_maven_cache_probe_also_requires_write(self, monkeypatch):
        repo = "/root/.m2/repository"
        monkeypatch.delenv("SWE_MILESTONE_CACHE_PATHS", raising=False)
        monkeypatch.setenv("SWE_MILESTONE_MAVEN_REPO_LOCAL", repo)
        calls = []

        def successful_run(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr("harness.e2e.container_setup.subprocess.run", successful_run)
        self._setup()._verify_quarantine_cache_access()

        assert len(calls) == 1
        assert 'test -w "$1"' in calls[0][-3]

    def test_inaccessible_cache_fails_fast(self, monkeypatch):
        repo = "/root/.m2/repository"
        monkeypatch.delenv("SWE_MILESTONE_CACHE_PATHS", raising=False)
        monkeypatch.setenv("SWE_MILESTONE_MAVEN_REPO_LOCAL", repo)
        monkeypatch.setattr(
            "harness.e2e.container_setup.subprocess.run",
            lambda *args, **kwargs: SimpleNamespace(
                returncode=1, stdout="", stderr="permission denied"
            ),
        )

        with pytest.raises(RuntimeError, match="not usable by fakeroot.*permission denied"):
            self._setup()._verify_quarantine_cache_access()

    def test_no_maven_repo_skips_offline_smoke(self, monkeypatch):
        self._clear_cache_env(monkeypatch)

        def unexpected_run(*args, **kwargs):
            raise AssertionError("subprocess should not run without Maven")

        monkeypatch.setattr("harness.e2e.container_setup.subprocess.run", unexpected_run)
        self._setup()._verify_maven_offline_smoke()

    def test_maven_offline_smoke_loads_project_as_fakeroot(self, monkeypatch):
        self._clear_cache_env(monkeypatch)
        repo = "/root/.m2/repository"
        monkeypatch.setenv("SWE_MILESTONE_MAVEN_REPO_LOCAL", repo)
        calls = []

        def successful_run(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr("harness.e2e.container_setup.subprocess.run", successful_run)
        self._setup()._verify_maven_offline_smoke()

        assert len(calls) == 1
        command, kwargs = calls[0]
        assert command[:5] == ["docker", "exec", "--user", "fakeroot", "-e"]
        assert ["-w", "/testbed", "trial-container", "mvn", "-q", "-o"] == command[6:12]
        assert f"-Dmaven.repo.local={repo}" in command
        assert ["-N", "-f", "pom.xml", "spotless:check"] == command[13:17]
        assert "-Dspotless.check.skip=false" in command
        assert "-Dspotless.check.skip=true" not in command
        assert kwargs == {"capture_output": True, "text": True, "timeout": 120}

    def test_maven_offline_smoke_runs_each_configured_module_probe(self, monkeypatch):
        repo = "/root/.m2/repository"
        monkeypatch.setenv("SWE_MILESTONE_MAVEN_REPO_LOCAL", repo)
        monkeypatch.setenv(
            "SWE_MILESTONE_MAVEN_PLUGIN_PROBES",
            json.dumps([
                {"pom": "pom.xml", "goal": "spotless:check", "timeout_seconds": 90},
                {
                    "pom": "dubbo-dependencies-bom/pom.xml",
                    "goal": "spotless:check",
                    "timeout_seconds": 180,
                },
            ]),
        )
        calls = []

        def successful_run(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr("harness.e2e.container_setup.subprocess.run", successful_run)
        self._setup()._verify_maven_offline_smoke()

        assert len(calls) == 2
        assert calls[0][0][15:17] == ["pom.xml", "spotless:check"]
        assert calls[1][0][15:17] == ["dubbo-dependencies-bom/pom.xml", "spotless:check"]
        assert calls[0][1]["timeout"] == 90
        assert calls[1][1]["timeout"] == 180

    def test_maven_offline_smoke_invalid_probe_env_fails_closed(self, monkeypatch):
        monkeypatch.setenv("SWE_MILESTONE_MAVEN_REPO_LOCAL", "/root/.m2/repository")
        monkeypatch.setenv("SWE_MILESTONE_MAVEN_PLUGIN_PROBES", '[{"pom":"../pom.xml"}]')
        with pytest.raises(RuntimeError, match="Invalid Maven plugin probe"):
            self._setup()._verify_maven_offline_smoke()

    def test_maven_offline_smoke_failure_is_fatal(self, monkeypatch):
        self._clear_cache_env(monkeypatch)
        repo = "/root/.m2/repository"
        monkeypatch.setenv("SWE_MILESTONE_MAVEN_REPO_LOCAL", repo)
        monkeypatch.setattr(
            "harness.e2e.container_setup.subprocess.run",
            lambda *args, **kwargs: SimpleNamespace(
                returncode=1, stdout="", stderr="extension unavailable offline"
            ),
        )

        with pytest.raises(RuntimeError, match="smoke test failed.*extension unavailable"):
            self._setup()._verify_maven_offline_smoke()


class TestExistingContainerCacheRepair:
    """Resume paths must not bypass the cache repair and verification gate."""

    @staticmethod
    def _setup(running):
        setup = object.__new__(ContainerSetup)
        setup.container_name = "trial-container"
        setup.container_exists = lambda: True
        setup.is_running = lambda: running
        return setup

    def test_running_container_is_repaired_then_verified(self, monkeypatch):
        setup = self._setup(running=True)
        events = []
        setup.verify_runtime_environment = lambda: events.append("runtime-gate")
        monkeypatch.setattr(
            "harness.e2e.container_setup.inspect_docker_image_id",
            lambda *_args, **_kwargs: "a" * 64,
        )

        setup.start_container()

        assert events == ["runtime-gate"]

    def test_stopped_container_starts_then_repairs_and_verifies(self, monkeypatch):
        setup = self._setup(running=False)
        events = []
        setup.verify_runtime_environment = lambda: events.append("runtime-gate")
        monkeypatch.setattr(
            "harness.e2e.container_setup.inspect_docker_image_id",
            lambda *_args, **_kwargs: "a" * 64,
        )

        def successful_run(command, **kwargs):
            events.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr("harness.e2e.container_setup.subprocess.run", successful_run)
        setup.start_container()

        assert events == [
            (["docker", "start", "trial-container"], {"check": True}),
            "runtime-gate",
        ]


class TestGoDisposableRuntime:
    @staticmethod
    def _setup():
        setup = object.__new__(ContainerSetup)
        setup.container_name = "trial-container"
        return setup

    def test_prepare_repairs_and_resets_before_runtime_verification(self, monkeypatch):
        monkeypatch.setenv("SWE_MILESTONE_GO_OFFLINE", "1")
        setup = self._setup()
        events = []
        setup._prepare_go_disposable_dirs = lambda **kwargs: events.append(
            ("prepare", kwargs["reset_module_cache"])
        )
        setup.verify_runtime_environment = lambda: events.append(("verify", None))

        setup.prepare_agent_invocation()

        assert events == [("prepare", True), ("verify", None)]

    def test_disposable_dir_repair_is_symlink_safe_and_covers_all_go_outputs(
        self, monkeypatch
    ):
        monkeypatch.setenv("SWE_MILESTONE_GO_OFFLINE", "1")
        setup = self._setup()
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr("harness.e2e.container_setup.subprocess.run", run)
        setup._prepare_go_disposable_dirs(reset_module_cache=True)

        command, kwargs = calls[0]
        script = command[-3]
        loop = (
            'for path in "$module" /home/fakeroot/.cache/go-build '
            '\\\n  /home/fakeroot/.cache/evoclaw-goenv /home/fakeroot/go/bin; do'
        )
        repair = 'test -L "$path" || test ! -d "$path"'
        reset_all = (
            'if test "$reset_disposable" = 1; then\n'
            '    find "$path" -xdev -mindepth 1 -delete\n'
            '  fi'
        )
        assert loop in script
        assert 'for parent in "$home/.cache" "$home/go"; do' in script
        assert 'test -L "$home" || test ! -d "$home"' in script
        assert script.index('for parent in "$home/.cache" "$home/go"; do') < script.index(loop)
        assert repair in script
        assert reset_all in script
        assert script.index(loop) < script.index(repair) < script.index(reset_all)
        assert script.index(reset_all) < script.index("\ndone", script.index(reset_all))
        assert script.count('find "$path" -xdev -mindepth 1 -delete') == 1
        assert 'find "$module" -xdev -mindepth 1 -delete' not in script
        assert 'rm -rf -- "$path"/*' not in script
        assert command[-1] == "1"
        assert kwargs["timeout"] == 300

class TestGitBaselineInitialization:
    """A source-only image must become a valid, fail-closed Git baseline."""

    @staticmethod
    def _setup():
        setup = object.__new__(ContainerSetup)
        setup.container_name = "trial-container"
        return setup

    def test_missing_repository_is_initialized_before_truncation(self, monkeypatch):
        calls = []

        def successful_run(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        monkeypatch.setattr("harness.e2e.container_setup.subprocess.run", successful_run)
        self._setup().truncate_git_history()

        assert len(calls) == 1
        command, kwargs = calls[0]
        script = command[-1]
        assert "git rev-parse --git-dir" in script
        assert "git init -q" in script
        assert 'git commit -q -m "Initial baseline"' in script
        assert kwargs == {"capture_output": True, "text": True}

    def test_git_setup_failure_blocks_agent_start(self, monkeypatch):
        monkeypatch.setattr(
            "harness.e2e.container_setup.subprocess.run",
            lambda *args, **kwargs: SimpleNamespace(
                returncode=128, stdout="", stderr="fatal: cannot initialize repository"
            ),
        )

        with pytest.raises(RuntimeError, match="Git baseline initialization.*cannot initialize"):
            self._setup().truncate_git_history()
