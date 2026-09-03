#!/usr/bin/env python3
"""
Hunk-level verification for milestone test/src separation.

Verifies:
1. Files removed from old→new patch should be test files
2. Hunks removed from old→new patch (same file) should be in test code regions
3. New patch should not contain test code changes
"""

import re
import sys
import json
import os
import tempfile
import subprocess
from pathlib import Path
from typing import List, Dict, Tuple, Set, Optional
from dataclasses import dataclass

try:
    from .test_detector import resolve_ast_grep
except ImportError:  # direct script execution (python verify_test_separation.py)
    from test_detector import resolve_ast_grep


@dataclass
class Hunk:
    file: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    content: str  # The actual diff content


def is_test_path(file_path: str) -> bool:
    """Check if path is a test file.

    Matches:
    - Files in tests/ directory (e.g., tests/foo.rs, crates/x/tests/bar.rs)
    - Files named tests.rs (e.g., crates/x/src/interface/tests.rs)
      These are typically included via `#[cfg(test)] mod tests;`
    """
    return "/tests/" in file_path or file_path.startswith("tests/") or file_path.endswith("/tests.rs")


def classify_hunk_simple(hunk_content: str) -> str:
    """Simple hunk classification based on content patterns.

    Returns: "test", "src", or "mixed"

    Note: We only use patterns that are clearly test-specific.
    assert!/assert_eq!/assert_ne!/panic! are NOT included because they are
    commonly used in production code for runtime validation.
    """
    # Test code patterns - only include patterns that are clearly test-specific
    test_patterns = [
        r"#\[test\]",
        r"#\[cfg\(test\)\]",
        r"#\[cfg_attr\([^)]*test",
        r"mod\s+tests?\s*\{",
        # rstest patterns
        r"#\[rstest\]",
        r"#\[case\b",
        r"#\[fixture\]",
    ]

    # Check added lines only (lines starting with +)
    added_lines = [line[1:] for line in hunk_content.split("\n") if line.startswith("+") and not line.startswith("+++")]

    has_test_code = False
    has_src_code = False

    for line in added_lines:
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # Check for test patterns
        is_test_line = False
        for pattern in test_patterns:
            if re.search(pattern, line):
                is_test_line = True
                has_test_code = True
                break

        if not is_test_line and line_stripped:
            # Non-empty, non-test line is source code
            has_src_code = True

    if has_test_code and has_src_code:
        return "mixed"
    elif has_test_code:
        return "test"
    else:
        return "src"


