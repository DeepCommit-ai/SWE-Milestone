"""
Go Test Report Parsing Utilities

This module provides utilities for parsing Go test output and converting it to
a standardized summary format compatible with pytest_report_utils output structure.

Supports two input formats:
1. JSON format (go test -json): NDJSON with structured events
2. Verbose format (go test -v): Plain text with "--- PASS/FAIL/SKIP:" markers

Go test -json output format (NDJSON - one JSON object per line):
    {"Time":"2024-01-01T00:00:00Z","Action":"start","Package":"github.com/example/pkg"}
    {"Time":"...","Action":"run","Package":"...","Test":"TestName"}
    {"Time":"...","Action":"output","Package":"...","Test":"TestName","Output":"..."}
    {"Time":"...","Action":"pass","Package":"...","Test":"TestName","Elapsed":0.123}
    {"Time":"...","Action":"fail","Package":"...","Test":"TestName","Elapsed":0.123}
    {"Time":"...","Action":"skip","Package":"...","Test":"TestName","Elapsed":0.123}

Go test -v output format (plain text):
    === RUN   TestFoo
    --- PASS: TestFoo (0.00s)
    === RUN   TestBar
    --- FAIL: TestBar (0.01s)
    === RUN   TestBaz
    --- SKIP: TestBaz (0.00s)
    PASS
    ok      github.com/example/pkg  0.123s

Action types (JSON format):
    - start: Package test begins
    - run: Test begins
    - output: Test output line
    - pass: Test passed
    - fail: Test failed
    - skip: Test skipped
    - pause: Test paused (parallel tests)
    - cont: Test continued

Usage:
    from harness.utils.go_report_utils import convert_go_report_to_summary

    # For JSON format (.jsonl)
    summary = convert_go_report_to_summary(Path("output.jsonl"))

    # For verbose format (.log)
    summary = convert_go_verbose_to_summary(Path("output.log"))

    # Auto-detect format
    summary = parse_go_test_output(Path("output.jsonl"))  # or output.log
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class GoTestResult:
    """Result for a single Go test."""

    package: str
    test_name: str
    action: str  # pass, fail, skip
    elapsed: float = 0.0
    output_lines: List[str] = field(default_factory=list)

    @property
    def nodeid(self) -> str:
        """Return pytest-compatible nodeid format."""
        return f"{self.package}/{self.test_name}"

    @property
    def full_output(self) -> str:
        """Return full test output as a single string."""
        return "".join(self.output_lines)


@dataclass
class GinkgoSpecResult:
    """Result for a single Ginkgo spec (individual test case).

    Ginkgo JSON report structure:
        {
            "ContainerHierarchyTexts": ["Describe Name", "Context Name", ...],
            "LeafNodeText": "It should do something",
            "LeafNodeLocation": {"FileName": "...", "LineNumber": 32},
            "State": "passed|failed|skipped|pending|panicked|interrupted|aborted",
            "RunTime": 1234567890,  # nanoseconds
            "Failure": {...}  # Optional, present when State is "failed"
        }
    """

    package: str  # e.g., "github.com/navidrome/navidrome/plugins"
    container_hierarchy: List[str]  # e.g., ["Adapter Media Agent", "Album methods"]
    leaf_node_text: str  # e.g., "should return album information"
    state: str  # passed, failed, skipped, pending, etc.
    run_time_ns: int = 0  # nanoseconds
    file_name: str = ""
    line_number: int = 0
    failure_message: str = ""

    @property
    def nodeid(self) -> str:
        """Return Ginkgo-style test ID with full hierarchy.

        Format: {package}::{Describe > Context > ... > It}
        Example: github.com/navidrome/navidrome/plugins::Adapter Media Agent > Album methods > GetAlbumInfo > should return album information
        """
        hierarchy_parts = self.container_hierarchy + [self.leaf_node_text]
        hierarchy_str = " > ".join(hierarchy_parts)
        return f"{self.package}::{hierarchy_str}"

    @property
    def elapsed(self) -> float:
        """Return elapsed time in seconds."""
        return self.run_time_ns / 1e9


@dataclass
class GinkgoSuiteResult:
    """Result for a Ginkgo test suite (one package).

    Ginkgo JSON report structure (per suite):
        {
            "SuitePath": "/path/to/package",
            "SuiteDescription": "Package Suite",
            "SpecReports": [...],
            "SuiteSucceeded": true/false,
            "RunTime": 1234567890
        }
    """

    suite_path: str  # Absolute path to the package
    suite_description: str
    package: str  # Extracted Go package path
    succeeded: bool
    run_time_ns: int = 0
    specs: List[GinkgoSpecResult] = field(default_factory=list)

    @property
    def elapsed(self) -> float:
        """Return elapsed time in seconds."""
        return self.run_time_ns / 1e9


@dataclass
class GinkgoTestSummary:
    """Aggregated summary of Ginkgo test execution."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    pending: int = 0
    panicked: int = 0
    duration: float = 0.0
    suites: List[GinkgoSuiteResult] = field(default_factory=list)
    specs: List[GinkgoSpecResult] = field(default_factory=list)


