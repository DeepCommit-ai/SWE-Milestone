#!/usr/bin/env python3
"""
Rust test region filtering utilities.

This module provides functions to replace agent-written test code with
ground truth tests in Rust source files within Docker containers.

The typical use case is during e2e evaluation:
1. Agent's src files are copied to evaluation container (may contain agent-written tests)
2. This module removes agent's test regions and appends GT test regions
3. Tests are run against GT tests, not agent-written tests

Only processes .rs files that were part of the filtered src snapshot.
"""

import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class RustTestFilterError(RuntimeError):
    """Raised when agent tests cannot be separated from production Rust safely."""


@dataclass(frozen=True)
class _RustTestRegion:
    start: int
    end: int
    reason: str
    identity: Tuple[str, str]
    nested: bool
    scope_path: Optional[Tuple[Tuple[str, str, int], ...]]
    insertion_safe: bool


@dataclass(frozen=True)
class _RustScope:
    start: int
    end: int
    path: Tuple[Tuple[str, str, int], ...]


def _run_docker_exec(container_name: str, command: str, check: bool = True) -> Tuple[bool, str, str]:
    """
    Run a command in Docker container.

    Args:
        container_name: Name of the Docker container
        command: Command to run
        check: If True, raise on non-zero exit code

    Returns:
        Tuple of (success, stdout, stderr)
    """
    cmd = ["docker", "exec", container_name, "bash", "-c", command]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if check and result.returncode != 0:
        return False, result.stdout, result.stderr

    return result.returncode == 0, result.stdout, result.stderr


def _read_file_from_container(container_name: str, file_path: str) -> Optional[str]:
    """
    Read a file from the container.

    Args:
        container_name: Name of the Docker container
        file_path: Path to file in container (relative to /testbed)

    Returns:
        File content or None if file doesn't exist
    """
    success, stdout, stderr = _run_docker_exec(container_name, f"cat /testbed/{file_path}", check=False)

    if not success:
        return None

    return stdout


# git-show stderr markers that prove the PATH is absent at the ref (the only
# case where "treat as new file" is sound). Anything else — bad/missing ref,
# docker hiccup, repo corruption — must fail closed, not masquerade as a new
# file with no ground-truth tests to graft.
_GIT_PATH_ABSENT_MARKERS = (
    "does not exist in",
    "exists on disk, but not in",
)


def _read_file_from_git_ref(container_name: str, file_path: str, ref: str) -> Optional[str]:
    """
    Read a file from a git ref in the container.

    Args:
        container_name: Name of the Docker container
        file_path: Path to file (relative to repo root)
        ref: Git ref (tag, branch, commit)

    Returns:
        File content, or None only when git proves the path does not exist
        at that ref.

    Raises:
        RustTestFilterError: on any other git/docker failure, so callers
        cannot fail open by treating an unread ground-truth file as new.
    """
    success, stdout, stderr = _run_docker_exec(
        container_name, f"cd /testbed && git show {ref}:{file_path}", check=False
    )

    if success:
        return stdout
    if any(marker in stderr for marker in _GIT_PATH_ABSENT_MARKERS):
        return None
    raise RustTestFilterError(
        f"git show {ref}:{file_path} failed without a missing-path error: "
        f"{stderr.strip()[:300]}"
    )


