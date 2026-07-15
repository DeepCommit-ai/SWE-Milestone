"""
Milestone attempt runner.

This module orchestrates milestone-specific concepts (start/end/original states,
git checkout, optional compilation patching) while delegating test execution and
report standardization to `core.test_executor`.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .classifier import TestClassifier
from .docker import DockerRunner
from .report_parser import FRAMEWORK_CONFIG, convert_to_summary, get_file_extension
from .test_executor import (
    build_test_cmd,
    extract_first_fatal_error,
    materialize_report,
    run_test,
)
from .types import MilestoneTestConfig

logger = logging.getLogger(__name__)


class RunnerInfrastructureError(RuntimeError):
    """The outer test runner did not complete, so its reports are untrusted.

    Test-command failures are intentionally masked inside ``run_test`` so the
    post-run report collector can finish. A nonzero *outer* return code means
    setup, collection, Docker exec, or the overall timeout failed; parsing any
    report left behind would score a partial or stale run.
    """


class RunnerBuildFailureError(RuntimeError):
    """The test command could not build/collect the intended test universe.

    ``run_test`` deliberately lets ordinary test failures continue so report
    collectors can run.  Shell pipelines (for example ``go test | tee``) also
    mask compilation/setup failures, though, and parsing those logs produces a
    deceptively small test universe. This deterministic error is intentionally
    distinct from :class:`RunnerInfrastructureError`: strict policy may fail
    closed, but it should never be retried as transient infrastructure.
    """


# Deliberately narrow, framework-specific signatures.  These describe failure
# to build/collect tests, not an ordinary failing test.  In particular, Maven
# modes use ``maven.test.failure.ignore=true`` so a completed test run ends in
# BUILD SUCCESS even when assertions fail.
_MASKED_BUILD_FAILURE_PATTERNS = {
    "go_test": (
        re.compile(r"^FAIL\s+\S+\s+\[(?:build|setup) failed\]\s*$"),
        re.compile(r"no required module provides package"),
        re.compile(r"updates to go\.mod needed; to update it"),
    ),
    "maven": (
        re.compile(r"^\[INFO\]\s+BUILD FAILURE\s*$"),
        re.compile(r"^\[ERROR\]\s+COMPILATION ERROR"),
    ),
    "cargo": (
        re.compile(r"^error: could not compile\b"),
    ),
    "gradle": (
        re.compile(r"Execution failed for task ['\"]:[^'\"]*compile[^'\"]*['\"]", re.IGNORECASE),
        re.compile(r"^Compilation failed; see the compiler error output", re.IGNORECASE),
    ),
}

APPLY_PATCHES_SCRIPT = """# Apply compilation patches if script exists
if [ -x /usr/local/bin/apply_patches.sh ]; then
    echo ">>> Applying compilation patches..."
    /usr/local/bin/apply_patches.sh
fi
"""

CARGO_CACHE_CLEANUP_SCRIPT = """# Force recompilation of test files for Rust projects to ensure new tests are included
if [ -f Cargo.toml ] || [ -f cargo.toml ]; then
    echo ">>> Touching test files to force recompilation..."
    # Touch all test files to update their timestamps
    # This forces cargo to recompile tests without rebuilding all dependencies
    find . -path '*/tests/*.rs' -type f -exec touch {} \\; 2>/dev/null || true
    find . -path '*/src/*test*.rs' -type f -exec touch {} \\; 2>/dev/null || true
    echo ">>> Test files touched, forcing recompilation"
fi
"""


def _build_surefire_collect_script(archive_name: str, label: str = "") -> str:
    label_suffix = f" {label}" if label else ""
    return f"""
echo ">>> Collecting Surefire XML reports{label_suffix}..."
mkdir -p /tmp/surefire_reports
# Find all surefire-reports directories and copy with module structure
find /testbed -path "*/target/surefire-reports" -type d | while read dir; do
    # Extract module path relative to /testbed
    module_path=$(dirname "$dir" | sed 's|/testbed/||' | sed 's|/target||')
    if [ -n "$module_path" ]; then
        mkdir -p "/tmp/surefire_reports/$module_path"
        cp -f "$dir"/TEST-*.xml "/tmp/surefire_reports/$module_path/" 2>/dev/null || true
    fi
