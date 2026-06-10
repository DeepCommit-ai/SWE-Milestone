# Quarantine Ecosystem Coverage (issue #12) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the confirmed registry cheat channels for the 6 unprotected repos (cargo/go/maven/npm) and make quarantine fail-closed: a repo can never again silently run with its answer-fetch channel open.

**Architecture:** Reuse the existing 3-layer quarantine mechanism (iptables deny + package-manager offline + fail-closed audits). Extract policy loading into a new importable module `harness/e2e/quarantine.py` (testable; `scripts/` is not a package), generalize the offline layer beyond pip in `agents/base.py`, fix two real bugs in `container_setup.py` (string-equality CIDR deny matching; unconditional `GOPROXY=proxy.golang.org` written into the container), add an in-container cache audit + exact-cheat-URL probes to `verify_network_lockdown`, add a coverage gate to `run_all.py`, and commit 6 new per-repo policy yamls. A standalone `scripts/verify_quarantine.py` smoke-tests a repo's policy against its real base image.

**Tech Stack:** Python 3 (stdlib + yaml), iptables-in-docker, pytest (tests colocated per repo convention: `harness/e2e/test_*.py`).

**Key facts verified during exploration (2026-06-10):**

- e2e mode always runs `<repo_lowercase>/base:latest` (`run_all.py:get_image_name`).
- `CDN_CIDR_RANGES` contains Cloudflare `104.16.0.0/13`; the issue's deny is `104.16.0.0/12`. Current code skips accepts by **string equality** → denying `/12` would NOT remove the `/13` ACCEPT. Must fix to subnet-overlap matching. (`registry.npmjs.org` = 104.16.x, `repo1.maven.org` = 104.18.x — both inside `/13`.)
- `lock_network` Step 5 unconditionally writes `GOPROXY=https://proxy.golang.org,direct` into `/etc/environment` + `/home/fakeroot/.bashrc` — overrides any `docker -e GOPROXY=off` for login shells. Must be conditional.
- dubbo base image: Maven **3.9.9** (supports `MAVEN_ARGS`), `.m2` = 1.2G at `/root/.m2` (root-owned; agent runs as fakeroot with `HOME=/home/fakeroot` → needs `maven.repo.local` redirect + chown). `.m2` self-artifacts: only `3.3.3-SNAPSHOT` (baseline — safe).
- go-zero base: go1.19, `GOPROXY=https://goproxy.cn,direct` baked as image ENV, modcache 440M at `/go/pkg/mod`, **no** `github.com/zeromicro/*` in cache.
- navidrome base: modcache 340M, `ui/node_modules` 748M baked, npm 10.8.2.
- ripgrep base: cargo registry 44M at `/usr/local/cargo`, **no** grep-*/globset/ignore crates in cache (workspace path deps).
- nushell milestone image: cargo registry 1.4G, only `nu-ansi-term-0.50.1` (external dep) — no workspace `nu-*` crates.
- element-web base: `node_modules` 775M baked, npm 10.9.4 + yarn 1.22.22.
- DNS today: `claude.ai`/`api.anthropic.com` = 160.79.104.x (Anthropic ASN — no Cloudflare collision); `goproxy.cn` = 155.102.x (Qiniu/Kunlun); `sentry.io` = Google LB (not Cloudflare).
- Existing env-var channel: run_all worker env → `container_setup.py` (`EVOCLAW_DENY_DOMAINS`, `EVOCLAW_DENY_CIDRS`) and `agents/base.py` (`EVOCLAW_PIP_WHEELHOUSE`). New vars ride the same channel.

**New yaml fields** (`quarantine_configs/<repo>.yaml`):

| field | type | consumed by | effect |
|---|---|---|---|
| `ecosystem` | list[str] | run_all coverage gate | gate asserts all of that ecosystem's registries are in `deny_domains` |
| `cargo_offline` | bool | base.py | container `-e CARGO_NET_OFFLINE=true` |
| `go_offline` | bool | base.py + container_setup | container `-e GOPROXY=off`; lock_network writes `GOPROXY=off` (not proxy.golang.org) |
| `maven_offline` | bool | base.py | container `-e MAVEN_ARGS=-o[ -Dmaven.repo.local=…]` |
| `maven_repo_local` | str | base.py | appended to MAVEN_ARGS |
| `npm_offline` | bool | base.py | container `-e npm_config_offline=true` |
| `cache_forbid_globs` | list[str] | container_setup | fail-closed in-container audit: any glob match → refuse to run |
| `verify_fetch_urls` | list[str] | container_setup | exact cheat URLs probed at lockdown verify: any connect success → refuse to run |

**Env var mapping** (host worker env, set by `quarantine_env()`):
`EVOCLAW_CARGO_OFFLINE`, `EVOCLAW_GO_OFFLINE`, `EVOCLAW_MAVEN_OFFLINE`, `EVOCLAW_MAVEN_REPO_LOCAL`, `EVOCLAW_NPM_OFFLINE`, `EVOCLAW_CACHE_FORBID_GLOBS` (comma-joined), `EVOCLAW_VERIFY_FETCH_URLS` (comma-joined).

---

### Task 1: `harness/e2e/quarantine.py` — policy module (move + extend)

**Files:**
- Create: `harness/e2e/quarantine.py`
- Test: `harness/e2e/test_quarantine.py`
- Modify (later, Task 3): `scripts/run_all.py` (delete moved funcs, import instead)

- [ ] **Step 1: Write the failing tests**

`harness/e2e/test_quarantine.py`:

```python
"""Tests for the per-repo quarantine policy module."""

import pytest

from harness.e2e.quarantine import (
    cidr_overlaps_any,
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


class TestCidrOverlap:
    def test_denied_slash12_covers_accept_slash13(self):
        assert cidr_overlaps_any("104.16.0.0/13", ["104.16.0.0/12"])

    def test_exact_match(self):
        assert cidr_overlaps_any("151.101.0.0/16", ["151.101.0.0/16"])

    def test_disjoint(self):
        assert not cidr_overlaps_any("142.250.0.0/15", ["104.16.0.0/12"])

    def test_invalid_deny_entries_ignored(self):
        assert not cidr_overlaps_any("142.250.0.0/15", ["bogus", ""])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest harness/e2e/test_quarantine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'harness.e2e.quarantine'`