@dataclass
class GoPackageResult:
    """Result for a Go package."""

    package: str
    action: str  # pass, fail, skip
    elapsed: float = 0.0
    tests: List[GoTestResult] = field(default_factory=list)
    output_lines: List[str] = field(default_factory=list)


@dataclass
class GoTestSummary:
    """Aggregated summary of Go test execution."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0  # Build errors
    duration: float = 0.0
    packages: List[GoPackageResult] = field(default_factory=list)
    test_results: List[GoTestResult] = field(default_factory=list)


def parse_go_test_jsonl(jsonl_path: Path) -> GoTestSummary:
    """
    Parse a Go test JSON Lines output file.

    Args:
        jsonl_path: Path to the Go test JSONL output

    Returns:
        GoTestSummary with parsed test results
    """
    summary = GoTestSummary()

    if not jsonl_path.exists():
        return summary

    # Track current tests and packages
    current_tests: Dict[str, GoTestResult] = {}  # key: "package/test"
    packages: Dict[str, GoPackageResult] = {}  # key: package name

    # Track benchmark results (collected from output lines)
    # key: "package/BenchmarkName", value: GoTestResult
    benchmark_results: Dict[str, GoTestResult] = {}

    # Regex pattern to match benchmark output lines
    # Example: "BenchmarkGoogleBreakerAllow-384     \t       1\t      3090 ns/op\n"
    benchmark_pattern = re.compile(r"^(Benchmark\S+)-(\d+)\s+(\d+)\s+([\d.]+)\s+ns/op")

    content = jsonl_path.read_text(errors="replace")

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        action = event.get("Action", "")
        package = event.get("Package", "")
        test_name = event.get("Test", "")
        elapsed = event.get("Elapsed", 0.0)
        output = event.get("Output", "")

        # Initialize package if needed
        if package and package not in packages:
            packages[package] = GoPackageResult(package=package, action="")

        # Handle test-level events (when Test field is present)
        if test_name:
            test_key = f"{package}/{test_name}"

            if action == "run":
                # Test started
                current_tests[test_key] = GoTestResult(
                    package=package,
                    test_name=test_name,
                    action="running",
                )
            elif action == "output":
                # Collect output
                if test_key in current_tests:
                    current_tests[test_key].output_lines.append(output)

                # Check if this output line contains benchmark results
                # Benchmark results appear in output lines during test execution
                if output:
                    benchmark_match = benchmark_pattern.match(output.strip())
                    if benchmark_match:
                        benchmark_name = benchmark_match.group(1)  # e.g., "BenchmarkGoogleBreakerAllow"
                        benchmark_key = f"{package}/{benchmark_name}"

                        # Only add if not already recorded
                        if benchmark_key not in benchmark_results:
                            benchmark_results[benchmark_key] = GoTestResult(
                                package=package,
                                test_name=benchmark_name,
                                action="pass",  # Benchmark completed = passed
                                elapsed=0.0,
                                output_lines=[output],
                            )
                        else:
                            # Append output to existing benchmark
                            benchmark_results[benchmark_key].output_lines.append(output)

            elif action in ("pass", "fail", "skip"):
                # Test finished
                if test_key in current_tests:
                    test_result = current_tests[test_key]
                    test_result.action = action
                    test_result.elapsed = elapsed
                    summary.test_results.append(test_result)

                    # Add to package
                    if package in packages:
                        packages[package].tests.append(test_result)
                else:
                    # Test finished without explicit run event
                    test_result = GoTestResult(
                        package=package,
                        test_name=test_name,
                        action=action,
                        elapsed=elapsed,
                    )
                    summary.test_results.append(test_result)
                    if package in packages:
                        packages[package].tests.append(test_result)

        # Handle package-level events (when Test field is absent)
        elif package:
            if action == "output":
                packages[package].output_lines.append(output)

                # Also check package-level output for benchmark results
                if output:
                    benchmark_match = benchmark_pattern.match(output.strip())
                    if benchmark_match:
                        benchmark_name = benchmark_match.group(1)
                        benchmark_key = f"{package}/{benchmark_name}"

                        if benchmark_key not in benchmark_results:
                            benchmark_results[benchmark_key] = GoTestResult(
                                package=package,
                                test_name=benchmark_name,
                                action="pass",
                                elapsed=0.0,
                                output_lines=[output],
                            )
                        else:
                            benchmark_results[benchmark_key].output_lines.append(output)

            elif action in ("pass", "fail", "skip"):
                packages[package].action = action
                packages[package].elapsed = elapsed

    # Some benchmarks emit their `BenchmarkName-N` header and timing payload
    # in two separate Output events (go test flushes between them when the
    # benchmark runs long enough). Per-line regex matching misses these.
    # Recover them here by concatenating all output for any Benchmark* test
    # that wasn't already captured as a benchmark result.
    for test_key, test_result in current_tests.items():
        if not test_result.test_name.startswith("Benchmark"):
            continue
        benchmark_key = f"{test_result.package}/{test_result.test_name}"
        if benchmark_key in benchmark_results:
            continue
        combined = "".join(test_result.output_lines)
        for fragment in combined.split("\n"):
            benchmark_match = benchmark_pattern.match(fragment.strip())
            if benchmark_match:
                benchmark_results[benchmark_key] = GoTestResult(
                    package=test_result.package,
                    test_name=test_result.test_name,
                    action="pass",
                    elapsed=0.0,
                    output_lines=list(test_result.output_lines),
                )
                break

    # Add benchmark results to test results
    for benchmark_key, benchmark_result in benchmark_results.items():
        # Only add benchmarks that don't already exist as regular tests
        existing_nodeids = {t.nodeid for t in summary.test_results}
        if benchmark_result.nodeid not in existing_nodeids:
            summary.test_results.append(benchmark_result)
            # Also add to package
            if benchmark_result.package in packages:
                packages[benchmark_result.package].tests.append(benchmark_result)

    # Convert packages dict to list
    summary.packages = list(packages.values())

    # Calculate totals
    _calculate_totals(summary)

    return summary


# Regex patterns for verbose output parsing
VERBOSE_PATTERNS = {
    # === RUN   TestName
    "run": re.compile(r"^=== RUN\s+(\S+)"),
    # --- PASS: TestName (0.00s)
    "pass": re.compile(r"^--- PASS:\s+(\S+)\s+\(([0-9.]+)s\)"),
    # --- FAIL: TestName (0.00s)
    "fail": re.compile(r"^--- FAIL:\s+(\S+)\s+\(([0-9.]+)s\)"),
    # --- SKIP: TestName (0.00s)
    "skip": re.compile(r"^--- SKIP:\s+(\S+)\s+\(([0-9.]+)s\)"),
    # ok      github.com/example/pkg  0.123s
    "pkg_pass": re.compile(r"^ok\s+(\S+)\s+([0-9.]+)s"),
    # FAIL    github.com/example/pkg  0.123s
    "pkg_fail": re.compile(r"^FAIL\s+(\S+)\s+([0-9.]+)s"),
    # ?       github.com/example/pkg  [no test files]
    "pkg_skip": re.compile(r"^\?\s+(\S+)\s+\[(.+)\]"),
}


def parse_go_test_verbose(log_path: Path) -> GoTestSummary:
    """
    Parse a Go test verbose (-v) output log file.

    This parses plain text output from `go test -v` commands.
    Correctly handles the case where a test is started (=== RUN) but
    has no explicit result (--- PASS/FAIL/SKIP) - such tests are treated
    as skipped.

    Args:
        log_path: Path to the Go test verbose output log

    Returns:
        GoTestSummary with parsed test results
    """
    summary = GoTestSummary()

    if not log_path.exists():
        return summary

    content = log_path.read_text(errors="replace")
    lines = content.splitlines()

    # Track test states
    running_tests: set = set()  # Tests that were started with === RUN
    passed_tests: set = set()  # Tests that passed
    failed_tests: set = set()  # Tests that failed
    skipped_tests: set = set()  # Tests that were skipped

    # Track test results with details
    test_results: Dict[str, GoTestResult] = {}
    packages: Dict[str, GoPackageResult] = {}

    # Current package context (inferred from output)
    current_package = ""

    # Current test output collection
    current_test_output: Dict[str, List[str]] = {}

    for line in lines:
        # Check for === RUN
        match = VERBOSE_PATTERNS["run"].match(line)
        if match:
            test_name = match.group(1)
            running_tests.add(test_name)
            current_test_output[test_name] = []
            continue

        # Check for --- PASS:
        match = VERBOSE_PATTERNS["pass"].match(line)
        if match:
            test_name = match.group(1)
            elapsed = float(match.group(2))
            passed_tests.add(test_name)
            running_tests.discard(test_name)

            test_result = GoTestResult(
                package=current_package,
                test_name=test_name,
                action="pass",
                elapsed=elapsed,
                output_lines=current_test_output.get(test_name, []),
            )
            test_results[test_name] = test_result
            continue

        # Check for --- FAIL:
        match = VERBOSE_PATTERNS["fail"].match(line)
        if match:
            test_name = match.group(1)
            elapsed = float(match.group(2))
            failed_tests.add(test_name)
            running_tests.discard(test_name)

            test_result = GoTestResult(
                package=current_package,
                test_name=test_name,
                action="fail",
                elapsed=elapsed,
                output_lines=current_test_output.get(test_name, []),
            )
            test_results[test_name] = test_result
            continue

        # Check for --- SKIP:
        match = VERBOSE_PATTERNS["skip"].match(line)
        if match:
            test_name = match.group(1)
            elapsed = float(match.group(2))
            skipped_tests.add(test_name)
            running_tests.discard(test_name)

            test_result = GoTestResult(
                package=current_package,
                test_name=test_name,
                action="skip",
                elapsed=elapsed,
                output_lines=current_test_output.get(test_name, []),
            )
            test_results[test_name] = test_result
            continue

        # Check for package pass: ok github.com/example/pkg 0.123s
        match = VERBOSE_PATTERNS["pkg_pass"].match(line)
        if match:
            pkg_name = match.group(1)
            elapsed = float(match.group(2))
            current_package = pkg_name
            packages[pkg_name] = GoPackageResult(package=pkg_name, action="pass", elapsed=elapsed)
            continue

        # Check for package fail: FAIL github.com/example/pkg 0.123s
        match = VERBOSE_PATTERNS["pkg_fail"].match(line)
        if match:
            pkg_name = match.group(1)
            elapsed = float(match.group(2))
            current_package = pkg_name
            packages[pkg_name] = GoPackageResult(package=pkg_name, action="fail", elapsed=elapsed)
            continue

        # Check for package skip: ? github.com/example/pkg [no test files]
        match = VERBOSE_PATTERNS["pkg_skip"].match(line)
        if match:
            pkg_name = match.group(1)
            reason = match.group(2)
            packages[pkg_name] = GoPackageResult(package=pkg_name, action="skip", output_lines=[reason])
            continue

        # Collect output for running tests
        for test_name in list(running_tests):
            if test_name in current_test_output:
                current_test_output[test_name].append(line)

    # Handle tests that were started but never finished (treat as skipped)
    # This is the FIX for the bug in nats-server parser
    # The bug was: if test_name not in failed_tests: continue
    # Which means it only added to skipped if it WAS in failed_tests (inverted logic)
    # Correct logic: if already passed or failed, don't add to skipped
    for test_name in running_tests:
        if test_name in passed_tests:
            continue  # Already has a pass result
        if test_name in failed_tests:
            continue  # Already has a fail result
        # Test was started but never completed - treat as skipped
        skipped_tests.add(test_name)
        test_result = GoTestResult(
            package=current_package,
            test_name=test_name,
            action="skip",
            output_lines=current_test_output.get(test_name, []),
        )
        test_results[test_name] = test_result

    # Build summary
    summary.test_results = list(test_results.values())
    summary.packages = list(packages.values())

    # Calculate totals
    _calculate_totals(summary)

    return summary


def convert_go_verbose_to_summary(log_path: Path, output_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Convert Go test verbose output to a standardized summary format.

    This produces output compatible with pytest_report_utils.convert_pytest_report_to_summary,
    allowing unified handling of test results across different frameworks.

    Args:
        log_path: Path to Go test verbose output log
        output_path: Optional path to save summary JSON

    Returns:
        Summary dict with standard structure
    """
    go_summary = parse_go_test_verbose(log_path)
    return _build_summary_dict(go_summary, output_path)


