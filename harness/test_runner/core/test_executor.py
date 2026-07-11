"""
Test execution utilities (framework-aware) for the test runner framework.

This module intentionally does NOT encode milestone-specific concepts like
"start/end/original" states, git checkout logic, or compilation patching.
Those concerns live in higher-level orchestration code.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import List, Optional, Tuple

from .docker import DockerRunner
from .report_parser import parse_test_report, merge_test_reports

logger = logging.getLogger(__name__)

OUTPUT_MOUNT_PATH = "/output"

# First-match-wins scan for the fatal line in raw build/test output, covering
# the benchmark's frameworks (cargo, go, maven/gradle, pytest, node).
_FATAL_LINE_PATTERNS = [
    re.compile(r"^error(\[E\d+\])?: "),  # rustc/cargo
    re.compile(r"panicked at"),  # rust panic
    re.compile(r"^\S.*\.go:\d+:\d+: "),  # go compile error
    re.compile(r"^# \S+"),  # go build failure package header
    re.compile(r"^\[ERROR\]"),  # maven
    re.compile(r"^Traceback \(most recent call last\)"),  # python
    re.compile(r"^(?:[A-Za-z_.]+)?(?:Error|Exception): "),  # python exception
    re.compile(r"Cannot find module"),  # node
]


def extract_first_fatal_error(
    text: str,
    *,
    context_lines: int = 8,
    max_chars: int = 1500,
) -> Optional[str]:
    """
    Return the first fatal error found in raw build/test output, with up to
    `context_lines` following lines for context, or None if nothing matches.

    Used to surface the real cause (e.g. a compile error buried in an
    eval_*.log) when no valid test report can be produced.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        for pattern in _FATAL_LINE_PATTERNS:
            if pattern.search(line):
                snippet = "\n".join(lines[i : i + 1 + context_lines])
                return snippet[:max_chars]
    return None


def get_default_test_cmd(
    workers: int,
    timeout: int,
    output_file: str,
    framework: str = "pytest",
) -> str:
    """
    Get default test command for a given framework.

    Args:
        workers: Number of parallel workers
        timeout: Test timeout in seconds
        output_file: Output file name for test results
        framework: Test framework name (pytest, go_test, maven, cargo, jest, mocha)

    Returns:
        Test command string
    """
    commands = {
        "pytest": f"pytest -n {workers} --timeout={timeout} --json-report --json-report-file={OUTPUT_MOUNT_PATH}/{output_file}",
        "unittest": f"python -m pytest -n {workers} --timeout={timeout} --json-report --json-report-file={OUTPUT_MOUNT_PATH}/{output_file}",
        "go_test": f"go test -json -timeout {timeout}s -parallel {workers} ./... 2>&1 | tee {OUTPUT_MOUNT_PATH}/{output_file}",
        "maven": f"mvn test -Dmaven.test.failure.ignore=true -Dsurefire.timeout={timeout} 2>&1 | tee {OUTPUT_MOUNT_PATH}/{output_file}",
        "gradle": f"gradle test --continue -Dtest.parallel=true -Dtest.maxParallelForks={workers} 2>&1 | tee {OUTPUT_MOUNT_PATH}/{output_file}",
        "cargo": f"cargo test --no-fail-fast -- --test-threads={workers} 2>&1 | tee {OUTPUT_MOUNT_PATH}/{output_file}",
        "jest": f"npx jest --json --outputFile={OUTPUT_MOUNT_PATH}/{output_file} --testTimeout={timeout * 1000} --maxWorkers={workers}",
        "mocha": f"npx mocha --reporter json --timeout {timeout * 1000} --parallel --jobs {workers} > {OUTPUT_MOUNT_PATH}/{output_file}",
    }

    return commands.get(framework, commands["pytest"])


def build_test_cmd(
    *,
    test_cmd_template: str,
    workers: int,
    timeout: int,
    output_file: str,
    milestone_id: str,
    framework: str = "pytest",
) -> str:
    """
    Build the concrete test command for a single run.

    If `test_cmd_template` is empty, falls back to the framework default command.
    Otherwise formats the template with:
      - {workers}, {timeout}, {output_file}, {milestone_id}
    """
    if test_cmd_template:
        return test_cmd_template.format(
            workers=workers,
            timeout=timeout,
            output_file=output_file,
            milestone_id=milestone_id,
        )
    return get_default_test_cmd(workers, timeout, output_file, framework)


# Known infrastructure-failure signatures (F-2a). Deliberately narrow: this
# deterministic layer only carries CONFIRMED mechanical signatures; the
# general sweep lives in docs/post_verify/infra-failure-audit.md and promotes new ones here.
INFRA_FAILURE_PATTERNS = [
    re.compile(r"Could not find a working container runtime strategy"),  # testcontainers
    re.compile(r"Cannot connect to the Docker daemon"),
    re.compile(r"error during connect: .*docker", re.IGNORECASE),
    re.compile(r"dial unix /var/run/docker\.sock"),
    re.compile(r"docker: (?:command )?not found"),
]


def detect_infrastructure_failure(text: str, *, max_chars: int = 300) -> Optional[str]:
    """
    Return a snippet around the first known infrastructure-failure signature,
    or None. A hit means the evaluation environment (not the agent's code)
    broke the test run — callers mark scoring untrusted and retry.

    Reports are often one giant JSON line, so the snippet is a window around
    the match itself, never the head of the containing line.
    """
    first: Optional[re.Match] = None
    for pattern in INFRA_FAILURE_PATTERNS:
        m = pattern.search(text)
        if m and (first is None or m.start() < first.start()):
            first = m
    if first is None:
        return None
    return text[first.start() : first.start() + max_chars].splitlines()[0].strip()


def run_test(
    runner: DockerRunner,
    *,
    output_dir: Path,
    pre_script: str,
    test_cmd: str,
    post_script: str = "",
    timeout_seconds: Optional[int] = None,
) -> Tuple[int, str, str]:
    """
    Run a single test command in a container, mounting `output_dir` to /output.

    The test command is executed as `{test_cmd} || true` so the script continues
    even when tests fail (allowing report artifacts to be collected).
    """
    parts = [pre_script.rstrip("\n"), f"{test_cmd} || true"]
    if post_script.strip():
        parts.append(post_script.rstrip("\n"))
    script = "\n".join(parts) + "\n"

    returncode, stdout, stderr = runner.run(
        script,
        timeout=timeout_seconds,
        extra_volumes={str(output_dir.absolute()): OUTPUT_MOUNT_PATH},
    )
    return returncode, stdout, stderr


def materialize_report(
    report_files: List[Path],
    *,
    output_path: Path,
    framework: str = "pytest",
    verbose: bool = False,
) -> bool:
    """
    Convert one or more raw report files into a single standardized JSON report.

    - If there is exactly one file: parse and write standardized JSON.
    - If there are multiple files: merge them using `merge_test_reports`.
    """
    if not report_files:
        return False

    if len(report_files) == 1:
        try:
            parsed = parse_test_report(report_files[0], framework)
            with open(output_path, "w") as f:
                json.dump(parsed, f, indent=2)
            return True
        except Exception as e:
            logger.warning(f"Failed to parse report {report_files[0]}: {e}")
            return False

    return merge_test_reports(report_files, output_path, framework, verbose)
