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
from harness.utils.snapshot import should_include_snapshot_file

# v2 predicate: only unambiguous agent-written code sources may be pruned.
# Non-code assets in src dirs (.sql/.pcss/SPI/meson.build/...) are
# environment/GT material and must survive (false-damage audit, spec §8.3).
#
# This is the DEFAULT (multi-language) whitelist. Callers pass a per-range
# `extensions` set to scope which languages a range actually prunes — phase 1
# navidrome uses {".go"} so its ui/src TypeScript is never pruned (review F3).
DEFAULT_PRUNE_EXTENSIONS: FrozenSet[str] = frozenset(
    {
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".scala",
        ".groovy",
        ".py",
        ".pyi",
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
        ".cxx",
        ".hxx",
        ".hh",
    }
)

# A repository opts into the trial-pinned residue policy by declaring at least
# one of these fields in its repo config.  Repositories that have not migrated
# retain the legacy metadata.json policy so existing trials keep their original
# semantics.
RESIDUE_PRUNE_POLICY_FIELDS: FrozenSet[str] = frozenset(
    {"residue_prune", "prune_extensions", "prune_keep_list"}
)

# Back-compat alias (old name).
CODE_SOURCE_EXTENSIONS = DEFAULT_PRUNE_EXTENSIONS

# Cap for stored file lists in reports (full counts are always exact).
_SAMPLE_CAP = 20


class ResiduePruneSafetyError(Exception):
    """A path in the prune set violates the never-delete classes (V3b)."""


def repo_config_has_residue_prune_policy(config: object) -> bool:
    """Whether a repo config is the authoritative residue-policy source.

    This explicit opt-in keeps unmigrated datasets on their historical
    metadata.json behavior while allowing new trials to freeze the complete
    policy in the already SHA-bound repo_config.yaml.
    """
    return isinstance(config, dict) and any(
        field in config for field in RESIDUE_PRUNE_POLICY_FIELDS
    )


def _has_code_extension(path: str, extensions: FrozenSet[str]) -> bool:
    dot = path.rfind(".")
    if dot == -1:
        return False
    return path[dot:].lower() in extensions


# Reasons the pruner could NOT run at all, so the additive overlay may have
# resurrected the GT solution — the cell's score is untrustworthy and must
# never count as resolved (codex F1, red-team vector 1). These are genuine
# mechanism failures, NOT a heuristic safety gate: there is deliberately no
# "snapshot looks suspicious -> skip pruning" reason here. tar-absence always
# means agent deletion, so a near-empty tar prunes the base source and scores
# an honest zero rather than being protected. "" (pruned fine / nothing to
# prune) is the only trusted empty state.
FAIL_CLOSED_SKIP_REASONS: FrozenSet[str] = frozenset(
    {"ls-tree-failed", "tar-unreadable", "config-invalid"}
)


def capture_filter_config(src_filter: SrcFileFilter) -> dict:
    """Serialize the fields that define a SrcFileFilter's classification.

    Recorded in the snapshot integrity sidecar so the eval side can rebuild the
    EXACT capture-time filter and derive the drift-proof prune witness from the
    START tree (codex F4), rather than trusting a stored file list.
    """
    return {
        "src_dirs": [d.rstrip("/") for d in src_filter.src_dirs],
        "test_dirs": list(src_filter.test_dirs),
        "exclude_patterns": list(src_filter.exclude_patterns),
        "generated_patterns": list(src_filter.generated_patterns),
        "modifiable_test_patterns": list(src_filter.modifiable_test_patterns),
    }


def capture_excluded_from_config(
    config: dict, start_files: Set[str]
) -> FrozenSet[str]:
    """Rebuild the capture-time filter from `config` and return the START-tree
    paths it would exclude from a snapshot (tests/excludes). These are the files
    whose tar-absence is by-design filtering, not agent deletion — the eval side
    must never prune them (codex F4).
    """
    cap = SrcFileFilter(
        src_dirs=config.get("src_dirs", []),
        test_dirs=config.get("test_dirs", []),
        exclude_patterns=config.get("exclude_patterns", []),
        generated_patterns=config.get("generated_patterns", []),
        modifiable_test_patterns=config.get("modifiable_test_patterns", []),
    )
    return frozenset(p for p in start_files if not cap.should_include_in_snapshot(p))


