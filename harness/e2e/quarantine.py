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
    denied registry reachable. Invalid deny entries are ignored (the
    resolved-IP prune logs them the same way).
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
    """Fail closed if the offline quarantine wheelhouse contains an artifact
    whose distribution name matches a forbidden prefix (the repo-under-test's own
    package). Without this, an un-audited wheelhouse could silently serve the
    answer offline via PIP_FIND_LINKS, defeating the network deny. See
    docs/quarantine.md.
    """
    norm_forbid = [f.strip().lower().replace("_", "-") for f in forbid if f.strip()]
    if not norm_forbid:
        return
    offending = []
    for name in os.listdir(wheelhouse):
        low = name.lower().replace("_", "-")
        if not low.endswith((".whl", ".tar.gz", ".zip")):
            continue
        if any(low.startswith(pref + "-") for pref in norm_forbid):
            offending.append(name)
    if offending:
        print(
            f"Error: quarantine wheelhouse {wheelhouse} contains forbidden "
            f"artifact(s) {sorted(offending)} matching wheelhouse_forbid={forbid}. "
            f"Refusing to run — this would serve the repo's own target source "
            f"offline. Rebuild the wheelhouse with scripts/build_quarantine_wheelhouse.py.",
            file=sys.stderr,
        )
        sys.exit(1)


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
    """Per-repo anti-cheat ("quarantine") policy → container env vars.

    Quarantine prevents an agent from fetching the repo-under-test's own
    target-version source (the answer) over the network: it denies the registry
    serving that source and forces the package manager offline against a vetted
    closure (pip: host wheelhouse; cargo/go/maven/npm: the cache pre-baked into
    the eval image). The policy is **repo-intrinsic** (scikit denies PyPI,
    go-zero the Go proxy, …), so it lives once per repo in
    `quarantine_configs/<repo>.yaml`.

    Auto-on: presence of the file IS the switch (no trial-config flag). Returns
    {} (quarantine off) if the file is absent. Applied only to THIS repo's
    container — not globally to the whole trial. Fails closed (sys.exit) on a
    malformed policy or a wheelhouse that ships the repo's own package.
    See docs/quarantine.md.
    """
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
        # Expand ${EVOCLAW_WHEELHOUSE_DIR} etc. so the policy file carries no
        # host-specific absolute path (set the base once in .env_private).
        wh = str(Path(os.path.expandvars(str(wh))).expanduser().resolve())
        if not Path(wh).is_dir():
            print(f"Error: {conf_path}: pip_wheelhouse not found: {wh} "
                  f"(is EVOCLAW_WHEELHOUSE_DIR set in .env_private?)", file=sys.stderr)
            sys.exit(1)
        # Fail closed if the wheelhouse ships the repo's own package — an
        # un-audited wheelhouse must not be able to serve the answer offline.
        forbid = q.get("wheelhouse_forbid") or []
        if isinstance(forbid, str):
            forbid = [forbid]
        _assert_wheelhouse_excludes(wh, forbid)
        if not forbid:
            print(
                f"Warning: {conf_path}: pip_wheelhouse set without wheelhouse_forbid "
                f"— cannot assert the wheelhouse excludes the repo's own package "
                f"(see docs/quarantine.md).",
                file=sys.stderr,
            )
        else:
            env["EVOCLAW_WHEELHOUSE_FORBID"] = ",".join(forbid)
        env["EVOCLAW_PIP_WHEELHOUSE"] = wh

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