def _write_file_to_container(
    container_name: str,
    file_path: str,
    content: str,
    owner: str = "fakeroot:fakeroot",
    mode: str = "644",
) -> bool:
    """
    Write a file to the container.

    Args:
        container_name: Name of the Docker container
        file_path: Path to file in container (relative to /testbed)
        content: File content to write
        owner: Owner in "user:group" format (default: fakeroot:fakeroot)
        mode: File permissions (default: 644)

    Returns:
        True if successful
    """
    # Write to temp file on host, then docker cp
    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(content)
        temp_path = f.name

    try:
        # Copy to container
        cmd = ["docker", "cp", temp_path, f"{container_name}:/testbed/{file_path}"]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            logger.error(f"Failed to write file to container: {result.stderr}")
            return False

        # Restore ownership (docker cp uses host user's uid/gid)
        chown_cmd = [
            "docker",
            "exec",
            "--user",
            "root",
            "-w",
            "/testbed",
            container_name,
            "chown",
            owner,
            file_path,
        ]
        subprocess.run(chown_cmd, capture_output=True)

        # Set permissions
        chmod_cmd = [
            "docker",
            "exec",
            "--user",
            "root",
            "-w",
            "/testbed",
            container_name,
            "chmod",
            mode,
            file_path,
        ]
        subprocess.run(chmod_cmd, capture_output=True)

        return True
    finally:
        Path(temp_path).unlink(missing_ok=True)


def _scope_header(kind: str, text: str) -> str:
    """Return a stable-enough identity for a Rust declaration scope."""
    header = text.split("{", 1)[0]
    header = re.sub(r"\s+", " ", header).strip()
    if kind == "mod":
        match = re.search(r"\bmod\s+([A-Za-z_]\w*)", header)
        return match.group(1) if match else header
    if kind in ("trait", "struct", "enum", "union"):
        match = re.search(rf"\b{kind}\s+([A-Za-z_]\w*)", header)
        return match.group(1) if match else header
    if kind == "fn":
        match = re.search(r"\bfn\s+([A-Za-z_]\w*)", header)
        return match.group(1) if match else header
    if kind == "impl":
        match = re.search(r"\bimpl\b", header)
        if not match:
            return header
        body = header[match.end() :].lstrip()
        # Generic parameter bounds and a trailing where-clause are not scope
        # identity.  Agents may change them while keeping the same impl target.
        if body.startswith("<"):
            depth = 0
            for index, char in enumerate(body):
                if char == "<":
                    depth += 1
                elif char == ">":
                    depth -= 1
                    if depth == 0:
                        body = body[index + 1 :].lstrip()
                        break
        body = re.split(r"\s+where\s+", body, maxsplit=1)[0]
        return re.sub(r"\s+", " ", body).strip()
    return header


def _build_scope_tree(
    ranges_by_kind: Dict[str, List[Tuple[int, int, str]]]
) -> List[_RustScope]:
    """Build comparable module/impl/trait paths for nested item placement."""
    raw_scopes = []
    for kind, node_kind in (
        ("mod", "mod_item"),
        ("impl", "impl_item"),
        ("trait", "trait_item"),
        ("foreign", "foreign_mod_item"),
        ("struct", "struct_item"),
        ("enum", "enum_item"),
        ("union", "union_item"),
        ("fn", "function_item"),
    ):
        for start, end, text in ranges_by_kind[node_kind]:
            # External ``mod foo;`` has no scope body and cannot contain tests.
            if kind == "mod" and "{" not in text:
                continue
            raw_scopes.append((start, end, kind, _scope_header(kind, text)))

    built: List[_RustScope] = []
    sibling_counts: Dict[
        Tuple[Tuple[Tuple[str, str, int], ...], str, str], int
    ] = {}
    for start, end, kind, header in sorted(raw_scopes, key=lambda item: (item[0], -item[1])):
        parents = [
            scope
            for scope in built
            if scope.start <= start and end <= scope.end
            and (scope.start, scope.end) != (start, end)
        ]
        parent_path = min(parents, key=lambda scope: scope.end - scope.start).path if parents else ()
        counter_key = (parent_path, kind, header)
        ordinal = sibling_counts.get(counter_key, 0)
        sibling_counts[counter_key] = ordinal + 1
        built.append(
            _RustScope(
                start=start,
                end=end,
                path=parent_path + ((kind, header, ordinal),),
            )
        )
    return built