def parse_go_test_output(file_path: Path, output_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Auto-detect format and parse Go test output.

    Determines whether the input is JSON Lines (.jsonl) or verbose text format
    and parses accordingly.

    Args:
        file_path: Path to Go test output (either .jsonl or .log)
        output_path: Optional path to save summary JSON

    Returns:
        Summary dict with standard structure
    """
    if not file_path.exists():
        return {
            "duration": 0.0,
            "summary": {"total": 0, "passed": 0, "failed": 0, "error": 0, "skipped": 0},
            "results": {"failed": [], "error": [], "skipped": [], "passed": []},
        }

    # Try to detect format by extension first
    suffix = file_path.suffix.lower()
    if suffix in (".jsonl", ".json"):
        # Some callers may copy NDJSON to a .json file (e.g., merged start.json).
        # Treat both as JSONL so we still parse events.
        go_summary = parse_go_test_jsonl(file_path)
    elif suffix in (".log", ".txt"):
        go_summary = parse_go_test_verbose(file_path)
    else:
        # Try to detect by content; skip blank and noise lines (e.g., "go: downloading ...")
        content = file_path.read_text(errors="replace")
        lines = content.split("\n") if content else []
        first_data_line = ""
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            # Ignore go module download noise
            if stripped.startswith("go: downloading"):
                continue
            first_data_line = stripped
            break

        # JSON format starts with '{' (go test -json). Also accept lines containing "Action"
        # to handle cases where download noise precedes the first JSON event.
        if first_data_line.startswith("{") or '"Action"' in first_data_line:
            go_summary = parse_go_test_jsonl(file_path)
        else:
            go_summary = parse_go_test_verbose(file_path)

    return _build_summary_dict(go_summary, output_path)


def _build_summary_dict(go_summary: GoTestSummary, output_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Build standardized summary dict from GoTestSummary.

    Internal helper used by both JSONL and verbose parsers.
    """
    # Build failed tests list
    failed_list = []
    for test in go_summary.test_results:
        if test.action == "fail":
            failed_list.append(
                {
                    "nodeid": test.nodeid,
                    "message": extract_fail_message(test),
                }
            )

    # Build error list (package build failures)
    error_list = []
    for pkg in go_summary.packages:
        if pkg.action == "fail" and not pkg.tests:
            error_list.append(
                {
                    "nodeid": pkg.package,
                    "message": extract_build_error(pkg),
                }
            )

    # Group skipped tests by reason
    skip_reasons: Dict[str, List[str]] = {}
    for test in go_summary.test_results:
        if test.action == "skip":
            reason = extract_skip_reason(test)
            if reason not in skip_reasons:
                skip_reasons[reason] = []
            skip_reasons[reason].append(test.nodeid)

    skipped_by_reason = [
        {"reason": reason, "count": len(tests), "tests": tests}
        for reason, tests in sorted(skip_reasons.items(), key=lambda x: -len(x[1]))
    ]

    # Build passed tests list
    passed_list = [test.nodeid for test in go_summary.test_results if test.action == "pass"]

    # Create standardized result
    result = {
        "duration": round(go_summary.duration, 2),
        "summary": {
            "total": go_summary.total,
            "passed": go_summary.passed,
            "failed": go_summary.failed,
            "error": go_summary.errors,
            "skipped": go_summary.skipped,
        },
        "results": {
            "failed": failed_list,
            "error": error_list,
            "skipped": skipped_by_reason,
            "passed": passed_list,
        },
    }

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2)

    return result


