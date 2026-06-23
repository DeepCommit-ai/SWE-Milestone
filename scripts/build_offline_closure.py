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

def render_union_dockerfile(repo_lower: str, milestones: list[str], cache_paths: list[str]) -> str:
    lines = ["# syntax=docker/dockerfile:1", f"FROM {repo_lower}/base:latest AS builder",
             "RUN command -v rsync || (apt-get update && apt-get install -y rsync) || true"]
    for i, m in enumerate(milestones):
        for j, cp in enumerate(cache_paths):
            lines.append(f"COPY --from={m} {cp} /milestone_{i}_{j}{cp}")
    # rsync-merge each milestone subtree into /staging (same-bytes dedup is harmless)
    merge = " && ".join(
        f"rsync -a /milestone_{i}_{j}{cp}/ /staging{cp}/"
        for i in range(len(milestones)) for j, cp in enumerate(cache_paths))
    lines.append(f"RUN mkdir -p /staging && {merge or 'true'}")
    lines.append(f"FROM {repo_lower}/base:latest AS final")
    for cp in cache_paths:
        lines.append(f"COPY --from=builder /staging{cp} {cp}")
    return "\n".join(lines) + "\n"