def _region_identity(content: str, start: int, end: int, reason: str) -> Tuple[str, str]:
    """Identify a test item independently of its implementation body."""
    snippet = "\n".join(content.split("\n")[start - 1 : end])
    patterns = (
        ("fn", r"\bfn\s+([A-Za-z_]\w*)"),
        ("mod", r"\bmod\s+([A-Za-z_]\w*)"),
        ("const", r"\bconst\s+([A-Za-z_]\w*)"),
        ("static", r"\bstatic\s+(?:mut\s+)?([A-Za-z_]\w*)"),
        ("type", r"\btype\s+([A-Za-z_]\w*)"),
        ("struct", r"\bstruct\s+([A-Za-z_]\w*)"),
        ("enum", r"\benum\s+([A-Za-z_]\w*)"),
        ("union", r"\bunion\s+([A-Za-z_]\w*)"),
        ("trait", r"\btrait\s+([A-Za-z_]\w*)"),
        ("macro", r"\bmacro_rules!\s*([A-Za-z_]\w*)"),
        (
            "field",
            r"(?m)^\s*(?:pub(?:\s*\([^)]*\))?\s+)?([A-Za-z_]\w*)\s*:",
        ),
    )
    for kind, pattern in patterns:
        match = re.search(pattern, snippet)
        if match:
            return kind, match.group(1)
    impl_match = re.search(r"\bimpl\b([^\{]*)\{", snippet, re.DOTALL)
    if impl_match:
        return "impl", re.sub(r"\s+", " ", impl_match.group(1)).strip()
    macro_match = re.search(
        r"(?:^|\n)\s*((?:[A-Za-z_]\w*::)*[A-Za-z_]\w*)!", snippet
    )
    if macro_match:
        return "macro-invocation", macro_match.group(1)
    # Unknown items remain auditable and can still be paired when their header
    # is unchanged.  Exclude bodies so a correct alternative implementation is
    # not rejected merely because test code differs.
    header = re.sub(r"\s+", " ", snippet.split("{", 1)[0]).strip()
    return reason, header


def _analyze_test_regions(
    content: str,
    file_path: str,
) -> Tuple[
    List[_RustTestRegion],
    Dict[Tuple[Tuple[str, str, int], ...], _RustScope],
]:
    """Detect tests and retain the declaration scope of every nested item."""
    if not file_path.endswith(".rs"):
        return [], {}

    from harness.prepare_repo.split_test_patches.test_detector import (
        RustTestDetectionError,
        _get_kind_ranges_by_kind,
        _is_inside_any_block,
        find_test_code_ranges,
    )

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".rs", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            raw_regions = [
                item
                for item in find_test_code_ranges(
                    tmp_path, only_root_level=False, strict=True
                )
                if "doc test" not in item[2]
            ]
            structural_ranges = _get_kind_ranges_by_kind(
                tmp_path,
                [
                    "declaration_list",
                    "block",
                    "mod_item",
                    "impl_item",
                    "trait_item",
                    "foreign_mod_item",
                    "struct_item",
                    "enum_item",
                    "union_item",
                    "function_item",
                    "field_declaration_list",
                    "field_initializer_list",
                    "enum_variant_list",
                    "match_block",
                ],
                strict=True,
            )
            declaration_lists = [
                (start, end)
                for start, end, _ in structural_ranges["declaration_list"]
            ]
            safe_nested_containers = declaration_lists + [
                (start, end)
                for kind in ("field_declaration_list", "enum_variant_list")
                for start, end, _ in structural_ranges[kind]
            ]
            unsafe_nested_containers = [
                (start, end)
                for kind in ("block", "field_initializer_list", "match_block")
                for start, end, _ in structural_ranges[kind]
            ]
            nested_containers = safe_nested_containers + unsafe_nested_containers
            scopes = _build_scope_tree(structural_ranges)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        regions = []
        for start, end, reason in raw_regions:
            nested = _is_inside_any_block(start, nested_containers)
            insertion_safe = nested and not _is_inside_any_block(
                start, unsafe_nested_containers
            )
            containing_scopes = [
                scope for scope in scopes if scope.start <= start and end <= scope.end
            ]
            scope_path = (
                min(containing_scopes, key=lambda scope: scope.end - scope.start).path
                if containing_scopes
                else None
            )
            regions.append(
                _RustTestRegion(
                    start=start,
                    end=end,
                    reason=reason,
                    identity=_region_identity(content, start, end, reason),
                    nested=nested,
                    scope_path=scope_path,
                    insertion_safe=insertion_safe,
                )
            )
        return regions, {scope.path: scope for scope in scopes}
    except RustTestFilterError:
        raise
    except RustTestDetectionError as exc:
        raise RustTestFilterError(f"failed to detect Rust tests in {file_path}: {exc}") from exc
    except Exception as exc:
        raise RustTestFilterError(f"failed to inspect Rust tests in {file_path}: {exc}") from exc


