"""Abstract base class for agent frameworks."""

import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional, Type

from harness.e2e.quarantine import GO_OFFLINE_FILE_PROXY, GO_OFFLINE_SHELL_ENV

_AGENT_CLI_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def validate_agent_cli_version(value: Optional[str], *, agent_label: str) -> Optional[str]:
    """Validate a trial-config ``agent_version`` selector for npm-installed CLIs.

    Accepts an exact semantic version (a reproducibility pin) or ``latest``
    (explicitly re-resolve the newest release at container setup time).
    """
    if value is None:
        return None
    value = str(value).strip()
    if value == "latest" or _AGENT_CLI_VERSION_RE.fullmatch(value):
        return value
    raise ValueError(
        f"{agent_label} agent_version must be a semantic version such as "
        "'1.2.3', or 'latest'"
    )


class AgentFramework(ABC):
    """Abstract base class for agent framework implementations.

    Each agent framework (e.g., Claude Code, OpenHands) should implement this
    interface to provide agent-specific configuration for:
    - Container mounts (credentials, binaries)
    - Container initialization scripts
    - Command building for run/resume operations
    """

    FRAMEWORK_NAME: str = "unknown"

    def __init__(self, **kwargs):
        """Initialize the framework.

        Args:
            **kwargs: Framework-specific options (e.g., reasoning_effort for Codex).
                      Subclasses should override to handle their specific options.
        """
        # Base class ignores unknown kwargs for forward compatibility
        pass

    @abstractmethod
    def get_container_mounts(self) -> List[str]:
        """Return Docker volume mount arguments for the agent.

        Returns:
            List of -v arguments for docker run (e.g., ["-v", "/src:/dst:ro"])
        """

    @abstractmethod
    def get_container_init_script(self, agent_name: str) -> str:
        """Return Python script for container initialization.

        This script runs as root inside the container after launch to set up
        agent-specific directories, credentials, and tools.

        Args:
            agent_name: Git user name for agent commits

        Returns:
            Python script as a string
        """

    @abstractmethod
    def build_run_command(
        self,
        model: str,
        session_id: str,
        prompt_path: str,
    ) -> str:
        """Build the shell command to run the agent.

        Args:
            model: Model identifier (e.g., "claude-sonnet-4-5-20250929")
            session_id: Session ID for conversation tracking
            prompt_path: Path to prompt file inside container

        Returns:
            Shell command string to execute
        """

    @abstractmethod
    def build_resume_command(
        self,
        model: str,
        session_id: str,
        message_path: str,
    ) -> str:
        """Build the shell command to resume an existing session.

        Args:
            model: Model identifier
            session_id: Session ID to resume
            message_path: Path to message file inside container

        Returns:
            Shell command string to execute
        """

    def get_container_env_vars(self) -> List[str]:
        """Return Docker environment variable arguments.

        Override this method to pass environment variables to the container.

        Returns:
            List of -e arguments for docker run (e.g., ["-e", "KEY=value"])
        """
        return []

    def get_network_endpoint_url(self) -> Optional[str]:
        """Return the LLM endpoint used to plan quarantine networking.

        Most frameworks use the unified proxy directly. Frameworks with a
        first-party OAuth mode can override this when no explicit base URL is
        configured.
        """
        return os.environ.get("UNIFIED_BASE_URL")

    @staticmethod
    def _validate_docker_env_args(args: List[str], *, source: str) -> List[str]:
        """Validate the ``-e KEY=value`` representation used by Docker."""
        if len(args) % 2:
            raise ValueError(f"{source} returned an odd number of Docker env arguments")
        values: List[str] = []
        for index in range(0, len(args), 2):
            flag, value = args[index:index + 2]
            if flag != "-e" or not isinstance(value, str):
                raise ValueError(
                    f"{source} returned malformed Docker env arguments at index {index}"
                )
            key = value.split("=", 1)[0]
            if not key:
                raise ValueError(f"{source} returned an empty Docker environment key")
            values.append(value)
        return values

    def get_effective_container_env_vars(self) -> List[str]:
        """Return framework env with the shared quarantine contract authoritative.

        Existing adapters historically appended ``get_quarantine_env_vars``
        themselves.  Core launch paths call this method instead: active shared
        keys are removed from the adapter result and appended exactly once.
        Consequently a future adapter cannot accidentally omit or override the
        hermetic Go/Maven/etc. environment merely by forgetting that convention.
        """
        framework_values = self._validate_docker_env_args(
            self.get_container_env_vars(),
            source=f"{self.FRAMEWORK_NAME}.get_container_env_vars",
        )
        quarantine_values = self._validate_docker_env_args(
            self.get_quarantine_env_vars(),
            source="get_quarantine_env_vars",
        )
        managed = {value.split("=", 1)[0] for value in quarantine_values}
        effective = [
            value
            for value in framework_values
            if value.split("=", 1)[0] not in managed
        ]
        effective.extend(quarantine_values)
        return [item for value in effective for item in ("-e", value)]

    def get_quarantine_mounts(self) -> List[str]:
        """Quarantine: return extra Docker volume mounts for offline operation.

        The pip dependency closure is baked into the repo's base-offline image
        under /wheelhouse by the closure builder — no host mount is needed or
        performed.  Other ecosystems (cargo, go, maven, npm) also rely solely
        on caches pre-baked into the image.  This method is kept for interface
        compatibility and as an extension point for future mounts.
        See docs/quarantine.md.
        """
        return []

    def get_quarantine_env_vars(self) -> List[str]:
        """Quarantine: force the repo's package manager(s) offline.

        Belt to the SWE_MILESTONE_DENY_* firewall suspenders, shared across agents.
        pip reads the in-image /wheelhouse (baked by the closure builder) when
        SWE_MILESTONE_PIP_OFFLINE is set; cargo/go/maven/npm run offline against
        their own image-baked caches. The local-only Go contract is also sealed
        in a root-owned BASH_ENV and written into the container profiles, since
        login shells can override a bare docker ``-e``. See docs/quarantine.md.
        """
        env: List[str] = []
        if os.environ.get("SWE_MILESTONE_PIP_OFFLINE"):
            env += ["-e", "PIP_NO_INDEX=1", "-e", "PIP_FIND_LINKS=/wheelhouse"]
        if os.environ.get("SWE_MILESTONE_CARGO_OFFLINE"):
            env += ["-e", "CARGO_NET_OFFLINE=true"]
        if os.environ.get("SWE_MILESTONE_GO_OFFLINE"):
            expected_go = os.environ.get("SWE_MILESTONE_GO_TOOLCHAIN", "").removeprefix("go")
            go_path = (
                "/home/fakeroot/go/bin:/usr/local/go/bin:/go/bin:"
                "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            )
            env += [
                "-e", f"GOPROXY={GO_OFFLINE_FILE_PROXY}",
                "-e", "GONOPROXY=none",
                "-e", "GOSUMDB=off",
                "-e", "GOTOOLCHAIN=local",
                "-e", "GOFLAGS=-buildvcs=false",
                "-e", "GOENV=/home/fakeroot/.cache/evoclaw-goenv/env",
                "-e", f"BASH_ENV={GO_OFFLINE_SHELL_ENV}",
                "-e", "GOMODCACHE=/home/fakeroot/.cache/evoclaw-gomodcache",
                "-e", "GOCACHE=/home/fakeroot/.cache/go-build",
                "-e", "GOBIN=/home/fakeroot/go/bin",
                "-e", f"PATH={go_path}",
            ]
            if expected_go:
                env += ["-e", f"GOLANG_VERSION={expected_go}"]
        if os.environ.get("SWE_MILESTONE_MAVEN_OFFLINE"):
            margs = "-o"
            repo_local = os.environ.get("SWE_MILESTONE_MAVEN_REPO_LOCAL")
            if repo_local:
                # The image's populated cache lives under root's home; the
                # agent runs as fakeroot, whose own ~/.m2 starts empty.
                margs += f" -Dmaven.repo.local={repo_local}"
            env += ["-e", f"MAVEN_ARGS={margs}"]
        if os.environ.get("SWE_MILESTONE_NPM_OFFLINE"):
            env += ["-e", "npm_config_offline=true"]
        return env

    def get_effective_reasoning_effort(self) -> Optional[str]:
        """Return the reasoning effort level actually used by the agent.

        Returns the effective value after applying agent-specific defaults.
        Returns None if the agent does not support reasoning effort.

        Override in subclasses that support reasoning effort.
        """
        return None

    def get_requested_version(self) -> Optional[str]:
        """Return the requested agent CLI version, if this framework supports pinning."""
        return None

    def get_version_command(self) -> Optional[List[str]]:
        """Return the in-container command used to report the agent CLI version."""
        return None

    def parse_version_output(self, output: str) -> Optional[str]:
        """Extract a normalized version from the agent CLI's version output."""
        value = output.strip()
        return value or None

    def version_matches_request(self, actual_version: str) -> bool:
        """Return whether an observed version satisfies the requested version.

        Frameworks with version channels or other non-exact selectors should
        override this method. The default only accepts an exact match.
        """
        requested = self.get_requested_version()
        return requested is None or actual_version == requested

    def extract_session_id_from_container(self, container_name: str) -> Optional[str]:
        """Extract the latest session ID directly from agent files inside the container.

        Override in subclasses that store session files (e.g., Codex rollout files,
        Gemini session files). Returns None by default (falls back to stdout parsing).
        """
        return None


# Registry of available agent frameworks
_FRAMEWORK_REGISTRY: Dict[str, Type[AgentFramework]] = {}


def register_framework(name: str):
    """Decorator to register an agent framework class.

    Args:
        name: Framework name for registration (e.g., "claude-code")

    Returns:
        Class decorator
    """

    def decorator(cls: Type[AgentFramework]) -> Type[AgentFramework]:
        _FRAMEWORK_REGISTRY[name] = cls
        return cls

    return decorator


def get_agent_framework(name: str, **kwargs) -> AgentFramework:
    """Factory function to get an agent framework instance.

    Args:
        name: Framework name (e.g., "claude-code")
        **kwargs: Additional arguments passed to the framework constructor.
                  For Codex: reasoning_effort ("low", "medium", "high")

    Returns:
        AgentFramework instance

    Raises:
        ValueError: If framework is not supported
    """
    # Import implementations to trigger registration
    from harness.e2e.agents import claude_code  # noqa: F401
    from harness.e2e.agents import codex  # noqa: F401
    from harness.e2e.agents import gemini  # noqa: F401
    from harness.e2e.agents import openhands  # noqa: F401

    if name not in _FRAMEWORK_REGISTRY:
        available = ", ".join(_FRAMEWORK_REGISTRY.keys()) or "none"
        raise ValueError(f"Unknown agent framework: {name}. Available: {available}")

    return _FRAMEWORK_REGISTRY[name](**kwargs)
