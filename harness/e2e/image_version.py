"""Single naming + version authority for benchmark Docker images.

Naming scheme (v1.0, policy: docs/versioning.md):

    local:  swe-milestone/<repo_full>__<milestone>:<tag>
    hub:    <org>/swe-milestone__<repo_full>__<milestone>:<tag>

The payload "<repo_full>__<milestone>" is byte-identical on both sides; the
only difference is the wrapper ("swe-milestone/" locally vs "<org>/swe-milestone__"
remotely). Parse safety rests on one invariant, enforced by validate_component:
repo_full and milestone never contain "__". "base" and "base-offline" are
ordinary milestones — no special-casing anywhere.

Version pinning: SWE_MILESTONE_IMAGE_TAG env var, default DEFAULT_IMAGE_TAG.
Resolution rules (deliberately loud, never silent):
- If <image>:<pinned> exists locally, use it.
- If the pin came from the DEFAULT (env var not set) and <image>:latest exists,
  fall back to :latest with a prominent warning. The content is NOT verified.
- If SWE_MILESTONE_IMAGE_TAG is set explicitly, never fall back: reproducibility
  runs must fail fast rather than grade against the wrong data version.

The ONLY legacy branch in the project lives in parse_local_ref(): pre-v1.0
trials recorded old-format names ("<repo_full>/<milestone>:<tag>") in
trial_metadata.json, and `run_e2e.py --resume` replays them verbatim; the
quarantine-config lookup must therefore still extract repo_full from old
names. Hub-side v0.9 naming gets NO compatibility code (docs/versioning.md).
"""

import os
import subprocess

DEFAULT_IMAGE_TAG = "v1.0"
PREFIX = "swe-milestone"
SEP = "__"


def validate_component(s: str) -> str:
    """Lowercase and validate a repo_full or milestone component.

    Rejects the characters that would break the mechanical local<->hub
    conversion: "__" (the separator), "/" and ":" (reference structure).
    Returns the lowercased component. Raises ValueError on violation.
    """
    if not s:
        raise ValueError("empty image-name component")
    s = s.lower()
    if SEP in s:
        raise ValueError(f"component {s!r} contains {SEP!r} (reserved separator)")
    if "/" in s or ":" in s:
        raise ValueError(f"component {s!r} contains '/' or ':'")
    return s


def local_ref(repo_full: str, milestone: str, tag: str | None = None) -> str:
    """Local image name: swe-milestone/<repo_full>__<milestone>[:<tag>]."""
    rf = validate_component(repo_full)
    ms = validate_component(milestone)
    base = f"{PREFIX}/{rf}{SEP}{ms}"
    return f"{base}:{tag}" if tag else base


def hub_ref(org: str, repo_full: str, milestone: str, tag: str) -> str:
    """DockerHub image name: <org>/swe-milestone__<repo_full>__<milestone>:<tag>."""
    rf = validate_component(repo_full)
    ms = validate_component(milestone)
    return f"{org}/{PREFIX}{SEP}{rf}{SEP}{ms}:{tag}"


def _split_tag(ref: str) -> tuple[str, str | None]:
    """Split a possibly-tagged reference into (name, tag|None)."""
    head, _, last = ref.rpartition("/")
    if ":" in last:
        name_last, _, tag = last.partition(":")
        name = f"{head}/{name_last}" if head else name_last
        return name, (tag or None)
    return ref, None


def local_to_hub(local: str, org: str) -> str:
    """Mechanical local -> hub conversion ("/" -> "__", org prefixed)."""
    repo_full, milestone = parse_local_ref(local, _strict=True)
    _, tag = _split_tag(local)
    if tag is None:
        raise ValueError(f"local ref {local!r} has no tag; hub refs must be tagged")
    return hub_ref(org, repo_full, milestone, tag)


def hub_to_local(hub: str) -> str:
    """Mechanical hub -> local conversion. Strict: 3 segments, prefix match."""
    name, tag = _split_tag(hub)
    org, slash, rest = name.partition("/")
    if not slash or not org or not rest:
        raise ValueError(f"hub ref {hub!r} lacks '<org>/' prefix")
    parts = rest.split(SEP)
    if len(parts) != 3 or parts[0] != PREFIX:
        raise ValueError(
            f"hub ref {hub!r} does not match <org>/{PREFIX}{SEP}<repo_full>{SEP}<milestone>"
        )
    return local_ref(parts[1], parts[2], tag)


def parse_local_ref(ref: str, _strict: bool = False) -> tuple[str, str]:
    """Extract (repo_full, milestone) from a local image reference.

    New format:  "swe-milestone/<rf>__<ms>[:tag]"  -> (rf, ms)
    Legacy (the ONLY compat branch, for resuming pre-v1.0 trials whose
    trial_metadata recorded old names):
                 "<rf>/<milestone...>[:tag]"       -> (rf, remainder)
    With _strict=True the legacy branch raises instead (used by local_to_hub,
    where old-format names must never silently produce hub refs).
    """
    name, _tag = _split_tag(ref)
    first, slash, rest = name.partition("/")
    if not slash or not first or not rest:
        raise ValueError(f"image ref {ref!r} has no '/' component")
    if first == PREFIX:
        rf, sep, ms = rest.partition(SEP)
        if not sep or not rf or not ms:
            raise ValueError(
                f"image ref {ref!r} lacks '{SEP}' between repo_full and milestone"
            )
        return rf, ms
    if _strict:
        raise ValueError(f"image ref {ref!r} is not in {PREFIX}/ format")
    return first, rest  # legacy