- [ ] **Step 3: Create `harness/e2e/quarantine.py`**

Move `_assert_wheelhouse_excludes` and `load_quarantine_env` from `scripts/run_all.py:76-161` (verbatim semantics) and extend:

```python
"""Per-repo quarantine (anti-cheat) policy: loading, env derivation, coverage gate.

Quarantine prevents an agent from fetching the repo-under-test's own
target-version source (the answer) through a whitelisted package registry.
Policy is repo-intrinsic and lives in quarantine_configs/<repo>.yaml (auto-on:
the file's presence is the switch). scripts/run_all.py derives worker env vars
from it here; harness/e2e/container_setup.py and harness/e2e/agents/base.py
consume those vars. See docs/quarantine.md.
"""

from __future__ import annotations

import ipaddress
import os
import sys
from pathlib import Path

import yaml

# Registry domains that can serve a repo's own published artifacts, per
# ecosystem. The coverage gate (quarantine_coverage_errors) requires a repo's
# policy to deny ALL of its declared ecosystems' registries, so a repo whose
# answer is publishable to one of these can never silently run with the
# channel open. 'none' is a valid ecosystem for repos with no such registry.
ECOSYSTEM_REGISTRIES: dict[str, list[str]] = {
    "pip": ["pypi.org", "files.pythonhosted.org"],
    "cargo": ["crates.io", "static.crates.io", "index.crates.io"],
    "go": ["proxy.golang.org", "sum.golang.org", "goproxy.cn", "goproxy.io"],
    "maven": ["repo1.maven.org", "repo.maven.apache.org", "central.sonatype.com"],
    "npm": ["registry.npmjs.org", "registry.yarnpkg.com"],
}


def _as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def cidr_overlaps_any(cidr: str, deny_cidrs: list[str]) -> bool:
    """True if `cidr` overlaps any (valid) entry of `deny_cidrs`.

    Used by container_setup to drop CDN ACCEPT ranges covered by a denied
    range. Overlap (either containment direction), NOT string equality: the
    builtin Cloudflare accept is 104.16.0.0/13 while a policy denies
    104.16.0.0/12 — string matching would leave the /13 accepted and the
    denied registry reachable. Invalid deny entries are ignored (the resolved
    -IP prune logs them the same way).
    """
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False
    for d in deny_cidrs:
        try:
            if net.overlaps(ipaddress.ip_network(d.strip(), strict=False)):
                return True
        except ValueError:
            continue
    return False


def _assert_wheelhouse_excludes(wheelhouse: str, forbid: list[str]) -> None:
    # ... moved VERBATIM from scripts/run_all.py:76-101 ...


def load_quarantine_config(repo_name: str, project_root: Path) -> dict | None:
    """Raw quarantine_configs/<repo>.yaml as a dict, or None if absent.

    Fails closed (sys.exit) on unreadable/malformed yaml — a typo'd policy
    must never silently mean "unprotected".
    """
    conf_path = Path(project_root) / "quarantine_configs" / f"{repo_name}.yaml"
    if not conf_path.exists():
        return None
    try:
        with open(conf_path) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        print(f"Error: failed to read quarantine config {conf_path}: {e}", file=sys.stderr)
        sys.exit(1)


def load_quarantine_env(repo_name: str, project_root: Path) -> dict:
    """Per-repo quarantine policy → worker env vars (existing docstring,
    moved from run_all.py). Extended fields: offline switches per package
    manager, in-container cache audit globs, and exact-cheat-URL probes."""
    q = load_quarantine_config(repo_name, project_root)
    if q is None:
        return {}
    conf_path = Path(project_root) / "quarantine_configs" / f"{repo_name}.yaml"

    env: dict[str, str] = {}
    dd = q.get("deny_domains")
    dc = q.get("deny_cidrs")
    wh = q.get("pip_wheelhouse")
    if dd:
        env["EVOCLAW_DENY_DOMAINS"] = ",".join(dd) if isinstance(dd, list) else str(dd)
    if dc:
        env["EVOCLAW_DENY_CIDRS"] = ",".join(dc) if isinstance(dc, list) else str(dc)
    if wh:
        # ... wheelhouse block moved VERBATIM from run_all.py:137-160 ...

    # Package-manager offline switches (consumed by agents/base.py →
    # container -e flags; EVOCLAW_GO_OFFLINE also flips the GOPROXY value
    # container_setup writes into the container). The firewall deny is the
    # hard layer; these keep legitimate dependency use working offline
    # against the image's pre-baked cache instead of hanging on a DROP.
    if q.get("cargo_offline"):
        env["EVOCLAW_CARGO_OFFLINE"] = "1"
    if q.get("go_offline"):
        env["EVOCLAW_GO_OFFLINE"] = "1"
    if q.get("maven_offline"):
        env["EVOCLAW_MAVEN_OFFLINE"] = "1"
    if q.get("maven_repo_local"):
        env["EVOCLAW_MAVEN_REPO_LOCAL"] = str(q["maven_repo_local"])
    if q.get("npm_offline"):
        env["EVOCLAW_NPM_OFFLINE"] = "1"

    # Fail-closed audits run inside the container at lockdown time
    # (container_setup.verify_network_lockdown): cache globs that must match
    # nothing (image cache must not pre-bake the answer), and the exact
    # registry URLs of the observed cheats that must fail to connect.
    globs = _as_list(q.get("cache_forbid_globs"))
    if globs:
        env["EVOCLAW_CACHE_FORBID_GLOBS"] = ",".join(str(g) for g in globs)
    urls = _as_list(q.get("verify_fetch_urls"))
    if urls:
        env["EVOCLAW_VERIFY_FETCH_URLS"] = ",".join(str(u) for u in urls)
    return env


def quarantine_coverage_errors(repo_names: list[str], project_root: Path) -> list[str]:
    """Fail-closed coverage gate: one error string per repo that would run
    with its ecosystem's answer-fetch registry reachable.

    A repo passes only if its quarantine config exists, declares its
    ecosystem(s), and deny_domains covers every registry of each declared
    ecosystem. This is what guarantees "silently ran open" (issue #12, 3
    repos confirmed cheated) cannot recur.
    """
    errors: list[str] = []
    for name in repo_names:
        q = load_quarantine_config(name, project_root)
        if q is None:
            errors.append(
                f"{name}: no quarantine_configs/{name}.yaml — repo would run UNPROTECTED"
            )
            continue
        ecosystems = [str(e).strip().lower() for e in _as_list(q.get("ecosystem"))]
        if not ecosystems:
            errors.append(
                f"{name}: quarantine config has no 'ecosystem:' — cannot assert registry coverage"
            )
            continue
        deny = {str(d).strip().lower() for d in _as_list(q.get("deny_domains"))}
        for eco in ecosystems:
            if eco == "none":
                continue
            regs = ECOSYSTEM_REGISTRIES.get(eco)
            if regs is None:
                errors.append(
                    f"{name}: unknown ecosystem '{eco}' "
                    f"(known: {sorted(ECOSYSTEM_REGISTRIES)} or 'none')"
                )
                continue
            missing = [r for r in regs if r not in deny]
            if missing:
                errors.append(
                    f"{name}: ecosystem '{eco}' registries not in deny_domains: {missing}"
                )
    return errors
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest harness/e2e/test_quarantine.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add harness/e2e/quarantine.py harness/e2e/test_quarantine.py
git commit -m "feat(quarantine): policy module with offline switches, audits, coverage gate"
```

