#!/usr/bin/env python3
"""Verify benchmark image content identity against the digest manifest.

The digest manifest (manifests/digests-<version>.tsv) freezes the byte-level
identity of every image that makes up a benchmark data version. Tags are
mutable pointers; digests are content hashes. This script reconciles the
three parties that can drift apart:

  --local  local <name>:<version> images vs the manifest.
           Catches "rebuilt/retagged locally but the manifest (and likely the
           Hub) never followed" — run BEFORE pushing images.
           Requires the images to exist locally (an evaluation machine).

  --hub    Docker Hub's <version> tags vs the manifest.
           Catches "the Hub tag was replaced but the manifest never recorded
           it" and "the manifest was updated but the image was never pushed".
           Needs only network access — this is the CI check.

  --all    both.

Exit code 0 = fully consistent; 1 = any mismatch (CI-friendly).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_manifest(version: str) -> list[tuple[str, str, str]]:
    """Return (local_ref, hub_name, digest) rows from the digest manifest."""
    path = REPO_ROOT / "manifests" / f"digests-{version}.tsv"
    if not path.exists():
        sys.exit(f"error: digest manifest not found: {path}")
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        local, hub = line.split("\t")
        hub_name, digest = hub.split("@")
        rows.append((local, hub_name, digest))
    return rows


def check_local(rows, version: str) -> list[str]:
    """Local images must carry the manifest digest in their RepoDigests."""
    problems = []
    for local, hub_name, digest in rows:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{json .RepoDigests}}", local],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            problems.append(f"LOCAL-MISSING   {local}")
        elif f"{hub_name}@{digest}" not in r.stdout:
            problems.append(f"LOCAL-MISMATCH  {local} (rebuilt/retagged without a manifest update?)")
    return problems


_DIGEST_RE = re.compile(r"Digest:\s+(sha256:[0-9a-f]{64})")


def _hub_digest(hub_ref: str) -> str | None:
    r = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", hub_ref],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    m = _DIGEST_RE.search(r.stdout)
    return m.group(1) if m else None


def check_hub(rows, version: str) -> list[str]:
    """Hub's <version> tag must still point at the manifest digest."""
    problems = []

    def one(row):
        local, hub_name, digest = row
        actual = _hub_digest(f"{hub_name}:{version}")
        if actual is None:
            return f"HUB-UNREACHABLE {hub_name}:{version}"
        if actual != digest:
            return (f"HUB-MISMATCH    {hub_name}:{version} "
                    f"manifest={digest[:19]}… hub={actual[:19]}…")
        return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        for problem in pool.map(one, rows):
            if problem:
                problems.append(problem)
    return problems


def main() -> int:
    default_version = (REPO_ROOT / "manifests" / "BENCHMARK_VERSION").read_text().strip()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", default=default_version,
                    help=f"benchmark data version (default from manifests/BENCHMARK_VERSION: {default_version})")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--local", action="store_true", help="verify local images vs manifest")
    mode.add_argument("--hub", action="store_true", help="verify Docker Hub tags vs manifest")
    mode.add_argument("--all", action="store_true", help="both checks")
    args = ap.parse_args()

    rows = load_manifest(args.version)
    problems: list[str] = []
    if args.local or args.all:
        problems += check_local(rows, args.version)
    if args.hub or args.all:
        problems += check_hub(rows, args.version)

    if problems:
        print(f"✗ {len(problems)} inconsistencies across {len(rows)} manifest rows:")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"✓ {len(rows)} images consistent with digests-{args.version}.tsv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