def _calculate_totals(summary: GoTestSummary) -> None:
    """Calculate total counts from individual test results."""
    for test in summary.test_results:
        summary.total += 1
        if test.action == "pass":
            summary.passed += 1
        elif test.action == "fail":
            summary.failed += 1
        elif test.action == "skip":
            summary.skipped += 1

    # Check for package-level failures (build errors)
    for pkg in summary.packages:
        if pkg.action == "fail" and not pkg.tests:
            # Package failed without any test results = build error
            summary.errors += 1

    # Calculate duration from packages
    summary.duration = sum(pkg.elapsed for pkg in summary.packages)


def extract_fail_message(test: GoTestResult) -> str:
    """
    Extract failure message from a Go test result.

    Parses the test output to find assertion failures, panics, or error messages.

    Args:
        test: GoTestResult object

    Returns:
        Failure message string
    """
    output = test.full_output
    if not output:
        return "Test failed"

    lines = output.splitlines()

    # Look for common failure patterns
    for i, line in enumerate(lines):
        # Ginkgo/Gomega failure pattern
        if "[FAILED]" in line or "FAIL!" in line:
            # Get the next few lines for context
            context_lines = lines[i : i + 5]
            return "\n".join(context_lines).strip()[:500]

        # Standard Go test failure: "--- FAIL:"
        if line.strip().startswith("--- FAIL:"):
            context_lines = lines[i : i + 5]
            return "\n".join(context_lines).strip()[:500]

        # Panic
        if "panic:" in line.lower():
            context_lines = lines[i : i + 10]
            return "\n".join(context_lines).strip()[:500]

        # Assertion error patterns
        if "Error:" in line or "error:" in line:
            return line.strip()[:500]

        # testify assertions
        if "Expected" in line and "to" in line:
            context_lines = lines[max(0, i - 1) : i + 3]
            return "\n".join(context_lines).strip()[:500]

    # Fallback: return last non-empty lines
    non_empty = [l for l in lines if l.strip()]
    if non_empty:
        return "\n".join(non_empty[-5:]).strip()[:500]

    return "Test failed"


