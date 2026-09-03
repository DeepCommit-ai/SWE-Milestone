#!/usr/bin/env python3
"""Test masking utilities to prevent information leakage during agent evaluation.

This module masks test files and inline test modules so that agents cannot
read expected test behaviors during code generation.

Supports multiple languages with unified test name parsing:
- Python (pytest): tests/test_foo.py::TestClass::test_method[param]
- TypeScript/JS (Jest/Vitest): /testbed/path/to/file.test.ts::describe it
- Java (Maven): module::org.package.TestClass::testMethod
- Go (Ginkgo): github.com/org/repo/pkg::Describe > Context > It (needs search)
- Rust (cargo): module::tests::test_fn (needs search)

For Python, JS/TS, and Java: file path is extracted directly from test name.
For Go and Rust: file path is found by searching in the container.

Special handling:
- Rust source files with inline #[cfg(test)] modules: removes inline tests
- All other test files: chmod 000 to prevent reading
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Literal

from harness.utils.rust_test_filter import (
    RustTestFilterError,
    _read_file_from_container,
    _write_file_to_container,
    find_test_ranges_from_content,
    remove_test_regions,
)
from harness.utils.src_filter import SrcFileFilter

logger = logging.getLogger("e2e.test_masking")


class TestMappingError(Exception):
    """Raised when test names cannot be mapped to files.

    This indicates either:
    1. A new/unsupported test framework
    2. Invalid test name format
    3. Test files that don't exist in the container
    """

    def __init__(self, message: str, unmapped_tests: list[str], details: dict | None = None):
        self.unmapped_tests = unmapped_tests
        self.details = details or {}
        super().__init__(message)

    def __str__(self):
        msg = super().__str__()
        if self.unmapped_tests:
            samples = self.unmapped_tests[:10]
            msg += f"\n\nUnmapped tests ({len(self.unmapped_tests)} total):\n"
            for t in samples:
                msg += f"  - {t}\n"
            if len(self.unmapped_tests) > 10:
                msg += f"  ... and {len(self.unmapped_tests) - 10} more\n"
        if self.details:
            msg += f"\nDetails: {self.details}"
        return msg


# File type constants - simplified to only what matters for masking strategy
FileType = Literal["rust_src", "test_file"]

# Source file extensions that might contain inline tests (Rust only for now)
INLINE_TEST_EXTENSIONS = (".rs",)

# All supported test file extensions (for direct file path extraction)
TEST_FILE_EXTENSIONS = (
    # Python
    ".py",
    # JavaScript/TypeScript
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    # Java
    ".java",
)


def detect_file_type(file_path: str, src_filter: SrcFileFilter | None = None) -> FileType:
    """Detect the type of a file for masking strategy.

    Simplified to two types:
    - rust_src: Rust source files that may contain inline #[cfg(test)] modules
    - test_file: All other test files (chmod 000)

    Args:
        file_path: Path to the file
        src_filter: Optional SrcFileFilter for additional context

    Returns:
        FileType indicating how to mask this file
    """
    # Rust source files with inline tests need special handling
    if file_path.endswith(".rs"):
        # Files in tests/ directory are standalone test files
        if file_path.startswith("tests/") or "/tests/" in file_path:
            return "test_file"
        # Source files may have inline #[cfg(test)] modules
        if src_filter and src_filter.is_src_file(file_path):
            return "rust_src"
        # Default to test_file for safety
        return "test_file"

    # All other files are test files (chmod 000)
    return "test_file"


def extract_file_from_test_name(test_name: str) -> tuple[str | None, str]:
    """Extract file path from test name for languages with direct file paths.

    Supports:
    - Python (pytest): tests/test_foo.py::TestClass::test_method[param]
    - TypeScript/JS (Jest): /testbed/path/to/file.test.ts::describe it
    - Java (Maven): module::org.package.TestClass::testMethod

    Args:
        test_name: The test name from test results

    Returns:
        Tuple of (file_path or None, extraction_method)
        extraction_method is one of: "python", "js_ts", "java", "go", "rust", "unknown"
    """
    if "::" not in test_name:
        # Go Ginkgo format: github.com/org/repo/pkg/TestFunc/subtest
        if test_name.startswith("github.com/"):
            return None, "go"
        # Rust standalone test file format: bare function name without module prefix
        # e.g., "test_float_equality_comparison" or "gitignore_skip_bom"
        # from crates/*/tests/*.rs - cargo test outputs just the function name
        # Note: Rust doesn't require test_ prefix - any #[test] fn is valid
        return None, "rust"

    prefix = test_name.split("::", 1)[0]

    # Python: tests/test_foo.py::...
    if prefix.endswith(".py"):
        # Remove /testbed/ prefix if present
        file_path = prefix.removeprefix("/testbed/").lstrip("/")
        return file_path, "python"

    # JavaScript/TypeScript: /testbed/path/to/file.test.ts::... or path/to/file.test.js::...
    js_ts_extensions = (".js", ".jsx", ".ts", ".tsx")
    if prefix.endswith(js_ts_extensions):
        file_path = prefix.removeprefix("/testbed/").lstrip("/")
        return file_path, "js_ts"

    # Java: module::org.package.ClassName::method
    # The prefix is module name, second part is class name
    parts = test_name.split("::")
    if len(parts) >= 2:
        potential_class = parts[1]
        # Java class names have dots (org.apache.dubbo.xxx.TestClass)
        if "." in potential_class and not potential_class.startswith("github.com"):
            module = parts[0]
            class_name = potential_class
            # Convert class name to file path
            # org.apache.dubbo.rpc.TestClass -> org/apache/dubbo/rpc/TestClass.java
            class_path = class_name.replace(".", "/") + ".java"
            file_path = f"{module}/src/test/java/{class_path}"
            return file_path, "java"

    # Go Ginkgo format: github.com/org/repo/pkg::Describe > Context > It
    if prefix.startswith("github.com/"):
        return None, "go"

    # Rust format: module::tests::test_fn (no file extension in prefix)
    # Need to search for file
    return None, "rust"


def mask_inline_tests_in_file(
    container_name: str,
    file_path: str,
) -> bool:
    """Remove inline test code from a Rust source file.

    Uses ast-grep based detection from rust_test_filter to accurately find
    and remove #[cfg(test)] modules and other test code.

    Args:
        container_name: Docker container name
        file_path: Path to Rust source file (relative to /testbed)

    Returns:
        True if tests were found and removed, False otherwise
    """
    content = _read_file_from_container(container_name, file_path)
    if content is None:
        logger.error(f"Failed to read {file_path}")
        return False

    # Find test ranges using ast-grep based detection
    # only_root_level=True to avoid breaking nested modules/impl blocks
    # Agent-side masking can only remove root items without breaking their
    # lexical scope.  Nested test helpers are restored authoritatively by the
    # evaluator's scope-preserving merger; keep the historical masking behavior
    # here instead of blocking otherwise runnable agents — a detector failure
    # on one file skips that file, it must not abort the whole setup.
    try:
        test_ranges = find_test_ranges_from_content(
            content,
            file_path,
            only_root_level=True,
            reject_nested=False,
        )
    except RustTestFilterError as exc:
        logger.warning(f"Skipping mask for {file_path}: test detection failed ({exc})")
        return False

    if not test_ranges:
        logger.debug(f"No inline tests found in {file_path}")
        return False

    logger.info(f"Found {len(test_ranges)} test regions in {file_path}")

    # Remove test regions
    masked_content = remove_test_regions(content, test_ranges)

    # Write back
    if _write_file_to_container(container_name, file_path, masked_content):
        logger.info(f"Masked inline tests in {file_path}")
        return True
    else:
        logger.error(f"Failed to write masked content to {file_path}")
        return False


def _find_bare_rust_test(
    container_name: str,
    test_name: str,
    src_dirs: list[str],
    workdir: str = "/testbed",
) -> str | None:
    """Find the file containing a bare Rust test function name.

    For tests in crates/*/tests/*.rs, cargo test outputs just the function name
    without module prefix (e.g., "test_float_equality_comparison").

    This function searches for the test function in:
    1. crates/*/tests/*.rs files
    2. tests/*.rs files in src_dirs

    Args:
        container_name: Docker container name
        test_name: Bare test function name (e.g., "test_float_equality_comparison")
        src_dirs: Source directories to search
        workdir: Working directory in container

    Returns:
        File path if found, None otherwise
    """
    # Build search paths: look in tests/ subdirectories under crates
    search_paths = []
    for src_dir in src_dirs:
        # For "crates/" style dirs, search crates/*/tests/
        if "crates" in src_dir:
            search_paths.append(src_dir.rstrip("/"))
        else:
            search_paths.append(src_dir.rstrip("/"))

    # Also search top-level tests/ directory
    search_paths.append("tests")

    # Use grep to find the test function definition
    # Pattern: fn test_name( or fn test_name<
    pattern = f"fn {test_name}\\s*[(<]"

    for search_path in search_paths:
        # First check if path exists
        check_cmd = ["docker", "exec", "--user", "root", "-w", workdir, container_name, "test", "-d", search_path]
        if subprocess.run(check_cmd, capture_output=True).returncode != 0:
            continue

        # Search for the test function
        grep_cmd = [
            "docker",
            "exec",
            "--user",
            "root",
            "-w",
            workdir,
            container_name,
            "grep",
            "-r",
            "-l",
            "-E",
            pattern,
            search_path,
            "--include=*.rs",
        ]
        result = subprocess.run(grep_cmd, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            # Return the first matching file
            file_path = result.stdout.strip().split("\n")[0]
            return file_path.lstrip("./")

    return None


def map_rust_tests_to_files(
    container_name: str,
    test_names: list[str],
    src_dirs: list[str],
    workdir: str = "/testbed",
) -> dict[str, list[str]]:
    """Map Rust test names to their source files.

    Rust test naming conventions:
    1. Integration tests (tests/*.rs):
       - "regression::r3127" -> tests/regression.rs
       - "feature::test_foo" -> tests/feature.rs
       - Format: {file_stem}::{test_fn} (only 2 parts, no "tests" submodule)

    2. Inline unit tests (in src files):
       - "glob::tests::matchalt18" -> crates/*/src/glob.rs
       - Format: {module}::tests::{test_fn} (has "tests" submodule)

    Args:
        container_name: Docker container name
        test_names: List of test names
        src_dirs: Source directories to search
        workdir: Working directory in container

    Returns:
        Dict mapping file paths to list of test names in that file
    """
    file_to_tests: dict[str, list[str]] = {}

    for test_name in test_names:
        parts = test_name.split("::")

        # Handle bare test function names (no module prefix)
        # e.g., "test_float_equality_comparison" or "gitignore_skip_bom" from crates/*/tests/*.rs
        # Note: Rust doesn't require test_ prefix - any #[test] fn is valid
        if len(parts) < 2:
            # Search for the function in all test files under crates/*/tests/
            found = _find_bare_rust_test(container_name, test_name, src_dirs, workdir)
            if found:
                if found not in file_to_tests:
                    file_to_tests[found] = []
                file_to_tests[found].append(test_name)
            continue

        module_name = parts[0]  # e.g., "glob", "regression"

        # Determine if this is an integration test or inline test
        # Integration tests: module::test_fn (no "tests" submodule)
        # Inline tests: module::tests::test_fn (has "tests" submodule)
        is_integration_test = len(parts) == 2 or (len(parts) > 1 and parts[1] != "tests")

        if is_integration_test:
            # Integration test: check tests/{module_name}.rs
            integration_path = f"tests/{module_name}.rs"
            check_cmd = [
                "docker",
                "exec",
                "--user",
                "root",
                "-w",
                workdir,
                container_name,
                "test",
                "-f",
                integration_path,
            ]
            result = subprocess.run(check_cmd, capture_output=True)
            if result.returncode == 0:
                if integration_path not in file_to_tests:
                    file_to_tests[integration_path] = []
                file_to_tests[integration_path].append(test_name)
                continue

        # Inline test: search for {module_name}.rs in src_dirs
        search_cmd = (
            [
                "docker",
                "exec",
                "--user",
                "root",
                "-w",
                workdir,
                container_name,
                "find",
            ]
            + src_dirs
            + [
                "-name",
                f"{module_name}.rs",
                "-type",
                "f",
            ]
        )

        result = subprocess.run(search_cmd, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            for file_path in result.stdout.strip().split("\n"):
                if file_path:
                    # Normalize path (remove leading ./)
                    file_path = file_path.lstrip("./")
                    # Verify this file has #[cfg(test)]
                    verify_cmd = [
                        "docker",
                        "exec",
                        "--user",
                        "root",
                        "-w",
                        workdir,
                        container_name,
                        "grep",
                        "-l",
                        "#\\[cfg(test)\\]",
                        file_path,
                    ]
                    verify_result = subprocess.run(verify_cmd, capture_output=True)
                    if verify_result.returncode == 0:
                        if file_path not in file_to_tests:
                            file_to_tests[file_path] = []
                        file_to_tests[file_path].append(test_name)

    return file_to_tests


def map_go_tests_to_files(
    container_name: str,
    test_names: list[str],
    src_dirs: list[str],
    workdir: str = "/testbed",
) -> dict[str, list[str]]:
    """Map Go/Ginkgo test names to their test files.

    Go Ginkgo test naming conventions:
    - "github.com/navidrome/navidrome/scanner::phaseMissingTracks > finalize > ..."
    - "github.com/navidrome/navidrome/core::Library Service > Library CRUD..."

    Format: {full_package_path}::{Suite/Describe} > {Context} > {It}

    Strategy: Extract the package's relative path (e.g., "scanner", "core/agents")
    and find all *_test.go files in that directory.

    Args:
        container_name: Docker container name
        test_names: List of Go test names
        src_dirs: Source directories (used to extract module prefix)
        workdir: Working directory in container

    Returns:
        Dict mapping file paths to list of test names in that file
    """
    file_to_tests: dict[str, list[str]] = {}
    packages_processed: set[str] = set()

    for test_name in test_names:
        # Parse Go test name format: "github.com/org/repo/pkg::TestName > SubTest"
        if "::" not in test_name:
            logger.debug(f"Skipping non-Go test format: {test_name}")
            continue

        pkg_path, _ = test_name.split("::", 1)

        # Skip JavaScript test files (handled by map_js_tests_to_files)
        if pkg_path.endswith((".js", ".jsx", ".ts", ".tsx")):
            continue

        # Extract relative package path from full package path
        # e.g., "github.com/navidrome/navidrome/scanner" -> "scanner"
        # e.g., "github.com/navidrome/navidrome/core/agents" -> "core/agents"
        parts = pkg_path.split("/")
        if len(parts) <= 3:
            # Not enough parts to extract relative path
            logger.debug(f"Cannot extract relative path from: {pkg_path}")
            continue

        # Skip first 3 parts: github.com/org/repo
        relative_pkg = "/".join(parts[3:])

        if relative_pkg in packages_processed:
            # Already processed this package
            continue
        packages_processed.add(relative_pkg)

        # Find all *_test.go files in this package directory
        find_cmd = [
            "docker",
            "exec",
            "--user",
            "root",
            "-w",
            workdir,
            container_name,
            "find",
            relative_pkg,
            "-maxdepth",
            "1",  # Only direct children, not subdirectories
            "-name",
            "*_test.go",
            "-type",
            "f",
        ]

        result = subprocess.run(find_cmd, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            for file_path in result.stdout.strip().split("\n"):
                if file_path:
                    # Normalize path
                    file_path = file_path.lstrip("./")
                    if file_path not in file_to_tests:
                        file_to_tests[file_path] = []
                    # Add all test names from this package to this file
                    file_to_tests[file_path].append(test_name)
        else:
            logger.debug(f"No test files found in package: {relative_pkg}")

    return file_to_tests


def map_js_tests_to_files(
    container_name: str,
    test_names: list[str],
    workdir: str = "/testbed",
) -> dict[str, list[str]]:
    """Map JavaScript/TypeScript test names to their test files.

    JavaScript test naming conventions:
    - "ui/src/utils/formatters.test.js::formatDuration2 > handles null"
    - "ui/src/dialogs/SelectPlaylistInput.test.jsx::SelectPlaylistInput > Playlist"

    Format: {file_path}::{Describe} > {It}

    The file path is directly extracted from the test name (before ::).

    Args:
        container_name: Docker container name
        test_names: List of JavaScript test names
        workdir: Working directory in container

    Returns:
        Dict mapping file paths to list of test names in that file
    """
    file_to_tests: dict[str, list[str]] = {}

    for test_name in test_names:
        if "::" not in test_name:
            continue

        file_path, _ = test_name.split("::", 1)

        # Check if this looks like a JS/TS test file
        if not file_path.endswith(
            (".test.js", ".test.jsx", ".test.ts", ".test.tsx", ".spec.js", ".spec.jsx", ".spec.ts", ".spec.tsx")
        ):
            continue

        # Verify file exists in container
        check_cmd = [
            "docker",
            "exec",
            "--user",
            "root",
            "-w",
            workdir,
            container_name,
            "test",
            "-f",
            file_path,
        ]
        result = subprocess.run(check_cmd, capture_output=True)
        if result.returncode == 0:
            if file_path not in file_to_tests:
                file_to_tests[file_path] = []
            file_to_tests[file_path].append(test_name)
        else:
            logger.debug(f"JS test file not found: {file_path}")

    return file_to_tests


def _mask_test_files(
    container_name: str,
    test_files: list[str],
    workdir: str = "/testbed",
) -> tuple[int, list[str]]:
    """Mask test files by changing owner to root and removing all permissions.

    Args:
        container_name: Docker container name
        test_files: List of test file paths to mask
        workdir: Working directory in container

    Returns:
        Tuple of (number of successfully masked files, list of failed files)
    """
    masked_count = 0
    failed_files = []

    for file_path in test_files:
        # Step 1: Change owner to root so agent cannot modify permissions
        chown_cmd = [
            "docker",
            "exec",
            "--user",
            "root",
            "-w",
            workdir,
            container_name,
            "chown",
            "root:root",
            file_path,
        ]
        chown_result = subprocess.run(chown_cmd, capture_output=True)
        if chown_result.returncode != 0:
            failed_files.append(file_path)
            logger.error(f"Failed to chown test file: {file_path}")
            continue

        # Step 2: Remove all permissions
        chmod_cmd = [
            "docker",
            "exec",
            "--user",
            "root",
            "-w",
            workdir,
            container_name,
            "chmod",
            "000",
            file_path,
        ]
        chmod_result = subprocess.run(chmod_cmd, capture_output=True)
        if chmod_result.returncode == 0:
            masked_count += 1
            logger.info(f"Masked test file: {file_path}")
        else:
            failed_files.append(file_path)
            logger.error(f"Failed to chmod test file: {file_path}")

    return masked_count, failed_files


def _verify_file_exists(
    container_name: str,
    file_path: str,
    workdir: str = "/testbed",
) -> bool:
    """Check if a file exists in the container."""
    check_cmd = [
        "docker",
        "exec",
        "--user",
        "root",
        "-w",
        workdir,
        container_name,
        "test",
        "-f",
        file_path,
    ]
    result = subprocess.run(check_cmd, capture_output=True)
    return result.returncode == 0


def _map_tests_to_files(
    container_name: str,
    test_names: list[str],
    src_filter: SrcFileFilter,
    workdir: str = "/testbed",
) -> tuple[dict[str, list[str]], list[str], list[str], dict[str, str]]:
    """Map test names to their corresponding files.

    Uses unified extraction logic:
    1. Try direct extraction for Python, JS/TS, Java
    2. Use search for Go and Rust

    Args:
        container_name: Docker container name
        test_names: List of test names
        src_filter: SrcFileFilter with src_dirs configured
        workdir: Working directory in container

    Returns:
        Tuple of:
        - file_to_tests: Dict mapping file paths to test names
        - unmapped_tests: List of tests that couldn't be mapped (unknown format)
        - file_not_found_tests: List of tests whose files don't exist in container
        - test_methods: Dict mapping test name to extraction method used
    """
    file_to_tests: dict[str, list[str]] = {}
    unmapped_tests: list[str] = []  # Unknown format - couldn't parse
    file_not_found_tests: list[str] = []  # Known format but file doesn't exist
    test_methods: dict[str, str] = {}  # test_name -> method

    # Group tests by extraction method for efficient processing
    go_tests: list[str] = []
    rust_tests: list[str] = []

    for test_name in test_names:
        file_path, method = extract_file_from_test_name(test_name)
        test_methods[test_name] = method

        if file_path is not None:
            # Direct extraction successful - verify file exists
            if _verify_file_exists(container_name, file_path, workdir):
                if file_path not in file_to_tests:
                    file_to_tests[file_path] = []
                file_to_tests[file_path].append(test_name)
                logger.debug(f"[{method}] Mapped: {test_name} -> {file_path}")
            else:
                logger.warning(f"[{method}] File not found: {file_path} (from {test_name})")
                file_not_found_tests.append(test_name)
        elif method == "go":
            go_tests.append(test_name)
        elif method == "rust":
            rust_tests.append(test_name)
        else:
            unmapped_tests.append(test_name)

    # Process Go tests (need directory search)
    if go_tests:
        go_files = map_go_tests_to_files(container_name, go_tests, src_filter.src_dirs, workdir)
        mapped_go_tests = set()
        for file_path, tests in go_files.items():
            if file_path not in file_to_tests:
                file_to_tests[file_path] = []
            file_to_tests[file_path].extend(tests)
            mapped_go_tests.update(tests)
            for t in tests:
                logger.debug(f"[go] Mapped: {t} -> {file_path}")

        # Track unmapped Go tests (known format but file not found)
        for t in go_tests:
            if t not in mapped_go_tests:
                file_not_found_tests.append(t)

    # Process Rust tests (need module search)
    if rust_tests:
        rust_files = map_rust_tests_to_files(container_name, rust_tests, src_filter.src_dirs, workdir)
        mapped_rust_tests = set()
        for file_path, tests in rust_files.items():
            if file_path not in file_to_tests:
                file_to_tests[file_path] = []
            file_to_tests[file_path].extend(tests)
            mapped_rust_tests.update(tests)
            for t in tests:
                logger.debug(f"[rust] Mapped: {t} -> {file_path}")

        # Track unmapped Rust tests (known format but file not found)
        for t in rust_tests:
            if t not in mapped_rust_tests:
                file_not_found_tests.append(t)

    return file_to_tests, unmapped_tests, file_not_found_tests, test_methods


def mask_tests_by_names(
    container_name: str,
    test_names: list[str],
    src_filter: SrcFileFilter,
    workdir: str = "/testbed",
    strict: bool = True,
) -> dict:
    """Targeted test masking based on test name list.

    Uses unified test name parsing to support multiple languages:
    - Python (pytest): Direct file path extraction
    - TypeScript/JS (Jest): Direct file path extraction
    - Java (Maven): Class name to file path conversion
    - Go (Ginkgo): Package path search
    - Rust (cargo): Module name search

    Masking strategy:
    - Rust source files (.rs with inline tests): Remove #[cfg(test)] modules
    - All other test files: chmod 000 to prevent reading

    Args:
        container_name: Docker container name
        test_names: List of test names
        src_filter: SrcFileFilter with src_dirs/test_dirs configured
        workdir: Working directory in container
        strict: If True, raise TestMappingError when tests cannot be mapped

    Returns:
        Summary dict with masked_test_files, masked_src_files, test_mapping, etc.

    Raises:
        TestMappingError: If strict=True and tests have unknown format (new framework)
    """
    result = {
        "masked_test_files": 0,
        "masked_src_files": 0,
        "test_mapping": {},
        "failed_files": [],
        "file_types": {},  # file_path -> FileType
        "unmapped_tests": [],  # Unknown format - couldn't parse
        "file_not_found_tests": [],  # Known format but file doesn't exist
        "test_methods": {},  # test_name -> extraction method
    }

    logger.info(f"Masking {len(test_names)} tests")

    # Map tests to files using unified logic
    file_to_tests, unmapped_tests, file_not_found_tests, test_methods = _map_tests_to_files(
        container_name, test_names, src_filter, workdir
    )

    result["test_mapping"] = file_to_tests
    result["unmapped_tests"] = unmapped_tests
    result["file_not_found_tests"] = file_not_found_tests
    result["test_methods"] = test_methods

    # Check for truly unmapped tests (unknown format) - these indicate framework issues
    if unmapped_tests and strict:
        # Gather diagnostic info
        method_counts: dict[str, int] = {}
        for t in unmapped_tests:
            m = test_methods.get(t, "unknown")
            method_counts[m] = method_counts.get(m, 0) + 1

        raise TestMappingError(
            f"Failed to parse {len(unmapped_tests)} tests (unknown format). "
            f"This may indicate a new/unsupported test framework. "
            f"Methods: {method_counts}",
            unmapped_tests=unmapped_tests,
            details={
                "method_counts": method_counts,
                "container": container_name,
                "workdir": workdir,
                "src_dirs": src_filter.src_dirs,
            },
        )

    logger.info(f"Mapped tests to {len(file_to_tests)} files")
    if unmapped_tests:
        logger.warning(f"Could not parse {len(unmapped_tests)} tests (unknown format, strict=False)")

    # File not found is a warning, not an error - tests may reference files from other versions
    if file_not_found_tests:
        logger.warning(
            f"Skipped {len(file_not_found_tests)} tests whose files don't exist in container "
            f"(may be from different version)"
        )

    # Categorize files by type and apply appropriate masking
    test_files_to_mask: list[str] = []  # Files to chmod 000
    rust_src_files: list[str] = []  # Rust source files needing inline test removal

    for file_path in file_to_tests.keys():
        file_type = detect_file_type(file_path, src_filter)
        result["file_types"][file_path] = file_type

        if file_type == "rust_src":
            rust_src_files.append(file_path)
            logger.debug(f"Rust source file (will remove inline tests): {file_path}")
        else:  # test_file
            test_files_to_mask.append(file_path)
            logger.debug(f"Test file (will chmod 000): {file_path}")

    # Mask test files (chmod 000)
    masked_count, failed = _mask_test_files(container_name, test_files_to_mask, workdir)
    result["masked_test_files"] = masked_count
    result["failed_files"].extend(failed)

    # Remove inline tests from Rust source files
    for file_path in rust_src_files:
        if mask_inline_tests_in_file(container_name, file_path):
            result["masked_src_files"] += 1
        else:
            result["failed_files"].append(file_path)

    # Log summary
    logger.info(
        f"Masking complete: {result['masked_test_files']} test files (chmod 000), "
        f"{result['masked_src_files']} src files (inline tests removed), "
        f"{len(file_not_found_tests)} skipped (file not found), "
        f"{len(unmapped_tests)} unknown format"
    )
    return result


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 4:
        print("Usage: python test_masking.py <container> <metadata_json> <test1> [test2] ...")
        print("       python test_masking.py --no-strict <container> <metadata_json> <test1> ...")
        print("")
        print("Supports multiple languages with unified test name parsing:")
        print("")
        print("  Python (pytest):     tests/test_foo.py::TestClass::test_method[param]")
        print("  TypeScript/JS:       /testbed/path/to/file.test.ts::describe it")
        print("  Java (Maven):        module::org.package.TestClass::testMethod")
        print("  Go (Ginkgo):         github.com/org/repo/pkg::Describe > Context > It")
        print("  Rust (cargo):        module::tests::test_fn")
        print("")
        print("Options:")
        print("  --no-strict    Don't fail on unmapped tests (default: fail)")
        print("")
        print("Example:")
        print("  python test_masking.py my-container metadata.json \\")
        print("    'tests/test_foo.py::test_bar' \\")
        print("    'github.com/org/repo/pkg::GoTest > SubTest' \\")
        print("    'module::tests::rust_test'")
        sys.exit(1)

    # Parse options
    strict = True
    args = sys.argv[1:]
    if args[0] == "--no-strict":
        strict = False
        args = args[1:]

    container = args[0]
    metadata_path = Path(args[1])
    test_names = args[2:]

    # Load metadata and create SrcFileFilter
    with open(metadata_path) as f:
        metadata = json.load(f)

    src_filter = SrcFileFilter(
        src_dirs=metadata["repo_src_dirs"],
        test_dirs=metadata["test_dirs"],
        exclude_patterns=metadata.get("exclude_patterns", []),
    )

    try:
        result = mask_tests_by_names(container, test_names, src_filter, strict=strict)
        print(f"\nResult:")
        print(f"  Masked test files: {result['masked_test_files']}")
        print(f"  Masked src files (inline tests removed): {result['masked_src_files']}")
        print(f"  Unmapped tests: {len(result['unmapped_tests'])}")
        print(f"  Failed files: {result['failed_files']}")
        print(f"  File types: {result['file_types']}")
        if result["unmapped_tests"]:
            print(f"\nUnmapped tests:")
            for t in result["unmapped_tests"][:10]:
                print(f"    - {t}")
            if len(result["unmapped_tests"]) > 10:
                print(f"    ... and {len(result['unmapped_tests']) - 10} more")
    except TestMappingError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)