done
# Create archive if any reports were collected
if [ -n "$(find /tmp/surefire_reports -name 'TEST-*.xml' 2>/dev/null)" ]; then
    cd /tmp && tar -czf /output/{archive_name} surefire_reports/
    echo ">>> Surefire reports archived to {archive_name}"
else
    echo ">>> No Surefire XML reports found"
fi
rm -rf /tmp/surefire_reports
"""


def get_switch_cmd(state: str, milestone_id: str, base_commit: Optional[str] = None) -> str:
    """
    Get git command to switch to a specific milestone state.

    Uses 'git checkout -f' to forcefully discard local changes before switching.
    This is necessary because Dockerfiles may apply compilation patches that modify
    tracked files, and these modifications would otherwise block state switching.
    """
    if state == "original":
        if not base_commit:
            raise ValueError("base_commit required for original state")
        return f"git checkout -f {base_commit}"
    if state == "start":
        return f"git checkout -f milestone-{milestone_id}-start"
    if state == "end":
        return f"git checkout -f milestone-{milestone_id}-end"
    raise ValueError(f"Unknown state: {state}")


def run_single_attempt(
    attempt_num: int,
    attempt_dir: Path,
    milestone_id: str,
    runner: DockerRunner,
    config: MilestoneTestConfig,
    base_commit: Optional[str],
    workers: int,
    timeout: int,
    verbose: bool,
    framework: str = "pytest",
) -> Dict[str, Any]:
    """
    Run a single test attempt for the milestone.

    Returns:
        Dictionary with attempt result
    """
    attempt_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"[{milestone_id}] Attempt {attempt_num}...")

    # Get all unique states from config
    all_states = set(config.get_all_states())

    # Add original if configured
    if config.include_original and base_commit:
        all_states.add("original")

    # Run each (state, mode) pair
    state_mode_files: Dict[str, List[Path]] = {state: [] for state in all_states}

    for state, mode in config.get_all_state_mode_pairs():
        # Use per-mode framework if specified, otherwise use default
        mode_framework = mode.framework if mode.framework else framework
        file_ext = get_file_extension(mode_framework)
        output_file = f"{state}_{mode.name}{file_ext}"

        test_cmd = build_test_cmd(
            test_cmd_template=mode.test_cmd,
            workers=workers,
            timeout=timeout,
            output_file=output_file,
            milestone_id=milestone_id,
            framework=mode_framework,
        )

        # Build switch command
        switch_cmd = get_switch_cmd(state, milestone_id, base_commit)

        # Build surefire collection script for Maven projects
        # This collects XML reports with module path prefixes for method-level granularity
        surefire_collect_script = ""
        if mode_framework in ("maven", "gradle"):
            surefire_archive_name = f"{state}_surefire_reports.tar.gz"
            surefire_collect_script = _build_surefire_collect_script(
                surefire_archive_name,
                label=f"for {state} state",
            )

        pre_script = f"""
set -e
cd /testbed
echo ">>> Switching to {state} state..."
{switch_cmd}
{APPLY_PATCHES_SCRIPT}
{CARGO_CACHE_CLEANUP_SCRIPT}
echo ">>> Running {mode.name} tests for {state} state..."
"""
        post_script = f"""
echo ">>> Done with {state}/{mode.name}"
{surefire_collect_script}
"""

        returncode, stdout, stderr = run_test(
            runner,
            output_dir=attempt_dir,
            pre_script=pre_script,
            test_cmd=test_cmd,
            post_script=post_script,
            timeout_seconds=timeout * 60,
        )

        if verbose:
            logger.debug(f"[{milestone_id}] {state}_{mode.name}: returncode={returncode}")

        output_path = attempt_dir / output_file
        if output_path.exists():
            state_mode_files[state].append(output_path)

    # Run original state if configured (with default mode only)
    if config.include_original and base_commit and "original" not in [s for s, m in config.get_all_state_mode_pairs()]:
        output_file = f"original_default{file_ext}"
        test_cmd = build_test_cmd(
            test_cmd_template="",
            workers=workers,
            timeout=timeout,
            output_file=output_file,
            milestone_id=milestone_id,
            framework=framework,
        )
        switch_cmd = get_switch_cmd("original", milestone_id, base_commit)

        pre_script = f"""