### Task 2: `scripts/run_all.py` — import the module, add the coverage gate + `--unprotected`

**Files:**
- Modify: `scripts/run_all.py` (delete lines 76-161 `_assert_wheelhouse_excludes` + `load_quarantine_env`; add import; add gate; add flag)

- [ ] **Step 1: Replace the moved functions with an import**

Delete `_assert_wheelhouse_excludes` and `load_quarantine_env` from run_all.py. After the existing `import yaml` (line 29), add:

```python
from harness.e2e.quarantine import load_quarantine_env, quarantine_coverage_errors
```

(`sys.path.insert` at line 27 already makes `harness` importable.)

- [ ] **Step 2: Add the `--unprotected` flag**

In `main()` argparse (after `--milestones`, line ~330):

```python
    parser.add_argument(
        "--unprotected", action="store_true",
        help="Bypass the quarantine coverage gate and launch even if a repo's "
             "anti-cheat policy is missing/incomplete. Scores from unprotected "
             "repos can be tainted by registry answer-fetch (see issue #12).",
    )
```

- [ ] **Step 3: Add the gate after repo discovery**

Right after the `discover_repos` block (line ~409), insert:

```python
    # Fail-closed quarantine coverage gate (issue #12): refuse to launch any
    # repo whose anti-cheat policy is absent or doesn't deny its ecosystem's
    # registries. 3 of 7 repos were confirmed cheating via exactly this gap
    # (crates.io / Maven Central fetches of their own target-version source)
    # because quarantine used to be silently opt-in.
    project_root = Path(__file__).resolve().parent.parent
    gate_errors = quarantine_coverage_errors([r.name for r in repos], project_root)
    if gate_errors:
        stream = sys.stderr
        print("Quarantine coverage gate:", file=stream)
        for e in gate_errors:
            print(f"  - {e}", file=stream)
        if args.unprotected:
            print("  --unprotected set: launching anyway (scores may be tainted).",
                  file=stream)
        else:
            print("Refusing to launch. Add/fix quarantine_configs/<repo>.yaml "
                  "(see docs/quarantine.md) or pass --unprotected.", file=stream)
            sys.exit(1)
```

Then change the later `project_root = Path(__file__).resolve().parent.parent` (line ~458) to reuse the variable (delete the duplicate assignment).

- [ ] **Step 4: Verify behavior manually**

