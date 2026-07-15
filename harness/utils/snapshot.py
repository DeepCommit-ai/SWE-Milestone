"""Snapshot creation utilities.

Build manifests do not use the ordinary additive source overlay:

* the agent's pre-task commit is the BASE;
* the milestone evaluator checkout is END;
* Maven manifests changed by AGENT relative to BASE are copied over END and
  deletions are recorded as tombstones;
* scoped Go manifests are an exact projection of AGENT, including explicit
  absence, so END cannot silently supply a different module graph.

This distinction matters for prepared milestone images.  Their END commits can
contain evaluator-owned POM rewrites that must survive when the agent did not
touch the corresponding POM.
"""

from dataclasses import dataclass
import hashlib
from pathlib import PurePosixPath
from pathlib import Path
from typing import Any, FrozenSet, Iterable, List, Mapping, Optional, Set


SNAPSHOT_METADATA_SCHEMA_VERSION = 2
MANIFEST_OVERLAY_SCHEMA_VERSION = 1
GO_MANIFEST_PROJECTION_SCHEMA_VERSION = 1
GO_MANIFEST_BASENAMES: FrozenSet[str] = frozenset(
    {"go.mod", "go.sum", "go.work", "go.work.sum"}
)

# Root build/config files to include in snapshots.
# These files define workspace/module dependencies and version numbers.
# Including them ensures agent's code is self-consistent (e.g., crate versions
# match root Cargo.toml). SRS constrains agents not to add new external
# dependencies, so base image's pre-installed deps remain sufficient.
ROOT_BUILD_FILES: List[str] = [
    # Rust
    "Cargo.toml",
    "Cargo.lock",
    # Go
    "go.mod",
    "go.sum",
    "go.work",
    "go.work.sum",
    # Maven
    "pom.xml",
    ".mvn/extensions.xml",
    ".mvn/maven.config",
    ".mvn/jvm.config",
]

# Maven and Go metadata can be distributed across modules outside the ordinary
# source roots (for example ``tools/goctl/go.mod``).  Recursive Go manifests
# are filtered against authoritative test/testdata paths by capture callers;
# Maven POMs retain their existing reactor semantics.
RECURSIVE_BUILD_MANIFEST_NAMES: Set[str] = {
    "pom.xml",
    "go.mod",
    "go.sum",
    "go.work",
    "go.work.sum",
}

# These basenames are one logical package-manager state within their directory
# even though Git observes independent byte changes.
ATOMIC_ROOT_MANIFEST_GROUPS: tuple[FrozenSet[str], ...] = (
    frozenset({"go.mod", "go.sum"}),
    frozenset({"go.work", "go.work.sum"}),
)


def normalize_snapshot_path(filepath: str) -> str:
    """Return a safe repository-relative POSIX path, or raise ``ValueError``.

    Snapshot metadata is later turned into ``rm`` targets inside an evaluator
    container.  Silently accepting absolute paths, ``..`` traversal, NULs, or
    platform-specific separators would turn a malformed sidecar into a deletion
    primitive.  Git paths are POSIX paths in this harness, so reject ambiguity.
    """
    if not isinstance(filepath, str):
        raise ValueError("snapshot path must be a string")
    if "\x00" in filepath or "\\" in filepath:
        raise ValueError(f"unsafe snapshot path: {filepath!r}")
    normalized = filepath.removeprefix("./").rstrip("/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or normalized.startswith("/")
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ValueError(f"unsafe snapshot path: {filepath!r}")
    return normalized


def _normalize_manifest_set(paths: Iterable[str], *, field: str) -> FrozenSet[str]:
    normalized: Set[str] = set()
    seen = 0
    for raw in paths:
        seen += 1
        path = normalize_snapshot_path(raw)
        if not is_build_manifest(path):
            raise ValueError(f"{field} contains a non-manifest path: {path}")
        normalized.add(path)
    if len(normalized) != seen:
        raise ValueError(f"{field} contains duplicate paths")
    return frozenset(normalized)


@dataclass(frozen=True)
class ManifestOverlay:
    """Agent-authoritative build-manifest delta relative to an explicit BASE."""

    baseline_commit: str
    upserts: FrozenSet[str]
    deletes: FrozenSet[str]

    @classmethod
    def create(
        cls,
        baseline_commit: str,
        upserts: Iterable[str] = (),
        deletes: Iterable[str] = (),
    ) -> "ManifestOverlay":
        baseline = str(baseline_commit).strip()
        if not baseline:
            raise ValueError("manifest overlay baseline_commit is empty")
        normalized_upserts = _normalize_manifest_set(upserts, field="manifest upserts")
        normalized_deletes = _normalize_manifest_set(deletes, field="manifest deletes")
        overlap = normalized_upserts & normalized_deletes
        if overlap:
            raise ValueError(
                f"manifest paths cannot be both upserted and deleted: {sorted(overlap)}"
            )
        return cls(baseline, normalized_upserts, normalized_deletes)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "schema_version": MANIFEST_OVERLAY_SCHEMA_VERSION,
            "baseline_commit": self.baseline_commit,
            "upserts": sorted(self.upserts),
            "deletes": sorted(self.deletes),
        }

    @classmethod
    def from_metadata(cls, value: object) -> "ManifestOverlay":
        if not isinstance(value, Mapping):
            raise ValueError("manifest_overlay must be an object")
        if value.get("schema_version") != MANIFEST_OVERLAY_SCHEMA_VERSION:
            raise ValueError(
                "unsupported manifest_overlay schema_version: "
                f"{value.get('schema_version')!r}"
            )
        upserts = value.get("upserts")
        deletes = value.get("deletes")
        if not isinstance(upserts, list) or not all(isinstance(p, str) for p in upserts):
            raise ValueError("manifest_overlay.upserts must be a list of strings")
        if not isinstance(deletes, list) or not all(isinstance(p, str) for p in deletes):
            raise ValueError("manifest_overlay.deletes must be a list of strings")
        baseline = value.get("baseline_commit")
        if not isinstance(baseline, str):
            raise ValueError("manifest_overlay.baseline_commit must be a string")
        return cls.create(baseline, upserts, deletes)