def find_test_ranges_from_content(
    content: str,
    file_path: str,
    only_root_level: bool = True,
    *,
    reject_nested: bool = True,
) -> List[Tuple[int, int]]:
    """Find Rust test ranges, failing closed on unsupported nested contexts."""
    regions, _ = _analyze_test_regions(content, file_path)
    nested = [region for region in regions if region.nested]
    if only_root_level and nested and reject_nested:
        sample = ", ".join(
            f"{region.start}-{region.end} ({region.reason})" for region in nested[:5]
        )
        raise RustTestFilterError(
            f"nested Rust test regions in {file_path} require scope-preserving replacement: "
            f"{sample}"
        )
    selected = [region for region in regions if not only_root_level or not region.nested]
    return [(region.start, region.end) for region in selected]


def _is_outer_doc_comment_or_empty(line: str) -> bool:
    """Check for whitespace or an outer line-doc comment.

    ``//!`` is deliberately excluded: it documents the enclosing module/crate,
    not the following test item.
    """
    stripped = line.strip()
    if not stripped:
        return True
    return stripped.startswith("///")


def _expand_range_to_include_doc_comments(lines: List[str], start: int, end: int) -> Tuple[int, int]:
    """
    Expand a range to include preceding doc comments.

    When removing a test function like:
        /// Some doc comment
        #[cfg(test)]
        fn test_foo() { ... }

    We need to also remove the doc comment, otherwise it becomes a dangling
    comment that causes compilation errors.

    Args:
        lines: All file lines (0-indexed)
        start: Start line (1-indexed)
        end: End line (1-indexed)

    Returns:
        Expanded (start, end) tuple (1-indexed)
    """
    start_idx = start - 1  # Convert to 0-indexed

    # Scan backwards from start to find doc comments
    while start_idx > 0:
        prev_line = lines[start_idx - 1]
        if _is_outer_doc_comment_or_empty(prev_line):
            start_idx -= 1
        elif prev_line.strip().endswith("*/"):
            # Include an outer /** ... */ block, but never an inner /*! ... */
            # module doc or an ordinary block comment. Walk up only within
            # THIS block: stop at the line that opens it, and bail if another
            # block's terminator appears first — scanning past the opener
            # would swallow unrelated production code into the test range.
            block_start = start_idx - 1
            if "/*" not in lines[block_start]:
                block_start -= 1
                while block_start >= 0 and "/*" not in lines[block_start]:
                    if "*/" in lines[block_start]:
                        block_start = -1
                        break
                    block_start -= 1
            if (
                block_start >= 0
                and "/**" in lines[block_start]
                and "/*!" not in lines[block_start]
            ):
                start_idx = block_start
            else:
                break
        else:
            break

    # Skip any leading empty lines we picked up (keep them in the file)
    while start_idx < start - 1 and not lines[start_idx].strip():
        start_idx += 1

    return (start_idx + 1, end)  # Convert back to 1-indexed