def extract_skip_reason(test: GoTestResult) -> str:
    """
    Extract skip reason from a Go test result.

    Args:
        test: GoTestResult object

    Returns:
        Skip reason string
    """
    output = test.full_output
    if not output:
        return "Test skipped"

    lines = output.splitlines()

    # Look for skip patterns
    for line in lines:
        # Ginkgo pending
        if "[PENDING]" in line or "PENDING" in line:
            return "Pending test (Ginkgo)"

        # Standard Go skip: "--- SKIP:"
        if line.strip().startswith("--- SKIP:"):
            return line.strip()

        # t.Skip() messages
        if "skip" in line.lower():
            return line.strip()[:200]

    return "Test skipped"


def extract_build_error(pkg: GoPackageResult) -> str:
    """
    Extract build error message from a package result.

    Args:
        pkg: GoPackageResult object

    Returns:
        Build error message string
    """
    output = "".join(pkg.output_lines)
    if not output:
        return "Build failed"

    lines = output.splitlines()

    # Look for build error patterns
    for i, line in enumerate(lines):
        # Compilation error
        if ": undefined:" in line or ": cannot " in line:
            context_lines = lines[i : i + 5]
            return "\n".join(context_lines).strip()[:500]

        # Import error
        if "could not import" in line.lower() or "cannot find package" in line.lower():
            return line.strip()[:500]

        # FAIL with build reason
        if line.startswith("FAIL") and "[build failed]" in line:
            return line.strip()

    # Fallback
    non_empty = [l for l in lines if l.strip() and not l.startswith("?")]
    if non_empty:
        return "\n".join(non_empty[:5]).strip()[:500]

    return "Build failed"


