"""Unified offline closure builder. Union all milestone images' deps into a
self-contained <repo>/base-offline:latest. See
docs/superpowers/specs/2026-06-23-offline-closure-builder-design.md."""
import argparse, subprocess, sys, yaml
from pathlib import Path
from typing import Optional

def discover_milestone_images(repo_lower: str, _docker_images: Optional[str] = None) -> list:
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
            seen[name] = f"{prefix}{name}:{tag if tag=='latest' else tag}"
    # 同 milestone 若有 latest 用 latest
    out = []
    for name in sorted(seen):
        out.append(f"{prefix}{name}:latest" if _has_latest(_docker_images, prefix, name) else seen[name])
    return out

def _has_latest(images: str, prefix: str, name: str) -> bool:
    return f"{prefix}{name}:latest" in images

def load_closure_config(repo_lower: str, project_root: Path) -> dict:
    conf = Path(project_root) / "quarantine_configs" / f"{repo_lower}.yaml"
    if not conf.exists():
        print(f"Error: no quarantine config {conf}", file=sys.stderr); sys.exit(1)
    data = yaml.safe_load(conf.read_text()) or {}
    closure = data.get("closure")
    if not closure or not closure.get("cache_paths"):
        print(f"Error: {conf}: missing 'closure.cache_paths'", file=sys.stderr); sys.exit(1)
    return closure
