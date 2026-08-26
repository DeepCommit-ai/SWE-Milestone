"""Tests for F-2b: surface the first fatal error from eval logs / test output
when no valid test report can be produced (see repair_scope_spec §6 F-2b).

The evaluator's top-level error used to be a bare
`RuntimeError: No valid test report files generated under ...`, hiding the
real cause (e.g. a cargo compile error) two layers down in eval_default.log.
"""

import json
from pathlib import Path

import pytest

from harness.test_runner.core.milestone_attempt import run_single_state_tests
from harness.test_runner.core.report_parser import get_file_extension
from harness.test_runner.core.test_executor import extract_first_fatal_error

CARGO_ERROR_OUTPUT = """\
   Compiling nu-protocol v0.107.0
warning: unused import: `std::path::PathBuf`
 --> crates/nu-cli/src/repl.rs:8:5
error[E0599]: no variant or associated item named `Sqlite` found for enum `HistoryFileFormat` in the current scope
   --> crates/nu-cli/src/config_files.rs:263:46
    |
263 |                 HistoryFileFormat::Sqlite => sqlite_history_path(),
    |                                    ^^^^^^ variant or associated item not found in `HistoryFileFormat`
error: could not compile `nu-cli` (lib) due to 1 previous error
"""

GO_ERROR_OUTPUT = """\
# github.com/zeromicro/go-zero/internal/health
internal/health/health.go:12:2: undefined: CreateHttpHandler
FAIL    github.com/zeromicro/go-zero/internal/health [build failed]
"""

MAVEN_ERROR_OUTPUT = """\
[INFO] Building dubbo-mutiny 3.3.6
[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.11.0:compile
[ERROR] /testbed/dubbo-mutiny/src/main/java/Foo.java:[10,8] cannot find symbol
"""

MAVEN_JAVAC_WARNING_BEFORE_ERROR_OUTPUT = """\
[ERROR] COMPILATION ERROR :
[INFO] -------------------------------------------------------------
[ERROR]   on the class path. A future release of javac may disable annotation processing
  unless at least one processor is specified by name (-processor), or a search
[ERROR] /testbed/dubbo-plugin/src/main/java/Foo.java:[42,17] cannot find symbol
[ERROR]   symbol:   class MissingType
[ERROR]   location: class Foo
"""

CLEAN_OUTPUT = """\
running 42 tests
test result: ok. 42 passed; 0 failed; 0 ignored
"""


class TestExtractFirstFatalError:
    def test_cargo_compile_error(self):
        snippet = extract_first_fatal_error(CARGO_ERROR_OUTPUT)
        assert snippet is not None
        assert "error[E0599]" in snippet
        assert "HistoryFileFormat" in snippet
        # The snippet must start at the fatal line, not at the earlier warning.
        assert not snippet.startswith("warning:")

    def test_go_compile_error(self):
        snippet = extract_first_fatal_error(GO_ERROR_OUTPUT)
        assert snippet is not None
        assert "undefined: CreateHttpHandler" in snippet

    def test_maven_error(self):
        snippet = extract_first_fatal_error(MAVEN_ERROR_OUTPUT)
        assert snippet is not None
        assert "Foo.java:[10,8] cannot find symbol" in snippet

    def test_maven_source_error_wins_over_javac_warning_continuation(self):
        snippet = extract_first_fatal_error(MAVEN_JAVAC_WARNING_BEFORE_ERROR_OUTPUT)
        assert snippet is not None
        assert snippet.startswith("[ERROR] /testbed/dubbo-plugin/src/main/java/Foo.java")
        assert "MissingType" in snippet
        assert "annotation processing" not in snippet

    def test_clean_output_returns_none(self):
        assert extract_first_fatal_error(CLEAN_OUTPUT) is None


class FakeRunner:
    """Duck-typed stand-in for DockerRunner: optionally materializes report
    files into the mounted output dir and returns canned stdout/stderr."""

    def __init__(self, output_dir: Path, stdout: str = "", files: dict | None = None):
        self._output_dir = output_dir
        self._stdout = stdout
        self._files = files or {}

    def run(self, script, timeout=None, extra_volumes=None):
        for name, content in self._files.items():
            (self._output_dir / name).write_text(content)
        return 1, self._stdout, ""


def test_missing_report_surfaces_stdout_diagnostic(tmp_path):
    """416 path: no report file at all -> diagnostic comes from captured output."""
    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    runner = FakeRunner(output_dir, stdout=CARGO_ERROR_OUTPUT)

    with pytest.raises(RuntimeError) as exc_info:
        run_single_state_tests(
            runner,
            workspace_root=workspace_root,
            milestone_id="M001",
            output_dir=output_dir,
            workers=1,
            timeout=60,
        )

    msg = str(exc_info.value)
    # Compatibility: collect_results.py greps for this exact substring.
    assert f"No valid test report files generated under {output_dir}" in msg
    assert "error[E0599]" in msg


def test_zero_parsed_tests_surfaces_report_content(tmp_path):
    """446 path: report file exists (tee'd compile errors) but parses to zero
    tests -> diagnostic comes from the raw report/log content."""
    workspace_root = tmp_path / "ws"
    config_dir = workspace_root / "dockerfiles" / "M001"
    config_dir.mkdir(parents=True)
    (config_dir / "test_config.json").write_text(
        json.dumps(
            [
                {
                    "name": "default",
                    "test_states": ["start", "end"],
                    "test_cmd": "cargo test --workspace 2>&1 | tee /output/{output_file}",
                }
            ]
        )
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    log_name = f"eval_default{get_file_extension('cargo')}"
    runner = FakeRunner(output_dir, files={log_name: CARGO_ERROR_OUTPUT})

    with pytest.raises(RuntimeError) as exc_info:
        run_single_state_tests(
            runner,
            workspace_root=workspace_root,
            milestone_id="M001",
            output_dir=output_dir,
            workers=1,
            timeout=60,
        )

    msg = str(exc_info.value)
    assert f"No valid test report files generated under {output_dir}" in msg
    assert "error[E0599]" in msg