def expand_atomic_manifest_overlay(
    overlay: ManifestOverlay,
    existing_paths: Iterable[str],
    source_dirs: Optional[Iterable[str]] = None,
) -> ManifestOverlay:
    """Project the submitted tree's scoped, exact Go manifest state.

    Go manifests are always captured, even when their bytes did not change.
    That prevents a prepared END manifest from supplying a dependency the
    agent never declared. Root manifests are always in scope. A nested module
    is in scope only when its source directory is one of ``source_dirs``;
    capturing a nested manifest without its source would create another mixed
    tree. Repositories that evaluate such a module must therefore declare its
    directory in ``repo_src_dirs``. Explicit deletions remain tombstones.
    """
    roots = tuple(source_dirs or ())
    existing = {
        normalize_snapshot_path(path)
        for path in existing_paths
        if is_build_manifest(path)
    }
    upserts = {
        path
        for path in overlay.upserts
        if not is_go_build_manifest(path) or is_go_manifest_in_scope(path, roots)
    }
    deletes = {
        path
        for path in overlay.deletes
        if not is_go_build_manifest(path) or is_go_manifest_in_scope(path, roots)
    }
    go_existing = {
        path
        for path in existing
        if is_go_manifest_in_scope(path, roots)
    }
    upserts.update(go_existing)
    deletes.difference_update(go_existing)
    return ManifestOverlay.create(overlay.baseline_commit, upserts, deletes)


def snapshot_sha256(path: Path) -> str:
    """Hash a completed snapshot so its sidecar cannot be mixed with another tar."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_snapshot_metadata(
    *,
    tag: str,
    snapshot_file: Path,
    manifest_overlay: ManifestOverlay,
    extra: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Create the versioned, tar-bound capture sidecar payload."""
    data: dict[str, Any] = dict(extra or {})
    data.update(
        {
            "schema_version": SNAPSHOT_METADATA_SCHEMA_VERSION,
            "tag": tag,
            "snapshot_sha256": snapshot_sha256(snapshot_file),
            "manifest_overlay": manifest_overlay.to_metadata(),
            # Unlike a delta, this explicit present-set also makes absence
            # authoritative. The evaluator removes scoped END/START Go
            # manifests not listed here before it resolves the module graph.
            "go_manifest_projection": {
                "schema_version": GO_MANIFEST_PROJECTION_SCHEMA_VERSION,
                "present": sorted(
                    path
                    for path in manifest_overlay.upserts
                    if is_go_build_manifest(path)
                ),
            },
            # Compatibility for diagnostics written by the v1 capture path.
            "build_manifests": sorted(manifest_overlay.upserts),
        }
    )
    return data


def is_build_manifest(filepath: str) -> bool:
    """Whether *filepath* is an agent-authoritative build manifest.

    Rust/workspace metadata is rooted. Maven ``pom.xml`` and Go module
    manifests are recursive because repositories may contain nested modules.
    """
    try:
        normalized = normalize_snapshot_path(filepath)
    except ValueError:
        return False
    if normalized in ROOT_BUILD_FILES:
        return True
    return PurePosixPath(normalized).name in RECURSIVE_BUILD_MANIFEST_NAMES


def is_go_build_manifest(filepath: str) -> bool:
    """Whether *filepath* is Go module/workspace metadata."""
    try:
        normalized = normalize_snapshot_path(filepath)
    except (TypeError, ValueError):
        return False
    return PurePosixPath(normalized).name in GO_MANIFEST_BASENAMES


