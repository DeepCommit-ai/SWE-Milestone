"""Residue prune: eval-tree reassembly semantics (docs/residue-prune-spec.md).

The evaluation tree is assembled as "checkout GT base tag + untar agent
snapshot" — a purely additive overlay. This module implements the deletion
pass that makes file authority explicit:

    source files  -> agent-authoritative (absent from tar => deleted)
    test files    -> GT-authoritative   (never deleted)
    env material  -> image/GT           (never deleted)

Pure decision logic only — no docker/subprocess here. The evaluator wires
these functions to real containers; V2 dry-runs and the V3b runtime
assertion cover the glue (spec §7).
"""

from dataclasses import dataclass, field
from typing import FrozenSet, Iterable, List, Optional, Set

from harness.utils.src_filter import SrcFileFilter

# v2 predicate: only unambiguous agent-written code sources may be pruned.
# Non-code assets in src dirs (.sql/.pcss/SPI/meson.build/...) are
# environment/GT material and must survive (false-damage audit, spec §8.3).
CODE_SOURCE_EXTENSIONS: FrozenSet[str] = frozenset(
    {
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".scala",
        ".groovy",
        ".py",
        ".pyx",
        ".pxd",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
    }
)

# Cap for stored file lists in reports (full counts are always exact).
_SAMPLE_CAP = 20


class ResiduePruneSafetyError(Exception):
    """A path in the prune set violates the never-delete classes (V3b)."""


def _has_code_extension(path: str) -> bool:
    dot = path.rfind(".")
    if dot == -1:
        return False
    return path[dot:].lower() in CODE_SOURCE_EXTENSIONS


def normalize_tar_members(names: Iterable[str]) -> Set[str]:
    """Normalize tar member names to repo-relative file paths.

    Strips leading './', drops directory entries (trailing '/') and empties.
    """
    normalized = set()
    for name in names:
        if not name:
            continue
        if name.startswith("./"):
            name = name[2:]
        if not name or name.endswith("/"):
            continue
        normalized.add(name)
    return normalized


def is_prunable(path: str, src_filter: SrcFileFilter, keep_list: FrozenSet[str]) -> bool:
    """v2 predicate: may `path` be deleted when absent from the agent tar?

    True only for unambiguous agent-authoritative code sources:
    is_src_file (excludes tests + exclude_patterns) AND not generated AND
    not a modifiable test AND code-source extension AND not on the keep-list.
    """
    if path in keep_list:
        return False
    if not src_filter.is_src_file(path):
        return False
    if src_filter.is_generated_file(path):
        return False
    if src_filter.is_modifiable_test_file(path):
        return False
    if not _has_code_extension(path):
        return False
    return True


def compute_prune_set(
    base_files: Set[str],
    tar_files: Set[str],
    start_files: Optional[Set[str]],
    src_filter: SrcFileFilter,
    keep_list: FrozenSet[str],
) -> List[str]:
    """Compute the files to delete from the assembled eval tree.

    Args:
        base_files: files in the checked-out base tree (END, or START on fallback).
        tar_files: normalized agent snapshot contents.
        start_files: files in the milestone START tree. When given, acts as the
            provenance guard (phase 1b): only files the agent deleted relative
            to START are pruned; GT-added files survive. Pass None to lift the
            guard (phase 2 semantics).
        src_filter: the range's snapshot-side filter (same class, same metadata).
        keep_list: exact repo-relative paths that must never be pruned.

    Returns:
        Sorted list of paths to delete.
    """
    candidates = base_files - tar_files
    if start_files is not None:
        candidates &= start_files
    return sorted(p for p in candidates if is_prunable(p, src_filter, keep_list))


def assert_prune_set_safe(
    prune_set: Iterable[str], src_filter: SrcFileFilter, keep_list: FrozenSet[str]
) -> None:
    """V3b runtime assertion: abort instead of deleting a protected file.

    Independently re-checks every path against the never-delete classes.
    Raises ResiduePruneSafetyError on the first violation.
    """
    for path in prune_set:
        if path in keep_list:
            raise ResiduePruneSafetyError(f"keep-list entry in prune set: {path}")
        if src_filter.is_test_file(path):
            raise ResiduePruneSafetyError(f"test file in prune set: {path}")
        if src_filter.is_modifiable_test_file(path):
            raise ResiduePruneSafetyError(f"modifiable test in prune set: {path}")
        if src_filter.is_generated_file(path):
            raise ResiduePruneSafetyError(f"generated file in prune set: {path}")
        if src_filter.is_excluded(path):
            raise ResiduePruneSafetyError(f"excluded file in prune set: {path}")
        if not _has_code_extension(path):
            raise ResiduePruneSafetyError(f"non-code asset in prune set: {path}")


@dataclass
class SnapshotIntegrityReport:
    """Result of comparing a snapshot tar against a reference tree."""

    expected_count: int
    missing_count: int
    missing_sample: List[str] = field(default_factory=list)
    ok: bool = True


def check_snapshot_integrity(
    reference_files: Set[str],
    tar_files: Set[str],
    src_filter: SrcFileFilter,
    max_missing: int = 10,
) -> SnapshotIntegrityReport:
    """Sanity-check a snapshot against a reference tree (phase 1a).

    The snapshot should contain roughly every snapshot-includable file of the
    reference tree (START tree on the eval side; the agent tag tree on the
    capture side) minus the agent's own deletions. Mass absence means the
    capture pipeline lost files — the prune inference "absent == deleted by
    agent" does not hold and pruning must be skipped for this cell.
    """
    expected = {p for p in reference_files if src_filter.should_include_in_snapshot(p)}
    missing = sorted(expected - tar_files)
    return SnapshotIntegrityReport(
        expected_count=len(expected),
        missing_count=len(missing),
        missing_sample=missing[:_SAMPLE_CAP],
        ok=len(missing) <= max_missing,
    )