def remove_test_regions(content: str, ranges: List[Tuple[int, int]]) -> str:
    """
    Remove test regions from content.

    Also removes preceding doc comments that would become dangling.

    Args:
        content: File content
        ranges: List of (start_line, end_line) tuples to remove (1-indexed, inclusive)

    Returns:
        Content with test regions removed
    """
    if not ranges:
        return content

    lines = content.split("\n")

    # Expand ranges to include doc comments, then sort descending
    expanded_ranges = [_expand_range_to_include_doc_comments(lines, start, end) for start, end in ranges]
    sorted_ranges = sorted(expanded_ranges, key=lambda x: x[0], reverse=True)

    for start, end in sorted_ranges:
        # Convert to 0-indexed
        start_idx = start - 1
        end_idx = end  # end is inclusive, so we delete up to end_idx (exclusive in slice)

        # Remove lines
        del lines[start_idx:end_idx]

    return "\n".join(lines)


def extract_test_regions(content: str, ranges: List[Tuple[int, int]]) -> str:
    """
    Extract test regions from content.

    Args:
        content: File content
        ranges: List of (start_line, end_line) tuples to extract (1-indexed, inclusive)

    Returns:
        Concatenated test regions with blank lines between them
    """
    if not ranges:
        return ""

    lines = content.split("\n")
    extracted_parts = []

    # Sort ranges by start line
    sorted_ranges = sorted(ranges, key=lambda x: x[0])

    for start, end in sorted_ranges:
        # Convert to 0-indexed
        start_idx = start - 1
        end_idx = end  # end is inclusive

        # Extract lines
        region_lines = lines[start_idx:end_idx]
        extracted_parts.append("\n".join(region_lines))

    return "\n\n".join(extracted_parts)


