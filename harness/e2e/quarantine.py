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
import sys
from pathlib import Path

import yaml

from harness.e2e.image_version import local_ref, resolve_image

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

# YAML key that forces each ecosystem's package manager offline. pip is NOT here:
# SWE_MILESTONE_PIP_OFFLINE is auto-derived from ecosystem=pip (no per-repo key). The
# gate requires the switch so a denied registry can't still be reached through
# the legitimate package-manager fetch path.
ECOSYSTEM_YAML_OFFLINE_KEY: dict[str, str] = {
    "cargo": "cargo_offline",
    "go": "go_offline",
    "maven": "maven_offline",
    "npm": "npm_offline",
}

# The ONLY domains a policy may list in firewall_exempt_domains: those that
# genuinely CANNOT be IP/CIDR-blocked because they ride Google's Vertex-shared
# ranges (blocking the range would cut the model path). Their defense is
# /etc/hosts poison + GOPROXY=off. This is a CODE-LEVEL whitelist (a fact, not a
# self-declaration): exempting anything else — e.g. a Fastly/Cloudflare registry
# that IS CIDR-blockable — would make the gate waive its deny_cidr requirement
# AND make verify skip its reachability probe, silently reopening the answer
# channel. So the gate rejects any exempt domain outside this set (F1).
FIREWALL_EXEMPTABLE_DOMAINS: frozenset[str] = frozenset({
    "proxy.golang.org",
    "sum.golang.org",
    "index.golang.org",
    "golang.org",
    "go.dev",
    "pkg.go.dev",
})

# Public module-proxy mirror domains. A Go module proxy mirrors ANY public repo
# with a v-prefixed semver tag (not just Go projects — element-web's v1.11.97 is
# reachable too) at proxy.golang.org/<host>/<owner>/<repo>/@v/<tag>.zip, so it is
# a cross-ecosystem answer-fetch channel. proxy/sum/index.golang.org ride Google
# IP ranges shared with Vertex aiplatform, so they can't be CIDR-denied without
# cutting the LLM path; the defense is domain-level /etc/hosts poisoning applied
# to EVERY quarantine container, plus GOPROXY=off. Poisoned ONLY under quarantine
# (container_setup._poison_domain_list) so non-quarantine/baseline containers keep
# working go module fetches (parity).
QUARANTINE_MIRROR_DOMAINS: list[str] = [
    "proxy.golang.org",
    "sum.golang.org",
    "index.golang.org",
    "goproxy.cn",
    "goproxy.io",
]


def goproxy_value(go_offline: bool, quarantine_active: bool) -> str:
    """GOPROXY to write into a container's shell profiles.

    'off' under go quarantine (the proxy itself is the answer channel:
    `go get <self>@<target>`) AND under any quarantine (the mirror domains are
    /etc/hosts-poisoned, so a bare proxy URL resolves to 0.0.0.0 and every fetch
    fails). Otherwise the sanctioned proxy, preserving the pre-quarantine
    baseline (a non-quarantine container must keep fetching go modules).
    """
    if go_offline or quarantine_active:
        return "off"
    return "https://proxy.golang.org,direct"


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


def image_for_repo(repo_name: str, project_root: Path) -> str:
    """Docker image tag for a repo's container.

    A quarantine repo (one with quarantine_configs/<repo>.yaml) runs under
    network lockdown + forced-offline package managers, so it must use the
    **offline-closure image** `swe-milestone/<repo_full>__base-offline` — the
    base image baked with the B-version dependency closure
    (scripts/build_offline_closure.py) so the locked-down container can still
    build A→B offline. Without the closure image, the agent would hit
    `No matching distribution` / `cargo offline` / `GOPROXY=off` errors on
    legitimate new deps (the plain base cache only has the A-version closure).
    Non-quarantine repos use the plain `__base` image as before.

    This applies to ALL quarantine ecosystems including pip: the closure builder
    bakes the pip wheelhouse directly into the base-offline image (instead of
    relying on a host-mounted wheelhouse), making the image fully self-contained
    and portable across machines.
    """
    q = load_quarantine_config(repo_name, project_root)
    milestone = "base-offline" if q is not None else "base"
    base = local_ref(repo_name, milestone)
    # resolve_image honors the SWE_MILESTONE_IMAGE_TAG pin (default in image_version.py) with a loud
    # :latest fallback — NOT a hardcoded :latest, which silently ignored the pin
    # so reproducibility runs graded against the wrong data version.
    return resolve_image(base)