def parse_patch_hunks(patch_content: str) -> Dict[str, List[Hunk]]:
    """Parse patch into {file: [hunks]} structure."""
    result = {}
    current_file = None
    current_hunk = None
    hunk_lines = []

    for line in patch_content.split("\n"):
        if line.startswith("diff --git"):
            # Save previous hunk
            if current_hunk and current_file:
                current_hunk.content = "\n".join(hunk_lines)
                if current_file not in result:
                    result[current_file] = []
                result[current_file].append(current_hunk)

            # Start new file
            match = re.search(r"b/(.+)$", line)
            current_file = match.group(1) if match else None
            current_hunk = None
            hunk_lines = []

        elif line.startswith("@@") and current_file:
            # Save previous hunk
            if current_hunk:
                current_hunk.content = "\n".join(hunk_lines)
                if current_file not in result:
                    result[current_file] = []
                result[current_file].append(current_hunk)

            # Parse new hunk header
            match = re.search(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if match:
                current_hunk = Hunk(
                    file=current_file,
                    old_start=int(match.group(1)),
                    old_count=int(match.group(2) or 1),
                    new_start=int(match.group(3)),
                    new_count=int(match.group(4) or 1),
                    content="",
                )
                hunk_lines = [line]

        elif current_hunk:
            hunk_lines.append(line)

    # Save last hunk
    if current_hunk and current_file:
        current_hunk.content = "\n".join(hunk_lines)
        if current_file not in result:
            result[current_file] = []
        result[current_file].append(current_hunk)

    return result


def _find_module_end_with_brace_counting(lines: List[str], start_idx: int) -> Optional[int]:
    """Find the end of a module by counting braces, starting from start_idx.

    WARNING: This function does not handle braces inside string literals correctly.
    Use _find_test_ranges_with_ast_grep for accurate test range detection.
    """
    brace_count = 0
    found_open = False
    for k in range(start_idx, len(lines)):
        for char in lines[k]:
            if char == "{":
                brace_count += 1
                found_open = True
            elif char == "}":
                brace_count -= 1
        if found_open and brace_count == 0:
            return k + 1  # 1-indexed
    return None


def _find_test_ranges_with_ast_grep(content: str, file_path: str) -> List[Tuple[int, int]]:
    """
    Find test code ranges from content using ast-grep for accurate detection.

    This function writes content to a temp file and uses ast-grep to properly
    parse Rust code, avoiding issues with braces inside string literals.

    Returns list of (start_line, end_line) tuples (1-indexed).
    """
    if not file_path.endswith(".rs"):
        return []

    # Write content to temp file
    fd, temp_path = tempfile.mkstemp(suffix=".rs")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)

        lines = content.split("\n")
        ranges = []

        # Pattern 1: Match test modules by name convention (mod tests { ... })
        # This is the most common pattern and ast-grep can match it accurately
        result = subprocess.run(
            [resolve_ast_grep(), "run", "--pattern", "mod tests { $$$BODY }", "--lang", "rust", "--json", temp_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            matches = json.loads(result.stdout)
            for match in matches:
                # ast-grep returns 0-indexed lines, convert to 1-indexed
                mod_start = match["range"]["start"]["line"] + 1
                mod_end = match["range"]["end"]["line"] + 1

                # Check if there's a #[cfg(test)] attribute before the mod
                # Look at the line(s) immediately before the mod declaration
                attr_line = mod_start - 1
                while attr_line > 0:
                    line_content = lines[attr_line - 1].strip()  # Convert to 0-indexed
                    if not line_content:
                        attr_line -= 1
                        continue
                    if line_content == "#[cfg(test)]":
                        # Include the attribute line in the range
                        mod_start = attr_line
                    break

                ranges.append((mod_start, mod_end))

        # Pattern 2: Find #[cfg(test)] attributes and their associated items
        # for cases where the module is not named "tests"
        result = subprocess.run(
            [resolve_ast_grep(), "run", "--pattern", "#[cfg(test)]", "--lang", "rust", "--json", temp_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            matches = json.loads(result.stdout)
            for match in matches:
                cfg_line = match["range"]["start"]["line"]  # 0-indexed

                # Look at the next non-empty line to see what's being attributed
                j = cfg_line + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1

                if j < len(lines):
                    next_line = lines[j].strip()
                    # Check if this is a mod block that we haven't already captured
                    # Note: We check for exact "mod tests" to avoid skipping modules like "mod hover_tests"
                    mod_match = re.match(r"mod\s+(\w+)\s*\{", next_line)
                    if next_line.startswith("mod ") and mod_match and mod_match.group(1) != "tests":
                        # Try to match this specific module with ast-grep
                        mod_name = mod_match.group(1)
                        result2 = subprocess.run(
                            [
                                resolve_ast_grep(),
                                "run",
                                "--pattern",
                                f"mod {mod_name} {{ $$$BODY }}",
                                "--lang",
                                "rust",
                                "--json",
                                temp_path,
                            ],
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        if result2.returncode == 0 and result2.stdout.strip():
                            matches2 = json.loads(result2.stdout)
                            for m2 in matches2:
                                m_start = m2["range"]["start"]["line"]
                                if abs(m_start - j) <= 1:  # Found the right module
                                    start_line = cfg_line + 1  # Include #[cfg(test)]
                                    end_line = m2["range"]["end"]["line"] + 1
                                    ranges.append((start_line, end_line))
                                    break

        # Merge overlapping ranges
        if ranges:
            ranges = _merge_ranges(ranges)

        return ranges
    except Exception:
        return []
    finally:
        try:
            os.unlink(temp_path)
        except Exception:
            pass


def _merge_ranges(ranges: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Merge overlapping ranges."""
    if not ranges:
        return []

    # Sort by start line
    sorted_ranges = sorted(ranges, key=lambda x: (x[0], -x[1]))

    merged = [sorted_ranges[0]]
    for start, end in sorted_ranges[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + 1:  # Overlapping or adjacent
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))

    return merged


def find_test_code_ranges(file_path: str) -> List[Tuple[int, int, str]]:
    """
    Find all test code regions in a Rust file using ast-grep.

    Detects:
    - #[cfg(test)] mod blocks
    - #[cfg(test)] use/fn statements
    - #[test] functions
    - #[bench] functions
    - #[tokio::test], #[async_std::test] etc.

    Returns list of (start_line, end_line, reason) tuples (1-indexed).
    """
    if not Path(file_path).exists() or not file_path.endswith(".rs"):
        return []

    try:
        with open(file_path, "r") as f:
            lines = f.readlines()

        ranges = []

        # Pattern 1: #[cfg(test)] - modules, use statements, functions
        result = subprocess.run(
            [resolve_ast_grep(), "run", "--pattern", "#[cfg(test)]", "--lang", "rust", "--json", file_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            matches = json.loads(result.stdout)
            for match in matches:
                cfg_test_line = match["range"]["start"]["line"]  # 0-indexed

                # Look at the next non-empty line
                j = cfg_test_line + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1

                if j < len(lines):
                    next_line = lines[j].strip()
                    if next_line.startswith("mod "):
                        # #[cfg(test)] mod block
                        start_line = cfg_test_line + 1
                        end_line = _find_module_end_with_brace_counting(lines, j)
                        if end_line:
                            ranges.append((start_line, end_line, "#[cfg(test)] mod"))
                    elif next_line.startswith("use "):
                        # #[cfg(test)] use statement (single line)
                        ranges.append((cfg_test_line + 1, j + 1, "#[cfg(test)] use"))
                    elif (
                        next_line.startswith("fn ")
                        or next_line.startswith("pub fn ")
                        or next_line.startswith("async fn ")
                        or next_line.startswith("pub async fn ")
                    ):
                        # #[cfg(test)] fn
                        start_line = cfg_test_line + 1
                        end_line = _find_function_end(lines, j)
                        if end_line:
                            ranges.append((start_line, end_line, "#[cfg(test)] fn"))

        # Pattern 2: #[test] functions
        result = subprocess.run(
            [resolve_ast_grep(), "run", "--pattern", "#[test]", "--lang", "rust", "--json", file_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            matches = json.loads(result.stdout)
            for match in matches:
                test_line = match["range"]["start"]["line"]  # 0-indexed
                # Find function end
                j = test_line + 1
                while j < len(lines) and not lines[j].strip().startswith("fn "):
                    j += 1
                if j < len(lines):
                    start_line = test_line + 1
                    end_line = _find_function_end(lines, j)
                    if end_line:
                        ranges.append((start_line, end_line, "#[test] fn"))

        # Pattern 3: #[bench] functions
        result = subprocess.run(
            [resolve_ast_grep(), "run", "--pattern", "#[bench]", "--lang", "rust", "--json", file_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            matches = json.loads(result.stdout)
            for match in matches:
                bench_line = match["range"]["start"]["line"]
                j = bench_line + 1
                while j < len(lines) and not lines[j].strip().startswith("fn "):
                    j += 1
                if j < len(lines):
                    start_line = bench_line + 1
                    end_line = _find_function_end(lines, j)
                    if end_line:
                        ranges.append((start_line, end_line, "#[bench] fn"))

        # Pattern 4: #[tokio::test], #[async_std::test] etc.
        for pattern in ["#[tokio::test]", "#[async_std::test]", "#[actix_rt::test]"]:
            result = subprocess.run(
                [resolve_ast_grep(), "run", "--pattern", pattern, "--lang", "rust", "--json", file_path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                matches = json.loads(result.stdout)
                for match in matches:
                    test_line = match["range"]["start"]["line"]
                    j = test_line + 1
                    while j < len(lines) and not (
                        lines[j].strip().startswith("fn ") or lines[j].strip().startswith("async fn ")
                    ):
                        j += 1
                    if j < len(lines):
                        start_line = test_line + 1
                        end_line = _find_function_end(lines, j)
                        if end_line:
                            ranges.append((start_line, end_line, pattern))

        # Merge overlapping ranges
        ranges = _merge_overlapping_ranges(ranges)
        return ranges

    except Exception:
        return []


def _find_function_end(lines: List[str], start_idx: int) -> Optional[int]:
    """Find the end of a function by counting braces."""
    return _find_module_end_with_brace_counting(lines, start_idx)


def _merge_overlapping_ranges(ranges: List[Tuple[int, int, str]]) -> List[Tuple[int, int, str]]:
    """Merge overlapping ranges, keeping the largest."""
    if not ranges:
        return []

    # Sort by start line
    sorted_ranges = sorted(ranges, key=lambda x: (x[0], -x[1]))

    merged = [sorted_ranges[0]]
    for start, end, reason in sorted_ranges[1:]:
        last_start, last_end, last_reason = merged[-1]
        if start <= last_end:
            # Overlapping - keep the larger range
            if end > last_end:
                merged[-1] = (last_start, end, last_reason)
        else:
            merged.append((start, end, reason))

    return merged


def find_test_module_ranges(file_path: str) -> List[Tuple[int, int]]:
    """
    Find all test code regions in a Rust file.
    Returns list of (start_line, end_line) tuples (1-indexed).
    """
    ranges = find_test_code_ranges(file_path)
    return [(start, end) for start, end, _ in ranges]


def get_actual_modified_lines(hunk: Hunk) -> Tuple[List[int], List[int]]:
    """
    Parse hunk content to get actual modified line numbers.

    Returns (old_deleted_lines, new_added_lines):
    - old_deleted_lines: line numbers of '-' lines in old file
    - new_added_lines: line numbers of '+' lines in new file

    Context lines (space prefix) are ignored.
    """
    old_deleted = []
    new_added = []

    old_line = hunk.old_start
    new_line = hunk.new_start

    for line in hunk.content.split("\n"):
        if line.startswith("@@"):
            continue  # skip hunk header
        elif line.startswith("-"):
            old_deleted.append(old_line)
            old_line += 1
        elif line.startswith("+"):
            new_added.append(new_line)
            new_line += 1
        elif line.startswith(" ") or line == "":
            # Context line or empty line
            old_line += 1
            new_line += 1

    return old_deleted, new_added


def get_modified_lines_with_content(hunk: Hunk) -> Tuple[List[Tuple[int, str]], List[Tuple[int, str]]]:
    """
    Parse hunk content to get actual modified line numbers WITH their content.

    Returns (old_deleted_lines, new_added_lines):
    - old_deleted_lines: list of (line_number, content) tuples for '-' lines
    - new_added_lines: list of (line_number, content) tuples for '+' lines

    Context lines (space prefix) are ignored.
    """
    old_deleted = []
    new_added = []

    old_line = hunk.old_start
    new_line = hunk.new_start

    for line in hunk.content.split("\n"):
        if line.startswith("@@"):
            continue  # skip hunk header
        elif line.startswith("-"):
            # Remove the '-' prefix to get actual content
            content = line[1:] if len(line) > 1 else ""
            old_deleted.append((old_line, content))
            old_line += 1
        elif line.startswith("+"):
            # Remove the '+' prefix to get actual content
            content = line[1:] if len(line) > 1 else ""
            new_added.append((new_line, content))
            new_line += 1
        elif line.startswith(" ") or line == "":
            # Context line or empty line
            old_line += 1
            new_line += 1

    return old_deleted, new_added


def get_insertion_point(hunk: Hunk) -> int:
    """
    For pure addition hunks (no deletions), get the insertion point in old file.

    Returns the old line number after which the new code is inserted.
    This is the last context line BEFORE the first '+' line.
    """
    old_line = hunk.old_start
    last_context_before_add = hunk.old_start - 1  # before first line
    found_addition = False

    for line in hunk.content.split("\n"):
        if line.startswith("@@"):
            continue
        elif line.startswith("-"):
            old_line += 1
        elif line.startswith("+"):
            found_addition = True
            # Don't increment old_line for additions
        elif line.startswith(" "):
            # Only count context lines BEFORE the first addition
            if not found_addition:
                last_context_before_add = old_line
            old_line += 1
        # Ignore empty strings (artifacts from split)

    return last_context_before_add


def classify_hunk(hunk: Hunk, test_ranges: List[Tuple[int, int]]) -> Tuple[str, int, int]:
    """Classify a hunk as 'src', 'test', or 'mixed'.

    Returns (classification, test_lines_count, src_lines_count).

    - 'test': ALL modified lines are in test regions
    - 'src': NO modified lines are in test regions
    - 'mixed': SOME modified lines are in test regions, some are not

    Note: Empty/whitespace-only lines are ignored in classification since they
    don't represent meaningful code changes.
    """
    old_deleted_with_content, new_added_with_content = get_modified_lines_with_content(hunk)

    # Filter out empty/whitespace-only lines - they don't count as src or test
    old_deleted = [(ln, content) for ln, content in old_deleted_with_content if content.strip()]
    new_added = [(ln, content) for ln, content in new_added_with_content if content.strip()]

    if not old_deleted and not new_added:
        return "src", 0, 0

    def line_in_test(line: int) -> bool:
        return any(ts <= line <= te for ts, te in test_ranges)

    # For deletions: count lines in/out of test regions
    if old_deleted:
        in_test = sum(1 for ln, _ in old_deleted if line_in_test(ln))
        not_in_test = len(old_deleted) - in_test

        if in_test == 0:
            return "src", 0, not_in_test
        elif not_in_test == 0:
            return "test", in_test, 0
        else:
            return "mixed", in_test, not_in_test

    # For pure additions: check insertion point
    if new_added:
        insertion_point = get_insertion_point(hunk)
        for test_start, test_end in test_ranges:
            if test_start < insertion_point <= test_end:
                return "test", len(new_added), 0
        return "src", 0, len(new_added)

    return "src", 0, 0


def is_hunk_in_test_region(hunk: Hunk, test_ranges: List[Tuple[int, int]]) -> bool:
    """Check if hunk's actual modifications are ENTIRELY within test regions.

    Only considers actual modified lines ('+' and '-' lines), not context.
    Test range is [test_start, test_end] inclusive.

    Returns True only if ALL modified lines are in test regions.
    A hunk that deletes both src and test code returns False.
    """
    classification, _, _ = classify_hunk(hunk, test_ranges)
    return classification == "test"


def hunk_contains_test_code(hunk: Hunk) -> Tuple[bool, str]:
    """Check if hunk content contains test code markers."""
    test_patterns = [
        (r"^\+.*#\[test\]", "Adds #[test] attribute"),
        (r"^\+.*#\[cfg\(test\)\]", "Adds #[cfg(test)] attribute"),
        (r"^\+\s*mod\s+tests\s*\{", "Adds mod tests block"),
    ]
    for pattern, reason in test_patterns:
        if re.search(pattern, hunk.content, re.MULTILINE):
            return True, reason
    return False, ""


def hunks_are_similar(h1: Hunk, h2: Hunk, tolerance: int = 5) -> bool:
    """Check if two hunks are likely the same (similar position)."""
    return abs(h1.new_start - h2.new_start) <= tolerance and abs(h1.new_count - h2.new_count) <= tolerance


def verify_milestone(old_dir: str, new_dir: str, milestone: str) -> Dict:
    """Verify a milestone at hunk level."""
    old_patch_path = Path(old_dir) / "milestone_patches" / f"{milestone}.patch"
    new_patch_path = Path(new_dir) / "milestone_patches" / f"{milestone}.patch"
    new_testbed = Path(new_dir) / "testbed"
    old_testbed = Path(old_dir) / "testbed"

    if not old_patch_path.exists() or not new_patch_path.exists():
        return {"error": "Patch not found", "overall_success": False}

    with open(old_patch_path) as f:
        old_patch = f.read()
    with open(new_patch_path) as f:
        new_patch = f.read()

    old_hunks = parse_patch_hunks(old_patch)
    new_hunks = parse_patch_hunks(new_patch)

    old_files = set(old_hunks.keys())
    new_files = set(new_hunks.keys())

    result = {
        "milestone": milestone,
        "file_level": {
            "removed_test_files": [],
            "removed_src_files": [],  # ✗ violation
        },
        "hunk_level": {
            "removed_test_hunks": [],
            "removed_src_hunks": [],  # ✗ violation
        },
        "new_patch": {
            "test_path_changes": [],  # ✗ violation
            "test_module_changes": [],  # ✗ violation (strict)
            "src_changes": [],
        },
    }

    # === File-level: Check removed files ===
    # For files not in tests/ directory, check if ALL hunks are in #[cfg(test)] regions
    removed_files = old_files - new_files
    old_start_tag = f"milestone-{milestone}-start"
    for f in removed_files:
        if is_test_path(f):
            result["file_level"]["removed_test_files"].append(f)
        else:
            # Check if all hunks in this file are within test regions
            file_hunks = old_hunks.get(f, [])
            if file_hunks:
                # Get test ranges from start tag state
                file_content = get_file_at_git_ref(old_testbed, f, old_start_tag)
                if file_content:
                    test_ranges = find_test_ranges_from_content(file_content, f)
                else:
                    test_ranges = []

                # Check if ALL hunks are in test regions
                all_in_test = True
                for hunk in file_hunks:
                    if not is_hunk_in_test_region(hunk, test_ranges):
                        all_in_test = False
                        break

                if all_in_test and test_ranges:
                    result["file_level"]["removed_test_files"].append(f)
                else:
                    result["file_level"]["removed_src_files"].append(f)
            else:
                result["file_level"]["removed_src_files"].append(f)

    # === Hunk-level: Check removed hunks in common files ===
    # Use start tag state for test region detection (consistent with analyze)
    common_files = old_files & new_files
    for f in common_files:
        old_file_hunks = old_hunks[f]
        new_file_hunks = new_hunks[f]

        # Get test ranges from START tag state (before patch applied)
        file_content = get_file_at_git_ref(old_testbed, f, old_start_tag)
        if file_content:
            test_ranges = find_test_ranges_from_content(file_content, f)
        else:
            test_ranges = []

        # Find hunks in old but not in new (approximately)
        for old_hunk in old_file_hunks:
            found_match = False
            for new_hunk in new_file_hunks:
                if hunks_are_similar(old_hunk, new_hunk):
                    found_match = True
                    break

            if not found_match:
                # This hunk was removed - check if it's test code
                # Method 1: Check if in test region (using start tag state)
                in_test_region = is_hunk_in_test_region(old_hunk, test_ranges)

                # Method 2: Check hunk content for test markers
                has_test_content, test_reason = hunk_contains_test_code(old_hunk)

                hunk_info = {
                    "file": f,
                    "old_lines": f"{old_hunk.old_start}-{old_hunk.old_start + old_hunk.old_count}",
                    "new_lines": f"{old_hunk.new_start}-{old_hunk.new_start + old_hunk.new_count}",
                    "in_test_region": in_test_region,
                    "has_test_content": has_test_content,
                    "test_reason": (
                        test_reason
                        if has_test_content
                        else (f"In #[cfg(test)] region {test_ranges}" if in_test_region else "")
                    ),
                }

                if in_test_region or has_test_content or is_test_path(f):
                    result["hunk_level"]["removed_test_hunks"].append(hunk_info)
                else:
                    result["hunk_level"]["removed_src_hunks"].append(hunk_info)

    # === New patch: Check for test code ===
    # Use start tag state for test region detection (hunk line numbers are relative to start tag)
    start_tag = f"milestone-{milestone}-start"
    for f, hunks in new_hunks.items():
        # Get test ranges from START tag state (before patch applied)
        file_content = get_file_at_git_ref(new_testbed, f, start_tag)
        if file_content:
            test_ranges = find_test_ranges_from_content(file_content, f)
        else:
            test_ranges = []

        for hunk in hunks:
            hunk_info = {
                "file": f,
                "lines": f"{hunk.new_start}-{hunk.new_start + hunk.new_count}",
            }

            if is_test_path(f):
                result["new_patch"]["test_path_changes"].append(hunk_info)
            else:
                # Check actual modified lines (not entire hunk range with context)
                in_test = is_hunk_in_test_region(hunk, test_ranges)
                if in_test:
                    hunk_info["test_region"] = test_ranges
                    result["new_patch"]["test_module_changes"].append(hunk_info)
                else:
                    result["new_patch"]["src_changes"].append(hunk_info)

    # === Determine success ===
    result["file_level"]["success"] = len(result["file_level"]["removed_src_files"]) == 0
    result["hunk_level"]["success"] = len(result["hunk_level"]["removed_src_hunks"]) == 0
    result["new_patch"]["success"] = (
        len(result["new_patch"]["test_path_changes"]) == 0 and len(result["new_patch"]["test_module_changes"]) == 0
    )

    result["overall_success"] = (
        result["file_level"]["success"] and result["hunk_level"]["success"] and result["new_patch"]["success"]
    )

    return result


def get_file_at_git_ref(testbed: Path, file_path: str, ref: str) -> Optional[str]:
    """Get file content at a specific git ref."""
    try:
        result = subprocess.run(
            ["git", "show", f"{ref}:{file_path}"], capture_output=True, text=True, cwd=testbed, timeout=10
        )
        if result.returncode == 0:
            return result.stdout
        return None
    except Exception:
        return None


def find_test_ranges_from_content(content: str, file_path: str) -> List[Tuple[int, int]]:
    """Find test code ranges from file content string.

    Uses ast-grep for accurate detection of test code regions,
    properly handling braces inside string literals.
    """
    # Use ast-grep based detection for accurate results
    return _find_test_ranges_with_ast_grep(content, file_path)


def analyze_patch_test_hunks(
    base_dir: str, milestone: str, patches_subdir: str = "milestone_patches", ref_tag: str = None
) -> Dict:
    """
    Analyze a single patch to find all test-related hunks.

    Uses the specified tag state to determine test regions (more accurate).
    Returns dict with test_hunks, src_hunks, and mixed_hunks lists.

    Args:
        ref_tag: Git tag/ref to use for test region detection. If None, uses 'milestone-{milestone}-start'.
                 For milestone_patches/start_diff_patches, should use 'milestone-{milestone}-start-old'.
    """
    patch_path = Path(base_dir) / patches_subdir / f"{milestone}.patch"
    testbed = Path(base_dir) / "testbed"
    if ref_tag is None:
        ref_tag = f"milestone-{milestone}-start"

    if not patch_path.exists():
        return {"error": "Patch not found"}

    with open(patch_path) as f:
        patch_content = f.read()

    hunks = parse_patch_hunks(patch_content)

    result = {
        "milestone": milestone,
        "test_hunks": [],
        "src_hunks": [],
        "mixed_hunks": [],
    }

    for f, file_hunks in hunks.items():
        # Get test ranges from specified tag state (before patch applied)
        file_content = get_file_at_git_ref(testbed, f, ref_tag)
        if file_content:
            test_ranges = find_test_ranges_from_content(file_content, f)
        else:
            test_ranges = []

        for hunk in file_hunks:
            hunk_info = {
                "file": f,
                "old_lines": f"{hunk.old_start}-{hunk.old_start + hunk.old_count}",
                "new_lines": f"{hunk.new_start}-{hunk.new_start + hunk.new_count}",
            }

            # Method 1: Test path - always test
            if is_test_path(f):
                hunk_info["reason"] = "test path"
                result["test_hunks"].append(hunk_info)
                continue

            # Method 2: Classify by test region
            if test_ranges:
                classification, test_lines, src_lines = classify_hunk(hunk, test_ranges)

                if classification == "test":
                    # Find which test region
                    old_deleted, _ = get_actual_modified_lines(hunk)
                    reason = ""
                    for ts, te in test_ranges:
                        if old_deleted:
                            if any(ts <= l <= te for l in old_deleted):
                                reason = f"in test region [{ts}-{te}]"
                                break
                        else:
                            insertion_point = get_insertion_point(hunk)
                            if ts < insertion_point <= te:
                                reason = f"inserted in test region [{ts}-{te}]"
                                break
                    hunk_info["reason"] = reason
                    result["test_hunks"].append(hunk_info)
                    continue

                elif classification == "mixed":
                    hunk_info["reason"] = f"mixed: {test_lines} test lines + {src_lines} src lines"
                    hunk_info["test_lines"] = test_lines
                    hunk_info["src_lines"] = src_lines
                    result["mixed_hunks"].append(hunk_info)
                    continue

            # Method 3: Content contains test markers
            has_test, content_reason = hunk_contains_test_code(hunk)
            if has_test:
                hunk_info["reason"] = content_reason
                result["test_hunks"].append(hunk_info)
                continue

            # Default: src hunk
            hunk_info["reason"] = ""
            result["src_hunks"].append(hunk_info)

    result["summary"] = {
        "total_hunks": len(result["test_hunks"]) + len(result["src_hunks"]) + len(result["mixed_hunks"]),
        "test_hunks": len(result["test_hunks"]),
        "src_hunks": len(result["src_hunks"]),
        "mixed_hunks": len(result["mixed_hunks"]),
    }

    return result


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Verify test/src separation in milestone patches (using ast-grep)")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Verify command (default behavior)
    verify_parser = subparsers.add_parser("verify", help="Verify test/src separation between two directories")
    verify_parser.add_argument("old_dir", help="Old baseline directory (with original patches)")
    verify_parser.add_argument("new_dir", help="New baseline directory (with updated patches)")
    verify_parser.add_argument("--milestone", help="Verify specific milestone only")
    verify_parser.add_argument("--json", action="store_true", help="Output as JSON")
    verify_parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed test hunk info")

    # Analyze command
    analyze_parser = subparsers.add_parser("analyze", help="Analyze test hunks in a single directory")
    analyze_parser.add_argument("base_dir", help="Baseline directory to analyze")
    analyze_parser.add_argument("--milestone", help="Analyze specific milestone only")
    analyze_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # Check command - verify milestone_patches has only src, milestone_patches/start_diff_patches has only test
    check_parser = subparsers.add_parser(
        "check",
        help="Check that milestone_patches has only src changes and milestone_patches/start_diff_patches has only test changes",
    )
    check_parser.add_argument("base_dir", help="Baseline directory to check")
    check_parser.add_argument("--milestone", help="Check specific milestone only")
    check_parser.add_argument("--json", action="store_true", help="Output as JSON")
    check_parser.add_argument(
        "--selected",
        action="store_true",
        help="Use selected_milestone_ids.txt to determine which milestones to check",
    )
    check_parser.add_argument(
        "--regenerate",
        action="store_true",
        help="Regenerate patches from testbed git tags (start->end for milestone_patches, start-old->start for start_diff_patches) before verification",
    )

    args = parser.parse_args()

    # Default to verify if no command specified (backward compatibility)
    if args.command is None:
        # Check if old positional args style
        if len(sys.argv) >= 3 and not sys.argv[1].startswith("-"):
            # Legacy mode: old_dir new_dir
            args.command = "verify"
            args.old_dir = sys.argv[1]
            args.new_dir = sys.argv[2]
            args.milestone = None
            args.json = "--json" in sys.argv
            args.verbose = "-v" in sys.argv or "--verbose" in sys.argv
        else:
            parser.print_help()
            return 1

    if args.command == "analyze":
        return run_analyze(args)
    elif args.command == "check":
        return run_check(args)
    else:
        return run_verify(args)


def run_check(args):
    """Run check command - verify milestone_patches has only src, milestone_patches/start_diff_patches has only test."""
    base_dir = Path(args.base_dir)
    src_patches_dir = base_dir / "milestone_patches"
    test_patches_dir = base_dir / "milestone_patches" / "start_diff_patches"

    if not src_patches_dir.exists():
        print(f"Error: {src_patches_dir} does not exist")
        return 1

    # Get milestones
    if args.milestone:
        milestones = [args.milestone]
    elif getattr(args, "selected", False):
        # Read from selected_milestone_ids.txt
        selected_file = base_dir / "selected_milestone_ids.txt"
        if not selected_file.exists():
            print(f"Error: {selected_file} does not exist")
            return 1
        milestones = [
            line.strip()
            for line in selected_file.read_text().strip().split("\n")
            if line.strip() and not line.strip().startswith("#")
        ]
    else:
        # Default: only check milestones that have start_diff_patches
        if not test_patches_dir.exists():
            print(f"Error: {test_patches_dir} does not exist")
            return 1
        src_milestones = set(f.stem for f in src_patches_dir.glob("*.patch"))
        test_milestones = set(f.stem for f in test_patches_dir.glob("*.patch"))
        milestones = sorted(src_milestones | test_milestones)

    results = {}
    all_pass = True

    regenerate = getattr(args, "regenerate", False)

    for m in milestones:
        result = {"milestone": m, "src_patch": {}, "test_patch": {}}

        # Check milestone_patches (should have 0 test hunks)
        src_patch_path = src_patches_dir / f"{m}.patch"

        # If --regenerate is set, always regenerate from testbed
        # Otherwise, only regenerate if patch file doesn't exist
        should_regenerate = regenerate or not src_patch_path.exists()

        if not should_regenerate:
            # Use existing patch file
            src_result = analyze_patch_test_hunks(str(base_dir), m, patches_subdir="milestone_patches")
            if "error" not in src_result:
                test_count = src_result["summary"].get("test_hunks", 0)
                mixed_count = src_result["summary"].get("mixed_hunks", 0)
                result["src_patch"] = {
                    "test_hunks": test_count,
                    "src_hunks": src_result["summary"].get("src_hunks", 0),
                    "mixed_hunks": mixed_count,
                    "pass": test_count == 0 and mixed_count == 0,
                    "violations": src_result.get("test_hunks", []) + src_result.get("mixed_hunks", []),
                }
                if not result["src_patch"]["pass"]:
                    all_pass = False
            else:
                result["src_patch"] = {"error": src_result["error"]}
        else:
            # Regenerate from testbed tags (or generate if file doesn't exist)
            testbed_dir = base_dir / "testbed"
            start_tag = f"milestone-{m}-start"
            end_tag = f"milestone-{m}-end"
            start_old_tag = f"milestone-{m}-start-old"

            if testbed_dir.exists():
                # Check if tags exist
                try:
                    tag_check = subprocess.run(
                        ["git", "tag", "-l", start_tag, end_tag, start_old_tag],
                        cwd=testbed_dir,
                        capture_output=True,
                        text=True,
                    )
                    existing_tags = [t for t in tag_check.stdout.strip().split("\n") if t]
                    has_start = start_tag in existing_tags
                    has_end = end_tag in existing_tags
                    has_start_old = start_old_tag in existing_tags

                    if has_start and has_end:
                        # Generate diff start->end and save to milestone_patches/
                        diff_result = subprocess.run(
                            ["git", "diff", start_tag, end_tag], cwd=testbed_dir, capture_output=True, text=True
                        )
                        if diff_result.returncode == 0:
                            diff_content = diff_result.stdout

                            # Save to milestone_patches/
                            milestone_patch_path = src_patches_dir / f"{m}.patch"
                            milestone_patch_path.write_text(diff_content)

                            # Also generate start_diff_patches if start-old exists
                            if has_start_old:
                                start_diff_result = subprocess.run(
                                    ["git", "diff", start_old_tag, start_tag],
                                    cwd=testbed_dir,
                                    capture_output=True,
                                    text=True,
                                )
                                if start_diff_result.returncode == 0 and start_diff_result.stdout.strip():
                                    # Ensure start_diff_patches directory exists
                                    test_patches_dir.mkdir(parents=True, exist_ok=True)
                                    start_diff_patch_path = test_patches_dir / f"{m}.patch"
                                    start_diff_patch_path.write_text(start_diff_result.stdout)

                            # Analyze the diff for test hunks
                            hunks = parse_patch_hunks(diff_content)
                            test_hunks = 0
                            src_hunks = 0
                            mixed_hunks = 0
                            test_violations = []

                            for file_path, file_hunks in hunks.items():
                                if is_test_path(file_path):
                                    test_hunks += len(file_hunks)
                                else:
                                    # Check each hunk for test code
                                    for hunk in file_hunks:
                                        hunk_type = classify_hunk_simple(hunk.content)
                                        if hunk_type == "test":
                                            test_hunks += 1
                                            test_violations.append(
                                                {
                                                    "file": file_path,
                                                    "new_lines": f"{hunk.new_start}-{hunk.new_start + hunk.new_count}",
                                                }
                                            )
                                        elif hunk_type == "mixed":
                                            mixed_hunks += 1
                                            test_violations.append(
                                                {
                                                    "file": file_path,
                                                    "new_lines": f"{hunk.new_start}-{hunk.new_start + hunk.new_count}",
                                                }
                                            )
                                        else:
                                            src_hunks += 1

                            if test_hunks == 0 and mixed_hunks == 0:
                                # No test code - no separation needed
                                result["src_patch"] = {
                                    "pass": True,
                                    "src_hunks": src_hunks,
                                    "test_hunks": 0,
                                    "mixed_hunks": 0,
                                    "from_testbed": True,
                                    "saved_to": str(milestone_patch_path),
                                    "note": "generated from testbed diff, no separation needed",
                                }
                            else:
                                # Has test code - needs separation
                                result["src_patch"] = {
                                    "pass": False,
                                    "src_hunks": src_hunks,
                                    "test_hunks": test_hunks,
                                    "mixed_hunks": mixed_hunks,
                                    "from_testbed": True,
                                    "saved_to": str(milestone_patch_path),
                                    "violations": test_violations[:5],
                                    "note": "generated from testbed diff, needs separation",
                                }
                                all_pass = False
                        else:
                            result["src_patch"] = {"skip": "git diff failed"}
                    else:
                        missing = []
                        if not has_start:
                            missing.append(start_tag)
                        if not has_end:
                            missing.append(end_tag)
                        result["src_patch"] = {"skip": f"missing tags: {', '.join(missing)}"}
                except Exception as e:
                    result["src_patch"] = {"error": str(e)}
            else:
                # Check if original patch exists
                original_patch_path = base_dir / "milestone_patches_original" / f"{m}.patch"
                if original_patch_path.exists():
                    result["src_patch"] = {"skip": "not processed (original exists, no testbed)"}
                else:
                    result["src_patch"] = {"skip": "not found"}

        # Check milestone_patches/start_diff_patches (should have 0 src hunks)
        # Use start-old tag for test region detection since diff is from old start to current start
        test_patch_path = test_patches_dir / f"{m}.patch" if test_patches_dir.exists() else None
        if test_patch_path and test_patch_path.exists():
            old_start_tag = f"milestone-{m}-start-old"
            test_result = analyze_patch_test_hunks(
                str(base_dir), m, patches_subdir="milestone_patches/start_diff_patches", ref_tag=old_start_tag
            )
            if "error" not in test_result:
                src_count = test_result["summary"].get("src_hunks", 0)
                mixed_count = test_result["summary"].get("mixed_hunks", 0)
                result["test_patch"] = {
                    "test_hunks": test_result["summary"].get("test_hunks", 0),
                    "src_hunks": src_count,
                    "mixed_hunks": mixed_count,
                    "pass": src_count == 0 and mixed_count == 0,
                    "violations": test_result.get("src_hunks", []) + test_result.get("mixed_hunks", []),
                }
                if not result["test_patch"]["pass"]:
                    all_pass = False
            else:
                result["test_patch"] = {"error": test_result["error"]}
        else:
            # No start_diff_patches - check if separation is needed
            if getattr(args, "selected", False):
                # When using --selected, we need to verify if this milestone needs separation
                # It needs separation if milestone_patches contains test code
                src = result.get("src_patch", {})
                if "error" in src or "skip" in src:
                    result["test_patch"] = {"skip": "not found (src patch unavailable)"}
                else:
                    test_hunks_in_src = src.get("test_hunks", 0)
                    mixed_hunks_in_src = src.get("mixed_hunks", 0)
                    if test_hunks_in_src == 0 and mixed_hunks_in_src == 0:
                        # No test code in milestone_patches, no separation needed
                        result["test_patch"] = {"skip": "not needed (no test code in src patch)"}
                    else:
                        # Test code exists in milestone_patches but no start_diff_patches
                        # This means separation is needed but not done
                        result["test_patch"] = {
                            "needs_separation": True,
                            "test_hunks_in_src": test_hunks_in_src,
                            "mixed_hunks_in_src": mixed_hunks_in_src,
                            "pass": False,
                        }
                        all_pass = False
            else:
                result["test_patch"] = {"skip": "not found"}

        # Update all_pass based on final status
        src = result.get("src_patch", {})
        test = result.get("test_patch", {})
        src_ok = src.get("pass", True) or "skip" in src or "error" in src
        if test.get("needs_separation"):
            test_ok = False
        elif "skip" in test:
            skip_reason = test.get("skip", "")
            test_ok = "not needed" in skip_reason
        else:
            test_ok = test.get("pass", True) or "error" in test
        if not (src_ok and test_ok):
            all_pass = False

        results[m] = result

    if args.json:
        print(json.dumps(results, indent=2))
        return 0 if all_pass else 1

    # Print results
    print("Check test/src separation")
    print(f"Directory: {base_dir}")
    print("=" * 70)

    for m, r in results.items():
        src = r["src_patch"]
        test = r["test_patch"]

        # Determine overall status
        src_ok = src.get("pass", True) or "skip" in src or "error" in src
        # For test_ok: needs_separation means FAIL, "not needed" skip means PASS
        if test.get("needs_separation"):
            test_ok = False
        elif "skip" in test:
            skip_reason = test.get("skip", "")
            test_ok = "not needed" in skip_reason  # "not needed" is OK, otherwise unknown
        else:
            test_ok = test.get("pass", True) or "error" in test
        status = "✓ PASS" if (src_ok and test_ok) else "✗ FAIL"

        print(f"\n{m}: {status}")

        # milestone_patches check
        if "skip" in src:
            print(f"  [milestone_patches] skipped ({src['skip']})")
        elif "error" in src:
            print(f"  [milestone_patches] error: {src['error']}")
        else:
            from_testbed = " (from testbed)" if src.get("from_testbed") else ""
            if src["pass"]:
                print(f"  [milestone_patches] ✓ {src['src_hunks']} src, 0 test hunks{from_testbed}")
            else:
                print(
                    f"  [milestone_patches] ✗ {src['src_hunks']} src, {src['test_hunks']} test, {src['mixed_hunks']} mixed hunks{from_testbed}"
                )
                for v in src.get("violations", [])[:3]:
                    print(f"      ✗ {v['file']} lines {v['new_lines']}")

        # milestone_patches/start_diff_patches check
        if "skip" in test:
            skip_reason = test.get("skip", "not found")
            if "not needed" in skip_reason:
                print(f"  [start_diff_patches] ✓ skipped ({skip_reason})")
            else:
                print(f"  [start_diff_patches] skipped ({skip_reason})")
        elif "error" in test:
            print(f"  [start_diff_patches] error: {test['error']}")
        elif test.get("needs_separation"):
            # Test code exists in src but no separation done
            print(
                f"  [start_diff_patches] ✗ NEEDS SEPARATION ({test['test_hunks_in_src']} test, {test['mixed_hunks_in_src']} mixed hunks in src patch)"
            )
        else:
            if test["pass"]:
                print(f"  [start_diff_patches] ✓ {test['test_hunks']} test, 0 src hunks")
            else:
                print(f"  [start_diff_patches] ✗ {test['test_hunks']} test, {test['src_hunks']} src hunks")
                for v in test.get("violations", [])[:3]:
                    print(f"      ✗ {v['file']} lines {v['new_lines']}")

    print(f"\n{'=' * 70}")
    print(f"Overall: {'ALL PASS ✓' if all_pass else 'SOME FAILURES ✗'}")
    return 0 if all_pass else 1


def run_analyze(args):
    """Run analyze command."""
    base_dir = args.base_dir
    patches_dir = Path(base_dir) / "milestone_patches"

    if args.milestone:
        milestones = [args.milestone]
    else:
        milestones = sorted(f.stem for f in patches_dir.glob("*.patch"))

    results = {}
    for m in milestones:
        results[m] = analyze_patch_test_hunks(base_dir, m)

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    print(f"Analyze test hunks in patches (using ast-grep)")
    print(f"Directory: {base_dir}")
    print("=" * 70)

    for m, r in results.items():
        if "error" in r:
            print(f"\n{m}: Error - {r['error']}")
            continue

        summary = r["summary"]
        parts = [f"{summary['test_hunks']} test", f"{summary['src_hunks']} src"]
        if summary.get("mixed_hunks", 0) > 0:
            parts.append(f"{summary['mixed_hunks']} mixed")
        print(f"\n{m}: {', '.join(parts)} hunks")

        if r["test_hunks"]:
            print("  Test hunks:")
            for h in r["test_hunks"]:
                print(f"    ✓ {h['file']} lines {h['new_lines']} ({h['reason']})")

        if r.get("mixed_hunks"):
            print("  Mixed hunks (src+test):")
            for h in r["mixed_hunks"]:
                print(f"    ⚠ {h['file']} lines {h['new_lines']} ({h['reason']})")

    print(f"\n{'=' * 70}")
    total_test = sum(r.get("summary", {}).get("test_hunks", 0) for r in results.values())
    total_src = sum(r.get("summary", {}).get("src_hunks", 0) for r in results.values())
    total_mixed = sum(r.get("summary", {}).get("mixed_hunks", 0) for r in results.values())
    parts = [f"{total_test} test", f"{total_src} src"]
    if total_mixed > 0:
        parts.append(f"{total_mixed} mixed")
    print(f"Total: {', '.join(parts)} hunks")

    return 0


def run_verify(args):
    """Run verify command."""
    old_dir = args.old_dir
    new_dir = args.new_dir

    old_patches = Path(old_dir) / "milestone_patches"
    new_patches = Path(new_dir) / "milestone_patches"

    if args.milestone:
        milestones = [args.milestone]
    else:
        milestones = sorted(
            set(f.stem for f in old_patches.glob("*.patch")) & set(f.stem for f in new_patches.glob("*.patch"))
        )

    results = {}
    for m in milestones:
        results[m] = verify_milestone(args.old_dir, args.new_dir, m)

    if args.json:
        print(json.dumps(results, indent=2))
        return

    print("Verify test/src separation (using ast-grep)")
    print("=" * 70)

    all_pass = True
    for m, r in results.items():
        success = r.get("overall_success", False)
        status = "✓ PASS" if success else "✗ FAIL"
        if not success:
            all_pass = False

        print(f"\n{m}: {status}")

        if "error" in r:
            print(f"  Error: {r['error']}")
            continue

        # File level
        fl = r["file_level"]
        print(f"  [File Level] Removed: {len(fl['removed_test_files'])} test files", end="")
        if fl["removed_src_files"]:
            print(f", ⚠ {len(fl['removed_src_files'])} src files")
            for f in fl["removed_src_files"]:
                print(f"      ✗ {f}")
        else:
            print()

        # Hunk level
        hl = r["hunk_level"]
        print(f"  [Hunk Level] Removed: {len(hl['removed_test_hunks'])} test hunks", end="")
        if hl["removed_src_hunks"]:
            print(f", ⚠ {len(hl['removed_src_hunks'])} src hunks")
            for h in hl["removed_src_hunks"][:3]:  # Show first 3
                print(f"      ✗ {h['file']} lines {h['new_lines']}")
            if len(hl["removed_src_hunks"]) > 3:
                print(f"      ... and {len(hl['removed_src_hunks']) - 3} more")
        else:
            print()

        if args.verbose and hl["removed_test_hunks"]:
            print(f"    Test hunks removed:")
            for h in hl["removed_test_hunks"][:5]:
                print(f"      ✓ {h['file']} lines {h['new_lines']} - {h['test_reason']}")

        # New patch
        np = r["new_patch"]
        print(f"  [New Patch] {len(np['src_changes'])} src changes", end="")

        test_violations = np["test_path_changes"] + np["test_module_changes"]
        if test_violations:
            print(f", ⚠ {len(test_violations)} test changes")
            for h in test_violations[:3]:
                print(f"      ✗ {h['file']} lines {h['lines']}")
            if len(test_violations) > 3:
                print(f"      ... and {len(test_violations) - 3} more")
        else:
            print()

    print(f"\n{'=' * 70}")
    print(f"Overall: {'ALL PASS ✓' if all_pass else 'SOME FAILURES ✗'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
