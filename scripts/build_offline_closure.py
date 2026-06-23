"""Unified offline closure builder. Union all milestone images' deps into a
self-contained <repo>/base-offline:latest. See
docs/superpowers/specs/2026-06-23-offline-closure-builder-design.md."""
import argparse, glob as _glob, subprocess, sys, yaml
from pathlib import Path

def assert_no_self_packages(staging_dir: Path, forbid_globs: list[str]) -> None:
    offending = []
    for g in forbid_globs or []:
        offending += _glob.glob(str(staging_dir / g))
    if offending:
        print(f"Error: closure contains forbidden self@B artifact(s): "
              f"{sorted(offending)[:10]} — refusing to build (would leak the answer).",
              file=sys.stderr)
        sys.exit(1)

def discover_milestone_images(repo_lower: str, _docker_images: str | None = None) -> list[str]:
    if _docker_images is None:
        _docker_images = subprocess.run(
            ["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True, text=True, check=True).stdout
    prefix = f"{repo_lower}/"
    seen = {}
    for line in _docker_images.splitlines():
        line = line.strip()
        if not line.startswith(prefix):
            continue
        repo_tag = line[len(prefix):]
        name, _, tag = repo_tag.partition(":")
        if name in ("base", "base-offline"):
            continue
        # 去重:优先 latest
        if name not in seen or tag == "latest":
            seen[name] = f"{prefix}{name}:latest" if tag == "latest" else f"{prefix}{name}:{tag}"
    return [seen[name] for name in sorted(seen)]

def _resolve_config_path(repo_lower: str, project_root: Path) -> Path | None:
    """Locate quarantine_configs/<repo>.yaml, tolerating filename case.

    `--repo` is lowercased so it matches the docker image prefix
    (burntsushi_ripgrep_…), but the policy file is checked into the repo with the
    project's natural case (BurntSushi_ripgrep_….yaml). Try the exact name first,
    then fall back to a case-insensitive directory scan so the driver finds the
    file regardless of how the repo id was cased on the command line.
    """
    confs = Path(project_root) / "quarantine_configs"
    exact = confs / f"{repo_lower}.yaml"
    if exact.exists():
        return exact
    if not confs.is_dir():
        return None
    target = f"{repo_lower}.yaml".lower()
    for p in sorted(confs.glob("*.yaml")):
        if p.name.lower() == target:
            return p
    return None

def load_closure_config(repo_lower: str, project_root: Path) -> dict:
    conf = _resolve_config_path(repo_lower, project_root)
    if conf is None:
        miss = Path(project_root) / "quarantine_configs" / f"{repo_lower}.yaml"
        print(f"Error: no quarantine config {miss}", file=sys.stderr); sys.exit(1)
    data = yaml.safe_load(conf.read_text()) or {}
    closure = data.get("closure")
    if not closure or "cache_paths" not in closure or "offline_build" not in closure:
        print(f"Error: {conf}: closure block must have cache_paths and offline_build", file=sys.stderr); sys.exit(1)
    return closure

def load_quarantine_yaml(repo_lower: str, project_root: Path) -> dict:
    """Full quarantine_configs/<repo>.yaml as a dict (case-insensitive lookup).

    Used by the driver to read the top-level `ecosystem` for assembly dispatch.
    """
    conf = _resolve_config_path(repo_lower, project_root)
    if conf is None:
        miss = Path(project_root) / "quarantine_configs" / f"{repo_lower}.yaml"
        print(f"Error: no quarantine config {miss}", file=sys.stderr); sys.exit(1)
    return yaml.safe_load(conf.read_text()) or {}

def cargo_vendor_cmd(milestone_testbeds: list[str], vendor_dir: str) -> str:
    """Return a single `cargo vendor` command that syncs all milestone Cargo.toml workspaces
    into vendor_dir in one shot.

    IMPORTANT: must be ONE invocation with multiple --sync flags. Running `cargo vendor` in a
    loop to the same dir REPLACES the vendor dir on each call (drops prior crates) — empirically
    confirmed EXIT=101 on cargo build --offline after loop approach.

    Self-exclusion note: workspace path-deps (no `source` line in vendor metadata) are
    auto-excluded by `cargo vendor`. Downstream self-exclusion audits must inspect the
    `source` line in each vendored crate's .cargo-checksum.json/Cargo.toml, NOT name
    prefixes — `nu-ansi-term`, `num-traits`, etc. are legitimate third-party crates.
    """
    syncs = " ".join(f"--sync {t}" for t in milestone_testbeds)
    parts = ["cargo vendor --versioned-dirs"]
    if syncs:
        parts.append(syncs)
    parts.append(vendor_dir)
    return " ".join(parts)


def cargo_config_toml(vendor_dir: str) -> str:
    """Return the content to write to $CARGO_HOME/config.toml (i.e. /usr/local/cargo/config.toml).

    DESTINATION REQUIREMENT: this content MUST be written to $CARGO_HOME/config.toml
    (/usr/local/cargo/config.toml), NEVER to /testbed/.cargo/config.toml.
    The agent's `git clean -xfd` inside /testbed wipes .cargo/ → the offline redirect
    silently disappears and cargo falls back to the network, defeating the closure.
    The driver (a later task) is responsible for placing the file at the correct path;
    this function only produces the content string.
    """
    return ('[source.crates-io]\n'
            'replace-with = "vendored-sources"\n'
            '[source.vendored-sources]\n'
            f'directory = "{vendor_dir}"\n')


def assemble_cargo_dockerfile(repo_lower: str, milestones: list[str],
                              toolchain: dict | None = None,
                              extra_vendor_crates: list[str] | None = None) -> str:
    """Multi-stage Dockerfile that vendors the UNION of every milestone's Cargo
    workspace into /opt/vendor, then bakes a $CARGO_HOME offline redirect.

    `cargo vendor` needs a cwd with a workspace manifest, so the FIRST milestone
    testbed (/tb/m0) is the cwd and the REST (m1..mN) are passed as `--sync`
    targets — one invocation (a loop would clobber the vendor dir each call). The
    base image's OWN /testbed/Cargo.toml (the A-baseline this stage is FROM) is also
    --sync'd, so the vendor spans A→B: with `--versioned-dirs` the result holds BOTH
    the A and B version of every crate (e.g. bstr-1.10.0 AND bstr-1.12.0). This is
    REQUIRED because the agent starts from the A-baseline /testbed and the
    $CARGO_HOME redirect points ALL crates.io at /opt/vendor — a B-only vendor would
    fail the agent's first `cargo build` (`failed to select a version`).
    Path-deps (the repo's own workspace crates) carry no `source` line and are
    auto-excluded by cargo vendor, so the answer is never baked in.

    The config is written to $CARGO_HOME (/usr/local/cargo/config.toml), NEVER to
    /testbed/.cargo — the agent's `git clean -xfd` would wipe the latter and the
    offline redirect would silently vanish.

    Optional `toolchain={"rust": "1.88.0", ...}`: when a rust version is given, the
    FINAL stage installs it ONLINE at build time
    (`rustup toolchain install <v> --profile minimal && rustup default <v>`).
    nushell's milestones pin `channel="1.88.0"` in rust-toolchain.toml, but the
    base/milestone images only ship 1.86 — the 1.88 toolchain CANNOT be COPY'd from
    a milestone (none carries it) and must be fetched from static.rust-lang.org
    during the (online) build. rustup's source is NOT an answer registry, so this
    is safe and necessary. `rustup default` makes cargo use 1.88 for any path
    outside /testbed too (rust-toolchain.toml only overrides inside the workspace).
    `--profile minimal` is sufficient: `cargo build` needs only rustc + rust-std +
    cargo, all of which minimal provides, so the milestone's `profile="default"`
    directive triggers no extra (offline-failing) component download at gate time.
    Repos with no toolchain key (ripgrep) emit no rust-install line — unchanged.

    Optional `extra_vendor_crates`: a list of TOML dependency spec strings (e.g.
    ["arbitrary = \\"=1.4.2\\"", "derive_arbitrary = \\"=1.4.2\\""]).  When present, a
    synthetic workspace `/tmp/extra_vendor/Cargo.toml` is created BEFORE the
    `cargo vendor` step and included as an extra `--sync` target.  This pulls the
    pinned crates into /opt/vendor even if no milestone testbed declares them yet
    (use case: an SRS task asks the agent to ADD a new dependency, so the current
    testbed src doesn't reference it, but the golden Cargo.lock already pins the
    version the agent will need at eval time).  The synthetic workspace is a
    plain package (not a workspace root) so it gets its own fresh Cargo.lock
    resolved from the live registry — the exact version spec in each entry (e.g.
    `= "=1.4.2"`) ensures cargo resolves to exactly that pinned version.
    """
    if not milestones:
        print("Error: assemble_cargo_dockerfile got no milestones", file=sys.stderr)
        sys.exit(1)
    vendor_dir = "/opt/vendor"
    cargo_home_cfg = "/usr/local/cargo/config.toml"
    lines = [
        "# syntax=docker/dockerfile:1",
        f"FROM {repo_lower}/base:latest AS vendor_builder",
    ]
    for i, m in enumerate(milestones):
        lines.append(f"COPY --from={m} /testbed /tb/m{i}")
    # cwd = first milestone's workspace; --sync the manifests of the rest, PLUS the
    # base image's OWN /testbed/Cargo.toml — the A-baseline.
    #
    # CLOSURE GAP (glm-5.2 validation): an agent doing the A→B task STARTS from the
    # A-baseline (`<repo>/base:latest`'s /testbed, whose Cargo.lock pins the OLD
    # versions, e.g. bstr 1.10.0). The milestone testbeds pin only B-versions
    # (bstr 1.12.0), so a vendor of milestones alone leaves /opt/vendor without the
    # A-version crates; the $CARGO_HOME redirect routes ALL crates.io to /opt/vendor,
    # and the agent's first `cargo build` fails `failed to select a version for the
    # requirement bstr = ...`. The vendor must span A→B. This stage is
    # `FROM <repo>/base:latest`, so /testbed IS the A-baseline (already present, no
    # COPY needed); `cargo vendor --versioned-dirs` keeps multiple versions of a
    # crate in separate <name>-<ver>/ dirs, so the result contains BOTH the A and B
    # versions. cwd stays /tb/m0 (a real workspace); /testbed/Cargo.toml is one more
    # --sync. (go/pip ADD milestone caches on top of the base cache so they keep the
    # A-versions; only cargo REPLACES via the vendor redirect, hence this fix.)
    sync_manifests = [f"/tb/m{i}/Cargo.toml" for i in range(1, len(milestones))]
    sync_manifests.append("/testbed/Cargo.toml")
    # extra_vendor_crates: create a synthetic workspace that pins the requested
    # crates so they land in /opt/vendor even if the current testbed src doesn't
    # declare them yet.  Resolves ONLINE (network is available at build time), so
    # the exact `= "=X.Y.Z"` version spec guarantees the pinned version is fetched.
    extra_vendor_manifest = "/tmp/extra_vendor/Cargo.toml"
    if extra_vendor_crates:
        dep_lines = "\n".join(spec for spec in extra_vendor_crates)
        raw_toml = (
            '[package]\n'
            'name = "extra-vendor-seed"\n'
            'version = "0.0.0"\n'
            'edition = "2021"\n'
            '\n'
            '[lib]\n'
            'name = "extra_vendor_seed"\n'
            '\n'
            '[dependencies]\n'
            + dep_lines + '\n'
        )
        # Encode for printf (same technique as cargo_config_toml above):
        # escape backslashes, single-quotes, and encode real newlines as \n.
        fmt_toml = (raw_toml
                    .replace("\\", "\\\\")
                    .replace("'", "'\\''")
                    .replace("\n", "\\n"))
        lines.append(
            f"RUN mkdir -p /tmp/extra_vendor/src && "
            f"touch /tmp/extra_vendor/src/lib.rs && "
            f"printf '{fmt_toml}' > {extra_vendor_manifest}"
        )
        sync_manifests.append(extra_vendor_manifest)
    vendor = cargo_vendor_cmd(sync_manifests, vendor_dir)
    lines.append(f"RUN cd /tb/m0 && {vendor}")
    lines.append(f"FROM {repo_lower}/base:latest AS final")
    lines.append(f"COPY --from=vendor_builder {vendor_dir} {vendor_dir}")
    config = cargo_config_toml(vendor_dir)
    # Emit the config via a SINGLE-physical-line RUN: literal newlines in the
    # config would otherwise be read by the Dockerfile parser as new instructions
    # ("unknown instruction: replace-with"). Encode them as \n and let printf's
    # format string expand them at build time.
    fmt = config.replace("\\", "\\\\").replace("'", "'\\''").replace("\n", "\\n")
    lines.append(
        f"RUN mkdir -p \"$CARGO_HOME\" && "
        f"printf '{fmt}' > {cargo_home_cfg}"
    )
    # Optional ONLINE toolchain install in the final stage. Some repos (nushell)
    # pin a rust channel (1.88.0) that the base/milestone images don't carry, so
    # it must be fetched at build time (network is available then). `rustup
    # default` ensures cargo uses it outside /testbed too. Emitted ONLY when a
    # rust version is configured — ripgrep (no toolchain key) stays unchanged.
    rust_ver = (toolchain or {}).get("rust")
    if rust_ver:
        lines.append(
            f"RUN rustup toolchain install {rust_ver} --profile minimal && "
            f"rustup default {rust_ver}"
        )
    return "\n".join(lines) + "\n"


def _probe_rust_channel(image: str) -> str:
    """`docker run --rm <image> cat /testbed/rust-toolchain.toml` → the pinned
    `channel = "X.Y.Z"` value (bare, e.g. "1.87.0"); "" if absent/unparseable.

    Pure read-only — never mutates the image. Split out so the channel survey can be
    unit-tested with a fake probe (mirrors `_probe_go_version`).
    """
    r = subprocess.run(
        ["docker", "run", "--rm", image, "cat", "/testbed/rust-toolchain.toml"],
        capture_output=True, text=True)
    if r.returncode != 0:
        return ""
    for line in (r.stdout or "").splitlines():
        s = line.strip()
        # The toolchain file's comments mention "1.72.0" etc.; only the real
        # `channel = "..."` assignment counts (leading `channel`, not `# ...`).
        if s.startswith("channel") and "=" in s:
            val = s.split("=", 1)[1].strip().strip('"').strip("'")
            if val:
                return val
    return ""


def cargo_pinned_channels(repo_lower: str, milestones: list[str],
                          configured: str | None = None,
                          _probe=_probe_rust_channel) -> list[str]:
    """The set of rust channels the closure must carry: every distinct
    `rust-toolchain.toml` channel pinned by the A-baseline (`<repo>/base:latest`)
    and each milestone, UNIONed with the configured `closure.toolchain.rust`.

    nushell's milestones are HETEROGENEOUS on the rust channel — A-baseline + most
    pin 1.86.0, a few pin 1.87.0, a few pin 1.88.0. The base image ships only 1.86.0
    as the active toolchain; if a milestone pins 1.87/1.88 and that channel is NOT
    installed, entering its /testbed makes rustup try to `sync channel updates for
    <ver>` from static.rust-lang.org → fails under `--network none` (the offline
    gate dies with a rustup dns error, NOT a cargo closure gap). So EVERY pinned
    channel must be installed (online) at build time. Returned sorted ascending so
    the highest is last (a stable, deterministic default pick).
    """
    chans = set()
    base = f"{repo_lower}/base:latest"
    for img in [base, *milestones]:
        ch = _probe(img)
        if ch:
            chans.add(ch)
    if configured:
        chans.add(str(configured))
    # Sort by (major, minor, patch) numeric tuple when possible; fall back to str.
    def _key(v):
        try:
            return (0, tuple(int(p) for p in v.split(".")))
        except ValueError:
            return (1, v)
    return sorted(chans, key=_key)


def assemble_cargo_rawcache_dockerfile(repo_lower: str, milestones: list[str],
                                       toolchain: dict | None = None,
                                       _probe=_probe_rust_channel) -> str:
    """Cargo closure via the RAW-CACHE + ONLINE-WARM pattern (NOT `cargo vendor`).

    Why not vendor: nushell pins `reedline` from BOTH crates.io AND a git branch,
    and one milestone (g04_1ddae02) carries the git checkout published as the SAME
    `version = 0.41.0` as the registry crate. `cargo vendor` lays every crate into a
    single `<name>[-<ver>]/` dir keyed by name (+version) and CANNOT hold two
    same-name+version crates from different sources — it aborts the union with
    `found duplicate version of package reedline v0.41.0 vendored from two sources`
    (confirmed empirically WITH and WITHOUT `--versioned-dirs`). The raw cargo cache
    has no such conflict: registry crates live under `registry/{cache,index,src}/`
    keyed by source-hash, and git deps live in a SEPARATE tree `git/{db,checkouts}/`
    keyed by repo-hash — so registry-0.41.0 and git-0.41.0 coexist by construction.

    Mechanism (mirrors go/maven/npm's "online-warm the union", adapted for cargo):
      fetch_builder  FROM <repo>/base:latest
        - The base image's `$CARGO_HOME` (/usr/local/cargo) is ALREADY warm with the
          A-baseline's registry crates, and its /testbed IS the A-baseline workspace
          (Cargo.lock pins the OLD versions). So the union STARTS from A — no extra
          COPY needed for the A side (mirrors go/maven/npm ADDing milestone caches on
          top of the base cache; only the old `cargo vendor` REPLACED via a redirect).
        - Install EVERY pinned rust toolchain ONLINE first (the milestones are
          heterogeneous: 1.86/1.87/1.88; `cargo fetch` inside a workspace honours its
          rust-toolchain.toml, so each channel must exist or rustup would try to
          auto-install mid-fetch).
        - COPY each milestone's FULL /testbed (cargo fetch reads the whole workspace
          + its Cargo.lock; the git revs are pinned in the lock) and run plain
          `cargo fetch` (NO --locked) per milestone INTO the shared `$CARGO_HOME`.
          Each fetch ADDs that milestone's registry crates AND any git checkouts
          (e.g. reedline @ branch=main) to the cache. `cargo fetch` for the base's
          OWN /testbed warms the A side too. --locked is NOT used: nushell's milestone
          locks are mid-migration checkpoints that don't match Cargo.toml (8/13 fail
          `cargo fetch --locked`), so the online fetch re-resolves+warms a superset;
          the offline gate's plain `cargo build --offline` re-resolves identically.
      final          FROM <repo>/base:latest
        - COPY the warmed `$CARGO_HOME` forward (registry cache/index/src + git
          db/checkouts). NO `$CARGO_HOME/config.toml` redirect — unlike vendor this
          path keeps the real registry index, so `cargo build --offline` resolves
          from the cache natively (the hand-built reference image proves a redirect
          is neither present nor needed here).
        - Install the rust toolchain ONLINE + `rustup default <ver>` so cargo uses it
          for any path outside /testbed too (rust-toolchain.toml only overrides
          inside the workspace; the B milestones pin 1.88, the A-baseline pins 1.86 —
          both channels end up present: 1.86 ships in the base, 1.88 is installed).

    Self-exclusion: the repo's OWN nu-* workspace crates are PATH deps (under
    /testbed/crates/*) — `cargo fetch` never downloads them into registry/cache or
    git/, so the cache cannot leak nu-*-0.10[6-9] / nushell-* @ B (verified: only
    legitimate third-party `nu-ansi-term` etc. appear). The generic forbid-glob audit
    stays as defense-in-depth.

    `static.rust-lang.org` (rustup's source) and crates.io/the git host are reachable
    at build time but the closure is SEALED offline at eval; warming from them is
    safe (they are not the answer — the answer is the repo's own B source, which is a
    path dep and never fetched).
    """
    if not milestones:
        print("Error: assemble_cargo_rawcache_dockerfile got no milestones",
              file=sys.stderr)
        sys.exit(1)
    cargo_home = "/usr/local/cargo"
    rust_ver = (toolchain or {}).get("rust")
    # Survey EVERY rust channel the A-baseline + milestones pin (heterogeneous:
    # 1.86/1.87/1.88), unioned with the configured one. Each must be installed or the
    # offline gate dies on a rustup channel-sync (see cargo_pinned_channels). Install
    # with `--profile default` because the workspaces pin `profile = "default"` and
    # the agent (and some gates) may invoke clippy/rustfmt — the minimal profile would
    # leave those absent and a `profile=default` workspace could trigger an offline
    # component sync. The base ships 1.86.0 (minimal) as the active default; we
    # install the rest (and re-assert the configured default). `rustup default` makes
    # cargo use it for any path OUTSIDE a workspace (a workspace's rust-toolchain.toml
    # still overrides inside /testbed).
    channels = cargo_pinned_channels(repo_lower, milestones, rust_ver, _probe=_probe)
    installs = [f"rustup toolchain install {c} --profile default" for c in channels]
    default_ver = rust_ver or (channels[-1] if channels else None)
    if default_ver:
        installs.append(f"rustup default {default_ver}")
    toolchain_run = (" && ".join(installs)) if installs else None

    lines = [
        "# syntax=docker/dockerfile:1",
        f"FROM {repo_lower}/base:latest AS fetch_builder",
    ]
    # Install ALL pinned toolchains BEFORE any fetch: `cargo fetch` inside a workspace
    # honours its rust-toolchain.toml, so the pinned channel must exist or rustup
    # would try to auto-install it mid-build. Network is available here; rustup's
    # source (static.rust-lang.org) is not an answer registry.
    if toolchain_run:
        lines.append(f"RUN {toolchain_run}")
    for i, m in enumerate(milestones):
        lines.append(f"COPY --from={m} /testbed /tb/m{i}")
    # Warm the shared $CARGO_HOME from the A-baseline (the base's own /testbed) and
    # every milestone's B-source. ONE `cargo fetch` per workspace; each ADDs its
    # resolved deps (registry crates AND git checkouts) to /usr/local/cargo. The base
    # /testbed (A) is fetched first so the A side is explicitly warmed; the pre-baked
    # A cache already covers it, but this makes the A→B span unconditional.
    #
    # NO `--locked`: nushell's milestone Cargo.lock files are mid-migration START
    # checkpoints whose lock does NOT match Cargo.toml even under their OWN pinned
    # rustc (empirically `cargo fetch --locked` fails 8/13 with "the lock file needs
    # to be updated but --locked was passed" — and the milestone IMAGES themselves
    # were built without --locked). The online fetch therefore re-resolves+updates the
    # lock in this throwaway builder and downloads the resulting (superset) closure;
    # the offline GATE later runs the config's plain `cargo build --offline` (also no
    # --locked) which re-resolves from this now-rich cache identically. `||` per
    # workspace is NOT used: a fetch failure must surface (fail the build), not be
    # swallowed — every workspace's deps must enter the cache.
    fetch_dirs = ["/testbed"] + [f"/tb/m{i}" for i in range(len(milestones))]
    fetches = " && ".join(f"cd {d} && cargo fetch" for d in fetch_dirs)
    lines.append(f"RUN {fetches}")

    lines.append(f"FROM {repo_lower}/base:latest AS final")
    # COPY the WHOLE warmed $CARGO_HOME (registry cache/index/src + git db/checkouts)
    # forward. Unlike vendor there is no config.toml redirect — the real registry
    # index is preserved, so `cargo build --offline` resolves from the cache.
    lines.append(f"COPY --from=fetch_builder {cargo_home} {cargo_home}")
    # ONLINE toolchain install in the final stage (the published image): install every
    # pinned channel + set the default, so the agent's A-baseline start (1.86) and any
    # milestone-pinned channel (1.87/1.88) all resolve offline.
    if toolchain_run:
        lines.append(f"RUN {toolchain_run}")
    return "\n".join(lines) + "\n"


def audit_staging_image(staging_tag: str, forbid_globs: list[str]) -> None:
    """In-image self-exclusion AUDIT: run the cache_forbid_globs INSIDE the
    staging image and fail-closed if any glob matches (self@B leaked).

    The closure cache lives IN the image (vendored/copied), not on the host, so
    the host-dir `assert_no_self_packages` (Task 2.1) is NOT the mechanism here.
    We `ls -d` the globs inside the container; a non-empty stdout means at least
    one forbidden self@B artifact made it into the closure → refuse (sys.exit 1).

    cargo/ripgrep note: with `cargo vendor` the registry/cache the globs target is
    never populated and workspace self-crates are auto-excluded by construction
    (proven in 4.2). Running the globs anyway is defense-in-depth, so an empty
    forbid_globs list (or all-clean match) is the normal, passing case.
    """
    globs = list(forbid_globs or [])
    if not globs:
        return
    # `ls -d <glob1> <glob2> ... 2>/dev/null; true` — unmatched globs print
    # nothing (errors suppressed), matched paths go to stdout; `true` keeps the
    # shell exit 0 so a non-match is not mistaken for a docker failure.
    quoted = " ".join(f"'{g}'" for g in globs)
    cmd = ["docker", "run", "--rm", staging_tag, "sh", "-c",
           f"ls -d {quoted} 2>/dev/null; true"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"Error: audit docker run failed (exit {r.returncode}):\n"
              f"{(r.stderr or r.stdout).strip()}", file=sys.stderr)
        sys.exit(1)
    matched = (r.stdout or "").strip()
    if matched:
        print(f"Error: offline closure AUDIT failed for {staging_tag}: cache "
              f"contains forbidden self@B artifact(s) — refusing (would leak the "
              f"answer):\n{matched}", file=sys.stderr)
        sys.exit(1)


# Module/package tokens that a Go build emits when something it imports is not
# resolvable from go.mod. Each capture group is the offending module *or* import
# path. We pull these out of the build error and then PROBE the closure's own
# module cache for the path — present-in-cache ⇒ the source's go.mod simply
# doesn't declare a module the bytes for which we already have (a SOURCE-STATE
# problem, e.g. a mid-migration START checkpoint), absent ⇒ a real CLOSURE GAP.
import re as _re
_GO_MISSING_TOKEN_RES = [
    _re.compile(r"no required module provides package ([^\s;]+)"),
    _re.compile(r"finding module for package ([^\s;]+)"),
    _re.compile(r"cannot find module providing package ([^\s;]+)"),
    # More-specific pattern first: "missing go.sum entry for module providing package <pkg>"
    _re.compile(r"missing go\.sum entry for module providing package ([^\s;]+)"),
    # Fallback: "missing go.sum entry for module <module>" (plain module path, not a package)
    _re.compile(r"missing go\.sum entry for module ([^\s;]+)"),
    _re.compile(r"^\s*([\w.\-/]+@[^\s:]+): .*(?:GOPROXY|network is unreachable|"
                r"module lookup disabled)", _re.MULTILINE),
]


def _go_cache_has_path(staging_tag: str, import_or_module: str) -> bool:
    """True iff the closure's module cache already holds bytes for `import_or_module`.

    `go build` reports a missing *package* import path (e.g.
    `github.com/go-redis/redis/v8` or `.../redis/v8/internal`) or a
    `module@version`. The download cache is keyed by *module* path, so we strip any
    `@version` and walk prefixes of the slash-path (longest first) looking for a
    populated `…/cache/download/<prefix>/@v` dir. A hit means the bytes are present
    and the build failure is a go.mod/source-state issue, not a closure gap.

    Classifier semantics (SOUND + SAFE, fail-closed):
    - cited missing module has <path>/@v in cache → source_state (the build failure
      is a go.mod/source mismatch or compile error, not a missing dependency).
    - cited module's @v ABSENT from cache → closure_gap (conservative: fail-closed;
      never false-pass).
    - unknown/no-token error → closure_gap (fail-closed), handled by the caller.

    The `@v` sub-dir check (not just the parent org-dir) is critical for soundness:
    the module cache stores a downloaded module at
    /go/pkg/mod/cache/download/<module-path>/@v/; testing only the parent dir
    (e.g. github.com/go-redis/) would HIT on an org dir even when the specific
    module version was never fetched.
    """
    path = import_or_module.split("@", 1)[0].strip().strip("/")
    if not path:
        return False
    parts = path.split("/")
    # Probe longest→shortest prefix: the module path is a prefix of the import path.
    # Each candidate must have an @v sub-dir to be a valid cache hit.
    cands = ["/go/pkg/mod/cache/download/" + "/".join(parts[:n])
             for n in range(len(parts), 0, -1)]
    quoted = " ".join(f"'{c}'" for c in cands)
    r = subprocess.run(
        ["docker", "run", "--rm", staging_tag, "sh", "-c",
         f"for d in {quoted}; do [ -d \"$d/@v\" ] && {{ echo HIT; break; }}; done; true"],
        capture_output=True, text=True)
    return "HIT" in (r.stdout or "")


def classify_offline_build_failure(staging_tag: str, output: str) -> tuple[str, str]:
    """Classify an offline-build failure as a CLOSURE GAP or a SOURCE-STATE error.

    Per the closure-gate contract, only a *missing dependency* (the closure cannot
    supply bytes the build needs) is a closure failure. A build that fails because
    the milestone's own source/go.mod is internally inconsistent (imports a module
    its go.mod doesn't require, a START-state mid-migration checkpoint, a type
    error, etc.) is NOT a closure problem — the bytes are there; the source isn't a
    clean buildable state.

    Heuristic (empirically tuned for go-zero): extract every module/import token go
    names as unresolved, then PROBE the closure's cache for each. If we found
    tokens and the cache holds ALL of them ⇒ "source_state". If any token's bytes
    are absent ⇒ "closure_gap". If we could not extract a recognised missing-module
    token at all (pure compile error: undefined symbol, redeclared, type mismatch)
    ⇒ "source_state" (a compile error is not a missing dependency). Returns
    (kind, detail) where detail summarises the tokens / probe result.
    """
    tokens = []
    for rx in _GO_MISSING_TOKEN_RES:
        for m in rx.finditer(output or ""):
            tok = m.group(1).strip()
            if tok and tok not in tokens:
                tokens.append(tok)
    if not tokens:
        # No missing-module token → not a dependency problem (compile/type error).
        return ("source_state", "no missing-module token (compile/type error)")
    absent = [t for t in tokens if not _go_cache_has_path(staging_tag, t)]
    if absent:
        return ("closure_gap", f"missing from closure cache: {absent[:8]}")
    return ("source_state",
            f"all {len(tokens)} unresolved module(s) ARE in cache "
            f"(go.mod/source inconsistency, not a gap): {tokens[:8]}")


# Maven offline-resolution failure signatures. When `mvn -o` cannot supply a
# dependency it prints, per artifact:
#   Cannot access <repo> (<url>) in offline mode and the artifact
#   <group>:<artifact>:<type>[:<classifier>]:<version> has not been downloaded ...
# and/or a summary `Could not (resolve dependencies for|find artifact) ... <coords>`.
# We pull the GAV(+classifier) coordinate out of each and then decide self@B vs gap.
import fnmatch as _fnmatch
_MVN_UNRESOLVED_COORD_RES = [
    # The authoritative per-artifact line under `Could not resolve dependencies`.
    _re.compile(r"the artifact\s+([\w.\-]+:[\w.\-]+:[\w.\-]+(?::[\w.\-]+)?:[\w.\-]+)"
                r"\s+has not been downloaded", _re.IGNORECASE),
    # `Could not find artifact <coords> in <repo>` (offline or cached-miss variant).
    _re.compile(r"Could not find artifact\s+([\w.\-]+:[\w.\-]+:[\w.\-]+(?::[\w.\-]+)?:[\w.\-]+)"),
    # The `dependency: <g>:<a>:<type>:<ver> (scope?)` line maven prints in the 3.9+
    # resolution-failure summary.
    _re.compile(r"^\s*(?:\[ERROR\]\s*)?dependency:\s+"
                r"([\w.\-]+:[\w.\-]+:[\w.\-]+(?::[\w.\-]+)?:[\w.\-]+)", _re.MULTILINE),
]


def _forbid_glob_to_group_version(glob: str) -> tuple[str, str] | None:
    """Map a cache_forbid_glob path to a (group-glob, version-glob) coordinate
    matcher.

    The maven forbid globs are filesystem paths under the local repo, e.g.
    `/root/.m2/repository/org/apache/dubbo/*/3.3.[4-9]*`. The layout is
    `<repo>/<group-as-dirs>/<artifact>/<version>/...`, so the path tail after
    `repository/` is `<group/dirs>/<artifactId-glob>/<version-glob>`. We turn the
    group dirs into a dotted group glob (`org.apache.dubbo`) and keep the trailing
    segment as the version glob (`3.3.[4-9]*`). Returns None if the glob doesn't
    contain the `.m2` `repository/` anchor (can't be mapped to a coordinate).
    """
    marker = "/repository/"
    i = glob.find(marker)
    if i < 0:
        return None
    rel = glob[i + len(marker):].strip("/")
    parts = rel.split("/")
    if len(parts) < 3:
        return None
    version_glob = parts[-1]
    # parts[:-2] are the group dirs; parts[-2] is the artifactId glob (unused for the
    # self@B decision — group + version uniquely identify the repo's own artifacts).
    group_glob = ".".join(parts[:-2])
    return (group_glob, version_glob)


def _maven_coord_is_self_at_b(coord: str, self_at_b_globs: list[str]) -> bool:
    """True iff a maven coordinate `<group>:<artifact>:<type>[:<classifier>]:<ver>`
    is one of the repo's OWN target-version (self@B) artifacts — i.e. its group and
    version match one of the cache_forbid_globs (the SAME patterns that drove the
    self@B rm). Such an artifact was deliberately removed from the closure; an
    offline build that can't resolve it is EXPECTED (at eval the agent builds the
    sibling reactor module from source), so it is source-state, not a closure gap.

    Matching is on group+version via fnmatch so the glob's character class
    (`3.3.[4-9]*`) is honoured; the artifactId segment is not constrained (the glob
    uses `*` there). A coordinate whose group/version matches NO forbid pattern is a
    third-party (or A-baseline) dependency — a real gap if unresolved.
    """
    bits = coord.split(":")
    if len(bits) < 4:
        return False
    group = bits[0]
    version = bits[-1]
    for g in self_at_b_globs or []:
        gv = _forbid_glob_to_group_version(g)
        if gv is None:
            continue
        group_glob, version_glob = gv
        if _fnmatch.fnmatch(group, group_glob) and _fnmatch.fnmatch(version, version_glob):
            return True
    return False


def classify_maven_offline_build_failure(self_at_b_globs: list[str]):
    """Return a maven-aware offline-build failure classifier
    `(staging_tag, output) -> (kind, detail)` for use with `run_offline_gate`.

    The go classifier doesn't recognise maven's offline-resolution strings, so a
    maven `Cannot access ... in offline mode` would fall through to its
    "no missing-module token" → source_state branch (fail-OPEN, masking a real gap).
    This classifier instead extracts every unresolved maven coordinate and splits
    them by self@B:
      - If ANY unresolved artifact is a THIRD-PARTY dep (not matching the
        cache_forbid_globs) → "closure_gap": the closure is genuinely missing a
        needed artifact → BLOCK.
      - If unresolved artifacts exist but they are ALL self@B (the repo's own
        target-version sibling modules we removed on purpose, e.g.
        org.apache.dubbo:*:3.3.6-SNAPSHOT) → "source_state": expected (the agent
        builds these from the reactor at eval time); the closure is not at fault.
      - If NO unresolved-artifact coordinate is found at all (a spotless/checkstyle/
        rat lint failure, or a pure java compile error) → "source_state": a lint or
        compile failure is not a missing dependency.

    `staging_tag` is accepted for signature-compatibility with the go classifier but
    unused — the maven decision is pure-text (the self@B set is known from config),
    no in-image cache probe is needed.
    """
    def _classify(staging_tag: str, output: str) -> tuple[str, str]:
        coords = []
        for rx in _MVN_UNRESOLVED_COORD_RES:
            for m in rx.finditer(output or ""):
                c = m.group(1).strip()
                if c and c not in coords:
                    coords.append(c)
        if not coords:
            # No unresolved-artifact line → lint (spotless/checkstyle/rat) or a pure
            # compile error. Neither is a missing dependency.
            return ("source_state",
                    "no unresolved-artifact coordinate (lint/spotless or compile error)")
        third_party = [c for c in coords
                       if not _maven_coord_is_self_at_b(c, self_at_b_globs)]
        if third_party:
            return ("closure_gap",
                    f"unresolved THIRD-PARTY artifact(s) missing from closure: "
                    f"{third_party[:8]}")
        return ("source_state",
                f"unresolved artifact(s) are ALL self@B (own reactor modules, "
                f"removed by design; agent builds them from source): {coords[:8]}")
    return _classify


# Yarn(-classic) offline-resolution failure signatures. Under
# `yarn install --offline`, a package whose bytes are NOT in the cache surfaces as
# one of these, OR as a network attempt (yarn falling back to the registry despite
# --offline — itself proof the package is missing from the cache). Each is a real
# CLOSURE GAP. Matched case-insensitively against the combined stdout+stderr.
_NPM_CLOSURE_GAP_SIGS = [
    _re.compile(r"error Couldn't find any versions for", _re.IGNORECASE),
    _re.compile(r"Couldn't find package", _re.IGNORECASE),
    _re.compile(r"No matching version found", _re.IGNORECASE),
    # yarn's offline-cache-miss ENOENT (the tarball isn't in the offline mirror).
    _re.compile(r"error An unexpected error occurred:.*ENOENT.*\.yarn-cache",
                _re.IGNORECASE | _re.DOTALL),
    # THE canonical yarn-classic offline cache-miss: yarn needs a tarball/version it
    # cannot serve from the mirror and would have to fetch, so under --offline it
    # aborts `error Can't make a request in offline mode ("<registry-url>")`. This is
    # the most common gap signal (e.g. caniuse-lite-1.0.x missing from the union) —
    # a network attempt blocked by --offline = the bytes weren't in the cache.
    _re.compile(r"Can't make a request in offline mode", _re.IGNORECASE),
    # Defensive: any other "<verb> in offline mode" abort that names a registry/http
    # URL is yarn refusing a network fetch it needed → the package wasn't cached.
    _re.compile(r"in offline mode.*https?://", _re.IGNORECASE | _re.DOTALL),
    # A network attempt under --offline = the package wasn't served from the cache.
    _re.compile(r"request to https?://registry", _re.IGNORECASE),
    # npm (NOT yarn) `npm ci --offline` cache-miss markers (navidrome's UI is
    # npm-managed). With --offline npm sets fetch cache mode `only-if-cached`; a
    # tarball not in _cacache then fails `code ENOTCACHED` / "cache mode is
    # \"only-if-cached\" but no cached response is available". Either is a definitive
    # closure gap (the warmed _cacache lacks a lockfile-pinned tarball).
    _re.compile(r"\bENOTCACHED\b", _re.IGNORECASE),
    _re.compile(r"only-if-cached.*no cached response", _re.IGNORECASE | _re.DOTALL),
]


def classify_npm_offline_build_failure(staging_tag: str, output: str) -> tuple[str, str]:
    """npm/yarn offline-build failure classifier `(staging_tag, output) ->
    (kind, detail)` for `run_offline_gate`.

    The go/maven classifiers don't recognise yarn's offline-resolution strings, so a
    yarn `--offline` failure would fall through to a "no token" → source_state branch
    (fail-OPEN, masking a real gap). This classifier instead pattern-matches yarn's
    cache-miss / registry-request signatures:
      - ANY of _NPM_CLOSURE_GAP_SIGS present (`Couldn't find any versions for`,
        `Couldn't find package`, `No matching version found`, an ENOENT against the
        yarn cache, the canonical `Can't make a request in offline mode ("<url>")`,
        or a `request to https://registry…` — a network attempt under --offline is
        itself proof the bytes weren't in the cache) → "closure_gap": the offline
        mirror is genuinely missing a needed package → BLOCK.
      - A `--frozen-lockfile` integrity failure (the milestone's yarn.lock disagrees
        with package.json / node_modules — a mid-change SOURCE state, the bytes ARE
        in the cache) → "source_state": not a closure gap.
      - Any other non-zero with no gap signature (a webpack/tsc/eslint build or lint
        error, a `package.json: command not found` script failure) → "source_state":
        a compile/lint/script error is not a missing dependency.

    `staging_tag` is accepted for signature-compatibility with the go classifier but
    unused — the yarn decision is pure-text (no in-image cache probe is needed: a
    cache-miss or registry-request string is definitive). Fail-closed: when a gap
    signature is present we BLOCK; when none is, the only non-gap explanations are
    frozen-lockfile/compile/lint, so source_state is the sound call.
    """
    text = output or ""
    for rx in _NPM_CLOSURE_GAP_SIGS:
        if rx.search(text):
            return ("closure_gap",
                    f"yarn offline resolution gap (missing from cache): "
                    f"matched {rx.pattern!r}")
    # No cache-miss / registry-request signature. A frozen-lockfile integrity error
    # is a yarn.lock-vs-tree mismatch (source-state, the cache has the bytes); any
    # other non-zero is a webpack/tsc/eslint/script failure — neither is a gap.
    if _re.search(r"frozen-?lockfile|lockfile.*(?:out of date|needs? to be updated|"
                  r"doesn't match)|Your lockfile needs to be updated",
                  text, _re.IGNORECASE):
        return ("source_state",
                "yarn --frozen-lockfile integrity mismatch (yarn.lock vs "
                "package.json/node_modules — source-state, not a closure gap)")
    return ("source_state",
            "no yarn cache-miss/registry-request signature "
            "(webpack/tsc/eslint/script error — not a missing dependency)")


# Cargo offline-resolution failure signatures — strings cargo emits when the warmed
# $CARGO_HOME cannot supply a crate the build needs (a real CLOSURE GAP). Distinct
# from a COMPILE error (rustc syntax/type error in the milestone's own source — a
# mid-migration START checkpoint), which is a SOURCE-STATE problem the closure is not
# at fault for. The go classifier doesn't recognise these, so a cargo gap would fall
# through its "no token → source_state" branch (fail-OPEN); this classifier closes
# that hole.
_CARGO_CLOSURE_GAP_RES = (
    # A git dep whose checkout isn't in git/checkouts (e.g. reedline @ branch=main).
    _re.compile(r"can't checkout from .*offline", _re.IGNORECASE),
    _re.compile(r"failed to (?:load|get|update|sync) .*source", _re.IGNORECASE),
    _re.compile(r"Unable to update .*(?:registry|git|https?://)", _re.IGNORECASE),
    # A registry crate version absent from the cache index/cache.
    _re.compile(r"failed to select a version", _re.IGNORECASE),
    _re.compile(r"not found in vendored sources", _re.IGNORECASE),
    _re.compile(r"no matching package(?: named)?\b", _re.IGNORECASE),
    _re.compile(r"unable to get packages from source", _re.IGNORECASE),
    _re.compile(r"failed to download .*(?:from|crate)", _re.IGNORECASE),
    # Any network attempt under --offline is itself proof the bytes weren't cached.
    _re.compile(r"you are in (?:the )?offline mode", _re.IGNORECASE),
    _re.compile(r"(?:dns error|name resolution|network is unreachable|"
                r"error sending request|Temporary failure in name resolution)",
                _re.IGNORECASE),
    _re.compile(r"the lock file .* needs to be updated but --locked", _re.IGNORECASE),
)


def classify_cargo_offline_build_failure(staging_tag: str, output: str) -> tuple[str, str]:
    """Cargo offline-build failure classifier `(staging_tag, output) -> (kind,
    detail)` for `run_offline_gate`.

    nushell's milestone /testbeds are mid-migration START checkpoints: several FAIL
    `cargo build --offline` on a rustc COMPILE error (e.g. `unexpected closing
    delimiter`, `could not compile <crate>`, `error[E0432] unresolved import`) — the
    SOURCE isn't a clean buildable state, but every dependency byte IS in the warmed
    cache, so this is NOT a closure gap. A REAL closure gap (a crate/git checkout the
    cache lacks) instead trips one of `_CARGO_CLOSURE_GAP_RES` (`can't checkout from
    … offline`, `failed to select a version`, `no matching package`, a network/dns
    attempt under --offline, …).

    Decision (fail-closed): ANY closure-gap signature present → "closure_gap" (BLOCK).
    Otherwise the non-zero is a rustc compile/type/syntax error (or a build-script
    failure) → "source_state" (record, do not block). `staging_tag` is accepted for
    signature-compatibility with the go classifier but unused — cargo's gap strings
    are definitive in-text (no cache probe needed). This is sound: cargo names a
    missing crate/source explicitly, so the absence of every gap signature means the
    bytes were present and rustc simply couldn't compile the source.
    """
    text = output or ""
    for rx in _CARGO_CLOSURE_GAP_RES:
        if rx.search(text):
            return ("closure_gap",
                    f"cargo offline resolution gap (crate/source missing from the "
                    f"warmed cache): matched {rx.pattern!r}")
    return ("source_state",
            "no cargo offline-resolution signature (rustc compile/type/syntax or "
            "build-script error — the milestone source isn't a clean buildable "
            "state; the closure has the dependency bytes)")


def run_offline_gate(staging_tag: str, milestone: str, offline_build: str,
                     goproxy_off: bool = False, classifier=None) -> str:
    """Per-milestone OFFLINE GATE: prove the closure is sufficient to build the
    milestone's B-source with no network.

    The milestone's own `/testbed` (the B-source) must be injected — NOT the
    A-baseline baked into the staging image. We materialise it on the host via an
    ephemeral `docker create`/`cp`/`rm`, bind-mount it over `/testbed`, and run
    `cd /testbed && <offline_build>` with `--network none`.

    For go ecosystems, pass `goproxy_off=True` to also set `-e GOPROXY=off` in the
    docker run. Without GOPROXY=off, a genuinely-missing module yields a network
    error prefixed `go: <module>@v: dial tcp ...` that escapes the classifier's
    `^<module@version>:` pattern (fail-OPEN). With GOPROXY=off, missing modules
    deterministically produce `module lookup disabled by GOPROXY=off`, which is a
    recognized closure-gap token.

    `classifier` is the `(staging_tag, output) -> (kind, detail)` callable used to
    label a non-zero build as "closure_gap" (BLOCK) vs "source_state" (record, do
    not block). Defaults to the go classifier `classify_offline_build_failure`;
    maven passes `classify_maven_offline_build_failure(forbid_globs)` (the go
    classifier doesn't recognise maven's `Cannot access ... in offline mode`
    strings and would fail-OPEN on a real maven dependency gap).

    Outcome:
      - build exit 0 → return "PASS".
      - build non-zero AND the failure is a real CLOSURE GAP (the cache cannot
        supply a needed module) → fail-closed (sys.exit 1); do NOT skip.
      - build non-zero but the failure is a SOURCE-STATE problem (the milestone's
        own go.mod/source is inconsistent though the closure HAS the bytes — a
        START-state mid-migration checkpoint, a compile/type error, …) → return
        "SOURCE_STATE" with the build tail printed. The closure is NOT at fault, so
        we do not block publish on it; the driver records and reports it.
    """
    import tempfile, shutil
    hosttmp = tempfile.mkdtemp(prefix="offline_gate.")
    try:
        # Ephemeral container, solely to copy the milestone's B-source /testbed out.
        cr = subprocess.run(["docker", "create", milestone],
                            capture_output=True, text=True)
        if cr.returncode != 0:
            print(f"Error: offline gate could not `docker create {milestone}`:\n"
                  f"{(cr.stderr or cr.stdout).strip()}", file=sys.stderr)
            sys.exit(1)
        cid = cr.stdout.strip()
        try:
            cp = subprocess.run(
                ["docker", "cp", f"{cid}:/testbed", f"{hosttmp}/testbed"],
                capture_output=True, text=True)
            if cp.returncode != 0:
                print(f"Error: offline gate could not cp /testbed from {milestone}:\n"
                      f"{(cp.stderr or cp.stdout).strip()}", file=sys.stderr)
                sys.exit(1)
        finally:
            subprocess.run(["docker", "rm", cid],
                          capture_output=True, text=True)
        # Bind-mount the milestone's /testbed over the image's baseline and build
        # fully offline. For go ecosystems, also set GOPROXY=off so missing modules
        # produce a deterministic "module lookup disabled by GOPROXY=off" token
        # (without it, dial-tcp errors escape the classifier and cause fail-OPEN).
        #
        # --ulimit nofile: the docker default soft limit is 1024, which a large
        # parallel `cargo build` (nushell's ~1000-crate graph) blows through with
        # `Too many open files (os error 24)` — an ENVIRONMENT artifact that would
        # masquerade as a (mis-classified) build failure and mask whether the closure
        # is actually complete. Raise it to the daemon hard limit's headroom
        # (65536:524288) so the gate genuinely exercises dependency availability. A
        # higher fd ceiling is harmless for every other ecosystem's gate.
        docker_run_argv = ["docker", "run", "--rm", "--network", "none",
                           "--ulimit", "nofile=65536:524288"]
        if goproxy_off:
            docker_run_argv += ["-e", "GOPROXY=off"]
        docker_run_argv += ["-v", f"{hosttmp}/testbed:/testbed", staging_tag,
                            "sh", "-c", f"cd /testbed && {offline_build}"]
        run = subprocess.run(docker_run_argv, capture_output=True, text=True)
        if run.returncode == 0:
            return "PASS"
        out = ((run.stdout or "") + (run.stderr or "")).strip()
        tail = "\n".join(out.splitlines()[-40:])
        classify = classifier or classify_offline_build_failure
        kind, detail = classify(staging_tag, out)
        if kind == "closure_gap":
            print(f"Error: OFFLINE GATE failed for milestone {milestone} "
                  f"(offline build exit {run.returncode}) — closure is "
                  f"INSUFFICIENT [{detail}]:\n{tail}", file=sys.stderr)
            sys.exit(1)
        # source_state: not a closure failure — report and continue.
        print(f"Warning: OFFLINE GATE milestone {milestone}: build failed "
              f"(exit {run.returncode}) but this is a SOURCE-STATE issue, NOT a "
              f"closure gap [{detail}]. Closure has the needed bytes; the "
              f"milestone source isn't a clean buildable state.\n{tail}",
              file=sys.stderr)
        return "SOURCE_STATE"
    finally:
        shutil.rmtree(hosttmp, ignore_errors=True)


# Signatures cargo emits when the vendored sources cannot satisfy a Cargo.lock
# requirement — i.e. a real CLOSURE GAP (the vendor is missing a version the
# A-baseline pins). These are the exact strings glm-5.2 validation hit when the
# vendor held only B-version crates and the agent started from the A-baseline.
_CARGO_CLOSURE_GAP_SIGS = (
    "failed to select a version",
    "not found in vendored sources",
)


def run_cargo_abaseline_gate(staging_tag: str, offline_build: str) -> None:
    """cargo A-BASELINE OFFLINE GATE: prove the closure also builds the exact state
    the AGENT STARTS FROM — the A-baseline /testbed — fully offline (fail-closed).

    The per-milestone gate (run_offline_gate) injects each milestone's B-source
    /testbed, so it ONLY exercises the B-version crates. But an A→B agent begins at
    `<repo>/base:latest`'s /testbed (the A Cargo.lock, e.g. bstr 1.10.0), and the
    $CARGO_HOME redirect routes ALL crates.io at /opt/vendor. If the vendor lacks
    the A-version crates, the agent's very first `cargo build` dies with `failed to
    select a version` / `not found in vendored sources` — a closure gap the
    milestone-only gate cannot see. This gate closes that hole.

    The staging image's OWN /testbed IS the A-baseline (it is `FROM
    <repo>/base:latest`), so NO injection is needed — unlike run_offline_gate we do
    NOT docker-create/cp a milestone. We simply
    `docker run --rm --network none <staging> sh -c 'cd /testbed && <offline_build>'`.

    Outcome: EXIT 0 → return (gate passed). Non-zero → the A-baseline is a clean,
    compilable state, so a build failure here is a REAL CLOSURE GAP (never a
    source-state problem the way a mid-migration B-milestone can be) → fail-closed
    (sys.exit 1). The closure-gap signatures above are called out explicitly in the
    error so the cause (missing vendored A-version) is unambiguous.
    """
    # --ulimit nofile: same reason as run_offline_gate — nushell's large parallel
    # cargo build exhausts the docker-default 1024 fd soft limit (`Too many open
    # files`), an environment artifact that would masquerade as a closure-gap failure
    # here (where ANY non-zero fails closed). Raise it so the A-baseline build runs to
    # completion and the gate's pass/fail reflects the closure, not the fd ceiling.
    r = subprocess.run(
        ["docker", "run", "--rm", "--network", "none",
         "--ulimit", "nofile=65536:524288", staging_tag,
         "sh", "-c", f"cd /testbed && {offline_build}"],
        capture_output=True, text=True)
    if r.returncode == 0:
        return
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    tail = "\n".join(out.splitlines()[-40:])
    sig = next((s for s in _CARGO_CLOSURE_GAP_SIGS if s in out), None)
    diag = (f" [closure gap: cargo could not satisfy the A-baseline Cargo.lock from "
            f"the vendor — matched {sig!r}; the vendor must span A→B]" if sig else
            " [A-baseline is a clean compilable state, so this offline-build failure "
            "is a closure gap, not a source-state issue]")
    print(f"Error: cargo A-BASELINE OFFLINE GATE failed for {staging_tag} "
          f"(offline build exit {r.returncode}) — the closure cannot build the state "
          f"the agent STARTS from{diag}:\n{tail}", file=sys.stderr)
    sys.exit(1)


def _dist_name(req: str) -> str:
    for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", "@", " "):
        if sep in req:
            return req.split(sep, 1)[0].strip().lower().replace("_", "-")
    return req.strip().lower().replace("_", "-")

def pip_union_requirements(freezes: list[str], forbid: list[str]) -> list[str]:
    fb = {f.strip().lower().replace("_", "-") for f in forbid}
    out = {}
    for txt in freezes:
        for line in txt.splitlines():
            s = line.strip()
            if not s or s.startswith("#") or s.startswith("-e") or s.startswith("git+") or "@ file://" in s:
                continue
            if _dist_name(s) in fb:
                continue
            out[s] = None
    return list(out)

def assert_single_version_or_explain(reqs: list[str]) -> None:
    seen = {}
    for r in reqs:
        n = _dist_name(r)
        seen.setdefault(n, set()).add(r)
    multi = {n: v for n, v in seen.items() if len(v) > 1}
    if multi:
        print(f"Error: pip closure has >1 version for {list(multi)} — "
              f"download per-version into the shared dir, do NOT merge into one -r.",
              file=sys.stderr)
        sys.exit(1)


def collect_pip_freezes(images: list[str]) -> list[str]:
    """`docker run --rm <image> pip freeze` for each milestone → list of freeze
    texts (read-only, never mutates the image).

    The freeze is the dependency snapshot we union: M06's `RUN pip install`
    additions (array-api-compat, array_api_strict) appear here even though they are
    in no lockfile — that's the structural gap the host-built A-only wheelhouse
    missed. Fail-closed if any freeze errors (a bad image / no pip): a missing
    milestone's deps would silently drop out of the closure.
    """
    out = []
    for img in images:
        r = subprocess.run(["docker", "run", "--rm", img, "pip", "freeze"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"Error: `pip freeze` failed for milestone {img} "
                  f"(exit {r.returncode}) — cannot collect its deps into the "
                  f"closure:\n{(r.stderr or r.stdout).strip()}", file=sys.stderr)
            sys.exit(1)
        out.append(r.stdout or "")
    return out


def assemble_pip_dockerfile(repo_lower: str, reqs_basename: str,
                            wheelhouse: str = "/wheelhouse",
                            reqs_in_image: str = "/tmp/union_reqs.txt") -> str:
    """Multi-stage pip closure Dockerfile.

    Like the npm/cargo "fetch the union online" assemblies (and unlike the raw
    cache-COPY ecosystems go/maven that union a milestone's raw cache), pip BUILDS
    its closure: a `wheel_builder` stage runs `pip download -r
    <union reqs> -d /wheelhouse` ONLINE in the repo's OWN base image (so the wheel
    platform/python tags match the runtime exactly — no tag mismatch), and the
    `final` stage (same base) carries `/wheelhouse` + the reqs file forward. The
    reqs file is `COPY`'d from the build context (the driver writes it there).

    `pip download` is given a one-version-per-package reqs file
    (assert_single_version_or_explain guarantees this), so resolution can't hit
    ResolutionImpossible. The reqs file is also present in the final image because
    the offline gate / runtime installs with `-r <reqs_in_image>`.
    """
    return (
        "# syntax=docker/dockerfile:1\n"
        f"FROM {repo_lower}/base:latest AS wheel_builder\n"
        f"COPY {reqs_basename} {reqs_in_image}\n"
        # ONLINE download of the whole union into the wheelhouse. One version per
        # package upstream, so no ResolutionImpossible.
        f"RUN pip download -r {reqs_in_image} -d {wheelhouse}\n"
        f"FROM {repo_lower}/base:latest AS final\n"
        f"COPY --from=wheel_builder {wheelhouse} {wheelhouse}\n"
        f"COPY {reqs_basename} {reqs_in_image}\n")


def _wheel_is_forbidden(wheel_filename: str, forbid: list[str]) -> bool:
    """True iff a wheel file's distribution name is in `forbid` (normalized).

    A wheel filename is `{dist}-{version}(-{build})?-{py}-{abi}-{plat}.whl`; the
    dist name is everything before the FIRST hyphen that begins the version. We
    take the leading dist token and normalize (lower, `_`→`-`) — matching is on the
    FULL dist name, never a prefix, so `scikit-image`/`scikit_image` and
    `scikit-learn-extra` are NOT matched by a `scikit-learn` forbid entry (full-name
    boundary). Only an exact normalized dist-name equality counts.
    """
    name = wheel_filename.strip()
    if name.endswith(".whl"):
        name = name[: -len(".whl")]
    # dist name = text up to the first '-' (the version segment starts after it).
    dist = name.split("-", 1)[0]
    norm = dist.strip().lower().replace("_", "-")
    fb = {f.strip().lower().replace("_", "-") for f in (forbid or [])}
    return norm in fb


def audit_wheelhouse_self_exclusion(staging_tag: str, forbid: list[str]) -> None:
    """pip-specific in-image self-exclusion AUDIT (fail-closed).

    The cache-COPY ecosystems audit host-glob paths inside the image
    (audit_staging_image); pip's closure is a `/wheelhouse` of downloaded wheels, so
    we instead `ls /wheelhouse` INSIDE the staging image and check each wheel's
    normalized DIST name against `forbid`. A forbidden wheel (e.g.
    `scikit_learn-1.6.0-…whl`, `sklearn-…whl`) means the offline index could serve
    the repo's own answer → refuse (sys.exit 1). `scikit_image` wheels are KEPT —
    the matcher is full-name, not prefix.
    """
    if not forbid:
        return
    r = subprocess.run(
        ["docker", "run", "--rm", staging_tag, "sh", "-c",
         "ls /wheelhouse 2>/dev/null; true"],
        capture_output=True, text=True)
    if r.returncode != 0:
        print(f"Error: wheelhouse audit docker run failed for {staging_tag} "
              f"(exit {r.returncode}):\n{(r.stderr or r.stdout).strip()}",
              file=sys.stderr)
        sys.exit(1)
    offending = [w for w in (r.stdout or "").split()
                 if w.endswith(".whl") and _wheel_is_forbidden(w, forbid)]
    if offending:
        print(f"Error: offline closure AUDIT failed for {staging_tag}: /wheelhouse "
              f"contains forbidden self@B wheel(s) — refusing (the offline index "
              f"would serve the answer):\n{sorted(offending)[:10]}", file=sys.stderr)
        sys.exit(1)


def run_pip_offline_gate(staging_tag: str, offline_build: str) -> None:
    """pip OFFLINE GATE: prove the whole union installs offline (fail-closed).

    `docker run --rm --network none <staging> sh -c '<offline_build>'` where
    offline_build is `pip install --no-index -f /wheelhouse -r /tmp/union_reqs.txt`.
    EXIT 0 proves the entire union resolves from `/wheelhouse` with no network — so
    every milestone's subset (a subset of the union) installs too. No `/testbed`
    injection is needed (unlike the go/cargo build gate): pip's gate is a pure
    install check, the milestone B-source is irrelevant. A non-zero install means a
    needed wheel (or transitive) is missing from the closure → a real CLOSURE GAP →
    sys.exit 1 with the install tail.
    """
    r = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", staging_tag,
         "sh", "-c", offline_build],
        capture_output=True, text=True)
    if r.returncode != 0:
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        tail = "\n".join(out.splitlines()[-40:])
        print(f"Error: pip OFFLINE GATE failed for {staging_tag} (install exit "
              f"{r.returncode}) — closure is INSUFFICIENT (a needed wheel/transitive "
              f"is missing from /wheelhouse):\n{tail}", file=sys.stderr)
        sys.exit(1)

def render_union_dockerfile(repo_lower: str, milestones: list[str], cache_paths: list[str]) -> str:
    lines = ["# syntax=docker/dockerfile:1", f"FROM {repo_lower}/base:latest AS builder",
             "RUN command -v rsync || (apt-get update -qq && apt-get install -y --no-install-recommends rsync)"]
    for i, m in enumerate(milestones):
        for j, cp in enumerate(cache_paths):
            lines.append(f"COPY --from={m} {cp} /milestone_{i}_{j}{cp}")
    # rsync-merge each milestone subtree into /staging (same-bytes dedup is harmless)
    merge = " && ".join(
        f"mkdir -p /staging{cp} && rsync -a /milestone_{i}_{j}{cp}/ /staging{cp}/"
        for i in range(len(milestones)) for j, cp in enumerate(cache_paths))
    lines.append(f"RUN mkdir -p /staging && {merge or 'true'}")
    lines.append(f"FROM {repo_lower}/base:latest AS final")
    for cp in cache_paths:
        lines.append(f"COPY --from=builder /staging{cp} {cp}")
    return "\n".join(lines) + "\n"


def _probe_go_version(image: str) -> str:
    """`docker run --rm <image> go version` → the reported go version token.

    Returns the bare version (e.g. "go1.21.13"); "" if the probe fails (image
    missing, daemon down, no `go` on PATH). Pure read-only — never mutates the
    image. Split out so the milestone picker can be unit-tested with a fake probe.
    """
    r = subprocess.run(["docker", "run", "--rm", image, "go", "version"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return ""
    # "go version go1.21.13 linux/amd64" → "go1.21.13"
    for tok in (r.stdout or "").split():
        if tok.startswith("go") and tok[2:3].isdigit():
            return tok
    return ""


def pick_go_toolchain_milestone(milestones: list[str], target_go: str,
                                _probe=_probe_go_version) -> str:
    """Choose the milestone image whose baked `/usr/local/go` reports `target_go`.

    The newer go toolchain (go-zero needs 1.21.13; base ships 1.19.13) lives in
    the B-milestone images at /usr/local/go. The simplest robust pick is the LAST
    milestone (B-end), but it is VERIFIED: we probe `go version` and, if the last
    one is wrong, scan the rest (newest-first) for a match. Fail-closed if none of
    the milestones report the target — a wrong/old toolchain COPY would make
    `go mod download` auto-fetch from proxy.golang.org and break offline.
    """
    if not milestones:
        print("Error: pick_go_toolchain_milestone got no milestones", file=sys.stderr)
        sys.exit(1)
    want = f"go{target_go}" if not str(target_go).startswith("go") else str(target_go)
    seen = {}
    # Probe last→first: the target toolchain is a B-side bump, so it lives at the end.
    for m in reversed(milestones):
        v = _probe(m)
        seen[m] = v
        if v == want:
            return m
    print(f"Error: no milestone image reports go version {want}; probed "
          f"{ {m: (seen.get(m) or '?') for m in milestones} } — cannot COPY a "
          f"correct /usr/local/go toolchain (an old toolchain would auto-download "
          f"from proxy.golang.org and break the offline gate).", file=sys.stderr)
    sys.exit(1)


# Where every base/milestone image keeps its go module download cache. The base
# image already ships the A-version cache here; the go_fetch stage warms it forward
# with the FULL declared module graph of each milestone, then the final stage carries
# the union back in. (Mirrors the single closure.cache_paths entry in the go config.)
_GO_MODCACHE_DIR = "/go/pkg/mod/cache/download"

# GOPROXY for the BUILD-TIME online fetch (the closure is sealed offline at eval, so a
# build-time fetch from a non-answer proxy is safe). proxy.golang.org is the public
# Google-hosted module mirror; `,direct` lets go fall back to the module's VCS origin
# for anything the proxy can't serve. The repo's OWN module (github.com/zeromicro/*)
# is never fetched here — `go mod download` only pulls the DEPENDENCIES named in
# go.mod, not the main module — so the cache cannot leak the answer.
_GO_FETCH_PROXY = "https://proxy.golang.org,direct"


def go_online_fetch_cmd(modroot_dir: str, modcache: str = _GO_MODCACHE_DIR,
                        proxy: str = _GO_FETCH_PROXY) -> str:
    """Render the per-milestone ONLINE go declared-deps fetch RUN body (no `RUN `
    prefix), warming the module cache from the go.mod+go.sum in `modroot_dir`.

    This is the go analogue of `maven_online_fetch_cmd` / `npm_online_fetch_cmd`:
    the build-warmed module cache shipped in a milestone image is INCOMPLETE — it
    holds only the modules `go build ./...` actually COMPILED, MISSING modules that
    go.mod DECLARES but the build never imports (indirect-only deps, test-only deps).
    `go mod download all` instead materialises the FULL module graph
    (the `all` package pattern's module closure — including indirect + test deps),
    so it fetches exactly the modules a later `go test ./...` / `go mod download` /
    `go mod verify` would demand. proxy.golang.org serves third-party modules (the
    repo's own module is the MAIN module, never downloaded by `go mod download`), so
    the build-time fetch is safe; the closure is sealed offline at eval.

    Flags:
      - `GOFLAGS=-mod=mod`: several milestones' go.mod still declare `go 1.19`, and
        go1.21's default `-mod=readonly` REFUSES to touch a module needing any
        go.mod/go.sum bookkeeping ("updates to go.mod needed") even just to download.
        `-mod=mod` lets go reconcile in-place so the download proceeds.
      - `GOPROXY=<proxy>`: force the public mirror (the image's baked GOPROXY may be a
        deny-listed CDN like goproxy.cn). `,direct` falls back to VCS for anything the
        proxy lacks.
      - `GOTOOLCHAIN=local`: forbid any auto-toolchain fetch (go.mod's `go 1.21`
        directive otherwise makes go try to pull a matching toolchain).
    `|| true`-guarded so a milestone whose graph has one un-fetchable corner never
    aborts the whole multi-milestone warm — every module it COULD download still
    enters the shared cache (a truly-gone module surfaces later as a gate gap).
    """
    return (f"cd {modroot_dir} && "
            f"GOFLAGS=-mod=mod GOPROXY={proxy} GOTOOLCHAIN=local "
            f"go mod download all 2>&1 | tail -3 || true")


def _go_fetch_stage(repo_lower: str, milestones: list[str], tc: str,
                    modcache: str = _GO_MODCACHE_DIR,
                    modsrc_dir: str = "/testbed") -> str:
    """Render the `go_fetch` stage: clean-replace the toolchain to the repo's go
    version FIRST, then COPY each milestone's go.mod+go.sum and `go mod download all`
    the FULL module graph into the shared module cache. Returns the stage text
    (ends with a trailing newline); the final stage COPYs `modcache` from it.

    The toolchain is clean-replaced (`rm -rf /usr/local/go` BEFORE the COPY) BEFORE
    any download so `go mod download` runs under the repo's real go (go-zero: 1.21.13,
    baked in the milestone images at /usr/local/go) — the base ships go1.19.13, and
    downloading with the wrong go can mis-resolve toolchain directives. GOTOOLCHAIN=
    local forbids any auto-toolchain fetch. `go mod download all` needs only
    go.mod+go.sum (the module GRAPH), not source, so we COPY just those two files per
    milestone (small context). `modsrc_dir` is where in the milestone image the
    manifests live (/testbed; navidrome's go module also roots at /testbed).
    """
    lines = [f"FROM {repo_lower}/base:latest AS go_fetch"]
    # Clean-replace the toolchain BEFORE downloading (right go resolves the graph;
    # GOTOOLCHAIN=local forbids an auto-toolchain fetch from proxy.golang.org).
    lines.append("RUN rm -rf /usr/local/go")
    lines.append(f"COPY --from={tc} /usr/local/go /usr/local/go")
    lines.append("ENV GOTOOLCHAIN=local")
    # COPY only go.mod+go.sum per milestone (the module graph; no source needed).
    for i, m in enumerate(milestones):
        lines.append(
            f"COPY --from={m} {modsrc_dir}/go.mod {modsrc_dir}/go.sum /m{i}/")
    # One `go mod download all` per milestone, all warming the SAME shared modcache.
    for i in range(len(milestones)):
        lines.append(f"RUN {go_online_fetch_cmd(f'/m{i}', modcache)}")
    return "\n".join(lines) + "\n"


def assemble_go_dockerfile(repo_lower: str, milestones: list[str],
                           cache_paths: list[str], target_go: str,
                           online_fetch: bool = True,
                           _probe=_probe_go_version) -> str:
    """Go closure Dockerfile: ONLINE-fetch the UNION of every milestone's *declared*
    module graph into the module cache (a `go_fetch` stage running `go mod download
    all` per milestone) — the robust pattern pip (`pip download` the union freeze),
    maven (`test-compile` the reactor), and npm (`yarn install` the lockfiles)
    already use, now for go — laid OVER the raw-cache rsync UNION of every milestone's
    build-warmed module cache, with a clean-replaced newer go toolchain in the final
    stage.

    Why not raw-COPY the milestone caches ALONE (the old go branch)?  Build-warmed
    module caches are INCOMPLETE: they hold only the modules `go build ./...` actually
    COMPILED, and MISS modules a milestone's go.mod DECLARES but the build never
    imports — indirect-only deps and test-only deps (go-zero: an SRS-vs-image audit
    caught 17 such modules, e.g. cel.dev/expr, github.com/gorilla/websocket,
    github.com/IBM/sarama, declared in m013/m018/m020/m025 go.mod but absent from the
    union of the build-warmed caches). The per-milestone `go build ./...` gate passes
    WITHOUT them precisely because building doesn't need them — but an agent running
    `go test ./...`, `go mod download`, or `go mod verify` WILL (same class as the
    maven-bcprov / npm-caniuse-lite / go-zip gaps). The raw-cache union can only carry
    what some milestone's build happened to cache; it cannot supply a
    declared-but-uncompiled module.

    Fix: a `go_fetch` stage (FROM <repo>/base:latest, so the base image's EXISTING
    A-version cache is the starting point — the union therefore spans A→B) COPYs ONLY
    each milestone's go.mod+go.sum and runs `go mod download all` (the FULL module
    graph — incl. indirect + test deps, NOT the build-scoped subset) per milestone,
    all warming the SAME shared module cache. The toolchain is clean-replaced to the
    repo's go (1.21.13) FIRST so the download resolves under the right go. The final
    stage COPYs the raw-cache union (belt-and-suspenders; it also pins the exact
    A-version bytes already shipped in base) AND the online-fetched graph (the
    superset) into the module cache, then clean-replaces the toolchain. `go mod
    download` only pulls the DEPENDENCIES named in go.mod — never the MAIN module — so
    the cache cannot leak github.com/zeromicro/*; the self-exclusion audit stays clean
    by construction.

    `online_fetch=False` falls back to the legacy raw-cache-union-only assembly (the
    `go_mechanism: raw-cache` config escape) for parity/debugging — but the DEFAULT
    online-fetch path is what closes the declared-but-uncompiled gap.

    Toolchain (unchanged): go-zero's B-source declares `go 1.21` but base ships
    go1.19.13. The target (1.21.13) is baked in the B-milestone images at
    /usr/local/go, so the final stage does `RUN rm -rf /usr/local/go` BEFORE
    `COPY --from=<verified milestone> /usr/local/go /usr/local/go` + `ENV
    GOTOOLCHAIN=local`. The `rm -rf` MUST precede the COPY: an overlay mixes
    go1.19+go1.21 stdlib and `go build` fails (`m0 redeclared`). GOTOOLCHAIN=local
    forbids an auto-toolchain fetch from proxy.golang.org under --network none.

    No `.info`-sidecar synthesis: the build-scoped gate (`go build -mod=mod ./...`)
    resolves straight from the cache.
    """
    union = render_union_dockerfile(repo_lower, milestones, cache_paths)
    tc = pick_go_toolchain_milestone(milestones, target_go, _probe=_probe)
    # Legacy escape: raw-cache union only (no online fetch). Kept for the
    # `go_mechanism: raw-cache` config fallback / debugging.
    if not online_fetch:
        tail = ("RUN rm -rf /usr/local/go\n"
                f"COPY --from={tc} /usr/local/go /usr/local/go\n"
                "ENV GOTOOLCHAIN=local\n")
        return union + tail
    # Online-fetch path (default). render_union_dockerfile emits `# syntax=...` then
    # `FROM ... AS builder` (rsync union) then `FROM ... AS final` (COPY the unioned
    # cache). We PREPEND the go_fetch stage (its own FROM) — Dockerfile stages may
    # appear in any order so long as a later COPY --from references an earlier-defined
    # stage — and APPEND, to the union's (open) final stage, a COPY of the
    # online-fetched module cache plus the clean-replace toolchain. Keep exactly ONE
    # `# syntax` directive at the very top.
    syntax = "# syntax=docker/dockerfile:1\n"
    union_body = union[len(syntax):] if union.startswith(syntax) else union
    # The go module cache the union COPYs into the final stage (the config's single
    # cache_paths entry); the go_fetch stage warms the same path so the final stage
    # can COPY both. Use the first cache_path that looks like the modcache, else the
    # canonical default (defensive — the go config has exactly one).
    modcache = next((cp for cp in (cache_paths or []) if "cache/download" in cp),
                    _GO_MODCACHE_DIR)
    fetch_stage = _go_fetch_stage(repo_lower, milestones, tc, modcache)
    # Tail appended to the union's (open) final stage: COPY the online-fetched graph
    # OVER the raw-cache union (module cache entries are content-addressed; the online
    # graph is the superset, so the overlay is additive and same-bytes-safe), then
    # clean-replace the toolchain. `rm -rf` BEFORE the COPY (overlay mixes stdlib).
    tail = (f"COPY --from=go_fetch {modcache} {modcache}\n"
            "RUN rm -rf /usr/local/go\n"
            f"COPY --from={tc} /usr/local/go /usr/local/go\n"
            "ENV GOTOOLCHAIN=local\n")
    return syntax + fetch_stage + union_body + tail


def maven_rm_self_at_b_cmd(forbid_globs: list[str]) -> str:
    """Render the FINAL-stage `RUN` that deletes the repo's OWN target-version
    artifacts (self@B) from the unioned `.m2`, derived from the config's
    `cache_forbid_globs`.

    The maven self-exclusion problem: a milestone's `/root/.m2/repository` holds
    dubbo's own B-version jars+`-sources.jar`
    (`org/apache/dubbo/<mod>/3.3.6-SNAPSHOT/*` — the cheat answer the agents copied
    from Maven Central). The raw-cache union (`render_union_dockerfile`) carries them
    into the closure, so we delete them in the FINAL stage AFTER the cache COPY.

    The rm targets are EXACTLY the config's `cache_forbid_globs` — the same patterns
    the generic `audit_staging_image` runs afterwards. Deleting precisely what the
    audit forbids guarantees the audit then matches NOTHING and passes (if the rm
    globs and the forbid globs ever diverged, the audit would fire on the leftover).
    Each glob is `rm -rf`'d; `2>/dev/null; true` keeps the layer exit 0 even when a
    glob matches nothing (the second forbid glob, `…/3.[4-9]*`, currently matches no
    cached artifact — that is the normal, passing case).
    """
    globs = list(forbid_globs or [])
    if not globs:
        # Nothing forbidden ⇒ nothing to delete. A no-op `true` keeps the assembly
        # well-formed (and the audit, also a no-op, stays clean by construction).
        return "RUN true\n"
    targets = " ".join(globs)
    return f"RUN rm -rf {targets} 2>/dev/null; true\n"


# Where the base/milestone images keep the maven local repository. The agent runs
# `mvn -o` against this via maven.repo.local (the config's maven_repo_local), so the
# closure must populate exactly this dir. (Mirrors closure.cache_paths in the dubbo
# config, whose single entry is this path.)
_M2_REPO_DIR = "/root/.m2/repository"

# Lint plugins that abort a reactor build BEFORE dependency resolution finishes.
# We skip them during the build-time online fetch so a spotless/checkstyle/rat
# format violation in a mid-migration milestone never stops the deps from being
# downloaded (we only want the .m2 warmed; formatting is irrelevant here). These
# mirror the lint-skips in the config's offline_build gate.
_MVN_FETCH_LINT_SKIPS = (
    "-Dspotless.check.skip=true -Dspotless.apply.skip=true "
    "-Dcheckstyle.skip=true -Drat.skip=true -Dmaven.gitcommitid.skip=true"
)


def maven_online_fetch_cmd(testbed_dir: str, m2_repo: str = _M2_REPO_DIR) -> str:
    """Render the per-milestone ONLINE declared-deps fetch RUN body (no `RUN `
    prefix), warming `m2_repo` from the reactor rooted at `testbed_dir`.

    The fetch runs TWO maven goals against the milestone's FULL multi-module
    reactor (dubbo is multi-module; `mvn` needs every module's pom to build the
    reactor model), both writing into the SHARED `-Dmaven.repo.local=<m2_repo>`:

      1. `dependency:go-offline` — a cheap broad first pass that resolves the
         compile+runtime dependency graph AND the build/reporting PLUGINS,
         downloading them (and their poms) into the local repo. It is RESOLUTION-ONLY
         (never compiles), so it can't be tripped by a mid-migration source state,
         but it has TWO known limitations: it does not pull TEST-scope-only deps, and
         for version-managed/ranged deps it can resolve a DIFFERENT (often older)
         version than the build actually uses, and may fetch only the `.pom` (not the
         `.jar`). So it is necessary (plugins!) but NOT sufficient.
      2. `test-compile` (ONLINE) on the SAME reactor — THE workhorse. The
         per-milestone offline GATE is `mvn -o test-compile` on this very testbed, so
         running `test-compile` ONLINE here downloads EXACTLY the artifacts (right
         versions, the `.jar` not just the `.pom`, INCLUDING all test-scope deps)
         that the offline gate will later demand — the tightest possible match.
         `go-offline`/`dependency:resolve` proved insufficient: for m006's reactor
         `go-offline` fetched only the bcprov-ext-jdk18on POM (no jar), and for
         m001.1's dubbo-cluster it resolved opentelemetry-sdk-testing 1.49.0 while
         test-compile needs 1.50.0 — only the online `test-compile` pulls the correct
         jar+version. `-fae` (fail-at-end) makes the reactor keep building (and thus
         keep DOWNLOADING each module's deps) past a module whose mid-migration
         source won't compile, so one un-buildable module never starves the rest of
         the cache; `-DskipTests` skips test EXECUTION (we only need test-SOURCES
         compiled, which still resolves+downloads the test-scope deps).

    Both goals are `-o`-free (this is the ONLINE build stage — repo1.maven.org /
    Maven Central is a non-answer registry that serves third-party artifacts, safe to
    fetch from at BUILD time; the closure is sealed offline at eval). Each goal is
    `|| true`-guarded so a milestone whose reactor can't fully build (a mid-migration
    source-state checkpoint, a JDK-gated module) still contributes every dep it COULD
    download to the shared cache, and one milestone's source state never aborts the
    whole multi-milestone fetch. The lint plugins are skipped (see
    `_MVN_FETCH_LINT_SKIPS`) so a spotless/rat format violation can't abort the
    reactor before resolution. `-q` keeps the log readable.

    Note the online `test-compile`/`go-offline` may itself pull the reactor's OWN
    3.3.6 artifacts (from Central, or install reactor modules) into the shared repo —
    the self@B rm in the final stage (run AFTER all fetches) deletes them, so the
    published closure never serves the answer.
    """
    goals = (
        # (1) cheap broad pass: plugins + compile/runtime graph (resolution-only).
        f"mvn -q dependency:go-offline {_MVN_FETCH_LINT_SKIPS} "
        f"-Dmaven.repo.local={m2_repo} || true ; "
        # (2) the workhorse: ONLINE test-compile downloads EXACTLY what the offline
        # `mvn -o test-compile` gate needs (right versions, jars, test-scope deps).
        # -fae so a non-compiling module doesn't starve the rest; -DskipTests skips
        # test EXECUTION but still compiles test-sources (→ resolves test-scope deps).
        f"mvn -q -fae test-compile -DskipTests {_MVN_FETCH_LINT_SKIPS} "
        f"-Dmaven.repo.local={m2_repo} || true"
    )
    return f"cd {testbed_dir} && ( {goals} )"


def assemble_maven_dockerfile(repo_lower: str, milestones: list[str],
                              cache_paths: list[str],
                              forbid_globs: list[str]) -> str:
    """Maven closure Dockerfile: ONLINE-fetch the UNION of every milestone's
    *declared* deps into the local `.m2`, then bake it — the robust pattern pip
    (`pip download` the union freeze), cargo (`cargo vendor` the union of locks), and
    npm (`yarn install` the union of lockfiles) already use, now for maven.

    Why not raw-COPY the milestone `.m2` caches (the old maven branch)?  Build-warmed
    `.m2` caches are INCOMPLETE: a dep can be DECLARED by a milestone's pom yet never
    land in that image's cache because no milestone's build path actually downloaded
    it (dubbo m006 declares `org.bouncycastle:bcprov-ext-jdk18on:1.78.1`, but it is in
    NO milestone's `.m2`, so `mvn -o test-compile` fail-closes at m006 — the SAME
    class as the npm caniuse-lite / go-zip gaps). The raw-cache union can only carry
    what some milestone happened to cache; it cannot supply a declared-but-uncached
    dep.

    Fix: a `fetch_builder` stage (FROM <repo>/base:latest, so the base image's
    EXISTING A-version `.m2` is the starting point — the union therefore spans A→B)
    COPYs each milestone's FULL `/testbed` into `/tb/m<i>/` (the WHOLE reactor source,
    not just one pom: dubbo is multi-module and `mvn` needs every module's pom to
    build the reactor model — heavier than npm's manifest-only COPY but necessary),
    then runs `dependency:go-offline` + `dependency:resolve -DincludeScope=test` per
    milestone (see `maven_online_fetch_cmd`), all warming the SAME shared
    `-Dmaven.repo.local=<m2_repo>`. Each fetch ADDs that milestone's declared deps
    (incl. the test-scope-only ones `go-offline` misses, e.g. bcprov-ext-jdk18on) to
    the shared cache. repo1.maven.org / Maven Central is a non-answer registry (it
    serves third-party artifacts, not dubbo's own published app — and the agent never
    fetches from it at eval), so the build-time fetch is safe; the closure is sealed
    offline at eval.

    The `final` stage (same base) COPYs the warmed `.m2` (`m2_repo`) back over its
    own, then REMOVES self@B: the online fetch (or a milestone's pre-warmed cache)
    can pull dubbo's OWN target-version artifacts
    (`org/apache/dubbo/<mod>/3.3.6*` — the answer agents copied from Central) into the
    cache, so we delete them in the final stage AFTER the COPY. The rm targets are
    EXACTLY the config's `cache_forbid_globs` (passed as `forbid_globs`), so the
    subsequent generic `audit_staging_image` (running the same globs) finds nothing.
    The rm MUST come after the fetch+COPY (remove what the fetch may have pulled).

    `cache_paths` (the config's single `.m2` repository path) selects the warm/COPY
    target; if it names a different/extra path the first entry is the maven repo and
    is used as `m2_repo` (defensive — dubbo's config has exactly one).
    """
    if not milestones:
        print("Error: assemble_maven_dockerfile got no milestones", file=sys.stderr)
        sys.exit(1)
    # The maven local repository to warm + bake. The config's cache_paths carries it
    # (a single entry for dubbo); fall back to the canonical default if unset so the
    # assembly is always well-formed.
    m2_repo = (cache_paths or [_M2_REPO_DIR])[0]
    lines = [
        "# syntax=docker/dockerfile:1",
        f"FROM {repo_lower}/base:latest AS fetch_builder",
    ]
    # COPY each milestone's FULL /testbed (the whole reactor — every module pom is
    # needed to build the multi-module model).
    for i, m in enumerate(milestones):
        lines.append(f"COPY --from={m} /testbed /tb/m{i}")
    # One ONLINE declared-deps fetch per milestone, all warming the SAME shared
    # local repo (m2_repo). go-offline (compile+runtime+plugins) AND resolve
    # -DincludeScope=test (the test-scope deps go-offline misses, e.g. bcprov).
    for i in range(len(milestones)):
        lines.append(f"RUN {maven_online_fetch_cmd(f'/tb/m{i}', m2_repo)}")
    lines.append(f"FROM {repo_lower}/base:latest AS final")
    lines.append(f"COPY --from=fetch_builder {m2_repo} {m2_repo}")
    # self@B removal AFTER the fetch+COPY: the online fetch may have pulled dubbo's
    # OWN 3.3.6 artifacts into the cache; delete exactly the cache_forbid_globs so
    # the closure never serves the answer and the generic audit (same globs) passes.
    df = "\n".join(lines) + "\n"
    return df + maven_rm_self_at_b_cmd(forbid_globs)


# Where yarn-classic keeps its global package cache in the base/milestone images.
# The base image already ships the A-version cache here; the fetch_builder stage
# warms it forward with every milestone's declared deps, then the final stage
# carries the union back in. (Mirrors closure.cache_paths in the npm config.)
_YARN_CACHE_DIR = "/usr/local/share/.cache/yarn/v6"


def assemble_npm_dockerfile(repo_lower: str, milestones: list[str],
                            yarn_cache: str = _YARN_CACHE_DIR,
                            global_npm_tools: list[str] | None = None) -> str:
    """npm/yarn-classic closure Dockerfile: ONLINE-fetch the UNION of every
    milestone's *declared* deps into the yarn cache, then bake it — the robust
    pattern pip (`pip download` the union freeze) and cargo (`cargo vendor` the
    union of locks) already use, now for npm.

    Why not raw-COPY the milestone caches (the old npm branch)?  Build-warmed yarn
    caches are INCOMPLETE: a dep can be pinned by a milestone's `yarn.lock` yet
    never land in that image's cache (element-web: `caniuse-lite@1.0.30001701` is
    pinned by 4 milestones' lockfile but is in NO milestone's cache — the union of
    the raw caches held only 30001699 + 30001704). So `yarn install --offline
    --frozen-lockfile` fails at those milestones (same class as maven-bcprov /
    go-zip — the milestone caches don't contain every lockfile-declared dep).

    Fix: a `fetch_builder` stage (FROM <repo>/base:latest, so the base image's
    EXISTING A-version yarn cache is the starting point — the union therefore spans
    A→B) COPYs ONLY `package.json` + `yarn.lock` from each milestone into `/m<i>/`
    (just the manifests, NOT the whole /testbed — keeps the build context small and
    pulls no source), then runs one `yarn install` per milestone, all warming the
    SAME shared cache. Each install ADDs that milestone's declared deps (incl.
    caniuse-lite@1.0.30001701) to the shared cache. registry.yarnpkg.com is a
    non-answer registry (it serves third-party npm packages, not element-web's own
    app — which is not published to npm), so fetching from it at BUILD time is
    safe; the closure is sealed offline at eval. The `final` stage (same base)
    COPYs the warmed cache (`yarn_cache`, the versioned dir) back over its own.

    THE --cache-folder GOTCHA (yarn-classic): `--cache-folder X` does NOT make yarn
    write into X — yarn APPENDS its own cache-version segment (`/v6` for yarn 1.22)
    and writes into `X/v6`. The base/milestone images' real cache is
    `/usr/local/share/.cache/yarn/v6` (that trailing `v6` IS yarn's segment;
    `yarn cache dir` reports exactly this), so to warm THAT dir the cache-folder
    must be its PARENT — `/usr/local/share/.cache/yarn` — and yarn re-appends `/v6`.
    Passing the versioned dir itself (`…/yarn/v6`) writes one level too deep, into
    `…/yarn/v6/v6/`, which the offline gate's `yarn install` (reading
    `…/yarn/v6`) never sees → a fetched-but-invisible cache and a false `Can't make
    a request in offline mode ("…caniuse-lite-1.0.30001701.tgz")` gate failure (the
    first integration run hit exactly this). So `--cache-folder` is the parent of
    `yarn_cache`; the COPY target stays `yarn_cache` (the versioned dir yarn fills).

    Per-milestone install flags:
      - `--cache-folder <parent-of-yarn_cache>` so yarn's appended `/v6` lands on
        the real shared cache (see the GOTCHA above); every milestone writes the
        same dir — that is the whole point, the union accumulates.
      - `--ignore-scripts` skips postinstall: we only need the cache POPULATED with
        tarballs, not a built node_modules (no native builds, much faster, and
        avoids a postinstall that wants tools the manifest-only dir lacks).
      - `--non-interactive` so a prompt can never hang the build.
      - PREFER `--frozen-lockfile` (fetch EXACTLY the lockfile-pinned versions); if
        that errors because the milestone's lockfile is itself mid-change
        (source-state: yarn.lock disagrees with package.json), FALL BACK to a plain
        `yarn install` for the cache-warming — we only want the deps fetched, and a
        non-frozen resolve still populates the cache. The fallback is a shell `||`
        inside the RUN so one milestone's source-state lockfile never aborts the
        whole build. `--frozen-lockfile` first keeps the common (clean) case exact.

    No self@B removal: element-web's app source is not published to npm, so there
    is no self@B tarball the cache could serve (cache_forbid_globs is empty → the
    generic audit stays a clean no-op).

    `global_npm_tools` (optional): a list of `<pkg>[@<version>]` specifiers for
    global npm tools declared by the repo's SRS but installed via
    `npm install -g <tool>` (NOT via yarn, so NOT in the yarn cache). Each tool is
    baked into the final stage with `RUN npm install -g <spec>` ONLINE at image
    build time — the binary is then present in the image without any offline install.
    registry.npmjs.org is a non-answer registry (it serves generic CLI tools, not
    the repo's own source), so this fetch is safe at build time. Example:
    `serve@14.2.5` (a static-file server used by playwright tests in some milestones).
    """
    if not milestones:
        print("Error: assemble_npm_dockerfile got no milestones", file=sys.stderr)
        sys.exit(1)
    # See THE --cache-folder GOTCHA: yarn appends its own version segment (/v6) to
    # --cache-folder, so to fill the real cache dir (`yarn_cache`, ending in /v6) we
    # pass its PARENT and let yarn re-append the segment. PurePosixPath keeps this
    # correct regardless of a trailing slash.
    from pathlib import PurePosixPath
    cache_folder = str(PurePosixPath(yarn_cache).parent)
    lines = [
        "# syntax=docker/dockerfile:1",
        f"FROM {repo_lower}/base:latest AS fetch_builder",
    ]
    # COPY only the two manifests from each milestone (small context, no source).
    for i, m in enumerate(milestones):
        lines.append(
            f"COPY --from={m} /testbed/package.json /testbed/yarn.lock /m{i}/")
    # One install per milestone, all warming the SAME shared cache. Prefer
    # --frozen-lockfile; fall back to a non-frozen resolve if the lockfile is
    # mid-change (source-state) — we only need the deps fetched into the cache.
    for i in range(len(milestones)):
        base = (f"yarn install --cache-folder {cache_folder} "
                f"--ignore-scripts --non-interactive")
        lines.append(
            f"RUN cd /m{i} && ( {base} --frozen-lockfile || {base} )")
    lines.append(f"FROM {repo_lower}/base:latest AS final")
    lines.append(f"COPY --from=fetch_builder {yarn_cache} {yarn_cache}")
    # Bake any SRS-declared global npm tools (e.g. serve@14.2.5) into the final
    # image so milestones can use them without an offline install. Each tool is
    # fetched from registry.npmjs.org at BUILD time (online; safe — it is a generic
    # CLI tool registry, not the repo's own answer source).
    for tool in (global_npm_tools or []):
        lines.append(f"RUN npm install -g {tool}")
    return "\n".join(lines) + "\n"


# Where npm (NOT yarn) keeps its global content-addressable cache in the
# base/milestone images. navidrome's UI is npm-managed (package-lock.json,
# `npm ci`), so the closure must warm THIS dir — the npm equivalent of yarn's
# _YARN_CACHE_DIR. (Mirrors the second closure.cache_paths entry in the
# navidrome config.)
_NPM_CACACHE_DIR = "/root/.npm/_cacache"


def npm_online_fetch_cmd(testbed_ui_dir: str, cacache: str = _NPM_CACACHE_DIR) -> str:
    """Render the per-milestone ONLINE npm declared-deps fetch RUN body (no `RUN `
    prefix), warming the npm content-addressable cache `cacache` from the
    package.json + package-lock.json in `testbed_ui_dir`.

    This is the npm (NOT yarn) analogue of `assemble_npm_dockerfile`'s yarn
    install: navidrome's UI uses npm (`/testbed/ui/package-lock.json`,
    `npm ci`), so the warm command is `npm`, not `yarn`. The build-warmed
    `_cacache` shipped in a milestone image is INCOMPLETE the same way yarn's was
    (a lockfile can pin a tarball — caniuse-lite-style — that no milestone's build
    actually fetched into _cacache), so we re-populate it ONLINE from each
    milestone's declared deps. registry.npmjs.org serves third-party packages
    (navidrome's UI package, private name `"ui"`, is unpublished), so the
    build-time fetch is safe; the closure is sealed offline at eval.

    PREFER `npm ci` (installs EXACTLY the lockfile-pinned tree → fetches exactly
    the tarballs the offline gate's `npm ci --offline` will later demand), with:
      - `--cache <cacache>` so the fetched tarballs land in the SHARED cache dir
        (every milestone writes the same dir — the union accumulates). Unlike
        yarn's `--cache-folder` GOTCHA, npm's `--cache` IS the literal cache
        directory (no version segment is appended), so we pass `_cacache`'s PARENT
        — npm itself appends `/_cacache`. The base/milestone real cache is
        `/root/.npm/_cacache`, so `--cache` must be `/root/.npm` and npm
        re-appends `_cacache`. (Passing `_cacache` directly would write into
        `/root/.npm/_cacache/_cacache`, invisible to the offline gate.)
      - `--prefer-offline=false` to force a real registry fetch of anything not
        already cached (we are ONLINE here precisely to fill the gaps a prior
        milestone's cache missed — never silently fall back to a stale cache).
      - `--ignore-scripts` so a postinstall (native build, husky, etc.) that wants
        tools the manifest-only dir lacks can't abort the cache-warming; we only
        need `_cacache` POPULATED with tarballs, not a built node_modules.
      - `--no-audit --no-fund` keep the log readable / avoid extra network calls.
    FALL BACK to `npm install` (same flags) when `npm ci` errors because the
    milestone's lockfile is itself mid-change (source-state: package-lock.json
    disagrees with package.json — `npm ci` is strict and aborts, but a non-strict
    `npm install` still resolves+fetches the deps into the cache). The fallback is
    a shell `||` inside the RUN so one milestone's source-state lockfile never
    aborts the whole multi-milestone fetch.
    """
    from pathlib import PurePosixPath
    cache_parent = str(PurePosixPath(cacache).parent)
    base = (f"npm ci --cache {cache_parent} --prefer-offline=false "
            f"--ignore-scripts --no-audit --no-fund")
    fallback = (f"npm install --cache {cache_parent} --prefer-offline=false "
                f"--ignore-scripts --no-audit --no-fund")
    return f"cd {testbed_ui_dir} && ( {base} || {fallback} )"


def assemble_go_npm_dockerfile(repo_lower: str, milestones: list[str],
                               cache_paths: list[str], target_go: str,
                               npm_cacache: str = _NPM_CACACHE_DIR,
                               _probe=_probe_go_version) -> str:
    """DUAL go+npm closure Dockerfile (navidrome): ONE staging image carrying BOTH
    a complete go closure AND a complete npm closure.

    navidrome is a dual-ecosystem repo — a Go backend plus an npm-managed React UI
    (`/testbed/ui`). Neither single-ecosystem assembly suffices alone, so this
    composes the two PROVEN mechanisms into one multi-stage build:

    GO part (identical to `assemble_go_dockerfile`, validated on go-zero):
      - ONLINE-fetch the UNION of every milestone's DECLARED module graph
        (`go mod download all` per milestone, see `_go_fetch_stage`) into the module
        cache, laid OVER the raw-cache rsync UNION of every milestone's build-warmed
        `/go/pkg/mod/cache/download`. The build-warmed caches hold only the modules
        `go build` COMPILED and MISS indirect-only / test-only deps a go.mod declares
        (the go-zero 17-module gap class); the online fetch closes that. Both span
        A→B because every stage is FROM base:latest (the A-baseline cache is the
        starting point) and ADDs each milestone's (B) modules on top.
      - clean-replace the go toolchain to `target_go` (1.24.5): `RUN rm -rf
        /usr/local/go` BEFORE `COPY --from=<milestone reporting target_go>
        /usr/local/go` + `ENV GOTOOLCHAIN=local`. navidrome's milestones split
        between `go 1.24.4` and `go 1.24.5` go.mod directives (and ship the
        matching toolchain); with GOTOOLCHAIN=local a 1.24.4 toolchain can't build
        a `go 1.24.5` milestone (it would try to fetch 1.24.5 from
        proxy.golang.org → fails offline). 1.24.5 builds BOTH, so we standardise on
        it. `pick_go_toolchain_milestone` finds a milestone that reports it. The
        go_fetch stage clean-replaces to this toolchain FIRST so `go mod download`
        resolves the graph under the right go.

    NPM part (the npm — not yarn — analogue of `assemble_npm_dockerfile`):
      - a `npm_fetch` stage (FROM base:latest, so base's A-version _cacache is the
        starting point) COPYs ONLY each milestone's `ui/package.json` +
        `ui/package-lock.json` (small context, no source) and runs one ONLINE
        `npm ci` (fallback `npm install`) per milestone (see
        `npm_online_fetch_cmd`), all warming the SAME shared `/root/.npm/_cacache`.
        Each fetch ADDs that milestone's declared UI deps to the cache, closing any
        caniuse-lite-style gap the build-warmed milestone _cacache missed.

    FINAL stage (FROM base:latest) COPYs all FOUR closures so both ecosystems land
    in one image:
      1. `COPY --from=builder /staging<go-cache> <go-cache>` — the raw-cache unioned
         go module cache (from the go union's `builder` stage).
      2. `COPY --from=go_fetch <go-cache> <go-cache>` — the online-fetched declared
         module graph (the superset; additive over the raw-cache union).
      3. `RUN rm -rf /usr/local/go` + `COPY --from=<tc> /usr/local/go
         /usr/local/go` + `ENV GOTOOLCHAIN=local` — the clean-replaced go
         toolchain.
      4. `COPY --from=npm_fetch <_cacache> <_cacache>` — the warmed npm cache.

    Stage names are disjoint (`builder`/`final` from the go union vs `go_fetch` vs
    `npm_fetch`), so the COPYs compose without collision. navidrome has NO self@B in
    either cache (the Go self-module github.com/navidrome/* is never in the module
    cache by construction — `go mod download` only pulls dependencies, never the main
    module; the UI package `"ui"` is private/unpublished), so there is NO removal step
    — the generic audit (cache_forbid_globs target the go modcache, which never holds
    the self-module) is a clean no-op.
    """
    if not milestones:
        print("Error: assemble_go_npm_dockerfile got no milestones", file=sys.stderr)
        sys.exit(1)
    # ---- GO: raw-cache union + clean-replace toolchain (reuse the proven go path).
    # render_union_dockerfile emits `... AS builder` (rsync union) then `... AS
    # final` (COPY the unioned cache). We append the npm fetch stage and the
    # toolchain/_cacache COPYs to that, keeping stage names disjoint.
    go_cache_paths = [cp for cp in cache_paths if cp != npm_cacache] or cache_paths
    union = render_union_dockerfile(repo_lower, milestones, go_cache_paths)
    tc = pick_go_toolchain_milestone(milestones, target_go, _probe=_probe)
    # The go module download cache the union COPYs into final; the go_fetch stage
    # warms the SAME path with the full declared graph (`go mod download all`) so
    # final can COPY both. Pick the modcache-looking go cache path (defensive).
    go_modcache = next((cp for cp in go_cache_paths if "cache/download" in cp),
                       _GO_MODCACHE_DIR)
    go_fetch_stage = _go_fetch_stage(repo_lower, milestones, tc, go_modcache)
    # render_union_dockerfile already wrote the `final` stage with the go-cache COPY.
    # Split off its trailing so we can interleave: keep the union (incl. its `final`
    # stage header + go-cache COPY), then add the npm fetch stage as a SEPARATE
    # stage BEFORE final would be cleaner, but Dockerfile stages may appear in any
    # order as long as a later COPY --from references an earlier-defined stage. We
    # therefore emit the npm_fetch stage AFTER the union's final stage is opened is
    # NOT allowed (can't reopen `final`). So instead: define npm_fetch FIRST (its own
    # FROM), then the go union (builder+final), then COPY from npm_fetch into final.
    #
    # Simplest correct layout: prepend the npm_fetch stage, then the full go union,
    # then the toolchain + _cacache COPY tail appended to the (already-open) final
    # stage.
    npm_lines = [f"FROM {repo_lower}/base:latest AS npm_fetch"]
    for i, m in enumerate(milestones):
        npm_lines.append(
            f"COPY --from={m} /testbed/ui/package.json "
            f"/testbed/ui/package-lock.json /ui_m{i}/")
    for i in range(len(milestones)):
        npm_lines.append(f"RUN {npm_online_fetch_cmd(f'/ui_m{i}', npm_cacache)}")
    npm_stage = "\n".join(npm_lines) + "\n"
    # The go union begins with `# syntax=...` then `FROM ... AS builder`. Strip the
    # union's leading `# syntax` line and put a single one at the very top, then the
    # npm_fetch stage, then the union's stages (builder + final). This keeps exactly
    # ONE syntax directive and a valid multi-stage ordering.
    syntax = "# syntax=docker/dockerfile:1\n"
    union_body = union
    if union_body.startswith(syntax):
        union_body = union_body[len(syntax):]
    # Tail appended to the union's (open) `final` stage: COPY the online-fetched go
    # module graph OVER the raw-cache union (additive, same-bytes-safe), clean-replace
    # the toolchain, then COPY the warmed npm _cacache. The `rm -rf` MUST precede the
    # toolchain COPY (overlay would mix go1.24.4/go1.24.5 stdlib → `go build` fails);
    # the go_fetch / npm_fetch COPYs pull from the stages defined at the top.
    tail = (f"COPY --from=go_fetch {go_modcache} {go_modcache}\n"
            "RUN rm -rf /usr/local/go\n"
            f"COPY --from={tc} /usr/local/go /usr/local/go\n"
            "ENV GOTOOLCHAIN=local\n"
            f"COPY --from=npm_fetch {npm_cacache} {npm_cacache}\n")
    return syntax + go_fetch_stage + npm_stage + union_body + tail


def classify_go_npm_offline_build_failure(staging_tag: str,
                                          output: str) -> tuple[str, str]:
    """DUAL go+npm offline-build failure classifier `(staging_tag, output) ->
    (kind, detail)` for navidrome's combined gate.

    The dual gate runs `npm ci --offline && npm run build && ... go build ./...`,
    so a failure can come from EITHER ecosystem. We first try the npm/yarn
    classifier: if the output carries an npm/yarn offline cache-miss /
    registry-request signature it is unambiguously an npm CLOSURE GAP (the go
    classifier doesn't recognise those strings and would fail-OPEN). Only if the
    npm classifier finds NO gap signature do we defer to the go classifier (which
    probes the in-image module cache for any cited go module) — so a go module gap
    BLOCKs and a go.mod/compile/source-state issue is recorded, not blocked.

    Ordering rationale (fail-closed): the npm classifier returns closure_gap ONLY
    on a definitive npm cache-miss/registry signature; for any other npm non-zero
    it returns source_state (frozen-lockfile / webpack / tsc / eslint). A real npm
    gap therefore wins immediately; otherwise the go classifier gets to probe the
    module cache, which is the right authority for go failures. A pure UI build
    error (tsc/webpack) → npm says source_state, go finds no module token → go also
    says source_state ⇒ recorded, not blocked (correct: not a missing dependency).
    """
    npm_kind, npm_detail = classify_npm_offline_build_failure(staging_tag, output)
    if npm_kind == "closure_gap":
        return (npm_kind, f"[npm] {npm_detail}")
    go_kind, go_detail = classify_offline_build_failure(staging_tag, output)
    if go_kind == "closure_gap":
        return (go_kind, f"[go] {go_detail}")
    # Neither ecosystem reports a missing-dependency gap. Prefer the go detail when
    # a go module token was seen (it probed the cache); otherwise the npm detail.
    if "no missing-module token" not in go_detail:
        return ("source_state", f"[go] {go_detail}")
    return ("source_state", f"[npm] {npm_detail}")


# --------------------------------------------------------------------------- #
# Driver: build staging → in-image self-exclusion audit → per-milestone        #
# offline gate → tag/publish base-offline:latest (fail-closed at each step).    #
# --------------------------------------------------------------------------- #

def _ecosystems_of(repo_lower: str, project_root: Path) -> list[str]:
    """Top-level `ecosystem` from the quarantine yaml as a normalized lowercase
    LIST (order preserved).

    Accepts a scalar (`ecosystem: pip` → `["pip"]`) or a list
    (`ecosystem: [go, npm]` → `["go", "npm"]`). This is the multi-aware reader the
    dual-ecosystem dispatch uses; `_ecosystem_of` stays the single-id reader for
    the single-ecosystem branches. Fail-closed if `ecosystem` is missing/empty.
    """
    data = load_quarantine_yaml(repo_lower, project_root)
    eco = data.get("ecosystem")
    if isinstance(eco, list):
        out = [str(e).strip().lower() for e in eco if str(e).strip()]
        if out:
            return out
    elif isinstance(eco, str) and eco.strip():
        return [eco.strip().lower()]
    print(f"Error: {repo_lower}: quarantine config has no `ecosystem`", file=sys.stderr)
    sys.exit(1)


def _ecosystem_of(repo_lower: str, project_root: Path) -> str:
    """Top-level `ecosystem` from the quarantine yaml, as a single lowercase id.

    Accepts a scalar or a one-element list (the configs use `ecosystem: [cargo]`).
    Multi-ecosystem repos are routed by the dual dispatch (see `_ecosystems_of`),
    not here.
    """
    data = load_quarantine_yaml(repo_lower, project_root)
    eco = data.get("ecosystem")
    if isinstance(eco, list):
        eco = [str(e).strip().lower() for e in eco if str(e).strip()]
        if len(eco) != 1:
            print(f"Error: {repo_lower}: expected exactly one ecosystem, got {eco}",
                  file=sys.stderr); sys.exit(1)
        return eco[0]
    if isinstance(eco, str) and eco.strip():
        return eco.strip().lower()
    print(f"Error: {repo_lower}: quarantine config has no `ecosystem`", file=sys.stderr)
    sys.exit(1)


def _docker_build(dockerfile: str, tag: str, project_root: Path) -> None:
    """Real `docker build -f <tmp Dockerfile> -t <tag> <project_root>`.

    The Dockerfile pulls everything via `COPY --from=<image>`, so the build
    context is irrelevant — project_root is used only because it must be a valid
    directory. Streams output; exits nonzero on build failure.
    """
    import tempfile, os
    fd, path = tempfile.mkstemp(prefix="closure.", suffix=".Dockerfile")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(dockerfile)
        print(f"--- Dockerfile ({tag}) ---\n{dockerfile}--- docker build ---", flush=True)
        r = subprocess.run(
            ["docker", "build", "-f", path, "-t", tag, str(project_root)])
        if r.returncode != 0:
            print(f"Error: docker build failed for {tag} (exit {r.returncode})",
                  file=sys.stderr)
            sys.exit(r.returncode)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _build_pip_closure(repo_lower: str, project_root: Path, milestones: list[str],
                       cfg: dict, staging_tag: str, latest_tag: str,
                       push: bool, keep: bool) -> None:
    """pip ASSEMBLY path (freeze → union → download → audit → gate → tag).

    Self-contained: collects every milestone's `pip freeze`, unions them (dropping
    editable/git/self-pkg lines), asserts one version per package, writes the reqs
    into the build context, builds the multi-stage wheelhouse image, runs the
    wheelhouse self-exclusion audit + the single-union offline-install gate, and on
    all-green tags :latest. The reqs file in the build context is removed in a
    `finally`; the staging image is cleaned up by the caller's `finally` (unless
    keep). Fail-closed: a version conflict, forbidden wheel, or failed offline
    install exits non-zero before :latest is tagged.
    """
    forbid = load_quarantine_yaml(repo_lower, project_root).get("wheelhouse_forbid") or []
    offline_build = cfg["offline_build"]

    print(f"pip: collecting `pip freeze` from {len(milestones)} milestone(s) ...",
          flush=True)
    freezes = collect_pip_freezes(milestones)
    reqs = pip_union_requirements(freezes, forbid)
    assert_single_version_or_explain(reqs)
    print(f"pip: union has {len(reqs)} requirement(s) (forbid={list(forbid)})",
          flush=True)

    # Write the union reqs INTO the build context so the wheel_builder stage can
    # `COPY` it. Named uniquely; removed in the finally so the repo tree stays clean.
    import tempfile, os
    fd, reqs_path = tempfile.mkstemp(prefix="union_reqs.", suffix=".txt",
                                     dir=str(project_root))
    reqs_basename = os.path.basename(reqs_path)
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(reqs) + ("\n" if reqs else ""))
        df = assemble_pip_dockerfile(repo_lower, reqs_basename)
        _docker_build(df, staging_tag, project_root)

        # 1) Wheelhouse self-exclusion AUDIT (fail-closed): no scikit_learn/sklearn
        #    wheel may sit in /wheelhouse (would serve the answer offline). Keeps
        #    scikit_image (full-name boundary, not a forbid match).
        audit_wheelhouse_self_exclusion(staging_tag, forbid)
        print(f"pip: wheelhouse audit clean: {staging_tag} "
              f"(forbid={list(forbid)})", flush=True)

        # 2) Offline GATE: the WHOLE union must `pip install --no-index` from
        #    /wheelhouse with --network none. EXIT 0 ⇒ every milestone's subset
        #    installs offline too. A non-zero is a real closure gap (missing wheel).
        print(f"pip: offline gate (union install --network none) ...", flush=True)
        run_pip_offline_gate(staging_tag, offline_build)
        print(f"pip: offline gate PASS (union installs offline from /wheelhouse)",
              flush=True)

        # 3) All green → publish.
        tr = subprocess.run(["docker", "tag", staging_tag, latest_tag])
        if tr.returncode != 0:
            print(f"Error: docker tag {staging_tag} -> {latest_tag} failed",
                  file=sys.stderr)
            sys.exit(tr.returncode)
        if push:
            pr = subprocess.run(["docker", "push", latest_tag])
            if pr.returncode != 0:
                print(f"Error: docker push {latest_tag} failed", file=sys.stderr)
                sys.exit(pr.returncode)
        print(f"SUCCESS: {latest_tag} published ({len(reqs)} wheel reqs, offline "
              f"install gate passed{', pushed' if push else ''}).")
    finally:
        try:
            os.unlink(reqs_path)
        except OSError:
            pass


# The DUAL go+npm per-milestone OFFLINE GATE command (navidrome). Run with
# `cd /testbed && <this>` (run_offline_gate prepends that), so it cd's into the UI
# first. `npm ci --offline` rebuilds node_modules from the warmed _cacache (the
# config's `npm run build` alone can't — it needs node_modules present), then
# `npm run build` builds the UI, then `GOPROXY=off go build ./...` build-scopes the
# Go backend (the same build-scoped gate go-zero uses — not a whole-graph
# `go mod download`). GOPROXY=off + GOTOOLCHAIN=local (baked in the image) make a
# missing module deterministic and forbid any toolchain auto-download. EXIT 0 =
# pass.
_GO_NPM_OFFLINE_GATE = ("cd /testbed/ui && npm ci --offline && npm run build && "
                        "cd /testbed && GOPROXY=off go build ./...")


def _build_go_npm_closure(repo_lower: str, project_root: Path,
                          milestones: list[str], cfg: dict,
                          forbid_globs: list[str], staging_tag: str,
                          latest_tag: str, push: bool, keep: bool) -> None:
    """DUAL go+npm ASSEMBLY path (navidrome): build ONE staging image carrying both
    the go closure (modcache union + clean-replaced 1.24.5 toolchain) and the npm
    closure (warmed /root/.npm/_cacache), AUDIT it, run the per-milestone DUAL
    offline gate, and on all-green tag :latest.

    Self-contained (like `_build_pip_closure`): the generic single-ecosystem
    audit/gate flow in `build_closure` is single-classifier; the dual gate needs
    BOTH the go and npm classifiers (`classify_go_npm_offline_build_failure`) and a
    build that first installs the UI offline then compiles both ecosystems, so this
    branch owns the whole pipeline and returns.

    Gate command: `_GO_NPM_OFFLINE_GATE` (npm ci --offline + npm run build +
    GOPROXY=off go build ./...), run per milestone with its B-source /testbed
    injected and `--network none` (+ `-e GOPROXY=off`). A real CLOSURE GAP (a UI
    tarball missing from _cacache, or a go module missing from the modcache union)
    fail-closes inside run_offline_gate; a SOURCE-STATE failure (a mid-migration
    milestone whose UI/backend source doesn't compile though the closure HAS the
    bytes) is recorded but does not block. navidrome has no self@B in either cache,
    so there is NO removal step and the generic in-image audit (forbid_globs) is a
    clean no-op.
    """
    cache_paths = cfg["cache_paths"]
    toolchain = cfg.get("toolchain") or {}
    target_go = toolchain.get("go")
    if not target_go:
        print(f"Error: {repo_lower}: go+npm ecosystem needs closure.toolchain.go "
              f"(target go version)", file=sys.stderr)
        sys.exit(1)

    # ---- assemble the ONE dual Dockerfile (go union+toolchain + npm cacache warm).
    df = assemble_go_npm_dockerfile(repo_lower, milestones, cache_paths, target_go)
    _docker_build(df, staging_tag, project_root)

    try:
        # 1) In-image self-exclusion AUDIT (defense-in-depth; navidrome's
        #    cache_forbid_globs target the go modcache, which never holds the
        #    self-module → clean no-op, but run it for parity/fail-closed).
        audit_staging_image(staging_tag, forbid_globs)
        print(f"audit clean: {staging_tag} (forbid_globs={len(forbid_globs)})",
              flush=True)

        # 2) Per-milestone DUAL OFFLINE GATE. GOPROXY=off (go half) + the combined
        #    go+npm classifier so a UI _cacache gap AND a go modcache gap each BLOCK,
        #    while a compile/source-state issue is recorded.
        source_state = []
        for i, m in enumerate(milestones, 1):
            print(f"offline gate [{i}/{len(milestones)}] {m} ...", flush=True)
            result = run_offline_gate(
                staging_tag, m, _GO_NPM_OFFLINE_GATE, goproxy_off=True,
                classifier=classify_go_npm_offline_build_failure)
            if result == "SOURCE_STATE":
                source_state.append(m)
                print(f"offline gate [{i}/{len(milestones)}] {m}: SOURCE-STATE "
                      f"(not a closure gap; recorded)", flush=True)
            else:
                print(f"offline gate [{i}/{len(milestones)}] {m}: PASS", flush=True)

        # 3) No closure gap (any gap exited above) → publish. Only now tag :latest.
        passed = len(milestones) - len(source_state)
        tr = subprocess.run(["docker", "tag", staging_tag, latest_tag])
        if tr.returncode != 0:
            print(f"Error: docker tag {staging_tag} -> {latest_tag} failed",
                  file=sys.stderr)
            sys.exit(tr.returncode)
        if push:
            pr = subprocess.run(["docker", "push", latest_tag])
            if pr.returncode != 0:
                print(f"Error: docker push {latest_tag} failed", file=sys.stderr)
                sys.exit(pr.returncode)
        if source_state:
            print(f"SUCCESS (with source-state concerns): {latest_tag} published — "
                  f"{passed}/{len(milestones)} milestone gate(s) built offline; "
                  f"{len(source_state)} milestone(s) failed on SOURCE-STATE issues "
                  f"(NOT closure gaps; closure has the bytes): {source_state}"
                  f"{', pushed' if push else ''}.")
        else:
            print(f"SUCCESS: {latest_tag} published "
                  f"({len(milestones)} milestone gate(s) passed"
                  f"{', pushed' if push else ''}).")
    finally:
        if not keep:
            subprocess.run(["docker", "rmi", "-f", staging_tag],
                          capture_output=True, text=True)


def build_closure(repo_lower: str, project_root: Path, push: bool = False,
                  keep: bool = False) -> None:
    """Build, AUDIT, GATE, and tag the offline dependency closure for a repo.

    Pipeline: assemble + `docker build` <repo_lower>/base-offline:staging → run
    the in-image self-exclusion AUDIT (cache_forbid_globs) → run the per-milestone
    OFFLINE GATE (each milestone's B-source /testbed must build offline against the
    closure) → on ALL-green, `docker tag` it <repo_lower>/base-offline:latest (and
    `docker push` if `push`). Any audit/gate/build failure is fail-closed: :latest
    is NEVER tagged and the process exits nonzero.

    The staging image is removed in a `finally` unless `keep` is set; the published
    :latest tag always stays.
    """
    cfg = load_closure_config(repo_lower, project_root)
    ecos = _ecosystems_of(repo_lower, project_root)
    # cache_forbid_globs is a TOP-LEVEL key in the quarantine yaml (not inside the
    # `closure` block), so read it from the full config. Default [] (audit no-op).
    forbid_globs = load_quarantine_yaml(repo_lower, project_root).get(
        "cache_forbid_globs") or []
    offline_build = cfg["offline_build"]
    milestones = discover_milestone_images(repo_lower)
    if not milestones:
        print(f"Error: no milestone images for {repo_lower} (run pull_images.sh)",
              file=sys.stderr)
        sys.exit(1)
    staging_tag = f"{repo_lower}/base-offline:staging"
    latest_tag = f"{repo_lower}/base-offline:latest"

    # DUAL go+npm (navidrome): a multi-ecosystem repo the single-id `_ecosystem_of`
    # rejects. Detect the set {go, npm} and route to the self-contained dual path
    # (build BOTH closures, audit, per-milestone dual gate, tag) which returns.
    if set(ecos) == {"go", "npm"}:
        _build_go_npm_closure(repo_lower, project_root, milestones, cfg,
                              forbid_globs, staging_tag, latest_tag, push, keep)
        return
    eco = _ecosystem_of(repo_lower, project_root)

    try:
        if eco == "cargo":
            # closure.toolchain.rust (optional): some repos pin a rust channel
            # (nushell → 1.88.0) the base image lacks; both cargo mechanisms then
            # install it ONLINE in the final stage. Absent → no rust-install line
            # (ripgrep unchanged).
            #
            # closure.cargo_mechanism selects HOW the cargo closure is built:
            #   "vendor"    (default): `cargo vendor` the union of milestone locks
            #               into /opt/vendor + a $CARGO_HOME redirect. Solves the
            #               sparse-index re-resolution problem and works for repos
            #               whose deps are all registry/path crates (ripgrep).
            #   "raw-cache": warm the raw $CARGO_HOME (registry cache/index/src +
            #               git db/checkouts) ONLINE via `cargo fetch` per milestone,
            #               then COPY it forward. REQUIRED for nushell:
            #               it pins `reedline` from crates.io AND a git branch at the
            #               SAME version 0.41.0, which `cargo vendor` cannot represent
            #               (one flat <name>-<ver>/ dir → "found duplicate version …
            #               from two sources"), but the raw cache holds trivially
            #               (registry and git live in separate trees).
            mechanism = (cfg.get("cargo_mechanism") or "vendor").lower()
            if mechanism == "raw-cache":
                df = assemble_cargo_rawcache_dockerfile(
                    repo_lower, milestones, toolchain=cfg.get("toolchain"))
            elif mechanism == "vendor":
                df = assemble_cargo_dockerfile(repo_lower, milestones,
                                               toolchain=cfg.get("toolchain"),
                                               extra_vendor_crates=cfg.get("extra_vendor_crates"))
            else:
                print(f"Error: {repo_lower}: unknown closure.cargo_mechanism "
                      f"{mechanism!r} (want 'vendor' or 'raw-cache')",
                      file=sys.stderr)
                sys.exit(1)
            _docker_build(df, staging_tag, project_root)
        elif eco == "go":
            # ONLINE-fetch the UNION of every milestone's DECLARED module graph
            # (`go mod download all`) into the module cache, laid OVER the raw-cache
            # rsync UNION of the build-warmed caches, plus the newer go toolchain
            # (COPY /usr/local/go from a milestone reporting the target version) +
            # GOTOOLCHAIN=local in the final stage (assemble_go_dockerfile). The old
            # raw-cache-only COPY was a closure gap: build-warmed module caches hold
            # only what `go build ./...` COMPILED and MISS modules go.mod DECLARES but
            # the build never imports (indirect-only / test-only deps — go-zero: 17
            # such modules, e.g. cel.dev/expr, github.com/gorilla/websocket,
            # github.com/IBM/sarama, declared in m013/m018/m020/m025 but absent from
            # every milestone cache, so `go test`/`go mod download`/`go mod verify`
            # would fail; same class as the maven-bcprov / npm-caniuse-lite gaps). The
            # go_fetch stage is FROM base:latest (whose A-version cache is the starting
            # point, so the union spans A→B), COPYs each milestone's go.mod+go.sum, and
            # runs `go mod download all` (the FULL graph) per milestone — each ADDs its
            # declared modules to the shared cache. proxy.golang.org serves third-party
            # modules (the repo's own main module is never downloaded), so the
            # build-time fetch is safe; the closure is sealed offline at eval. NO
            # self@B removal (cache_forbid_globs target the modcache, which never holds
            # the main module → the generic audit below is a clean no-op). The
            # per-milestone B-source gate (go build -mod=mod ./..., GOPROXY=off)
            # applies unchanged, with the go classifier.
            #
            # closure.go_mechanism (optional): "online-fetch" (default) is the path
            # above; "raw-cache" is the legacy raw-cache-union-only escape (no online
            # fetch) for parity/debugging — it does NOT close the declared-but-
            # uncompiled gap and should only be used deliberately.
            cache_paths = cfg["cache_paths"]
            toolchain = cfg.get("toolchain") or {}
            target_go = toolchain.get("go")
            if not target_go:
                print(f"Error: {repo_lower}: go ecosystem needs closure.toolchain.go "
                      f"(target go version)", file=sys.stderr)
                sys.exit(1)
            go_mechanism = (cfg.get("go_mechanism") or "online-fetch").lower()
            if go_mechanism not in ("online-fetch", "raw-cache"):
                print(f"Error: {repo_lower}: unknown closure.go_mechanism "
                      f"{go_mechanism!r} (want 'online-fetch' or 'raw-cache')",
                      file=sys.stderr)
                sys.exit(1)
            df = assemble_go_dockerfile(
                repo_lower, milestones, cache_paths, target_go,
                online_fetch=(go_mechanism == "online-fetch"))
            _docker_build(df, staging_tag, project_root)
        elif eco == "maven":
            # ONLINE-fetch the UNION of every milestone's DECLARED deps into the
            # local `.m2`, then bake it (assemble_maven_dockerfile) — the robust
            # pattern pip (`pip download` the union freeze), cargo (`cargo vendor`
            # the union of locks), and npm (`yarn install` the union of lockfiles)
            # already use. The old raw-cache COPY was a closure gap: build-warmed
            # `.m2` caches are INCOMPLETE — e.g. org.bouncycastle:bcprov-ext-jdk18on:
            # 1.78.1 is DECLARED by m006's reactor but is in NO milestone's `.m2`, so
            # `mvn -o test-compile` fail-closed at m006 (same class as the npm
            # caniuse-lite / go-zip gaps). The fetch_builder stage is FROM base:latest
            # (whose A-version `.m2` is the starting point, so the union spans A→B),
            # COPYs each milestone's FULL /testbed (the multi-module reactor needs
            # every module pom), and runs `dependency:go-offline` +
            # `dependency:resolve -DincludeScope=test` per milestone — each ADDs its
            # declared deps (incl. the test-scope ones go-offline misses, e.g.
            # bcprov-ext-jdk18on) to the shared maven.repo.local. repo1.maven.org /
            # Maven Central serves third-party artifacts (dubbo's own app is fetched
            # by no one at eval), so the build-time fetch is safe; the closure is
            # sealed offline at eval. The final stage COPYs the warmed `.m2` forward
            # then REMOVES self@B (the online fetch can pull dubbo's OWN 3.3.6
            # artifacts — the answer): the rm targets are EXACTLY the config's
            # cache_forbid_globs, run AFTER the fetch+COPY, so the generic audit below
            # (same globs) matches nothing. The per-milestone B-source gate (mvn -o
            # test-compile) applies unchanged, with the maven classifier.
            cache_paths = cfg["cache_paths"]
            df = assemble_maven_dockerfile(repo_lower, milestones, cache_paths,
                                           forbid_globs)
            _docker_build(df, staging_tag, project_root)
        elif eco == "npm":
            # ONLINE-fetch the UNION of every milestone's DECLARED deps into the
            # yarn cache, then bake it (assemble_npm_dockerfile) — the robust
            # pattern pip (`pip download` the union freeze) and cargo (`cargo
            # vendor` the union of locks) already use. The old raw-cache COPY was a
            # closure gap: build-warmed yarn caches are INCOMPLETE — e.g.
            # caniuse-lite@1.0.30001701 is pinned by 4 milestones' yarn.lock but is
            # in NO milestone's cache, so `yarn install --offline --frozen-lockfile`
            # failed at those milestones (same class as maven-bcprov / go-zip). The
            # fetch_builder stage is FROM base:latest (whose A-version yarn cache is
            # the starting point, so the union spans A→B), COPYs ONLY each
            # milestone's package.json+yarn.lock, and runs one `yarn install
            # --cache-folder <shared>` per milestone — each ADDs its declared deps
            # (incl. caniuse-lite@30001701) to the shared cache. registry.yarnpkg.com
            # serves third-party packages (element-web's app is not on npm), so the
            # build-time fetch is safe; the closure is sealed offline at eval. No
            # toolchain step (yarn ships in the base) and NO self@B removal
            # (cache_forbid_globs empty → the generic audit below is a clean no-op).
            # The per-milestone B-source gate (yarn install --offline
            # --frozen-lockfile) applies unchanged, with the npm/yarn classifier.
            # closure.global_npm_tools (optional): SRS-declared global tools
            # (e.g. serve@14.2.5) that are NOT in the yarn cache but are installed
            # by milestones via `npm install -g`. Baked into the final stage ONLINE
            # so milestones can use them without any offline install step.
            global_npm_tools = cfg.get("global_npm_tools") or []
            df = assemble_npm_dockerfile(repo_lower, milestones,
                                         global_npm_tools=global_npm_tools or None)
            _docker_build(df, staging_tag, project_root)
        elif eco == "pip":
            # pip is ASSEMBLED, not cache-COPY'd: collect each milestone's
            # `pip freeze`, union (dropping editable/git/self entries), assert one
            # version per package, then `pip download` the union into an in-image
            # /wheelhouse (online, in the repo's OWN base so wheel tags match). The
            # audit + gate are pip-specific (wheelhouse glob, single union install),
            # so this branch is self-contained and returns — the generic
            # host-glob audit / per-milestone B-source gate below do not apply.
            _build_pip_closure(repo_lower, project_root, milestones, cfg,
                               staging_tag, latest_tag, push, keep)
            return
        else:
            print(f"Error: {repo_lower}: unsupported ecosystem {eco!r}", file=sys.stderr)
            sys.exit(1)

        # 1) In-image self-exclusion AUDIT (defense-in-depth; fail-closed).
        audit_staging_image(staging_tag, forbid_globs)
        print(f"audit clean: {staging_tag} (forbid_globs={len(forbid_globs)})",
              flush=True)

        # 2) Per-milestone OFFLINE GATE — each milestone's B-source /testbed must
        #    build offline against the closure. A real CLOSURE GAP fail-closes
        #    inside run_offline_gate (sys.exit 1); a SOURCE-STATE failure (the
        #    milestone's own source/go.mod is inconsistent though the closure has
        #    the bytes) is recorded but does NOT block publish — the closure is not
        #    at fault.
        # For go ecosystems set GOPROXY=off so missing modules produce a
        # deterministic "module lookup disabled by GOPROXY=off" classifier token
        # (without it, dial-tcp errors escape the pattern and cause fail-OPEN).
        gate_goproxy_off = (eco == "go")
        # Maven needs a maven-aware failure classifier: the default (go) classifier
        # doesn't recognise maven's `Cannot access ... in offline mode` strings and
        # would fail-OPEN on a real maven gap. The maven classifier treats an
        # unresolved self@B sibling (org.apache.dubbo:*:3.3.6-SNAPSHOT — removed by
        # design; the agent builds it from the reactor) as source_state and any
        # unresolved THIRD-PARTY artifact as a real closure gap → BLOCK. The self@B
        # patterns are the SAME cache_forbid_globs that drove the rm.
        # npm needs a yarn-aware classifier for the same reason: the default (go)
        # classifier doesn't recognise yarn's `Couldn't find any versions for` /
        # `request to https://registry…` offline strings and would fail-OPEN on a
        # real yarn cache gap. The npm classifier treats a yarn cache-miss /
        # registry-request as a closure gap → BLOCK, and a --frozen-lockfile
        # integrity mismatch (yarn.lock vs tree — source-state) as source_state.
        # cargo needs a cargo-aware classifier for the same reason: the default (go)
        # classifier doesn't recognise cargo's offline-resolution strings (`can't
        # checkout from … offline`, `failed to select a version`, `no matching
        # package`, a network/dns attempt under --offline) and would fall through to
        # "no token → source_state" (fail-OPEN on a real crate/git gap). The cargo
        # classifier treats those as a closure gap → BLOCK, and a rustc compile/type/
        # syntax error (nushell's mid-migration START checkpoints) as source_state.
        if eco == "maven":
            gate_classifier = classify_maven_offline_build_failure(forbid_globs)
        elif eco == "npm":
            gate_classifier = classify_npm_offline_build_failure
        elif eco == "cargo":
            gate_classifier = classify_cargo_offline_build_failure
        else:
            gate_classifier = None
        source_state = []
        for i, m in enumerate(milestones, 1):
            print(f"offline gate [{i}/{len(milestones)}] {m} ...", flush=True)
            result = run_offline_gate(staging_tag, m, offline_build,
                                     goproxy_off=gate_goproxy_off,
                                     classifier=gate_classifier)
            if result == "SOURCE_STATE":
                source_state.append(m)
                print(f"offline gate [{i}/{len(milestones)}] {m}: SOURCE-STATE "
                      f"(not a closure gap; recorded)", flush=True)
            else:
                print(f"offline gate [{i}/{len(milestones)}] {m}: PASS", flush=True)

        # 2b) cargo A-BASELINE OFFLINE GATE — the per-milestone gates above only
        #     exercise the B-version crates (each injects a milestone B-source
        #     /testbed). But the AGENT STARTS from the A-baseline /testbed and the
        #     $CARGO_HOME redirect points ALL crates.io at /opt/vendor, so the
        #     vendor must also satisfy the A Cargo.lock. The staging image's OWN
        #     /testbed IS the A-baseline (it is FROM <repo>/base:latest), so no
        #     injection is needed — build it offline in place. A failure here is a
        #     real CLOSURE GAP (the A-baseline is a clean compilable state) →
        #     fail-closed inside run_cargo_abaseline_gate before :latest is tagged.
        if eco == "cargo":
            print(f"cargo A-baseline offline gate (staging /testbed, --network none) "
                  f"...", flush=True)
            run_cargo_abaseline_gate(staging_tag, offline_build)
            print(f"cargo A-baseline offline gate: PASS (the agent's A-start state "
                  f"builds offline against the closure)", flush=True)

        # 3) No closure gap (any gap would have exited above) → publish. Only now
        #    is :latest tagged. Source-state-only failures do not block.
        passed = len(milestones) - len(source_state)
        tr = subprocess.run(["docker", "tag", staging_tag, latest_tag])
        if tr.returncode != 0:
            print(f"Error: docker tag {staging_tag} -> {latest_tag} failed",
                  file=sys.stderr)
            sys.exit(tr.returncode)
        if push:
            pr = subprocess.run(["docker", "push", latest_tag])
            if pr.returncode != 0:
                print(f"Error: docker push {latest_tag} failed", file=sys.stderr)
                sys.exit(pr.returncode)
        if source_state:
            print(f"SUCCESS (with source-state concerns): {latest_tag} published — "
                  f"{passed}/{len(milestones)} milestone gate(s) built offline; "
                  f"{len(source_state)} milestone(s) failed on SOURCE-STATE issues "
                  f"(NOT closure gaps; closure has the bytes): {source_state}"
                  f"{', pushed' if push else ''}.")
        else:
            print(f"SUCCESS: {latest_tag} published "
                  f"({len(milestones)} milestone gate(s) passed"
                  f"{', pushed' if push else ''}).")
    finally:
        if not keep:
            subprocess.run(["docker", "rmi", "-f", staging_tag],
                          capture_output=True, text=True)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Build <repo>/base-offline:staging (offline dependency closure).")
    ap.add_argument("--repo", required=True,
                    help="repo id, e.g. burntsushi_ripgrep_14.1.1_15.0.0")
    ap.add_argument("--push", action="store_true",
                    help="push the published base-offline:latest image")
    ap.add_argument("--keep-staging", action="store_true",
                    help="keep the staging image (default: removed after tagging)")
    args = ap.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    build_closure(args.repo.lower(), root, args.push, args.keep_staging)


if __name__ == "__main__":
    main()