def normalize_extensions(entries: Optional[Iterable[str]]) -> Optional[FrozenSet[str]]:
    """Normalize a per-range extension whitelist (review F6).

    None (absent) -> None so the caller falls back to the default; an explicit
    (possibly empty) list -> a frozenset with each entry lowercased and given a
    leading dot. Empty list therefore means "prune nothing", distinct from
    absent.
    """
    if entries is None:
        return None
    out = set()
    for e in entries:
        e = e.strip().lower()
        if not e:
            continue
        if not e.startswith("."):
            e = "." + e
        out.add(e)
    return frozenset(out)


def normalize_keep_list(entries: Iterable[str]) -> FrozenSet[str]:
    """Normalize keep-list entries to repo-relative paths (review L4).

    Strips whitespace, leading './', and trailing '/', so metadata written as
    './core/x.go' or 'core/x.go/' still matches the git-relative path.
    """
    out = set()
    for e in entries:
        e = e.strip()
        if e.startswith("./"):
            e = e[2:]
        e = e.rstrip("/")
        if e:
            out.add(e)
    return frozenset(out)


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


def resolve_prune_enablement(
    flag: Optional[bool], has_partition: bool
) -> "tuple[bool, str]":
    """Compatibility-first, default-OFF enablement policy.

    Returns (requested, reason):
    - flag is None + partition present -> (False, "default-off"): preserve the
      historical additive overlay so old and new trials remain comparable.
    - flag is None + no partition -> (False, "legacy-no-partition"): pruning is
      UNDEFINED for such metadata, not promised-and-broken — stay additive and
      do NOT mark scoring_untrusted (backward compatibility for old datasets).
    - explicit flag -> honored verbatim ("explicit"). Explicit True without a
      partition still goes through the config-invalid fail-closed path in the
    evaluator (that combination IS promised-and-broken).
    """
    if flag is None:
        return (False, "default-off") if has_partition else (False, "legacy-no-partition")
    return bool(flag), "explicit"


def parse_status_porcelain_z(out: str) -> List[str]:
    """Parse `git status --porcelain -z` into candidate lost-work paths.

    -z entries are `XY <path>NUL`; rename/copy entries (R/C in the status
    pair) are followed by one extra NUL-terminated field holding the ORIGINAL
    path, which is skipped. Deletions are dropped: a deleted-but-uncommitted
    file is not lost work (the tar simply keeps the tagged version). Returns
    the current path of every other dirty/untracked entry.
    """
    paths: List[str] = []
    tokens = out.split("\0")
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if len(entry) < 4 or entry[2] != " ":
            continue
        xy, path = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:
            i += 1  # skip the original-path field of a rename/copy
        if xy == "!!" or "D" in xy:
            continue
        paths.append(path)
    return paths


def capture_scope_covered(path: str, snapshot_paths: Iterable[str]) -> bool:
    """Is `path` inside the capture scope (the git-archive pathspecs)?

    `snapshot_paths` entries are source directories (with or without a
    trailing '/') or exact root build files (go.mod, Cargo.toml, ...).
    """
    for sp in snapshot_paths:
        sp = sp.rstrip("/")
        if sp and (path == sp or path.startswith(sp + "/")):
            return True
    return False


def classify_capture_loss(
    paths: Iterable[str],
    src_filter: SrcFileFilter,
    snapshot_paths: Iterable[str],
) -> "tuple[List[str], List[str]]":
    """Split candidate lost paths into (in_scope, out_of_scope) loss buckets.

    - in_scope: covered by the capture pathspecs — this work belongs in the
      tar and is not there (uncommitted case).
    - out_of_scope: not covered by any pathspec — work that can NEVER reach
      the tar regardless of committing (wrong location, spec §11.4-H).
    Test/excluded paths are dropped from both buckets: their absence from the
    snapshot is by-design filtering, not loss.
    """
    in_scope: List[str] = []
    out_of_scope: List[str] = []
    sps = [sp for sp in snapshot_paths if sp]
    for p in paths:
        if src_filter.is_test_file(p) or src_filter.is_excluded(p):
            continue
        if capture_scope_covered(p, sps):
            in_scope.append(p)
        else:
            out_of_scope.append(p)
    return sorted(in_scope), sorted(out_of_scope)