def convert_go_report_to_summary(jsonl_path: Path, output_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Convert Go test JSONL output to a standardized summary format.

    This produces output compatible with pytest_report_utils.convert_pytest_report_to_summary,
    allowing unified handling of test results across different frameworks.

    Args:
        jsonl_path: Path to Go test JSONL output
        output_path: Optional path to save summary JSON

    Returns:
        Summary dict with structure:
        {
            "duration": float,
            "summary": {"total": int, "passed": int, "failed": int, "error": int, "skipped": int},
            "results": {
                "failed": [{"nodeid": str, "message": str}, ...],
                "error": [{"nodeid": str, "message": str}, ...],
                "skipped": [{"reason": str, "count": int, "tests": [...]}, ...],
                "passed": [nodeid, ...]
            }
        }
    """
    go_summary = parse_go_test_jsonl(jsonl_path)
    return _build_summary_dict(go_summary, output_path)


def get_failed_tests(summary: GoTestSummary) -> List[str]:
    """
    Get list of failed test nodeids.

    Args:
        summary: Parsed GoTestSummary

    Returns:
        List of nodeids
    """
    return [test.nodeid for test in summary.test_results if test.action == "fail"]


def get_passed_tests(summary: GoTestSummary) -> List[str]:
    """
    Get list of passed test nodeids.

    Args:
        summary: Parsed GoTestSummary

    Returns:
        List of nodeids
    """
    return [test.nodeid for test in summary.test_results if test.action == "pass"]


def get_skipped_tests(summary: GoTestSummary) -> List[str]:
    """
    Get list of skipped test nodeids.

    Args:
        summary: Parsed GoTestSummary

    Returns:
        List of nodeids
    """
    return [test.nodeid for test in summary.test_results if test.action == "skip"]


def print_summary(summary: GoTestSummary) -> None:
    """
    Print a human-readable summary of test results.

    Args:
        summary: Parsed GoTestSummary
    """
    print(f"\n{'=' * 60}")
    print("Go Test Summary")
    print(f"{'=' * 60}")
    print(f"Total Tests:  {summary.total}")
    print(f"Passed:       {summary.passed}")
    print(f"Failed:       {summary.failed}")
    print(f"Skipped:      {summary.skipped}")
    print(f"Build Errors: {summary.errors}")
    print(f"Duration:     {summary.duration:.2f}s")
    print(f"Packages:     {len(summary.packages)}")

    # Show failed tests
    failed = [t for t in summary.test_results if t.action == "fail"]
    if failed:
        print(f"\n{'─' * 60}")
        print("Failed Tests:")
        for test in failed[:20]:
            print(f"  ✗ {test.nodeid}")
            msg = extract_fail_message(test)
            if msg:
                # Show first line of message
                first_line = msg.split("\n")[0][:80]
                print(f"    {first_line}")
        if len(failed) > 20:
            print(f"  ... and {len(failed) - 20} more")

    # Show build errors
    build_errors = [p for p in summary.packages if p.action == "fail" and not p.tests]
    if build_errors:
        print(f"\n{'─' * 60}")
        print("Build Errors:")
        for pkg in build_errors:
            print(f"  ✗ {pkg.package}")
            msg = extract_build_error(pkg)
            if msg:
                first_line = msg.split("\n")[0][:80]
                print(f"    {first_line}")

    print(f"{'=' * 60}\n")


# =============================================================================
# Ginkgo JSON Report Parsing
# =============================================================================


def extract_package_from_suite_path(suite_path: str, go_module: str = "") -> str:
    """
    Extract Go package path from Ginkgo suite path.

    Args:
        suite_path: Absolute path to the test suite (e.g., "/testbed/plugins")
        go_module: Optional Go module path (e.g., "github.com/navidrome/navidrome")

    Returns:
        Go package path (e.g., "github.com/navidrome/navidrome/plugins")
    """
    # Common root directory names used in Docker containers or test environments
    root_prefixes = {"testbed", "workdir", "src", "app", "code", "work", "project"}

    parts = list(Path(suite_path).parts)

    # Skip leading "/" (root) and known root prefixes
    start_idx = 0
    for i, part in enumerate(parts):
        part_lower = part.lower()
        # Skip filesystem root "/" or common container/project root directories
        if part == "/" or part_lower in root_prefixes:
            start_idx = i + 1
        else:
            break

    # Get the relative path after skipping root prefixes
    relative_parts = parts[start_idx:] if start_idx < len(parts) else []
    relative_path = "/".join(relative_parts)

    if not go_module:
        # No module provided, return relative path as-is
        return relative_path if relative_path else suite_path

    # If go_module is provided, construct full package path
    # e.g., go_module="github.com/navidrome/navidrome", suite_path="/testbed/plugins"
    # Result: "github.com/navidrome/navidrome/plugins"
    # e.g., go_module="github.com/navidrome/navidrome", suite_path="/testbed"
    # Result: "github.com/navidrome/navidrome"
    if relative_path:
        return f"{go_module}/{relative_path}"
    return go_module


def _extract_package_from_file_path(file_path: str, go_module: str = "") -> str:
    """
    Extract Go package path from a test file path.

    Args:
        file_path: Absolute path to test file (e.g., "/testbed/plugins/runtime_test.go")
        go_module: Optional Go module path (e.g., "github.com/navidrome/navidrome")

    Returns:
        Go package path (e.g., "github.com/navidrome/navidrome/plugins")
    """
    if not file_path:
        return go_module or ""

    # Get directory containing the test file
    dir_path = str(Path(file_path).parent)

    # Extract relative path after common root prefixes
    root_prefixes = {"testbed", "workdir", "src", "app", "code", "work", "project"}
    parts = list(Path(dir_path).parts)

    start_idx = 0
    for i, part in enumerate(parts):
        part_lower = part.lower()
        if part == "/" or part_lower in root_prefixes:
            start_idx = i + 1
        else:
            break

    relative_parts = parts[start_idx:] if start_idx < len(parts) else []
    relative_path = "/".join(relative_parts)

    if go_module and relative_path:
        return f"{go_module}/{relative_path}"
    elif go_module:
        return go_module
    return relative_path


def parse_ginkgo_json_report(
    report_path: Path,
    go_module: str = "",
) -> GinkgoTestSummary:
    """
    Parse a Ginkgo JSON report file.

    Ginkgo generates a JSON report with --json-report flag. The report contains
    an array of suite results, each with spec-level details.

    Args:
        report_path: Path to the Ginkgo JSON report file
        go_module: Optional Go module path for constructing full package paths

    Returns:
        GinkgoTestSummary with parsed spec-level results
    """
    summary = GinkgoTestSummary()

    if not report_path.exists():
        return summary

    try:
        content = report_path.read_text(errors="replace")
        data = json.loads(content)
    except (json.JSONDecodeError, IOError):
        return summary

    # Ginkgo report is an array of suite results
    suites = data if isinstance(data, list) else [data]

    for suite_data in suites:
        suite_path = suite_data.get("SuitePath", "")
        suite_description = suite_data.get("SuiteDescription", "")
        # Use suite path as default package (may be overridden per-spec)
        default_package = extract_package_from_suite_path(suite_path, go_module)

        suite = GinkgoSuiteResult(
            suite_path=suite_path,
            suite_description=suite_description,
            package=default_package,
            succeeded=suite_data.get("SuiteSucceeded", False),
            run_time_ns=suite_data.get("RunTime", 0),
        )

        # Parse spec reports (may be None for non-Ginkgo packages)
        spec_reports = suite_data.get("SpecReports") or []
        for spec_data in spec_reports:
            # Skip container nodes (BeforeEach, AfterEach, etc.) - only process leaf nodes
            leaf_node_type = spec_data.get("LeafNodeType", "")
            if leaf_node_type not in ("It", "Specify", "Entry", ""):
                # Not a test spec, skip
                continue

            container_hierarchy = spec_data.get("ContainerHierarchyTexts", [])
            leaf_node_text = spec_data.get("LeafNodeText", "")

            # Skip if no leaf text (not a real test)
            if not leaf_node_text and not container_hierarchy:
                continue

            location = spec_data.get("LeafNodeLocation", {})
            state = spec_data.get("State", "unknown")

            # Extract package from test file location (more accurate than suite path)
            spec_file = location.get("FileName", "")
            package = _extract_package_from_file_path(spec_file, go_module) if spec_file else default_package

            # Extract failure message if present
            failure_message = ""
            failure = spec_data.get("Failure", {})
            if failure:
                failure_message = failure.get("Message", "")
                if not failure_message:
                    failure_message = failure.get("ForwardedPanic", "")

            spec = GinkgoSpecResult(
                package=package,
                container_hierarchy=container_hierarchy,
                leaf_node_text=leaf_node_text,
                state=state,
                run_time_ns=spec_data.get("RunTime", 0),
                file_name=location.get("FileName", ""),
                line_number=location.get("LineNumber", 0),
                failure_message=failure_message,
            )

            suite.specs.append(spec)
            summary.specs.append(spec)

            # Update counts
            summary.total += 1
            if state == "passed":
                summary.passed += 1
            elif state == "failed":
                summary.failed += 1
            elif state == "skipped":
                summary.skipped += 1
            elif state == "pending":
                summary.pending += 1
            elif state in ("panicked", "interrupted", "aborted"):
                summary.panicked += 1

        summary.suites.append(suite)
        summary.duration += suite.elapsed

    return summary


def convert_ginkgo_report_to_dict(
    report_path: Path,
    go_module: str = "",
    output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Convert Ginkgo JSON report to standardized test report format.

    This produces output compatible with the unified report parser format:
    {
        "tests": [{"nodeid": str, "outcome": str}, ...],
        "summary": {...},
        "duration": float,
        "_framework": "ginkgo"
    }

    Args:
        report_path: Path to Ginkgo JSON report
        go_module: Optional Go module path
        output_path: Optional path to save result

    Returns:
        Standardized test report dict
    """
    ginkgo_summary = parse_ginkgo_json_report(report_path, go_module)

    # Map Ginkgo states to standard outcomes
    state_to_outcome = {
        "passed": "passed",
        "failed": "failed",
        "skipped": "skipped",
        "pending": "skipped",  # Pending tests are treated as skipped
        "panicked": "error",
        "interrupted": "error",
        "aborted": "error",
    }

    tests = []
    for spec in ginkgo_summary.specs:
        outcome = state_to_outcome.get(spec.state, "failed")
        tests.append(
            {
                "nodeid": spec.nodeid,
                "outcome": outcome,
            }
        )

    result = {
        "tests": tests,
        "collectors": [],
        "summary": {
            "passed": ginkgo_summary.passed,
            "failed": ginkgo_summary.failed,
            "skipped": ginkgo_summary.skipped + ginkgo_summary.pending,
            "error": ginkgo_summary.panicked,
            "total": ginkgo_summary.total,
        },
        "duration": round(ginkgo_summary.duration, 2),
        "_framework": "ginkgo",
    }

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2)

    return result