set -e
cd /testbed
echo ">>> Switching to original state..."
{switch_cmd}
{APPLY_PATCHES_SCRIPT}
{CARGO_CACHE_CLEANUP_SCRIPT}
echo ">>> Running tests for original state..."
"""
        post_script = """
echo ">>> Done with original state"
"""
        run_test(
            runner,
            output_dir=attempt_dir,
            pre_script=pre_script,
            test_cmd=test_cmd,
            post_script=post_script,
            timeout_seconds=timeout * 60,
        )

        output_path = attempt_dir / output_file
        if output_path.exists():
            state_mode_files["original"].append(output_path)

    # Merge results per state (within-attempt merging)
    for state in all_states:
        mode_files = state_mode_files.get(state, [])
        print(f"DEBUG [{milestone_id}] {state} mode_files: {[str(f) for f in mode_files]}")
        if not mode_files:
            continue
        merged_path = attempt_dir / f"{state}.json"
        if not materialize_report(mode_files, output_path=merged_path, framework=framework, verbose=verbose):
            logger.warning(f"[{milestone_id}] Failed to materialize {state} report")

    # Verify both start.json and end.json exist
    start_file = attempt_dir / "start.json"
    end_file = attempt_dir / "end.json"

    if not start_file.exists() or not end_file.exists():
        return {
            "attempt": attempt_num,
            "status": "error",
            "error": f"Missing output files: start={start_file.exists()}, end={end_file.exists()}",
        }

    # Generate summary files with fail/skip reasons
    try:
        convert_to_summary(start_file, attempt_dir / "start_summary.json", framework)
        convert_to_summary(end_file, attempt_dir / "end_summary.json", framework)
        if config.include_original and (attempt_dir / "original.json").exists():
            convert_to_summary(attempt_dir / "original.json", attempt_dir / "original_summary.json", framework)
    except Exception as e:
        logger.warning(f"[{milestone_id}] Failed to generate summary files: {e}")

    # Classify results for this attempt (using framework-aware classifier)
    classifier = TestClassifier(framework=framework)
    classification = classifier.classify_from_files(start_file, end_file)
    summary = classifier.generate_summary(classification)

    # Put summary first in output
    classification_result = {"summary": summary, **classification}

    # Save classification for this attempt
    classification_file = attempt_dir / "classification.json"
    with open(classification_file, "w") as f:
        json.dump(classification_result, f, indent=2)

    logger.info(
        f"[{milestone_id}] Attempt {attempt_num} success: "
        f"fail_to_pass={summary['fail_to_pass']}, "
        f"pass_to_fail={summary['pass_to_fail']}"
    )

    return {
        "attempt": attempt_num,
        "status": "success",
        "statistics": summary,
        "classification_file": str(classification_file.name),
        "run_configs": [m.name for m in config.modes],
    }


def _infer_framework_from_modes(config: MilestoneTestConfig) -> str:
    """
    Infer test framework from configured test commands.

    Priority:
    1. Explicit 'framework' field in any mode (if specified)
    2. Heuristic detection from test command text

    This is used by single-state evaluation runners that want to reuse
    milestone test configs but are not explicitly state-aware.
    """
    # First, check if any mode has an explicit framework field
    for mode in config.modes:
        if mode.framework:
            return mode.framework

    # Fall back to command-based heuristic
    cmds = [m.test_cmd for m in config.modes if m.test_cmd]
    joined = "\n".join(cmds).lower()

    if "cargo test" in joined:
        return "cargo"
    if "ginkgo" in joined:
        return "ginkgo"
    if "go test" in joined:
        return "go_test"
    if "mvn " in joined or "mvnw" in joined:
        return "maven"
    if "gradle " in joined or "gradlew" in joined:
        return "gradle"
    if "vitest" in joined:
        return "vitest"
    if "jest" in joined:
        return "jest"
    if "mocha" in joined:
        return "mocha"
    if "pytest" in joined:
        return "pytest"

    return "pytest"


def _safe_read_text(path: Path, max_bytes: int = 1_000_000) -> str:
    """Read up to `max_bytes` of a report/log file for diagnostics."""
    try:
        with open(path, "rb") as f:
            return f.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def detect_masked_build_failure(text: str, framework: str) -> Optional[str]:
    """Return the first build/setup-failure line for a supported framework.

    This is intentionally separate from generic fatal-error extraction: an
    assertion failure is a valid test result, whereas a compiler/setup failure
    means the report represents only a fragment of the expected test universe.
    """
    patterns = _MASKED_BUILD_FAILURE_PATTERNS.get(framework, ())
    if not patterns:
        return None

    for line in text.splitlines():
        stripped = line.strip()
        for pattern in patterns:
            if pattern.search(stripped):
                return stripped[:500]
    return None


def _primary_build_failure_signature(
    framework: str,
    terminal_signature: str,
    preceding_lines: List[str],
) -> str:
    """Prefer the compiler diagnostic over Go's terminal package marker.

    Parallel ``go test -json ./...`` output can associate a terminal line such
    as ``FAIL core/bloom [build failed]`` with compiler diagnostics emitted for
    a different package immediately beforehand.  The terminal marker is still
    useful evidence that the test universe is incomplete, but it is a poor
    top-level error summary.  When a Go compiler package header is present,
    report its first diagnostic and retain the terminal marker in the context.
    """
    if framework != "go_test" or not terminal_signature.startswith("FAIL"):
        return terminal_signature

    package_header_index = -1
    for index, line in enumerate(preceding_lines):
        if line.strip().startswith("# "):
            package_header_index = index

    if package_header_index < 0:
        return terminal_signature

    for line in preceding_lines[package_header_index + 1 :]:
        candidate = line.strip()
        if not candidate or candidate.startswith(("# ", "{", "FAIL\t", "FAIL ")):
            continue
        return candidate[:500]

    return terminal_signature


def _scan_report_for_masked_build_failure_details(
    path: Path,
    framework: str,
    *,
    context_lines: int = 12,
    max_chars: int = 3000,
) -> Optional[Tuple[str, str]]:
    """Return a build-failure signature and context anchored at its log line.

    Maven test output commonly contains ordinary application ``[ERROR]``
    lines long before a real compiler failure.  Generic first-error extraction
    therefore points at test noise, and reading only the first megabyte misses
    late reactor failures entirely.  Scan the report as a stream and retain
    context beginning at the exact build/setup signature that caused the
    fail-closed verdict.
    """
    if framework not in _MASKED_BUILD_FAILURE_PATTERNS:
        return None
    from collections import deque

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as report:
            preceding = deque(maxlen=context_lines)
            for line in report:
                signature = detect_masked_build_failure(line, framework)
                if signature:
                    # Compilers commonly print the actual cause BEFORE their
                    # terminal BUILD FAILURE / `FAIL ... [build failed]` line.
                    # With parallel Go packages, the following lines may even
                    # belong to unrelated successful tests. Preserve the lead-in
                    # so the evaluator records the real END compile error.
                    include_preceding = not (
                        framework == "maven" and "COMPILATION ERROR" in signature
                    )
                    preceding_lines = list(preceding)
                    primary_signature = _primary_build_failure_signature(
                        framework, signature, preceding_lines
                    )
                    context = (
                        preceding_lines if include_preceding else []
                    ) + [line.rstrip("\n")]
                    for _ in range(context_lines):
                        next_line = report.readline()
                        if not next_line:
                            break
                        context.append(next_line.rstrip("\n"))
                    return primary_signature, "\n".join(context)[:max_chars]
                preceding.append(line.rstrip("\n"))
    except OSError:
        return None
    return None


def _scan_report_for_masked_build_failure(path: Path, framework: str) -> Optional[str]:
    """Scan a raw report line-by-line without loading a large log at once."""
    details = _scan_report_for_masked_build_failure_details(path, framework)
    return details[0] if details else None


def _first_fatal_diagnostic(sources: List[tuple], tail_lines: int = 15) -> str:
    """
    Build a diagnostic suffix from `(label, raw_text)` sources: the first
    fatal error found, else the tail of the first non-empty output. Keeps the
    real cause (e.g. a compile error tee'd into eval_*.log) visible in the
    top-level exception instead of only two layers down in artifacts.
    """
    for label, text in sources:
        if not text:
            continue
        snippet = extract_first_fatal_error(text)
        if snippet:
            return f"\nFirst fatal error ({label}):\n{snippet}"
    for label, text in sources:
        if text and text.strip():
            tail = "\n".join(text.splitlines()[-tail_lines:])
            return f"\nLast output lines ({label}):\n{tail}"
    return ""


def run_single_state_tests(
    runner: object,
    *,
    workspace_root: Path,
    milestone_id: str,
    output_dir: Path,
    workers: int,
    timeout: int,
    workdir: str = "/testbed",
    test_dir: Optional[str] = None,
    verbose: bool = False,
    output_prefix: str = "eval",
    build_failure_fail_closed: bool = False,
    build_failure_diagnostics: Optional[List[str]] = None,
) -> Path:
    """
    Run milestone test modes once against the current working tree (single state).

    This wrapper exists to reuse `dockerfiles/<milestone_id>/test_config.json` and
    the unified report parsing/merging logic without exposing internal helpers like
    `build_test_cmd` or requiring the caller to pass explicit framework/state info.

    Assumptions:
    - The caller has already checked out the desired git state and applied any patches.
    - The container has a writable /output mapped to `output_dir` (or the runner adapts it).

    ``build_failure_fail_closed`` controls only deterministic build/setup
    failures reported by an otherwise completed test command.  When disabled,
    reports from packages/modules that did run are still parsed and scored.
    Outer-runner timeouts and nonzero exits remain fail-closed because those
    artifacts may be truncated or stale rather than a valid partial universe.

    Returns:
        Path to the merged standardized JSON report: `{output_dir}/{output_prefix}.json`.
    """
    from .report_parser import parse_test_report

    output_dir.mkdir(parents=True, exist_ok=True)

    # Retries may reuse the same output directory. Never let a prior merged
    # report or summary survive into a failed attempt.
    for stale_name in (f"{output_prefix}.json", f"{output_prefix}_summary.json"):
        stale_path = output_dir / stale_name
        if stale_path.exists():
            stale_path.unlink()

    config_path = workspace_root / "dockerfiles" / milestone_id / "test_config.json"
    if config_path.exists():
        config = MilestoneTestConfig.from_file(config_path, include_original=False)
    else:
        config = MilestoneTestConfig.default(include_original=False)

    # Default framework for modes without explicit framework
    default_framework = _infer_framework_from_modes(config)
    if default_framework not in FRAMEWORK_CONFIG:
        default_framework = "pytest"

    # Track (output_path, framework) tuples for per-mode parsing
    mode_reports: List[tuple] = []
    # Track (label, raw stdout+stderr) per mode for failure diagnostics
    mode_outputs: List[tuple] = []

    for mode in config.modes:
        # Use per-mode framework if specified, otherwise use default
        mode_framework = mode.framework if mode.framework else default_framework
        if mode_framework not in FRAMEWORK_CONFIG:
            mode_framework = default_framework

        file_ext = get_file_extension(mode_framework)
        output_file = f"{output_prefix}_{mode.name}{file_ext}"
        output_path = output_dir / output_file
        if output_path.exists():
            output_path.unlink()
        test_cmd = build_test_cmd(
            test_cmd_template=mode.test_cmd,
            workers=workers,
            timeout=timeout,
            output_file=output_file,
            milestone_id=milestone_id,
            framework=mode_framework,
        )

        if mode_framework in ("pytest", "unittest") and not mode.test_cmd and test_dir:
            test_cmd = f"{test_cmd} {test_dir}"

        # Maven/Gradle: collect Surefire XML for method-level granularity.
        # The parser will look for `{output_prefix}_surefire_reports.tar.gz`.
        surefire_collect_script = ""
        if mode_framework in ("maven", "gradle"):
            surefire_archive_name = f"{output_prefix}_surefire_reports.tar.gz"
            surefire_archive_path = output_dir / surefire_archive_name
            if surefire_archive_path.exists():
                surefire_archive_path.unlink()
            surefire_collect_script = _build_surefire_collect_script(surefire_archive_name)

        pre_script = f"""
set -e
mkdir -p /output
cd {workdir}
echo ">>> Running {mode.name} tests (framework={mode_framework})..."
"""
        post_script = f"""
echo ">>> Done with {mode.name}"
{surefire_collect_script}
"""

        # Duck-typed runner: DockerRunner (baseline) or docker-exec runner adapter (e2e).
        # Use a generous timeout for the entire test run (60 minutes by default,
        # aligned with the baseline runner's `timeout * 60`). Full Dubbo reactors
        # repeatedly crossed the old 30-minute limit and left plausible partial
        # Maven logs, so shortening this path relative to baseline was unsafe.
        # Note: `timeout` param is per-test timeout in seconds, but run_test needs
        # overall timeout. Use 30 minutes as a reasonable default for full test suite.
        returncode, run_stdout, run_stderr = run_test(  # type: ignore[arg-type]
            runner,  # pyright: ignore[reportArgumentType]
            output_dir=output_dir,
            pre_script=pre_script,
            test_cmd=test_cmd,
            post_script=post_script,
            timeout_seconds=mode.run_timeout_seconds or 3600,  # e2e modes may override via test_config
        )
        combined_output = "\n".join(part for part in (run_stdout, run_stderr) if part)
        mode_outputs.append((f"mode '{mode.name}' output", combined_output))

        if returncode != 0:
            partial_text = _safe_read_text(output_path) if output_path.exists() else ""
            diagnostics = list(mode_outputs)
            if partial_text:
                diagnostics.append((f"partial report '{output_path.name}'", partial_text))
            raise RunnerInfrastructureError(
                f"{milestone_id}/{mode.name}: outer test runner returned {returncode}; "
                "refusing to parse partial or stale reports. "
                f"No valid test report files generated under {output_dir}"
                + _first_fatal_diagnostic(diagnostics)
            )

        if output_path.exists():
            build_failure_details = _scan_report_for_masked_build_failure_details(
                output_path, mode_framework
            )
            if build_failure_details:
                build_failure, build_failure_context = build_failure_details
                diagnostic = (
                    f"{milestone_id}/{mode.name}: test command reported a "
                    f"{mode_framework} build/setup failure ({build_failure}); "
                    "partial test universe detected"
                    f"\nFirst build failure (report '{output_path.name}'):\n"
                    f"{build_failure_context}"
                )
                if build_failure_diagnostics is not None:
                    build_failure_diagnostics.append(diagnostic)
                if build_failure_fail_closed:
                    raise RunnerBuildFailureError(
                        diagnostic.replace(
                            "partial test universe detected",
                            "refusing to parse a partial test universe",
                        )
                    )

                warning = (
                    f"⚠️  {milestone_id}/{mode.name}: build/setup failure detected; "
                    "compatibility policy is enabled, so completed package/module "
                    "reports will still be parsed and scored"
                )
                logger.warning(warning)
                print(warning)
            mode_reports.append((output_path, mode_framework))

    if not mode_reports:
        raise RuntimeError(
            f"No valid test report files generated under {output_dir}"
            + _first_fatal_diagnostic(mode_outputs)
        )

    # Parse each report with its own framework and merge results
    merged_tests: List[Dict[str, Any]] = []
    merged_summary = {"total": 0, "passed": 0, "failed": 0, "error": 0, "skipped": 0}

    dropped_reports: List[tuple] = []
    for report_path, fw in mode_reports:
        try:
            parsed = parse_test_report(report_path, fw)
            tests = parsed.get("tests") if parsed else None
            if tests:
                merged_tests.extend(tests)
                summary = parsed.get("summary", {})
                for key in merged_summary:
                    merged_summary[key] += summary.get(key, 0)
                if verbose:
                    logger.info(f"Parsed {report_path.name} ({fw}): {summary.get('total', 0)} tests")
            else:
                dropped_reports.append((report_path.name, "parsed to 0 tests"))
        except Exception as e:
            dropped_reports.append((report_path.name, f"parse error: {e}"))

    # A mode whose report exists but contributes nothing silently shrinks the
    # test universe (its scored tests become "missing") — say so loudly. The
    # usual cause is a truncated report from a run killed at the mode timeout.
    for name, why in dropped_reports:
        msg = (
            f"🚨 report {name} dropped from merge ({why}) — test universe shrank; "
            f"check the mode's run_timeout_seconds / output truncation"
        )
        logger.error(msg)
        print(msg)

    # Build merged report
    merged_report = {
        "tests": merged_tests,
        "summary": merged_summary,
    }

    merged_path = output_dir / f"{output_prefix}.json"
    with open(merged_path, "w") as f:
        json.dump(merged_report, f, indent=2)

    if not merged_tests:
        report_texts = [
            (report_path.name, _safe_read_text(report_path))
            for report_path, _ in mode_reports
        ]
        raise RuntimeError(
            f"No valid test report files generated under {output_dir}"
            + _first_fatal_diagnostic(report_texts + mode_outputs)
        )

    return merged_path
