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
                              toolchain: dict | None = None) -> str:
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


def offline_gate_cmd(staging_image: str, milestone: str, offline_build: str) -> list[str]:
    # NOTE: /testbed inside the container should contain the B-source from `milestone`,
    # NOT the A-baseline baked into `staging_image`. This function returns the command
    # skeleton; the driver (Task 4.2/4.3) is responsible for injecting the milestone's
    # /testbed — either via `docker create <milestone>` + `docker cp <cid>:/testbed` +
    # `-v <host_path>:/testbed:ro`, or via `COPY --from=<milestone> /testbed /verify_testbed`
    # at assembly time. Until injection is wired, the staging image's own /testbed
    # (A-baseline) is used as a placeholder.
    return ["docker", "run", "--rm", "--network", "none", staging_image,
            "sh", "-c", f"cd /testbed && {offline_build}"]


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
        docker_run_argv = ["docker", "run", "--rm", "--network", "none"]
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
    r = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", staging_tag,
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

    Unlike the cache-COPY ecosystems (cargo/go/maven/npm union a milestone's raw
    cache), pip BUILDS its closure: a `wheel_builder` stage runs `pip download -r
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


def assemble_go_dockerfile(repo_lower: str, milestones: list[str],
                           cache_paths: list[str], target_go: str,
                           _probe=_probe_go_version) -> str:
    """Go closure Dockerfile: raw-cache rsync UNION of every milestone's go module
    cache (render_union_dockerfile) PLUS a clean-replaced newer go toolchain in the
    final stage.

    go-zero's B-source declares `go 1.21`, but base ships go1.19.13. The target
    toolchain (1.21.13) is baked into the B-milestone images at /usr/local/go, so
    the final stage gets:
        RUN rm -rf /usr/local/go
        COPY --from=<verified milestone> /usr/local/go /usr/local/go
        ENV GOTOOLCHAIN=local
    The `rm -rf /usr/local/go` MUST precede the COPY: COPY merges into an existing
    directory, so without the rm the milestone's go1.21.13 tree is overlaid ON TOP
    of the base's go1.19.13 tree — files present in 1.19 but renamed/removed in 1.21
    survive, yielding a mixed stdlib that breaks `go build` (empirically
    `runtime/internal/sys: m0 redeclared in this block`). Clean-replace removes the
    base toolchain first so the result is a pristine 1.21.13.
    GOTOOLCHAIN=local is REQUIRED: without it a `go 1.21` directive makes go try to
    auto-download a matching toolchain from proxy.golang.org → fails under
    --network none.

    No `.info`-sidecar synthesis: the build-scoped gate (`go build -mod=mod ./...`,
    see the go-zero quarantine config) resolves what it needs straight from the
    rsync'd cache. An earlier whole-graph `go mod download` gate needed synthesised
    `<v>.info` sidecars for version resolution, but switching to the build-scoped
    gate made them unnecessary — empirically the per-milestone gate passes
    identically with and without synthesis (21/23 build offline either way; the 2
    failures are source-state, not missing `.info`). The module cache never
    contains the repo's own module (github.com/zeromicro/*), so the self-exclusion
    audit stays clean by construction.
    """
    df = render_union_dockerfile(repo_lower, milestones, cache_paths)
    tc = pick_go_toolchain_milestone(milestones, target_go, _probe=_probe)
    # render_union_dockerfile's last stage is `FROM ... AS final`; append the
    # clean-replace toolchain layers to that final stage (the rendered file ends
    # with a trailing newline). The `rm -rf` MUST come before the COPY so the new
    # toolchain replaces (not overlays) the base's /usr/local/go — an overlay mixes
    # go1.19 + go1.21 stdlib and `go build` fails (m0 redeclared).
    tail = ("RUN rm -rf /usr/local/go\n"
            f"COPY --from={tc} /usr/local/go /usr/local/go\n"
            "ENV GOTOOLCHAIN=local\n")
    return df + tail


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


def assemble_maven_dockerfile(repo_lower: str, milestones: list[str],
                              cache_paths: list[str],
                              forbid_globs: list[str]) -> str:
    """Maven closure Dockerfile: raw-cache rsync UNION of every milestone's `.m2`
    repository (`render_union_dockerfile`) PLUS a critical self@B removal in the
    final stage.

    Like go/pip, the union ADDS the milestone `.m2` deps on top of the base image's
    own A-version `.m2`, so the cache spans A→B (no A-baseline gap like cargo's
    vendor-replace had). The one maven-specific step is removing the repo's OWN
    target-version artifacts: the milestone `.m2` caches contain dubbo's
    `org/apache/dubbo/<mod>/3.3.6-SNAPSHOT/*.jar` + `*-sources.jar` (the answer the
    agents copied from Maven Central). `render_union_dockerfile`'s last stage is
    `FROM <repo>/base:latest AS final`; we append the self@B rm (derived from the
    config's `cache_forbid_globs`) AFTER the cache COPY so the published image cannot
    serve the answer offline and the subsequent `audit_staging_image` (running the
    same globs) finds nothing.
    """
    df = render_union_dockerfile(repo_lower, milestones, cache_paths)
    return df + maven_rm_self_at_b_cmd(forbid_globs)


# --------------------------------------------------------------------------- #
# Driver (Task 4.2): through the staging build only. Audit + offline-gate +    #
# tag/publish are Task 4.3 and are intentionally NOT done here.                #
# --------------------------------------------------------------------------- #

def _ecosystem_of(repo_lower: str, project_root: Path) -> str:
    """Top-level `ecosystem` from the quarantine yaml, as a single lowercase id.

    Accepts a scalar or a one-element list (the configs use `ecosystem: [cargo]`).
    Multi-ecosystem repos are out of scope for the closure builder.
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
    eco = _ecosystem_of(repo_lower, project_root)
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

    try:
        if eco == "cargo":
            # closure.toolchain.rust (optional): some repos pin a rust channel
            # (nushell → 1.88.0) the base image lacks; assemble_cargo_dockerfile
            # then installs it ONLINE in the final stage. Absent → no rust-install
            # line (ripgrep unchanged).
            df = assemble_cargo_dockerfile(repo_lower, milestones,
                                           toolchain=cfg.get("toolchain"))
            _docker_build(df, staging_tag, project_root)
        elif eco == "go":
            # Raw-cache rsync UNION of every milestone's go module cache, plus the
            # newer go toolchain (COPY /usr/local/go from a milestone that reports
            # the target version) + GOTOOLCHAIN=local in the final stage.
            cache_paths = cfg["cache_paths"]
            toolchain = cfg.get("toolchain") or {}
            target_go = toolchain.get("go")
            if not target_go:
                print(f"Error: {repo_lower}: go ecosystem needs closure.toolchain.go "
                      f"(target go version)", file=sys.stderr)
                sys.exit(1)
            df = assemble_go_dockerfile(repo_lower, milestones, cache_paths, target_go)
            _docker_build(df, staging_tag, project_root)
        elif eco == "maven":
            # Raw-cache rsync UNION of every milestone's `.m2` repository
            # (render_union_dockerfile), then REMOVE self@B in the final stage: the
            # milestone `.m2` caches carry dubbo's OWN target-version artifacts
            # (org/apache/dubbo/<mod>/3.3.6-SNAPSHOT/*.jar + *-sources.jar — the
            # answer agents copied from Maven Central). The rm targets are EXACTLY
            # the config's cache_forbid_globs, so the generic audit below (same
            # globs) then matches nothing. Like go/pip the union ADDS the milestone
            # `.m2` to the base image's A-version `.m2`, so it spans A→B (no
            # A-baseline gap like cargo's vendor-replace had); the generic
            # per-milestone B-source gate (mvn -o test-compile) applies unchanged.
            cache_paths = cfg["cache_paths"]
            df = assemble_maven_dockerfile(repo_lower, milestones, cache_paths,
                                           forbid_globs)
            _docker_build(df, staging_tag, project_root)
        elif eco == "npm":
            # Raw-cache rsync UNION of every milestone's yarn cache
            # (render_union_dockerfile). Like go/maven the union ADDS the milestone
            # yarn caches on top of the base image's OWN yarn cache, so it spans A→B
            # (no A-baseline gap like cargo's vendor-replace had). No toolchain step
            # (yarn ships in the base) and NO self@B removal: element-web's app source
            # is not published to npm, so there is no self@B tarball to strip
            # (cache_forbid_globs is empty → the generic audit below is a clean
            # no-op). The per-milestone B-source gate (yarn install --offline
            # --frozen-lockfile) applies unchanged, with the npm/yarn classifier.
            cache_paths = cfg["cache_paths"]
            df = render_union_dockerfile(repo_lower, milestones, cache_paths)
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
        if eco == "maven":
            gate_classifier = classify_maven_offline_build_failure(forbid_globs)
        elif eco == "npm":
            gate_classifier = classify_npm_offline_build_failure
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
                    help="(Task 4.3) push the published image")
    ap.add_argument("--keep-staging", action="store_true",
                    help="keep the staging image (default: kept; cleanup is Task 4.3)")
    args = ap.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    build_closure(args.repo.lower(), root, args.push, args.keep_staging)


if __name__ == "__main__":
    main()