def merge_src_with_gt_tests(agent_content: str, gt_content: str, file_path: str) -> Tuple[str, Dict[str, int]]:
    """
    Merge agent's src code with GT test regions.

    Args:
        agent_content: Agent's file content (may contain agent-written tests)
        gt_content: Ground truth file content (contains GT tests)
        file_path: File path (for detection)

    Returns:
        Tuple of (merged_content, stats_dict)
    """
    stats = {
        "agent_test_regions_removed": 0,
        "gt_test_regions_appended": 0,
        "nested_test_regions_replaced": 0,
        "nested_test_regions_inserted": 0,
    }

    agent_regions, agent_scopes = _analyze_test_regions(agent_content, file_path)
    gt_regions, _ = _analyze_test_regions(gt_content, file_path)
    agent_root = [region for region in agent_regions if not region.nested]
    gt_root = [region for region in gt_regions if not region.nested]
    agent_nested = [region for region in agent_regions if region.nested]
    gt_nested = [region for region in gt_regions if region.nested]

    stats["agent_test_regions_removed"] = len(agent_regions)
    stats["gt_test_regions_appended"] = len(gt_regions)

    def region_text(content: str, region: _RustTestRegion) -> str:
        return "\n".join(content.split("\n")[region.start - 1 : region.end])

    gt_by_key: Dict[
        Tuple[Tuple[Tuple[str, str, int], ...], Tuple[str, str]],
        List[_RustTestRegion],
    ] = {}
    for region in gt_nested:
        if region.scope_path is None:
            raise RustTestFilterError(
                f"nested GT test {region.identity} in {file_path}:{region.start} "
                "is inside an unsupported lexical scope"
            )
        gt_by_key.setdefault((region.scope_path, region.identity), []).append(region)

    # Line edits are expressed against the original agent file and applied from
    # bottom to top, so replacing one nested item cannot shift another range.
    replacement_ops: List[Tuple[int, int, str]] = [
        (region.start, region.end, "") for region in agent_root
    ]
    paired_counts: Dict[
        Tuple[Tuple[Tuple[str, str, int], ...], Tuple[str, str]], int
    ] = {}
    for region in agent_nested:
        if region.scope_path is None:
            raise RustTestFilterError(
                f"nested agent test {region.identity} in {file_path}:{region.start} "
                "is inside an unsupported lexical scope"
            )
        key = (region.scope_path, region.identity)
        index = paired_counts.get(key, 0)
        candidates = gt_by_key.get(key, [])
        replacement = region_text(gt_content, candidates[index]) if index < len(candidates) else ""
        if replacement:
            stats["nested_test_regions_replaced"] += 1
        replacement_ops.append((region.start, region.end, replacement))
        paired_counts[key] = index + 1

    missing_by_scope: Dict[Tuple[Tuple[str, str, int], ...], List[_RustTestRegion]] = {}
    for key, regions in gt_by_key.items():
        used = paired_counts.get(key, 0)
        if used < len(regions):
            missing = regions[used:]
            unsafe = [region for region in missing if not region.insertion_safe]
            if unsafe:
                sample = ", ".join(
                    f"{region.identity}@{region.start}" for region in unsafe[:5]
                )
                raise RustTestFilterError(
                    f"cannot insert nested GT test nodes inside a lexical expression "
                    f"scope in {file_path}: {sample}"
                )
            missing_by_scope.setdefault(key[0], []).extend(missing)

    insertion_ops: List[Tuple[int, str]] = []
    for scope_path, regions in missing_by_scope.items():
        agent_scope = agent_scopes.get(scope_path)
        if agent_scope is None:
            rendered_scope = " / ".join(
                f"{kind}:{header}[{ordinal}]" for kind, header, ordinal in scope_path
            )
            raise RustTestFilterError(
                f"cannot place nested GT tests in {file_path}: agent scope "
                f"{rendered_scope!r} does not exist"
            )
        snippets = [
            region_text(gt_content, region)
            for region in sorted(regions, key=lambda item: item.start)
        ]
        insertion_ops.append((agent_scope.end, "\n\n".join(snippets)))
        stats["nested_test_regions_inserted"] += len(snippets)

    lines = agent_content.split("\n")
    edits: List[Tuple[int, int, List[str], int]] = []
    for start, end, replacement in replacement_ops:
        replacement_lines = replacement.split("\n") if replacement else []
        edits.append((start - 1, end, replacement_lines, 0))
    for scope_end, insertion in insertion_ops:
        # Insert immediately before the scope's closing-brace line.
        edits.append((scope_end - 1, scope_end - 1, [""] + insertion.split("\n") + [""], 1))
    for start_idx, end_idx, replacement_lines, _ in sorted(
        edits, key=lambda edit: (edit[0], edit[3]), reverse=True
    ):
        lines[start_idx:end_idx] = replacement_lines
    src_only = "\n".join(lines)

    gt_tests = extract_test_regions(
        gt_content, [(region.start, region.end) for region in gt_root]
    )

    # Merge: src + blank lines + GT tests
    if gt_tests.strip():
        # Ensure src ends with newline
        if not src_only.endswith("\n"):
            src_only += "\n"
        # Add blank line before tests
        merged = src_only + "\n" + gt_tests
    else:
        merged = src_only

    # Ensure file ends with newline
    if not merged.endswith("\n"):
        merged += "\n"

    return merged, stats


