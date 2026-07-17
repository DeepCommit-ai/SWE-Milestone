"""
Core data types for the test runner framework.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Tuple
from pathlib import Path
import json


# =============================================================================
# Default Validation Configurations for Common Language/Framework Combinations
# =============================================================================

DEFAULT_VALIDATION_CONFIGS: Dict[Tuple[str, str], Dict[str, Any]] = {
    ("python", "pytest"): {
        "env_check_cmd": "python --version && python -c 'import sys; print(sys.executable)'",
        "package_check_template": "python -c 'import {package}'",
        "collect_cmd": "pytest --collect-only -q {test_dir} 2>&1",
        "collect_pattern": r"(\d+)\s+tests?\s+collected",
        "test_file_pattern": "test_*.py",
        "run_cmd": "pytest {test_dir} --json-report --json-report-file=/output/{output_file} --timeout={timeout} -q {extra_args}",
    },
    ("python", "unittest"): {
        "env_check_cmd": "python --version && python -c 'import sys; print(sys.executable)'",
        "package_check_template": "python -c 'import {package}'",
        "collect_cmd": "python -c \"import unittest; suite=unittest.defaultTestLoader.discover('{test_dir}'); print('%d tests collected' % suite.countTestCases())\" 2>&1",
        "collect_pattern": r"(\d+)\s+tests?\s+collected",
        "test_file_pattern": "test_*.py",
        "run_cmd": "python -m unittest discover -s {test_dir} -v 2>&1 | tee /output/{output_file}",
    },
    ("java", "maven"): {
        "env_check_cmd": "java --version && mvn --version",
        "package_check_template": None,
        "collect_cmd": "mvn test-compile -q",
        "collect_pattern": None,
        "test_file_pattern": "*Test.java",
        "run_cmd": "mvn test -Dmaven.test.failure.ignore=true -q",
    },
    ("java", "gradle"): {
        "env_check_cmd": "java --version && gradle --version",
        "package_check_template": None,
        "collect_cmd": "gradle testClasses -q",
        "collect_pattern": None,
        "test_file_pattern": "*Test.java",
        "run_cmd": "gradle test --continue -q",
    },
    ("javascript", "jest"): {
        "env_check_cmd": "node --version && npm --version",
        "package_check_template": "node -e \"require('{package}')\"",
        "collect_cmd": "npx jest --listTests 2>&1",
        "collect_pattern": None,
        "test_file_pattern": "*.test.js",
        "run_cmd": "npx jest --json --outputFile=/output/{output_file}",
    },
    ("javascript", "mocha"): {
        "env_check_cmd": "node --version && npm --version",
        "package_check_template": "node -e \"require('{package}')\"",
        "collect_cmd": "npx mocha --dry-run 2>&1",
        "collect_pattern": None,
        "test_file_pattern": "*.test.js",
        "run_cmd": "npx mocha --reporter json > /output/{output_file}",
    },
    ("go", "go_test"): {
        "env_check_cmd": "go version",
        "package_check_template": None,
        "collect_cmd": "go test -tags netgo -list . ./... 2>&1",
        "collect_pattern": None,
        "test_file_pattern": "*_test.go",
        "run_cmd": "go test -json ./... > /output/{output_file}",
    },
    ("rust", "cargo"): {
        "env_check_cmd": "rustc --version && cargo --version",
        "package_check_template": None,
        "collect_cmd": "cargo test --no-run 2>&1",
        "collect_pattern": None,
        "test_file_pattern": "*.rs",
        "run_cmd": "cargo test -- --format json > /output/{output_file}",
    },
}


# =============================================================================
# Base Image Validation Configuration
# =============================================================================


@dataclass
class BaseValidationMode:
    """
    A test mode for base image validation.

    Attributes:
        name: Mode name (e.g., "default", "integration")
        test_cmd: Complete test command with template variables
        description: Optional description of this mode
    """

    name: str
    test_cmd: str = ""
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "test_cmd": self.test_cmd,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BaseValidationMode":
        return cls(
            name=data["name"],
            test_cmd=data.get("test_cmd", ""),
            description=data.get("description", ""),
        )


@dataclass
class BaseValidationConfig:
    """
    Configuration for base Docker image validation.

    Supports multiple languages and test frameworks through configurable commands.

    The config separates two concerns:
    1. Validation commands (env_check, package_check, collect) - from DEFAULT_VALIDATION_CONFIGS
    2. Test run commands (modes) - from test_config.json, repo-specific

    test_config.json format (same as milestone/commit level, without test_states):
    [
        {
            "name": "default",
            "test_cmd": "pytest {test_dir} --json-report --json-report-file=/output/{output_file}",
            "description": "Normal tests"
        },
        {
            "name": "integration",
            "test_cmd": "pytest {test_dir} --json-report --json-report-file=/output/{output_file} --integration",
            "description": "Integration tests"
        }
    ]

    Template variables available in test_cmd:
        {test_dir} - Test directory path
        {workers} - Number of parallel workers
        {timeout} - Timeout in seconds
        {output_file} - Output file name
        {extra_args} - Extra arguments
    """

    language: str = "python"
    test_framework: str = "pytest"
    env_check_cmd: str = ""
    package_check_template: Optional[str] = None
    collect_cmd: str = ""
    collect_pattern: Optional[str] = None
    test_file_pattern: str = "test_*.py"
    modes: List[BaseValidationMode] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "language": self.language,
            "test_framework": self.test_framework,
            "env_check_cmd": self.env_check_cmd,
            "package_check_template": self.package_check_template,
            "collect_cmd": self.collect_cmd,
            "collect_pattern": self.collect_pattern,
            "test_file_pattern": self.test_file_pattern,
            "modes": [m.to_dict() for m in self.modes],
        }

    def to_json(self, path: Path):
        """Save configuration to JSON file."""
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def from_file(
        cls,
        path: Path,
        language: str = "python",
        test_framework: str = "pytest",
    ) -> "BaseValidationConfig":
        """
        Load configuration from JSON file.

        The file should contain a list of test modes (same format as milestone/commit test_config.json).
        Validation commands (env_check, package_check, etc.) are loaded from DEFAULT_VALIDATION_CONFIGS
        based on the specified language/test_framework.

        Args:
            path: Path to test_config.json
            language: Programming language (for loading validation commands)
            test_framework: Test framework (for loading validation commands)

        Returns:
            BaseValidationConfig with modes from file and validation commands from defaults
        """
        with open(path) as f:
            data = json.load(f)

        # Get validation commands from defaults
        key = (language, test_framework)
        defaults = DEFAULT_VALIDATION_CONFIGS.get(key, {})

        if isinstance(data, list):
            # Standard format: list of modes
            modes = [BaseValidationMode.from_dict(m) for m in data]
        elif isinstance(data, dict):
            # Dict format with modes key
            modes_data = data.get("modes", [])
            modes = [BaseValidationMode.from_dict(m) for m in modes_data]
        else:
            raise ValueError(f"Invalid config format in {path}")

        return cls(
            language=language,
            test_framework=test_framework,
            env_check_cmd=defaults.get("env_check_cmd", ""),
            package_check_template=defaults.get("package_check_template"),
            collect_cmd=defaults.get("collect_cmd", ""),
            collect_pattern=defaults.get("collect_pattern"),
            test_file_pattern=defaults.get("test_file_pattern", "test_*.py"),
            modes=modes,
        )

    @classmethod
    def from_defaults(cls, language: str, test_framework: str) -> "BaseValidationConfig":
        """
        Create config from built-in defaults for a language/framework combination.

        Args:
            language: Programming language (python, java, javascript, go, rust)
            test_framework: Test framework (pytest, maven, jest, etc.)

        Returns:
            BaseValidationConfig with default settings

        Raises:
            ValueError: If no default config exists for the combination
        """
        key = (language, test_framework)
        if key not in DEFAULT_VALIDATION_CONFIGS:
            available = [f"{l}/{f}" for l, f in DEFAULT_VALIDATION_CONFIGS.keys()]
            raise ValueError(
                f"No default config for {language}/{test_framework}. " f"Available: {', '.join(available)}"
            )

        defaults = DEFAULT_VALIDATION_CONFIGS[key]
        return cls(
            language=language,
            test_framework=test_framework,
            env_check_cmd=defaults.get("env_check_cmd", ""),
            package_check_template=defaults.get("package_check_template"),
            collect_cmd=defaults.get("collect_cmd", ""),
            collect_pattern=defaults.get("collect_pattern"),
            test_file_pattern=defaults.get("test_file_pattern", "test_*.py"),
            modes=[
                BaseValidationMode(
                    name="default", test_cmd=defaults.get("run_cmd", ""), description=f"Default {test_framework} tests"
                )
            ],
        )

    def get_run_cmd(self, mode_name: str = "default") -> str:
        """Get run command for a specific mode."""
        for mode in self.modes:
            if mode.name == mode_name:
                return mode.test_cmd
        # Fall back to first mode or empty
        if self.modes:
            return self.modes[0].test_cmd
        return ""


@dataclass
class TestMode:
    """
    A test mode defines how to run tests.

    Attributes:
        name: Mode name (e.g., "default", "integration")
        test_states: States to run this mode on (e.g., ["test", "fix"])
        test_cmd: Complete test command with template variables
        description: Optional description of this mode
    """

    name: str
    test_states: List[str] = field(default_factory=lambda: ["test", "fix"])
    test_cmd: str = ""
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "test_states": self.test_states,
            "test_cmd": self.test_cmd,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TestMode":
        return cls(
            name=data["name"],
            test_states=data.get("test_states", ["test", "fix"]),
            test_cmd=data.get("test_cmd", ""),
            description=data.get("description", ""),
        )


@dataclass
class CommitTestConfig:
    """
    Configuration for commit-level tests.

    The config file format is a JSON array of TestMode objects:
    [
      {
        "name": "default",
        "test_states": ["test", "fix"],
        "test_cmd": "pytest -n {workers} --json-report --json-report-file=/output/{output_file}",
        "description": "Normal tests"
      },
      {
        "name": "integration",
        "test_states": ["fix"],
        "test_cmd": "pytest -n {workers} --json-report --json-report-file=/output/{output_file} --integration",
        "description": "Integration tests"
      }
    ]

    Template variables available in test_cmd:
        {workers} - Number of parallel workers
        {timeout} - Timeout in seconds
        {output_file} - Output file name for this test run
    """

    modes: List[TestMode] = field(default_factory=list)

    def to_list(self) -> List[Dict[str, Any]]:
        """Convert to list format for JSON serialization."""
        return [m.to_dict() for m in self.modes]

    def to_json(self, path: Path):
        """Save configuration to JSON file."""
        with open(path, "w") as f:
            json.dump(self.to_list(), f, indent=2)

    @classmethod
    def from_list(cls, data: List[Dict[str, Any]]) -> "CommitTestConfig":
        """Create from list of mode dicts."""
        modes = [TestMode.from_dict(m) for m in data]
        return cls(modes=modes)

    @classmethod
    def from_file(cls, path: Path) -> "CommitTestConfig":
        """Load configuration from JSON file."""
        with open(path) as f:
            data = json.load(f)
        return cls.from_list(data)

    @classmethod
    def default(cls) -> "CommitTestConfig":
        """Get default configuration with a single default mode."""
        return cls(
            modes=[
                TestMode(
                    name="default",
                    test_states=["test", "fix"],
                    test_cmd="",  # Empty means use default pytest command
                    description="Default test mode",
                )
            ]
        )

    def get_mode_by_name(self, name: str) -> Optional[TestMode]:
        """Get mode by name."""
        for mode in self.modes:
            if mode.name == name:
                return mode
        return None

    def get_all_state_mode_pairs(self) -> List[tuple]:
        """
        Get all (state, mode) pairs to run.

        Returns:
            List of (state_name, mode) tuples
            e.g., [("test", mode1), ("fix", mode1), ("fix", mode2)]
        """
        pairs = []
        for mode in self.modes:
            for state in mode.test_states:
                pairs.append((state, mode))
        return pairs

    def get_classification_pairs(self) -> List[tuple]:
        """
        Get pairs of output files to classify.

        For each mode that runs on both "test" and "fix" states,
        generate a classification pair.

        Returns:
            List of (before_key, after_key) tuples
            e.g., [("test_default", "fix_default")]
        """
        pairs = []
        for mode in self.modes:
            if "test" in mode.test_states and "fix" in mode.test_states:
                before_key = f"test_{mode.name}"
                after_key = f"fix_{mode.name}"
                pairs.append((before_key, after_key))
        return pairs


# =============================================================================
# Milestone-level Test Configuration
# =============================================================================


@dataclass
class MilestoneTestMode:
    """
    A test mode for milestone-level tests.

    Attributes:
        name: Mode name (e.g., "default", "integration")
        test_states: States to run this mode on (e.g., ["start", "end"])
        test_cmd: Complete test command with template variables
        description: Optional description of this mode
        framework: Test framework for parsing results (e.g., "pytest", "ginkgo", "vitest")
                   If not specified, uses the global framework setting.
        requires_docker_socket: Whether this mode needs Docker socket mounted
                                (for testcontainers/e2e tests that spawn containers)
        run_timeout_seconds: Overall wall-clock budget for one run of this mode
                             (None = runner default). E2e modes that spin real
                             containers per test can legitimately exceed the
                             default; a too-small budget kills the run mid-way
                             and truncates the report.
    """

    name: str
    test_states: List[str] = field(default_factory=lambda: ["start", "end"])
    test_cmd: str = ""
    description: str = ""
    framework: Optional[str] = None  # Override global framework for this mode
    requires_docker_socket: bool = False  # For e2e tests using testcontainers
    run_timeout_seconds: Optional[int] = None  # Overall per-run budget override

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "name": self.name,
            "test_states": self.test_states,
            "test_cmd": self.test_cmd,
            "description": self.description,
        }
        if self.framework:
            result["framework"] = self.framework
        if self.requires_docker_socket:
            result["requires_docker_socket"] = self.requires_docker_socket
        if self.run_timeout_seconds is not None:
            result["run_timeout_seconds"] = self.run_timeout_seconds
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MilestoneTestMode":
        # Handle legacy format: pytest_args -> test_cmd
        test_cmd = data.get("test_cmd", "")
        if not test_cmd and "pytest_args" in data:
            # Legacy format: convert pytest_args to test_cmd
            pytest_args = data.get("pytest_args", "")
            test_cmd = pytest_args  # Will be combined with default command later

        return cls(
            name=data["name"],
            test_states=data.get("test_states", ["start", "end"]),
            test_cmd=test_cmd,
            description=data.get("description", ""),
            framework=data.get("framework"),  # Optional framework override
            requires_docker_socket=data.get("requires_docker_socket", False),
            run_timeout_seconds=data.get("run_timeout_seconds"),
        )


@dataclass
class MilestoneTestConfig:
    """
    Configuration for milestone-level tests.

    The config file format is a JSON array of MilestoneTestMode objects:
    [
      {
        "name": "default",
        "test_states": ["start", "end"],
        "test_cmd": "pytest -n {workers} --json-report --json-report-file=/output/{output_file}",
        "description": "Normal tests"
      },
      {
        "name": "integration",
        "test_states": ["end"],
        "test_cmd": "pytest -n {workers} --json-report --json-report-file=/output/{output_file} --integration",
        "description": "Integration tests (only at end state)"
      }
    ]

    Template variables available in test_cmd:
        {workers} - Number of parallel workers
        {timeout} - Timeout in seconds
        {output_file} - Output file name for this test run
        {milestone_id} - Milestone ID (e.g., M001)
    """

    modes: List[MilestoneTestMode] = field(default_factory=list)
    include_original: bool = False  # Whether to test at base_commit (original state)

    def to_list(self) -> List[Dict[str, Any]]:
        """Convert to list format for JSON serialization."""
        return [m.to_dict() for m in self.modes]

    def to_json(self, path: Path):
        """Save configuration to JSON file."""
        with open(path, "w") as f:
            json.dump(self.to_list(), f, indent=2)

    @classmethod
    def from_list(cls, data: List[Dict[str, Any]], include_original: bool = False) -> "MilestoneTestConfig":
        """Create from list of mode dicts."""
        modes = [MilestoneTestMode.from_dict(m) for m in data]
        return cls(modes=modes, include_original=include_original)

    @classmethod
    def from_file(cls, path: Path, include_original: bool = False) -> "MilestoneTestConfig":
        """Load configuration from JSON file."""
        with open(path) as f:
            data = json.load(f)

        # Handle both list format (new) and dict format (legacy)
        if isinstance(data, list):
            return cls.from_list(data, include_original)
        elif isinstance(data, dict):
            # Legacy format: convert to new format
            pytest_args = data.get("pytest_extra_args", "")
            modes = [
                MilestoneTestMode(
                    name="default",
                    test_states=["start", "end"],
                    test_cmd=pytest_args,  # Will be formatted later
                    description="Default tests (converted from legacy format)",
                )
            ]
            return cls(modes=modes, include_original=include_original)
        else:
            raise ValueError(f"Invalid config format in {path}")

    @classmethod
    def default(cls, include_original: bool = False) -> "MilestoneTestConfig":
        """Get default configuration with a single default mode."""
        return cls(
            modes=[
                MilestoneTestMode(
                    name="default",
                    test_states=["start", "end"],
                    test_cmd="",  # Empty means use default pytest command
                    description="Default test mode",
                )
            ],
            include_original=include_original,
        )

    def get_mode_by_name(self, name: str) -> Optional[MilestoneTestMode]:
        """Get mode by name."""
        for mode in self.modes:
            if mode.name == name:
                return mode
        return None

    def get_all_state_mode_pairs(self) -> List[tuple]:
        """
        Get all (state, mode) pairs to run.

        Returns:
            List of (state_name, mode) tuples
            e.g., [("start", mode1), ("end", mode1), ("end", mode2)]
        """
        pairs = []
        for mode in self.modes:
            for state in mode.test_states:
                pairs.append((state, mode))
        return pairs

    def get_all_states(self) -> List[str]:
        """
        Get all unique states from all modes.

        Returns:
            List of unique state names (e.g., ["start", "end"])
        """
        states = set()
        for mode in self.modes:
            states.update(mode.test_states)
        # Add "original" if include_original is True
        if self.include_original:
            states.add("original")
        return sorted(states)

    def get_classification_pairs(self) -> List[tuple]:
        """
        Get pairs of states to classify.

        For milestone tests, the default classification is start -> end.

        Returns:
            List of (before_state, after_state) tuples
        """
        # Default: compare start to end
        return [("start", "end")]

    def requires_docker_socket_any(self) -> bool:
        """
        Check if any mode requires Docker socket mounting.

        This is used for testcontainers/e2e tests that need to spawn
        Docker containers from within the test container.

        Returns:
            True if any mode has requires_docker_socket=True
        """
        return any(mode.requires_docker_socket for mode in self.modes)