def load_quarantine_env(repo_name: str, project_root: Path) -> dict:
    """Per-repo anti-cheat ("quarantine") policy → container env vars.

    Quarantine prevents an agent from fetching the repo-under-test's own
    target-version source (the answer) over the network: it denies the registry
    serving that source and forces the package manager offline against a vetted
    closure (all ecosystems: the dependency closure pre-baked into the
    base-offline:latest image via scripts/build_offline_closure.py). The policy
    is **repo-intrinsic** (scikit denies PyPI, go-zero the Go proxy, …), so it
    lives once per repo in `quarantine_configs/<repo>.yaml`.

    Auto-on: presence of the file IS the switch (no trial-config flag). Returns
    {} (quarantine off) if the file is absent. Applied only to THIS repo's
    container — not globally to the whole trial. Fails closed (sys.exit) on a
    malformed policy. See docs/quarantine.md.
    """
    q = load_quarantine_config(repo_name, project_root)
    if q is None:
        return {}

    env: dict[str, str] = {}
    # Quarantine is active for this repo (policy file present). Signal it so
    # container_setup poisons the mirror domains + forces GOPROXY off, and the
    # run_e2e fail-closed guard can tell an env-injected launch from a raw one.
    env["SWE_MILESTONE_QUARANTINE"] = "1"
    dd = q.get("deny_domains")
    dc = q.get("deny_cidrs")
    if dd:
        env["SWE_MILESTONE_DENY_DOMAINS"] = ",".join(dd) if isinstance(dd, list) else str(dd)
    if dc:
        env["SWE_MILESTONE_DENY_CIDRS"] = ",".join(dc) if isinstance(dc, list) else str(dc)
    # Domains the policy declares un-CIDR-blockable (share a Google/Vertex range);
    # verify_network_lockdown exempts ONLY these from the reachability assertion.
    fe = q.get("firewall_exempt_domains")
    if fe:
        env["SWE_MILESTONE_FIREWALL_EXEMPT"] = ",".join(fe) if isinstance(fe, list) else str(fe)

    # pip offline: the pip wheelhouse is now baked INTO base-offline:latest at
    # /wheelhouse (scripts/build_offline_closure.py). Signal the in-image path
    # via SWE_MILESTONE_PIP_OFFLINE; agents/base.py turns this into PIP_NO_INDEX=1 +
    # PIP_FIND_LINKS=/wheelhouse. No host path expansion, no is_dir() check —
    # self-exclusion is audited at BUILD TIME (audit_wheelhouse_self_exclusion
    # in the closure builder, Phase 2.1). Detect pip from the top-level ecosystem
    # field (list or str), consistent with how cargo/go/maven/npm are detected.
    ecosystems = [str(e).strip().lower() for e in _as_list(q.get("ecosystem"))]
    if "pip" in ecosystems:
        env["SWE_MILESTONE_PIP_OFFLINE"] = "1"

    # Package-manager offline switches (consumed by agents/base.py →
    # container -e flags; SWE_MILESTONE_GO_OFFLINE also flips the GOPROXY value
    # container_setup writes into the container). The firewall deny is the
    # hard layer; these keep legitimate dependency use working offline
    # against the image's pre-baked cache instead of hanging on a DROP.
    if q.get("cargo_offline"):
        env["SWE_MILESTONE_CARGO_OFFLINE"] = "1"
    if q.get("go_offline"):
        env["SWE_MILESTONE_GO_OFFLINE"] = "1"
    if q.get("maven_offline"):
        env["SWE_MILESTONE_MAVEN_OFFLINE"] = "1"
    if q.get("maven_repo_local"):
        env["SWE_MILESTONE_MAVEN_REPO_LOCAL"] = str(q["maven_repo_local"])
    if q.get("npm_offline"):
        env["SWE_MILESTONE_NPM_OFFLINE"] = "1"

    # Fail-closed audits run inside the container at lockdown time
    # (container_setup.verify_network_lockdown): cache globs that must match
    # nothing (image cache must not pre-bake the answer), and the exact
    # registry URLs of the observed cheats that must fail to connect.
    globs = _as_list(q.get("cache_forbid_globs"))
    if globs:
        env["SWE_MILESTONE_CACHE_FORBID_GLOBS"] = ",".join(str(g) for g in globs)
    urls = _as_list(q.get("verify_fetch_urls"))
    if urls:
        env["SWE_MILESTONE_VERIFY_FETCH_URLS"] = ",".join(str(u) for u in urls)
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
        deny_cidrs = _as_list(q.get("deny_cidrs"))
        exempt = {str(d).strip().lower() for d in _as_list(q.get("firewall_exempt_domains"))}
        # F1: firewall_exempt is a CIDR-deny + verify-probe waiver, so it must be
        # restricted to genuinely un-CIDR-blockable domains. A CIDR-blockable
        # registry listed here would pass the gate with no deny_cidr AND be
        # skipped by verify — a declaration-driven fail-open. Reject it up front.
        illegal_exempt = sorted(exempt - FIREWALL_EXEMPTABLE_DOMAINS)
        if illegal_exempt:
            errors.append(
                f"{name}: firewall_exempt_domains has CIDR-blockable domain(s) "
                f"{illegal_exempt} — only Google-shared un-blockable domains "
                f"{sorted(FIREWALL_EXEMPTABLE_DOMAINS)} may be exempt; deny the "
                f"rest with deny_cidrs"
            )
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
            # (a) deny_domains must name every registry (keeps them off the
            #     whitelist so their resolved IPs aren't ACCEPTed individually).
            missing = [r for r in regs if r not in deny]
            if missing:
                errors.append(
                    f"{name}: ecosystem '{eco}' registries not in deny_domains: {missing}"
                )
            # (b) the ecosystem's package manager must be forced offline, else a
            #     denied registry is still reachable via the legitimate fetch path.
            off_key = ECOSYSTEM_YAML_OFFLINE_KEY.get(eco)
            if off_key and not q.get(off_key):
                errors.append(
                    f"{name}: ecosystem '{eco}' has no '{off_key}: true' — the "
                    f"package manager would fetch online despite the deny"
                )
            # (c) each registry must be dropped at the IP layer. deny_domains
            #     alone doesn't: registries ride shared CDN ranges that stay
            #     ACCEPTed unless a deny_cidr overlaps them. Require deny_cidrs,
            #     UNLESS the registry is firewall_exempt (un-CIDR-able because it
            #     shares a Google/Vertex range; defended by /etc/hosts poison +
            #     the offline switch — the known proxy.golang.org residual).
            need_cidr = [r for r in regs if r.lower() not in exempt]
            if need_cidr and not deny_cidrs:
                errors.append(
                    f"{name}: ecosystem '{eco}' registries {need_cidr} reachable "
                    f"via CDN — add deny_cidrs (their CDN ranges) or list them in "
                    f"firewall_exempt_domains"
                )
    return errors


