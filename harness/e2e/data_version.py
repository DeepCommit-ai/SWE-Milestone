"""Benchmark data-version pinning — the data half of docs/versioning.md.

The benchmark version axis is the image tag (``SWE_MILESTONE_IMAGE_TAG``,
default ``DEFAULT_IMAGE_TAG`` in image_version.py). resolve_image() pins the
image side; this module pins the other score-moving input: the workspace
data checkout (the SWE-Milestone-data git clone). The data repo carries the
same ``vX.Y`` tags as the images; a run is "on version vX.Y" iff the data
HEAD is the commit that tag points at.

Semantics deliberately mirror resolve_image():
- env var unset (default pin): any non-match prints a loud warning and the
  run continues — day-to-day development must not be blocked;
- ``SWE_MILESTONE_IMAGE_TAG`` set explicitly: a reproducibility run — any
  non-match or unverifiable state refuses the launch;
- ``SWE_MILESTONE_DATA_VERSION_CHECK=off``: explicit escape hatch, recorded
  as ``checked: false`` in trial metadata so the trial is honest about it.

Verification is a runtime fact check against the data repo's git object
database (read-only ``git rev-parse``) — never a trust-the-declaration
shortcut: a VERSION file can go stale, refs cannot.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

from harness.e2e.image_version import DEFAULT_IMAGE_TAG

VERSION_ENV = "SWE_MILESTONE_IMAGE_TAG"
DATA_VERSION_CHECK_ENV = "SWE_MILESTONE_DATA_VERSION_CHECK"


def expected_benchmark_version() -> Tuple[str, bool]:
    """Return (version tag, pinned_explicitly)."""
    env_tag = os.environ.get(VERSION_ENV)
    return (env_tag or DEFAULT_IMAGE_TAG, env_tag is not None)


def _git(cwd: Path, *args: str) -> Optional[str]:
    """Read-only git query; None on any failure (missing git, not a repo...)."""
    try:
        res = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    out = res.stdout.strip()
    return out or None


def inspect_data_version(data_path: Path) -> Dict:
    """Compare the data checkout containing ``data_path`` against the version tag.

    ``data_path`` may be the data root or any path inside it (e.g. a repo
    workspace_root); the enclosing git toplevel is located first (rev-parse
    searches upward, so nested trial/testbed repos below are never picked up).

    Returns {state, commit, tag_commit, expected_tag, explicit_pin}
    with state one of: match | mismatch | tag-missing | not-a-git-repo.
    """
    tag, explicit = expected_benchmark_version()
    info: Dict = {
        "state": "not-a-git-repo",
        "commit": None,
        "tag_commit": None,
        "expected_tag": tag,
        "explicit_pin": explicit,
    }
    top = _git(data_path, "rev-parse", "--show-toplevel")
    head = _git(data_path, "rev-parse", "HEAD^{commit}") if top else None
    if not top or not head:
        # Unborn/corrupt repos verify exactly like plain directories: they
        # cannot prove a version.
        return info
    info["commit"] = head
    tag_commit = _git(data_path, "rev-parse", f"refs/tags/{tag}^{{commit}}")
    if tag_commit is None:
        info["state"] = "tag-missing"
    elif tag_commit == head:
        info["state"] = "match"
        info["tag_commit"] = tag_commit
    else:
        info["state"] = "mismatch"
        info["tag_commit"] = tag_commit
    return info


def _refuse_or_warn(
    context: str,
    tag: str,
    explicit: bool,
    detail: str,
    *,
    fatal_remedy: str = "",
    warn_remedy: str = "",
) -> None:
    """Shared enforcement voice: explicit pin refuses, default pin warns loud."""
    if explicit:
        sys.exit(
            f"[{context}] ERROR: {VERSION_ENV}={tag} is an explicit "
            f"reproducibility pin, but {detail}. {fatal_remedy}".rstrip()
        )
    print(
        f"⚠️  [{context}] WARNING: {detail}; scores may not be comparable "
        f"to {tag}. {warn_remedy}".rstrip()
    )


def _describe(info: Dict, data_path: Path) -> str:
    state = info["state"]
    tag = info["expected_tag"]
    if state == "mismatch":
        return (
            f"HEAD {info['commit'][:12]} is not tag {tag} "
            f"({info['tag_commit'][:12]})"
        )
    if state == "tag-missing":
        return f"tag {tag} does not exist in the data repo"
    return f"{data_path} is not a git checkout, so its version cannot be verified"


def check_data_version(data_path: Path, *, context: str) -> Dict:
    """Verify the data checkout against the benchmark version; fail/warn loud.

    Returns the trial-metadata fragment:
        {"benchmark_version": <tag>,
         "data_version": {state, commit, expected_tag, explicit_pin, checked}}
    """
    tag, explicit = expected_benchmark_version()
    if os.environ.get(DATA_VERSION_CHECK_ENV, "").lower() == "off":
        print(
            f"[{context}] NOTE: {DATA_VERSION_CHECK_ENV}=off — benchmark data "
            f"version NOT verified against {tag}; recorded as unchecked."
        )
        return {
            "benchmark_version": tag,
            "data_version": {
                "state": "unchecked",
                "commit": None,
                "expected_tag": tag,
                "explicit_pin": explicit,
                "checked": False,
            },
        }

    info = inspect_data_version(data_path)
    if info["state"] != "match":
        _refuse_or_warn(
            context,
            tag,
            explicit,
            f"the benchmark data checkout does not verify: {_describe(info, data_path)}",
            fatal_remedy=(
                f"Align the data first (scripts/pull_data.sh --checkout, see "
                f"docs/versioning.md) or set {DATA_VERSION_CHECK_ENV}=off to "
                f"run unverified."
            ),
            warn_remedy=(
                f"Align with scripts/pull_data.sh --checkout, or set "
                f"{VERSION_ENV} explicitly to make this fatal."
            ),
        )
    return {
        "benchmark_version": tag,
        "data_version": {
            "state": info["state"],
            "commit": info["commit"],
            # Deliberately repeats benchmark_version so this block stays
            # self-contained when consumed on its own.
            "expected_tag": tag,
            "explicit_pin": explicit,
            "checked": True,
        },
    }


def check_image_tag_consistency(image: str, *, context: str) -> Dict:
    """Check an image reference's tag against the benchmark version.

    Warns (default pin) or refuses (explicit pin) on a tag mismatch. A
    digest-pinned reference (``name@sha256:...``) is accepted as-is: a digest
    is a stronger, immutable pin than any tag. The normal path never trips
    this — resolve_image() emits version-tagged refs — but --image is
    caller-supplied and a hand-built or :latest-fallback ref must not
    masquerade as the pinned version silently.

    Returns the trial-metadata fragment
        {image, observed_tag, expected_tag, state, explicit_pin}
    with state one of: match | mismatch | digest-pinned — so a fallback is a
    structured, queryable fact rather than only a transient launch warning.
    """
    tag, explicit = expected_benchmark_version()
    name = image.split("@", 1)[0]
    digest_pinned = "@" in image
    _, sep, img_tag = name.rpartition(":")
    if not sep or "/" in img_tag:
        img_tag = None  # untagged reference
    if digest_pinned:
        state = "digest-pinned"
    elif img_tag == tag:
        state = "match"
    else:
        state = "mismatch"
        _refuse_or_warn(
            context,
            tag,
            explicit,
            f"image {image!r} is tagged {img_tag!r}, benchmark version is {tag}",
            fatal_remedy="Refusing to launch.",
        )
    return {
        "image": image,
        "observed_tag": img_tag,
        "expected_tag": tag,
        "state": state,
        "explicit_pin": explicit,
    }
