"""Unified offline closure builder. Union all milestone images' deps into a
self-contained <repo>/base-offline:latest. See
docs/superpowers/specs/2026-06-23-offline-closure-builder-design.md."""
import argparse, subprocess, sys, yaml
from pathlib import Path

def load_closure_config(repo_lower: str, project_root: Path) -> dict:
    conf = Path(project_root) / "quarantine_configs" / f"{repo_lower}.yaml"
    if not conf.exists():
        print(f"Error: no quarantine config {conf}", file=sys.stderr); sys.exit(1)
    data = yaml.safe_load(conf.read_text()) or {}
    closure = data.get("closure")
    if not closure or not closure.get("cache_paths"):
        print(f"Error: {conf}: missing 'closure.cache_paths'", file=sys.stderr); sys.exit(1)
    return closure