def _image_exists(ref: str) -> bool:
    return (
        subprocess.run(
            ["docker", "image", "inspect", ref],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def resolve_image(image_base: str) -> str:
    """Return a fully-tagged image ref for an untagged image name.

    image_base must not contain a tag (e.g. "swe-milestone/<rf>__milestone_001").
    """
    env_tag = os.environ.get("SWE_MILESTONE_IMAGE_TAG")
    tag = env_tag or DEFAULT_IMAGE_TAG
    ref = f"{image_base}:{tag}"
    if _image_exists(ref):
        return ref
    if env_tag is None and _image_exists(f"{image_base}:latest"):
        print(
            f"⚠️  WARNING: {ref} not found locally; falling back to "
            f"{image_base}:latest (content unverified — run "
            f"scripts/pull_images.sh or tag your images, see SWE_MILESTONE_IMAGE_TAG)"
        )
        return f"{image_base}:latest"
    # Let the caller fail with its normal image-not-found handling.
    return ref


# ──────────────────────────────────────────────
# Manifest (image inventory) + plan CLI
# ──────────────────────────────────────────────

from pathlib import Path


def default_manifest_path(version: str) -> Path:
    """<repo_root>/manifests/images-<version>.tsv (one file per benchmark version)."""
    return Path(__file__).resolve().parents[2] / "manifests" / f"images-{version}.tsv"


def load_manifest(path: Path) -> list[tuple[str, str, str]]:
    """Parse the inventory TSV -> [(short, repo_full, milestone), ...].

    Blank lines and '#' comments are skipped. Every row is validated through
    validate_component so a bad future addition fails here, loudly, not at
    docker-pull time.
    """
    rows = []
    for lineno, raw in enumerate(Path(path).read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            raise ValueError(f"{path}:{lineno}: expected 3 tab-separated columns, got {len(parts)}")
        short, repo_full, milestone = (p.strip() for p in parts)
        rows.append((short, validate_component(repo_full), validate_component(milestone)))
    if not rows:
        raise ValueError(f"{path}: manifest is empty")
    return rows


def _cli(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from harness.e2e.env_guard import reject_legacy_env
    reject_legacy_env()

    ap = argparse.ArgumentParser(
        prog="python3 -m harness.e2e.image_version",
        description="Emit hub<->local image plans from the version manifest. "
        "Output: one '<src>\\t<dst>' pair per line (consumed by "
        "scripts/pull_images.sh / push_images.sh).",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, hlp in (
        ("pull-plan", "lines: <hub_ref>\\t<local_ref> (docker pull + tag)"),
        ("push-plan", "lines: <local_ref>\\t<hub_ref> (docker tag + push)"),
        ("retag-plan", "lines: <old_local>\\t<new_local> (v1.0 release migration)"),
    ):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--org", default=os.environ.get("DOCKERHUB_ORG", "hyd2apse"))
        p.add_argument("--version", default=None,
                       help="benchmark data version (default: SWE_MILESTONE_IMAGE_TAG or "
                            f"{DEFAULT_IMAGE_TAG})")
        p.add_argument("--repo", action="append", default=[], metavar="SHORT",
                       help="filter by short name (repeatable)")
        p.add_argument("--manifest", type=Path, default=None)
        if name == "retag-plan":
            p.add_argument("--from-version", required=True,
                           help="source tag of existing OLD-format local images")
            p.add_argument("--base-offline-from", default=None,
                           help="source tag for base-offline rows (default: --from-version; "
                                "builders only produce :latest)")

    args = ap.parse_args(argv)
    version = args.version or os.environ.get("SWE_MILESTONE_IMAGE_TAG") or DEFAULT_IMAGE_TAG
    manifest = args.manifest or default_manifest_path(version)
    rows = load_manifest(manifest)

    known_shorts = {r[0] for r in rows}
    unknown = [s for s in args.repo if s not in known_shorts]
    if unknown:
        print(f"error: unknown --repo {unknown}; known: {sorted(known_shorts)}",
              file=sys.stderr)
        return 2
    if args.repo:
        rows = [r for r in rows if r[0] in args.repo]

    for _, rf, ms in rows:
        local = local_ref(rf, ms, version)
        hub = hub_ref(args.org, rf, ms, version)
        if args.cmd == "pull-plan":
            print(f"{hub}\t{local}")
        elif args.cmd == "push-plan":
            print(f"{local}\t{hub}")
        else:  # retag-plan: OLD-format source -> new-format destination
            src_tag = args.from_version
            if ms == "base-offline" and args.base_offline_from:
                src_tag = args.base_offline_from
            print(f"{rf}/{ms}:{src_tag}\t{local}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
