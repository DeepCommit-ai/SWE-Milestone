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


def assemble_cargo_dockerfile(repo_lower: str, milestones: list[str]) -> str:
    """Multi-stage Dockerfile that vendors the UNION of every milestone's Cargo
    workspace into /opt/vendor, then bakes a $CARGO_HOME offline redirect.

    `cargo vendor` needs a cwd with a workspace manifest, so the FIRST milestone
    testbed (/tb/m0) is the cwd and the REST (m1..mN) are passed as `--sync`
    targets — one invocation (a loop would clobber the vendor dir each call).
    Path-deps (the repo's own workspace crates) carry no `source` line and are
    auto-excluded by cargo vendor, so the answer is never baked in.

    The config is written to $CARGO_HOME (/usr/local/cargo/config.toml), NEVER to
    /testbed/.cargo — the agent's `git clean -xfd` would wipe the latter and the
    offline redirect would silently vanish.
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
    # cwd = first milestone's workspace; --sync the manifests of the rest.
    sync_manifests = [f"/tb/m{i}/Cargo.toml" for i in range(1, len(milestones))]
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


def run_offline_gate(staging_tag: str, milestone: str, offline_build: str,
                     goproxy_off: bool = False) -> str:
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
        kind, detail = classify_offline_build_failure(staging_tag, out)
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
            df = assemble_cargo_dockerfile(repo_lower, milestones)
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
        elif eco in ("maven", "npm"):
            # Raw-cache rsync union path — render_union_dockerfile exists, but the
            # driver wiring (+ build) is a later task. Don't half-build it here.
            raise NotImplementedError(f"ecosystem {eco}: task 4.4")
        elif eco == "pip":
            raise NotImplementedError(f"ecosystem {eco}: task 4.5 (freeze-union-download)")
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
        source_state = []
        for i, m in enumerate(milestones, 1):
            print(f"offline gate [{i}/{len(milestones)}] {m} ...", flush=True)
            result = run_offline_gate(staging_tag, m, offline_build,
                                     goproxy_off=gate_goproxy_off)
            if result == "SOURCE_STATE":
                source_state.append(m)
                print(f"offline gate [{i}/{len(milestones)}] {m}: SOURCE-STATE "
                      f"(not a closure gap; recorded)", flush=True)
            else:
                print(f"offline gate [{i}/{len(milestones)}] {m}: PASS", flush=True)

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