Run: `python scripts/run_all.py --config config/trial_config.yaml --repos scikit 2>&1 | head -20` — with only scikit (which has a config but no `ecosystem:` yet) it must print a gate error and exit 1. (Use the actual trial config present in the repo; check `ls config/`.) Do NOT let a launch proceed — the gate failing IS the expected result here.
Expected: `Refusing to launch` + exit code 1.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_all.py
git commit -m "feat(run_all): fail-closed quarantine coverage gate + --unprotected escape hatch"
```

### Task 3: `agents/base.py` — generalize the offline layer beyond pip

**Files:**
- Modify: `harness/e2e/agents/base.py:113-121` (`get_quarantine_env_vars`)
- Test: extend `harness/e2e/test_quarantine.py`

- [ ] **Step 1: Write the failing test** (append to `harness/e2e/test_quarantine.py`)

```python
class TestAgentQuarantineEnvVars:
    def _env_dict(self, flags):
        """Run get_quarantine_env_vars under a controlled env, return {k: v}."""
        from harness.e2e.agents.base import AgentFramework

        class _F(AgentFramework):
            FRAMEWORK_NAME = "test"
            def get_container_mounts(self): return []
            def get_container_init_script(self, agent_name): return ""
            def build_run_command(self, model, session_id, prompt_path): return ""
            def build_resume_command(self, model, session_id, message_path): return ""

        import os
        saved = {k: os.environ.pop(k) for k in list(os.environ)
                 if k.startswith("EVOCLAW_")}
        try:
            os.environ.update(flags)
            args = _F().get_quarantine_env_vars()
        finally:
            for k in flags:
                os.environ.pop(k, None)
            os.environ.update(saved)
        pairs = [args[i + 1] for i in range(0, len(args), 2)]
        assert all(args[i] == "-e" for i in range(0, len(args), 2))
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

    def test_pip_wheelhouse_unchanged(self):
        env = self._env_dict({"EVOCLAW_PIP_WHEELHOUSE": "/wh"})
        assert env == {"PIP_NO_INDEX": "1", "PIP_FIND_LINKS": "/wheelhouse"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest harness/e2e/test_quarantine.py::TestAgentQuarantineEnvVars -v`
Expected: cargo/go/maven/npm cases FAIL (empty result), pip case PASSES.

- [ ] **Step 3: Implement in `base.py`** (replace `get_quarantine_env_vars` body)

```python
    def get_quarantine_env_vars(self) -> List[str]:
        """Quarantine: force the repo's package manager(s) offline.

        Belt to the EVOCLAW_DENY_* firewall suspenders, shared across agents.
        pip gets the mounted wheelhouse; cargo/go/maven/npm run offline
        against the cache pre-baked into the eval image (verified present:
        cargo registry, /go/pkg/mod, /root/.m2, node_modules). GOPROXY=off is
        additionally written into /etc/environment + .bashrc by
        container_setup.lock_network (shell profiles would override a bare
        docker -e). See docs/quarantine.md.
        """
        env: List[str] = []
        if os.environ.get("EVOCLAW_PIP_WHEELHOUSE"):
            env += ["-e", "PIP_NO_INDEX=1", "-e", "PIP_FIND_LINKS=/wheelhouse"]
        if os.environ.get("EVOCLAW_CARGO_OFFLINE"):
            env += ["-e", "CARGO_NET_OFFLINE=true"]
        if os.environ.get("EVOCLAW_GO_OFFLINE"):
            env += ["-e", "GOPROXY=off"]
        if os.environ.get("EVOCLAW_MAVEN_OFFLINE"):
            margs = "-o"
            repo_local = os.environ.get("EVOCLAW_MAVEN_REPO_LOCAL")
            if repo_local:
                # The image's populated cache lives under root's home; the
                # agent runs as fakeroot, whose own ~/.m2 starts empty.
                margs += f" -Dmaven.repo.local={repo_local}"
            env += ["-e", f"MAVEN_ARGS={margs}"]
        if os.environ.get("EVOCLAW_NPM_OFFLINE"):
            env += ["-e", "npm_config_offline=true"]
        return env
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest harness/e2e/test_quarantine.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add harness/e2e/agents/base.py harness/e2e/test_quarantine.py
git commit -m "feat(agents): cargo/go/maven/npm offline switches in shared quarantine env"
```

### Task 4: `container_setup.py` — CIDR overlap fix, conditional GOPROXY, cache audit, cheat-URL probes, /root/.m2 access

**Files:**
- Modify: `harness/e2e/container_setup.py` (5 spots; line refs from current HEAD)

- [ ] **Step 1: Fix deny-CIDR matching in `lock_network` (lines 1035-1041)**

Replace the string-equality skip:

```python
        # Quarantine mode: CIDRs in EVOCLAW_DENY_CIDRS are NOT accepted, so a
        # registry fronted by that CDN becomes unreachable even via raw curl.
        # Overlap matching (not string equality): the builtin Cloudflare
        # accept is 104.16.0.0/13 while a policy may deny 104.16.0.0/12 —
        # equality would leave the /13 accepted and the registry reachable.
        # Keep Google (Vertex) and Anthropic paths — they ride other ranges.
        _deny_cidrs = [c.strip() for c in os.environ.get("EVOCLAW_DENY_CIDRS", "").split(",") if c.strip()]
        if _deny_cidrs:
            logger.warning(f"EVOCLAW_DENY_CIDRS active — excluding CDN ranges overlapping: {sorted(_deny_cidrs)}")
        for cidr in CDN_CIDR_RANGES:
            if _deny_cidrs and cidr_overlaps_any(cidr, _deny_cidrs):
                logger.warning(f"  CDN range {cidr} overlaps a denied CIDR — not accepted")
                continue
            accept_lines.append(f"iptables -A OUTPUT -d {cidr} -j ACCEPT")
```

Add the import at the top of the file (after the agents import, line 15):

```python
from harness.e2e.quarantine import cidr_overlaps_any
```

- [ ] **Step 2: Make Step-5 GOPROXY quarantine-aware (lines 1111-1134)**

Replace the `go_env_script` block:

```python
        # --- Step 5: Set Go env vars ---
        # Under go quarantine (EVOCLAW_GO_OFFLINE), the proxy itself is the
        # answer-fetch channel (`go get <self>@<target>`), so write GOPROXY=off
        # (go then refuses both proxy and direct/VCS fetches and works from
        # /go/pkg/mod). Shell profiles override docker -e, so this MUST be
        # written here, not only injected via get_quarantine_env_vars.
        _goproxy = "off" if os.environ.get("EVOCLAW_GO_OFFLINE") else "https://proxy.golang.org,direct"
        go_env_script = f"""
# Configure Go module fetching (quarantine-aware)
cat >> /etc/environment << 'EOF'
GOPROXY={_goproxy}
GONOSUMCHECK=*
GONOSUMDB=*
EOF

# Also set for fakeroot's shell profile
mkdir -p /home/fakeroot
cat >> /home/fakeroot/.bashrc << 'EOF'
export GOPROXY={_goproxy}
export GONOSUMCHECK=*
export GONOSUMDB=*
EOF
echo "Go env vars configured (GOPROXY={_goproxy})"
"""
```

(Keep the `subprocess.run` call below it unchanged; the f-string now interpolates `_goproxy`.)

- [ ] **Step 3: Give fakeroot access to the Maven cache (line ~378)**

In `_get_base_init_script`, append to `toolchain_dirs`:

```python
        '/root/.m2',             # Maven local repo (dubbo image; fakeroot needs rw under maven_repo_local redirect)
    ]