def is_go_manifest_in_scope(
    filepath: str,
    source_dirs: Iterable[str],
) -> bool:
    """Whether a Go manifest has a matching source-authority channel.

    Root module/workspace files are always part of the submitted repository
    state. Nested modules are included only below a configured source root so
    their manifest is never projected over unrelated evaluator-owned source.
    """
    if not is_go_build_manifest(filepath):
        return False
    normalized = normalize_snapshot_path(filepath)
    if "/" not in normalized:
        return True
    roots = {
        normalize_snapshot_path(str(root).rstrip("/"))
        for root in source_dirs
        if str(root).strip().strip("/")
    }
    return any(normalized.startswith(root + "/") for root in roots)


def find_build_manifests(paths: Iterable[str], protected_filter=None) -> Set[str]:
    """Select normalized build manifests, excluding protected Go test fixtures.

    ``protected_filter`` is intentionally duck-typed to avoid coupling this
    utility module to ``SrcFileFilter``.  Maven POMs keep their reactor behavior;
    only recursive Go manifests under authoritative test/testdata paths are
    excluded from the agent snapshot channel.
    """
    manifests: Set[str] = set()
    for path in paths:
        try:
            normalized = normalize_snapshot_path(path)
        except (TypeError, ValueError):
            continue
        if not is_build_manifest(normalized):
            continue
        basename = PurePosixPath(normalized).name
        if (
            protected_filter is not None
            and basename in GO_MANIFEST_BASENAMES
            and (
                protected_filter.is_test_file(normalized)
                or protected_filter.is_excluded(normalized)
            )
        ):
            continue
        if is_build_manifest(normalized):
            manifests.add(normalized)
    return manifests


def should_include_snapshot_file(
    filepath: str,
    src_filter,
    extra_build_manifests: Optional[Set[str]] = None,
) -> bool:
    """Keep ordinary source files plus explicitly captured build manifests.

    Every build manifest, including a root ``go.mod``/``Cargo.toml`` and a POM
    nested under an ordinary source directory, bypasses the ordinary source
    decision.  Only explicit three-way-overlay upserts are retained.  Letting an
    unchanged POM fall through to ``SrcFileFilter`` was the stale-POM pollution
    bug: a broad source directory caused it to overwrite milestone END anyway.
    """
    try:
        normalized = normalize_snapshot_path(filepath)
    except ValueError:
        return False
    explicit = extra_build_manifests or set()
    if is_build_manifest(normalized):
        return normalized in explicit
    return src_filter.should_include_in_snapshot(normalized)


def get_snapshot_paths(
    repo_src_dirs: List[str],
    existing_root_files: Optional[Set[str]] = None,
    existing_src_dirs: Optional[Set[str]] = None,
    extra_build_manifests: Optional[Set[str]] = None,
) -> List[str]:
    """Get all paths to include in snapshot.

    Includes source directories and the build manifests selected by the caller's
    overlay policy. Maven normally contributes only manifests changed since the
    agent baseline. Scoped Go manifests contribute the exact submitted set, so
    unchanged Go metadata is included too.

    Args:
        repo_src_dirs: Source directories (e.g., ["src/", "crates/"])
        existing_root_files: Optional set of root build files that exist.  Used
            to guard explicit root-manifest upserts against capture races.
        existing_src_dirs: Optional set of source directories that exist.
            If provided, only directories in this set are included.
            If None, all source directories are included (legacy behavior).
        extra_build_manifests: Optional exact set of build manifests selected
            by the capture overlay. Manifests already covered by a selected
            source directory are not added twice.

    Returns:
        List of paths for git archive command
    """
    # Filter source directories if existence check was performed
    if existing_src_dirs is not None:
        paths = [d for d in repo_src_dirs if d in existing_src_dirs]
    else:
        paths = list(repo_src_dirs)

    explicit = {
        normalized
        for raw in (extra_build_manifests or set())
        if is_build_manifest(raw)
        for normalized in [normalize_snapshot_path(raw)]
    }

    # Root manifests are selected by policy. Maven remains a delta; Go capture
    # passes its exact projection here, including unchanged root metadata.
    for manifest in ROOT_BUILD_FILES:
        if manifest not in explicit:
            continue
        if existing_root_files is not None and manifest not in existing_root_files:
            continue
        paths.append(manifest)

    # Add reactor manifests outside the ordinary source capture scope.  A
    # manifest nested under a selected source directory is already brought in
    # by git archive/tar and should not be listed twice.
    selected_dirs = [path.rstrip("/") for path in paths if path.rstrip("/") not in ROOT_BUILD_FILES]
    for manifest in sorted(explicit):
        if manifest in paths:
            continue
        if any(manifest.startswith(directory + "/") for directory in selected_dirs):
            continue
        paths.append(manifest)

    return paths