def quarantine_guard_error(
    repo_name: str,
    project_root: Path,
    quarantine_active: bool,
    unprotected: bool,
) -> str | None:
    """Fail-closed guard for direct entry points (e.g. run_e2e).

    The coverage gate + env injection live in scripts/run_all.py; a direct
    `run_e2e.py --repo-name X` launch bypasses them. Returns an error string when
    the repo HAS a quarantine policy but this launch didn't apply it
    (quarantine_active False — SWE_MILESTONE_QUARANTINE not injected) and --unprotected
    wasn't passed — exactly the 'silently ran unprotected' condition issue #12
    set out to make impossible. Returns None to proceed.
    """
    if quarantine_active or unprotected:
        return None
    if load_quarantine_config(repo_name, project_root) is None:
        return None
    return (
        f"{repo_name}: quarantine_configs/{repo_name}.yaml exists but this launch "
        f"has no quarantine env (SWE_MILESTONE_QUARANTINE unset). Launch via "
        f"scripts/run_all.py (applies the policy), or pass --unprotected to run "
        f"without protection (scores may be tainted)."
    )


def metadata_wants_unprotected(metadata) -> bool:
    """True if a resumed trial's saved metadata recorded it as --unprotected.

    run_e2e persists the flag alongside model/image so a resumed open baseline
    stays open — without it, ContainerSetup.__init__ would recover the policy and
    silently re-harden a trial that ran with the network open before the
    interruption (the resume corollary of F2-b).
    """
    return bool(metadata and metadata.get("unprotected"))