def replace_agent_tests_with_ground_truth(
    container_name: str,
    file_path: str,
    milestone_id: str,
    gt_tag_suffix: str = "end",
) -> Dict[str, Any]:
    """
    Replace agent's test regions with ground truth tests for a single file.

    Args:
        container_name: Name of the Docker container
        file_path: Path to file (relative to /testbed)
        milestone_id: Milestone ID for git ref

    Returns:
        Dict with operation results
    """
    result = {
        "file": file_path,
        "success": False,
        "skipped": False,
        "reason": "",
        "agent_test_regions_removed": 0,
        "gt_test_regions_appended": 0,
    }

    # Only process .rs files
    if not file_path.endswith(".rs"):
        result["skipped"] = True
        result["reason"] = "not a Rust file"
        return result

    # Read agent's version (current state in container)
    agent_content = _read_file_from_container(container_name, file_path)
    if agent_content is None:
        result["reason"] = "failed to read agent file"
        return result

    # Read GT version from specified tag (end or start)
    # By default uses END tag (complete implementation with tests)
    # Fallback to START tag when agent code only compiles against baseline
    gt_tag = f"milestone-{milestone_id}-{gt_tag_suffix}"
    gt_content = _read_file_from_git_ref(container_name, file_path, gt_tag)

    if gt_content is None:
        # File doesn't exist in GT - might be a new file created by agent
        # Removing a nested item in place is safe; only relocating it would
        # lose scope.  Strip every agent-authored test region from a new file.
        agent_test_ranges = find_test_ranges_from_content(
            agent_content, file_path, only_root_level=False
        )
        if agent_test_ranges:
            src_only = remove_test_regions(agent_content, agent_test_ranges)
            if _write_file_to_container(container_name, file_path, src_only):
                result["success"] = True
                result["agent_test_regions_removed"] = len(agent_test_ranges)
                result["reason"] = "new file, removed agent tests"
            else:
                result["reason"] = "failed to write file"
        else:
            result["skipped"] = True
            result["reason"] = "new file, no tests to remove"
        return result

    # Merge src with GT tests
    merged_content, stats = merge_src_with_gt_tests(agent_content, gt_content, file_path)

    # Check if any changes were made
    if stats["agent_test_regions_removed"] == 0 and stats["gt_test_regions_appended"] == 0:
        result["skipped"] = True
        result["reason"] = "no test regions in either file"
        return result

    # Write merged content back
    if _write_file_to_container(container_name, file_path, merged_content):
        result["success"] = True
        result["agent_test_regions_removed"] = stats["agent_test_regions_removed"]
        result["gt_test_regions_appended"] = stats["gt_test_regions_appended"]
    else:
        result["reason"] = "failed to write merged file"

    return result


def process_rust_files_in_container(
    container_name: str,
    milestone_id: str,
    rust_files: List[str],
    gt_tag_suffix: str = "end",
) -> Dict[str, Any]:
    """
    Process all Rust files to replace agent tests with GT tests.

    Args:
        container_name: Name of the Docker container
        milestone_id: Milestone ID
        rust_files: List of .rs file paths (relative to /testbed)

    Returns:
        Dict with processing results
    """
    results = {
        "total_files": len(rust_files),
        "processed": 0,
        "skipped": 0,
        "failed": 0,
        "total_agent_tests_removed": 0,
        "total_gt_tests_appended": 0,
        "details": [],
    }

    for file_path in rust_files:
        try:
            file_result = replace_agent_tests_with_ground_truth(
                container_name, file_path, milestone_id, gt_tag_suffix
            )
        except Exception as exc:
            # Security boundary: a detector/tool failure is not equivalent to
            # "this file has no tests".  Report a failed file so the evaluator
            # can invalidate the cell before running any tests.
            file_result = {
                "file": file_path,
                "success": False,
                "skipped": False,
                "reason": f"Rust test filtering failed closed: {exc}",
                "agent_test_regions_removed": 0,
                "gt_test_regions_appended": 0,
            }
        results["details"].append(file_result)

        if file_result["skipped"]:
            results["skipped"] += 1
        elif file_result["success"]:
            results["processed"] += 1
            results["total_agent_tests_removed"] += file_result["agent_test_regions_removed"]
            results["total_gt_tests_appended"] += file_result["gt_test_regions_appended"]
        else:
            results["failed"] += 1

    return results


def get_rust_files_from_tar(tar_path: Path) -> List[str]:
    """
    Get list of .rs files from a tar archive.

    Args:
        tar_path: Path to tar file

    Returns:
        List of .rs file paths in the tar
    """
    import tarfile

    rust_files = []

    try:
        with tarfile.open(tar_path, "r") as tar:
            for member in tar.getmembers():
                if member.isfile() and member.name.endswith(".rs"):
                    rust_files.append(member.name)
    except Exception as e:
        raise RustTestFilterError(f"failed to enumerate Rust files in snapshot: {e}") from e

    return rust_files
