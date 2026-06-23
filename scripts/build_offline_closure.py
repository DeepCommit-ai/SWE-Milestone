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

def load_closure_config(repo_lower: str, project_root: Path) -> dict:
    conf = Path(project_root) / "quarantine_configs" / f"{repo_lower}.yaml"
    if not conf.exists():
        print(f"Error: no quarantine config {conf}", file=sys.stderr); sys.exit(1)
    data = yaml.safe_load(conf.read_text()) or {}
    closure = data.get("closure")
    if not closure or "cache_paths" not in closure or "offline_build" not in closure:
        print(f"Error: {conf}: closure block must have cache_paths and offline_build", file=sys.stderr); sys.exit(1)
    return closure

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
    return f"cargo vendor --versioned-dirs {syncs} {vendor_dir}"


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