def build_ginkgo_summary_dict(
    ginkgo_summary: GinkgoTestSummary,
    output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Build standardized summary dict from GinkgoTestSummary.

    This produces output compatible with pytest_report_utils.convert_pytest_report_to_summary.

    Args:
        ginkgo_summary: Parsed GinkgoTestSummary
        output_path: Optional path to save summary JSON

    Returns:
        Summary dict with standard structure
    """
    # Build failed tests list
    failed_list = []
    for spec in ginkgo_summary.specs:
        if spec.state == "failed":
            failed_list.append(
                {
                    "nodeid": spec.nodeid,
                    "message": spec.failure_message or "Test failed",
                }
            )

    # Build error list (panicked/interrupted specs)
    error_list = []
    for spec in ginkgo_summary.specs:
        if spec.state in ("panicked", "interrupted", "aborted"):
            error_list.append(
                {
                    "nodeid": spec.nodeid,
                    "message": spec.failure_message or f"Test {spec.state}",
                }
            )

    # Group skipped/pending tests
    skip_reasons: Dict[str, List[str]] = {}
    for spec in ginkgo_summary.specs:
        if spec.state == "skipped":
            reason = "Skipped"
            if reason not in skip_reasons:
                skip_reasons[reason] = []
            skip_reasons[reason].append(spec.nodeid)
        elif spec.state == "pending":
            reason = "Pending (Ginkgo)"
            if reason not in skip_reasons:
                skip_reasons[reason] = []
            skip_reasons[reason].append(spec.nodeid)

    skipped_by_reason = [
        {"reason": reason, "count": len(tests), "tests": tests}
        for reason, tests in sorted(skip_reasons.items(), key=lambda x: -len(x[1]))
    ]

    # Build passed tests list
    passed_list = [spec.nodeid for spec in ginkgo_summary.specs if spec.state == "passed"]

    result = {
        "duration": round(ginkgo_summary.duration, 2),
        "summary": {
            "total": ginkgo_summary.total,
            "passed": ginkgo_summary.passed,
            "failed": ginkgo_summary.failed,
            "error": ginkgo_summary.panicked,
            "skipped": ginkgo_summary.skipped + ginkgo_summary.pending,
        },
        "results": {
            "failed": failed_list,
            "error": error_list,
            "skipped": skipped_by_reason,
            "passed": passed_list,
        },
    }

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2)

    return result


# CLI support
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python go_report_utils.py <input_file> [output_json]")
        print("       input_file: .jsonl (JSON Lines) or .log/.txt (verbose text)")
        sys.exit(1)

    input_file = Path(sys.argv[1])
    output_file = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    if not input_file.exists():
        print(f"Error: Input file not found: {input_file}")
        sys.exit(1)

    # Auto-detect format and parse
    summary_dict = parse_go_test_output(input_file, output_file)

    # Parse again for detailed print (detect format)
    suffix = input_file.suffix.lower()
    if suffix == ".jsonl":
        go_summary = parse_go_test_jsonl(input_file)
    elif suffix in (".log", ".txt"):
        go_summary = parse_go_test_verbose(input_file)
    else:
        # Detect by content
        content = input_file.read_text(errors="replace")
        first_line = content.split("\n")[0].strip() if content else ""
        if first_line.startswith("{"):
            go_summary = parse_go_test_jsonl(input_file)
        else:
            go_summary = parse_go_test_verbose(input_file)

    print_summary(go_summary)

    if output_file:
        print(f"Summary saved to: {output_file}")

    # Exit with non-zero if there are failures
    if go_summary.failed > 0 or go_summary.errors > 0:
        sys.exit(1)