def is_prunable(
    path: str,
    src_filter: SrcFileFilter,
    keep_list: FrozenSet[str],
    extensions: FrozenSet[str] = DEFAULT_PRUNE_EXTENSIONS,
) -> bool:
    """v2 predicate: may `path` be deleted when absent from the agent tar?

    True only for unambiguous agent-authoritative code sources:
    is_src_file (excludes tests + exclude_patterns) AND not generated AND
    not a modifiable test AND extension in the per-range whitelist AND not on
    the keep-list.
    """
    if path in keep_list:
        return False
    if not src_filter.is_src_file(path):
        return False
    if src_filter.is_generated_file(path):
        return False
    if src_filter.is_modifiable_test_file(path):
        return False
    if not _has_code_extension(path, extensions):
        return False
    return True


def compute_prune_set(
    base_files: Set[str],
    tar_files: Set[str],
    start_files: Optional[Set[str]],
    src_filter: SrcFileFilter,
    keep_list: FrozenSet[str],
    extensions: FrozenSet[str] = DEFAULT_PRUNE_EXTENSIONS,
    capture_excluded: Optional[FrozenSet[str]] = None,
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
        extensions: per-range code-source extension whitelist (language scope).
        capture_excluded: paths the CAPTURE-TIME filter dropped from the tar
            (tests/excludes). Their tar-absence is by-design filtering, not
            agent deletion, so they must never be pruned — this is the
            independent witness that survives eval-vs-capture filter drift
            (review F2). None = no witness available (legacy tars).

    Returns:
        Sorted list of paths to delete.
    """
    candidates = base_files - tar_files
    if start_files is not None:
        candidates &= start_files
    if capture_excluded:
        candidates -= capture_excluded
    return sorted(p for p in candidates if is_prunable(p, src_filter, keep_list, extensions))


def assert_prune_set_safe(
    prune_set: Iterable[str],
    src_filter: SrcFileFilter,
    keep_list: FrozenSet[str],
    extensions: FrozenSet[str] = DEFAULT_PRUNE_EXTENSIONS,
    capture_excluded: Optional[FrozenSet[str]] = None,
) -> None:
    """V3b runtime assertion: abort instead of deleting a protected file.

    Independently re-checks every path against the never-delete classes. The
    capture_excluded witness is checked FIRST and is independent of the eval
    filter, so it catches capture-vs-eval filter drift that the filter-based
    checks (which share the eval filter) structurally cannot (review F2).
    Raises ResiduePruneSafetyError on the first violation.
    """
    capture_excluded = capture_excluded or frozenset()
    for path in prune_set:
        if path in capture_excluded:
            raise ResiduePruneSafetyError(f"capture-excluded (filter drift) in prune set: {path}")
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
        if not _has_code_extension(path, extensions):
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
    max_missing_frac: float = 0.10,
    extra_build_manifests: Optional[Set[str]] = None,
) -> SnapshotIntegrityReport:
    """Sanity-check a snapshot against a reference tree (phase 1a).

    The snapshot should contain roughly every snapshot-includable file of the
    reference tree (START tree on the eval side; the agent tag tree on the
    capture side) minus the agent's own deletions. Mass absence means the
    capture pipeline lost files — the prune inference "absent == deleted by
    agent" does not hold and pruning must be skipped for this cell.

    ok is False when EITHER an absolute floor (`max_missing`) OR a relative
    fraction (`max_missing_frac` of the expected set) is exceeded (review F1) —
    a large tree missing 20% is flagged even though it clears the absolute
    floor, while a small tree missing a couple of files is not. NOTE: the
    caller must treat ok=False as fail-closed (do not silently grade), not
    merely "skip pruning" — see evaluator._maybe_prune_residue.
    """
    expected = {
        p
        for p in reference_files
        if should_include_snapshot_file(
            p,
            src_filter,
            extra_build_manifests=extra_build_manifests,
        )
    }
    missing = sorted(expected - tar_files)
    over_absolute = len(missing) > max_missing
    over_relative = len(expected) > 0 and (len(missing) / len(expected)) > max_missing_frac
    return SnapshotIntegrityReport(
        expected_count=len(expected),
        missing_count=len(missing),
        missing_sample=missing[:_SAMPLE_CAP],
        ok=not (over_absolute and over_relative),
    )