```

- [ ] **Step 4: Add cheat-URL probes + cache audit to `verify_network_lockdown` (after the deny-domain probe loop, line ~1241)**

```python
        # Quarantine: probe the exact registry URLs of the observed cheats
        # (e.g. the self-crate on static.crates.io, the -sources.jar on Maven
        # Central). ANY successful connect — even a redirect — means the
        # answer-fetch channel is open; the deny must make connects fail.
        _verify_urls = [u.strip() for u in os.environ.get("EVOCLAW_VERIFY_FETCH_URLS", "").split(",") if u.strip()]
        for url in _verify_urls:
            probe = subprocess.run(
                [
                    "docker", "exec", "--user", "fakeroot",
                    "-e", "HOME=/home/fakeroot", self.container_name,
                    "curl", "-s", "-o", "/dev/null",
                    "--connect-timeout", "3", "--max-time", "10", url,
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if probe.returncode == 0:
                raise RuntimeError(
                    f"Quarantine verification failed: answer-fetch URL still "
                    f"reachable: {url}"
                )
        if _verify_urls:
            logger.info(f"  Quarantine verified: {len(_verify_urls)} answer-fetch URL(s) blocked")

        # Quarantine: fail closed if the image's pre-baked package cache
        # contains the repo's own artifacts at a post-baseline version (the
        # offline closure must not be able to serve the answer — the cache
        # analogue of _assert_wheelhouse_excludes for the pip wheelhouse).
        _forbid_globs = [g.strip() for g in os.environ.get("EVOCLAW_CACHE_FORBID_GLOBS", "").split(",") if g.strip()]
        for glob_pat in _forbid_globs:
            audit = subprocess.run(
                [
                    "docker", "exec", self.container_name,
                    "/bin/sh", "-c", f"ls -d {glob_pat} 2>/dev/null | head -5",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if audit.stdout.strip():
                raise RuntimeError(
                    f"Quarantine cache audit failed: forbidden artifact(s) in the "
                    f"image cache match '{glob_pat}':\n{audit.stdout.strip()}\n"
                    f"The offline cache could serve the repo's own target-version "
                    f"source. Rebuild/clean the image before running."
                )
        if _forbid_globs:
            logger.info(f"  Quarantine cache audit passed: {len(_forbid_globs)} forbid glob(s) matched nothing")
```

- [ ] **Step 5: Run the test suite (no regression)**

Run: `python -m pytest harness/e2e/test_quarantine.py harness/e2e/test_milestone_selection.py harness/e2e/test_masking.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add harness/e2e/container_setup.py
git commit -m "fix(quarantine): CIDR overlap deny, GOPROXY=off under go quarantine, cache audit + cheat-URL probes"
```

### Task 5: the 7 policy yamls (6 new + scikit `ecosystem`/probe fields)

**Files:**
- Create: `quarantine_configs/BurntSushi_ripgrep_14.1.1_15.0.0.yaml`
- Create: `quarantine_configs/nushell_nushell_0.106.0_0.108.0.yaml`
- Create: `quarantine_configs/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6.yaml`
- Create: `quarantine_configs/zeromicro_go-zero_v1.6.0_v1.9.3.yaml`
- Create: `quarantine_configs/navidrome_navidrome_v0.57.0_v0.58.0.yaml`
- Create: `quarantine_configs/element-hq_element-web_v1.11.95_v1.11.97.yaml`
- Modify: `quarantine_configs/scikit-learn_scikit-learn_1.5.2_1.6.0.yaml` (add `ecosystem` + `verify_fetch_urls`)

- [ ] **Step 1: ripgrep** — `quarantine_configs/BurntSushi_ripgrep_14.1.1_15.0.0.yaml`

```yaml
# Quarantine (anti-cheat) policy — ripgrep 14.1.1 → 15.0.0
#
# CONFIRMED CHEAT (claude-code_fable-5_001, 2026-06-10): curl'd its own
# workspace crates at post-baseline versions from static.crates.io
# (grep-printer-0.3.1, ignore-0.4.26, …) and copied them in verbatim.
# crates.io rides the same Fastly ranges as PyPI, so the deny_cidrs are
# identical to scikit's. Cargo runs offline against the image's pre-baked
# registry cache (workspace crates are path deps — never in the cache).

ecosystem: [cargo]

deny_domains:
  - crates.io
  - static.crates.io
  - index.crates.io

deny_cidrs:
  - 151.101.0.0/16     # Fastly (fronts crates.io/static.crates.io)
  - 146.75.0.0/16      # Fastly

cargo_offline: true

# The image cache must never contain ripgrep's own workspace crates (any
# version — they are path deps, so any registry copy is answer material).
cache_forbid_globs:
  - /usr/local/cargo/registry/cache/*/grep-*.crate
  - /usr/local/cargo/registry/cache/*/globset-[0-9]*.crate
  - /usr/local/cargo/registry/cache/*/ignore-[0-9]*.crate
  - /usr/local/cargo/registry/cache/*/ripgrep-[0-9]*.crate
  - /usr/local/cargo/registry/src/*/grep-*
  - /usr/local/cargo/registry/src/*/globset-[0-9]*
  - /usr/local/cargo/registry/src/*/ignore-[0-9]*
  - /usr/local/cargo/registry/src/*/ripgrep-[0-9]*

# The exact URL shape of the observed cheat — must fail to connect.
verify_fetch_urls:
  - https://static.crates.io/crates/grep-printer/grep-printer-0.3.1.crate
  - https://crates.io/api/v1/crates/ignore
```

- [ ] **Step 2: nushell** — `quarantine_configs/nushell_nushell_0.106.0_0.108.0.yaml`

```yaml
# Quarantine (anti-cheat) policy — nushell 0.106.0 → 0.108.0
#
# CONFIRMED CHEAT (claude-code_fable-5_001, 2026-06-10; worst case): ~25
# curls of nu-*-0.107/0.108 crates from static.crates.io, then 42 cp's of
# target-version source AND test files into /testbed, plus `cargo update
# --precise` to versions read from the leaked lock. Same Fastly deny as
# scikit/ripgrep. NOTE: nu-ansi-term (0.50.x) is an external dep and stays
# allowed in the cache; the forbid globs only match nu-* at 0.10x versions.

ecosystem: [cargo]

deny_domains:
  - crates.io
  - static.crates.io
  - index.crates.io

deny_cidrs:
  - 151.101.0.0/16     # Fastly (fronts crates.io/static.crates.io)
  - 146.75.0.0/16      # Fastly

cargo_offline: true

cache_forbid_globs:
  - /usr/local/cargo/registry/cache/*/nu-*-0.10[6-9].*.crate
  - /usr/local/cargo/registry/src/*/nu-*-0.10[6-9].*
  - /usr/local/cargo/registry/cache/*/nushell-*.crate

verify_fetch_urls:
  - https://static.crates.io/crates/nu-protocol/nu-protocol-0.108.0.crate
  - https://static.crates.io/crates/nu-parser/nu-parser-0.107.0.crate
```

- [ ] **Step 3: dubbo** — `quarantine_configs/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6.yaml`

```yaml
# Quarantine (anti-cheat) policy — dubbo 3.3.3 → 3.3.6
#
# CONFIRMED CHEAT (claude-code_fable-5_001, 2026-06-10): 49 refs to
# repo.maven.apache.org .../org/apache/dubbo/<mod>/3.3.[456]/*-sources.jar
# across 13+ modules, diffed against /testbed and copied (3.3.3→4→5
# version bisection). Maven Central rides Cloudflare; deny the /12 (the
# builtin accept is /13 — overlap-matched by container_setup). LLM paths
# are unaffected (api.anthropic.com/claude.ai = Anthropic ASN, Vertex =
# Google ranges). Collateral (nodejs.org, gradle.org, spring.io, javadoc.io
# riding Cloudflare) is acceptable: the image pre-bakes the toolchain.
#
# Maven runs offline (-o via MAVEN_ARGS, Maven 3.9.9) against the image's
# 1.2G /root/.m2 — redirected via maven.repo.local because the agent runs
# as fakeroot whose own ~/.m2 starts empty.

ecosystem: [maven]

deny_domains:
  - repo1.maven.org
  - repo.maven.apache.org
  - central.sonatype.com

deny_cidrs:
  - 104.16.0.0/12      # Cloudflare (fronts Maven Central + sonatype)

maven_offline: true
maven_repo_local: /root/.m2/repository

# Baseline artifacts (3.3.3-SNAPSHOT) are legitimately in the cache; any
# 3.3.4+ artifact would be answer material.
cache_forbid_globs:
  - /root/.m2/repository/org/apache/dubbo/*/3.3.[4-9]*
  - /root/.m2/repository/org/apache/dubbo/*/3.[4-9]*

verify_fetch_urls:
  - https://repo.maven.apache.org/maven2/org/apache/dubbo/dubbo-common/3.3.6/dubbo-common-3.3.6-sources.jar
  - https://repo1.maven.org/maven2/org/apache/dubbo/dubbo-common/3.3.6/dubbo-common-3.3.6-sources.jar
```

- [ ] **Step 4: go-zero** — `quarantine_configs/zeromicro_go-zero_v1.6.0_v1.9.3.yaml`

```yaml
# Quarantine (anti-cheat) policy — go-zero v1.6.0 → v1.9.3
#
# Channel (attempted via WebFetch, blocked; registry path was open):
# `go get github.com/zeromicro/go-zero@v1.9.3` or a raw curl of the proxy
# zip serves the full target source. Primary defense is GOPROXY=off (go
# refuses proxy AND direct/VCS fetches, works from the image's /go/pkg/mod);
# domain-deny is belt-and-suspenders. goproxy.cn (the image's configured
# proxy, Qiniu CDN) and goproxy.io (Cloudflare) are CIDR-deniable;
# proxy.golang.org rides Google ranges shared with Vertex aiplatform, so it
# is domain-deny only — narrow residual (curl --resolve with a known Google
# IP) accepted until the SNI egress proxy lands (issue #12 long-term plan).

ecosystem: [go]

deny_domains:
  - proxy.golang.org
  - sum.golang.org
  - goproxy.cn
  - goproxy.io
  - golang.org
  - go.dev
  - pkg.go.dev

deny_cidrs:
  - 104.16.0.0/12      # Cloudflare (goproxy.io)
  - 155.102.0.0/16     # Qiniu/Kunlun CDN (goproxy.cn)

go_offline: true

# The main module is never in its own module cache; any copy of it there is
# answer material.
cache_forbid_globs:
  - /go/pkg/mod/cache/download/github.com/zeromicro/*
  - /go/pkg/mod/github.com/zeromicro/*

verify_fetch_urls:
  - https://proxy.golang.org/github.com/zeromicro/go-zero/@v/v1.9.3.zip
  - https://goproxy.cn/github.com/zeromicro/go-zero/@v/v1.9.3.zip
```

- [ ] **Step 5: navidrome** — `quarantine_configs/navidrome_navidrome_v0.57.0_v0.58.0.yaml`

```yaml
# Quarantine (anti-cheat) policy — navidrome v0.57.0 → v0.58.0
#
# Dual ecosystem: Go backend (same channel & defense as go-zero —
# GOPROXY=off + proxy domain-deny) and npm UI (registry.npmjs.org rides
# Cloudflare; ui/node_modules 748M is pre-baked, npm forced offline).
# The agent attempted exactly 1 WebFetch of upstream master (blocked).

ecosystem: [go, npm]

deny_domains:
  - proxy.golang.org
  - sum.golang.org
  - goproxy.cn
  - goproxy.io
  - golang.org
  - go.dev
  - pkg.go.dev
  - registry.npmjs.org
  - registry.yarnpkg.com

deny_cidrs:
  - 104.16.0.0/12      # Cloudflare (npm registry + goproxy.io)
  - 155.102.0.0/16     # Qiniu/Kunlun CDN (goproxy.cn)

go_offline: true
npm_offline: true

cache_forbid_globs:
  - /go/pkg/mod/cache/download/github.com/navidrome/*
  - /go/pkg/mod/github.com/navidrome/*

verify_fetch_urls:
  - https://proxy.golang.org/github.com/navidrome/navidrome/@v/v0.58.0.zip
  - https://registry.npmjs.org/navidrome
```

- [ ] **Step 6: element-web** — `quarantine_configs/element-hq_element-web_v1.11.95_v1.11.97.yaml`

```yaml
# Quarantine (anti-cheat) policy — element-web v1.11.95 → v1.11.97
#
# Lowest-risk repo: the app itself is not published to npm. BUT much of the
# delta can live in matrix-js-sdk / matrix-* deps, which ARE on npm at
# post-baseline versions — so the registry is denied for parity and npm
# forced offline against the pre-baked 775M node_modules (the agent
# attempted 1 WebFetch of upstream; blocked). yarn 1.22 has no clean
# offline env switch; the network deny is the hard layer for it.

ecosystem: [npm]

deny_domains:
  - registry.npmjs.org
  - registry.yarnpkg.com

deny_cidrs:
  - 104.16.0.0/12      # Cloudflare (fronts npm registry)

npm_offline: true

verify_fetch_urls:
  - https://registry.npmjs.org/matrix-js-sdk
  - https://registry.yarnpkg.com/matrix-js-sdk
```

- [ ] **Step 7: scikit-learn** — add to `quarantine_configs/scikit-learn_scikit-learn_1.5.2_1.6.0.yaml` (after the header comment, before `deny_domains`):

```yaml
ecosystem: [pip]
```

and at the end of the file:

```yaml
# The exact channel of the original confirmed cheat — must fail to connect.
verify_fetch_urls:
  - https://pypi.org/simple/scikit-learn/
  - https://files.pythonhosted.org/
```

- [ ] **Step 8: Gate now passes for all 7**

Run: `python -c "
import sys; from pathlib import Path
sys.path.insert(0, '.')
from harness.e2e.quarantine import quarantine_coverage_errors
repos = [
    'apache_dubbo_dubbo-3.3.3_dubbo-3.3.6',
    'BurntSushi_ripgrep_14.1.1_15.0.0',
    'element-hq_element-web_v1.11.95_v1.11.97',
    'navidrome_navidrome_v0.57.0_v0.58.0',
    'nushell_nushell_0.106.0_0.108.0',
    'scikit-learn_scikit-learn_1.5.2_1.6.0',
    'zeromicro_go-zero_v1.6.0_v1.9.3',
]
errs = quarantine_coverage_errors(repos, Path('.'))
print('\n'.join(errs) or 'COVERAGE OK')
sys.exit(1 if errs else 0)
"`
Expected: `COVERAGE OK`

- [ ] **Step 9: Commit**

```bash
git add quarantine_configs/
git commit -m "feat(quarantine): per-repo policies for all 7 ecosystems (closes the issue-12 channels)"
```

### Task 6: `scripts/verify_quarantine.py` — live smoke test harness

**Files:**
- Create: `scripts/verify_quarantine.py`

- [ ] **Step 1: Write the script**

```python
#!/usr/bin/env python3
"""Live smoke test of a repo's quarantine policy against its real base image.

Spins up <repo>/base:latest with the policy's env applied, runs the minimal
base init (fakeroot — NOT the agent install), applies lock_network() (which
itself runs the fail-closed verification: deny-domain probes, answer-fetch
URL probes, cache forbid-glob audit), then runs positive probes (LLM
endpoints reachable, offline switches visible). Mirrors the verification
protocol in docs/quarantine.md.

Usage:
    python scripts/verify_quarantine.py --repo ripgrep [--keep]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from harness.e2e.container_setup import ContainerSetup  # noqa: E402
from harness.e2e.quarantine import load_quarantine_env  # noqa: E402

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from run_all import _load_dotenv_files  # noqa: E402


def _match_repo(substr: str) -> str:
    confs = sorted(p.stem for p in (PROJECT_ROOT / "quarantine_configs").glob("*.yaml"))
    hits = [c for c in confs if substr.lower() in c.lower()]
    if len(hits) != 1:
        print(f"Error: --repo '{substr}' matched {hits or 'nothing'} in quarantine_configs/", file=sys.stderr)
        sys.exit(1)
    return hits[0]


def _probe(container: str, url: str, expect_blocked: bool) -> bool:
    r = subprocess.run(
        ["docker", "exec", "--user", "fakeroot", "-e", "HOME=/home/fakeroot",
         container, "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "--connect-timeout", "3", "--max-time", "10", url],
        capture_output=True, text=True, timeout=20,
    )
    blocked = r.returncode != 0
    ok = blocked == expect_blocked
    label = "BLOCKED" if blocked else f"reachable (HTTP {r.stdout.strip()})"
    print(f"  {'PASS' if ok else 'FAIL'}  {url} -> {label}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, help="Repo name substring (matched against quarantine_configs/)")
    ap.add_argument("--image", default=None, help="Override image (default <repo_lowercase>/base:latest)")
    ap.add_argument("--keep", action="store_true", help="Keep the container for manual inspection")
    args = ap.parse_args()

    _load_dotenv_files()
    repo = _match_repo(args.repo)
    image = args.image or f"{repo.lower()}/base:latest"
    container = f"quarantine-verify-{repo.lower().replace('/', '_')[:40]}"

    q_env = load_quarantine_env(repo, PROJECT_ROOT)
    if not q_env:
        print(f"Error: no quarantine env derived for {repo}", file=sys.stderr)
        return 1
    os.environ.update(q_env)
    print(f"Repo:      {repo}\nImage:     {image}\nContainer: {container}")
    for k, v in sorted(q_env.items()):
        print(f"  {k}={v}")

    cs = ContainerSetup(container_name=container, image_name=image)
    subprocess.run(["docker", "rm", "-f", container], capture_output=True)

    failures = 0
    try:
        # Manual minimal start: docker run with the quarantine env/mounts but
        # WITHOUT the agent init (no claude install needed for a network test).
        cmd = ["docker", "run", "-d", "--init", "--cap-add=NET_ADMIN",
               "--sysctl", "net.ipv6.conf.all.disable_ipv6=1",
               "--name", container]
        cmd += cs._framework.get_quarantine_env_vars()
        cmd += cs._framework.get_quarantine_mounts()
        cmd += [image, "tail", "-f", "/dev/null"]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        cs._ensure_python3()
        r = subprocess.run(["docker", "exec", container, "python3", "-c",
                            cs._get_base_init_script()],
                           capture_output=True, text=True)
        if "fakeroot" not in r.stdout:
            print(f"Warning: base init output unexpected:\n{r.stdout}\n{r.stderr}")

        # lock_network runs the fail-closed verification suite internally
        # (deny-domain probes, verify_fetch_urls, cache_forbid_globs audit).
        cs.lock_network()
        print("lock_network + built-in quarantine verification: PASS")

        print("Positive probes (must stay reachable):")
        for url in ("https://api.anthropic.com", "https://aiplatform.googleapis.com"):
            if not _probe(container, url, expect_blocked=False):
                failures += 1

        print("Offline switches visible in container env:")
        want = {
            "EVOCLAW_CARGO_OFFLINE": "CARGO_NET_OFFLINE=true",
            "EVOCLAW_GO_OFFLINE": "GOPROXY=off",
            "EVOCLAW_MAVEN_OFFLINE": "MAVEN_ARGS=-o",
            "EVOCLAW_NPM_OFFLINE": "npm_config_offline=true",
            "EVOCLAW_PIP_WHEELHOUSE": "PIP_NO_INDEX=1",
        }
        for flag, expect in want.items():
            if not q_env.get(flag):
                continue
            r = subprocess.run(["docker", "exec", container, "env"],
                               capture_output=True, text=True)
            ok = any(line.startswith(expect.split("=")[0] + "=") and expect in line
                     for line in r.stdout.splitlines())
            print(f"  {'PASS' if ok else 'FAIL'}  {expect}")
            if not ok:
                failures += 1
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        failures += 1
    finally:
        if args.keep:
            print(f"Container kept: docker exec -it {container} bash")
        else:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)

    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Commit**

```bash
git add scripts/verify_quarantine.py
git commit -m "feat(quarantine): live smoke-test harness against real base images"
```

### Task 7: Live smoke verification (all 6 new policies + scikit non-regression)

**Files:** none (verification only). Each run takes ~1-3 min (iptables install + probes).

- [ ] **Step 1: ripgrep**

Run: `python scripts/verify_quarantine.py --repo ripgrep`
Expected: `lock_network + built-in quarantine verification: PASS`, both positive probes PASS, `CARGO_NET_OFFLINE=true` PASS, exit `ALL PASS`.

- [ ] **Step 2: nushell**

Run: `python scripts/verify_quarantine.py --repo nushell`
Expected: ALL PASS. (Image: `nushell_nushell_0.106.0_0.108.0/base:latest` — if no `base` tag exists for nushell, pass `--image nushell_nushell_0.106.0_0.108.0/milestone_core_development.1:latest`.)

- [ ] **Step 3: dubbo**

Run: `python scripts/verify_quarantine.py --repo dubbo`
Expected: ALL PASS — specifically the two `*-sources.jar` URLs BLOCKED (this is the /13-vs-/12 overlap fix working; before the fix they'd connect).

- [ ] **Step 4: go-zero**

Run: `python scripts/verify_quarantine.py --repo go-zero --keep`
Expected: ALL PASS. Then verify GOPROXY in a login shell (the .bashrc override path):
`docker exec --user fakeroot -e HOME=/home/fakeroot quarantine-verify-zeromicro_go-zero_v1.6.0_v1.9.3 bash -lc 'go env GOPROXY'` → `off`.
Then: `docker rm -f quarantine-verify-zeromicro_go-zero_v1.6.0_v1.9.3`.

- [ ] **Step 5: navidrome**

Run: `python scripts/verify_quarantine.py --repo navidrome`
Expected: ALL PASS.

- [ ] **Step 6: element-web**

Run: `python scripts/verify_quarantine.py --repo element-web`
Expected: ALL PASS.

- [ ] **Step 7: scikit-learn non-regression**

Run: `python scripts/verify_quarantine.py --repo scikit`
Expected: ALL PASS (wheelhouse env intact, pypi probes blocked — confirms the new fields didn't break the proven config).

- [ ] **Step 8: deeper offline-build spot check (ripgrep)**

```bash
docker run --rm -e CARGO_NET_OFFLINE=true burntsushi_ripgrep_14.1.1_15.0.0/base:latest \
  sh -c 'cd /testbed && timeout 300 cargo build -q 2>&1 | tail -5; echo "exit=$?"'
```
Expected: builds (or is already fresh) with `exit=0` — proves the pre-baked cache is a sufficient offline closure for the baseline build.

### Task 8: docs

**Files:**
- Modify: `docs/quarantine.md` (Status section + wiring: new fields, coverage gate, per-ecosystem offline switches)
- Modify: `docs/quarantine-rollout.md` (tick the per-repo checkboxes if present)

- [ ] **Step 1: Update `docs/quarantine.md`**

- In "How it's wired in EvoClaw", extend the yaml example with the new fields (`ecosystem`, `cargo_offline`/`go_offline`/`maven_offline`+`maven_repo_local`/`npm_offline`, `cache_forbid_globs`, `verify_fetch_urls`) and document the coverage gate + `--unprotected`.
- Replace the "Status" section: all 7 repos covered; per-ecosystem offline switch table marked implemented; note the GOPROXY=off mechanism, the Cloudflare /12-vs-/13 overlap fix, the residual `proxy.golang.org` SNI hole (Google ranges shared with Vertex) pending the SNI egress proxy.
- Update "Verification protocol" to mention `scripts/verify_quarantine.py` and the built-in `verify_fetch_urls`/`cache_forbid_globs` checks.

- [ ] **Step 2: Update `docs/quarantine-rollout.md`** — mark the 6 ecosystems' rollout items done / superseded by this implementation; leave the SNI-proxy long-term item open.

- [ ] **Step 3: Commit**

```bash
git add docs/quarantine.md docs/quarantine-rollout.md
git commit -m "docs(quarantine): all 7 ecosystems covered, coverage gate, verification harness"
```

### Task 9: Final review

- [ ] **Step 1: Full test suite**

Run: `python -m pytest harness/e2e/ -v`
Expected: all PASS

- [ ] **Step 2: Gate end-to-end check**

Run: `python scripts/run_all.py --config <the real trial config> 2>&1 | head -30` against the real data root — with all 7 yamls in place the gate must NOT trip; abort before any actual launch (Ctrl-C / use a nonexistent trial name) or run with `--repos none-matching` to stop early. Alternative safe check: temporarily `mv` one yaml aside, confirm `Refusing to launch`, move it back.

- [ ] **Step 3: `git status` clean except the pre-existing `harness/e2e/pricing.py` modification (NOT ours — leave uncommitted/untouched).**
