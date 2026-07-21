"""OpenAI Codex agent framework implementation."""

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional

from harness.e2e.agents.base import (
    AgentFramework,
    register_framework,
    validate_agent_cli_version,
)

logger = logging.getLogger(__name__)


@register_framework("codex")
class CodexFramework(AgentFramework):
    """Agent framework implementation for OpenAI Codex CLI.

    Codex CLI is OpenAI's coding agent that runs in the terminal.
    https://github.com/openai/codex

    This implementation supports two auth modes:
    1. API mode (preferred): UNIFIED_API_KEY/UNIFIED_BASE_URL
    2. OAuth file mode: host ~/.codex/auth.json mounted into container

    Environment variables:
        UNIFIED_API_KEY: API key for the unified proxy
        UNIFIED_BASE_URL: Base URL for the unified proxy
    """

    FRAMEWORK_NAME = "codex"

    # Default model for Codex
    DEFAULT_MODEL = "gpt-5.2-codex"

    # Valid reasoning effort levels. gpt-5.6-sol (and later) add "max"
    # (supported_reasoning_levels in the OAuth models cache); older codex
    # models top out at xhigh. Passing an effort the model doesn't support is
    # rejected by the CLI, so only set it for models that advertise it.
    VALID_REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")

    # Models that need litellm's /openai_passthrough endpoint because
    # litellm's native /v1/responses reconstructs the SSE stream and drops
    # events (response.output_item.added, response.content_part.added),
    # causing Codex CLI to fail with "OutputTextDelta without active item".
    PASSTHROUGH_MODELS: set[str] = set()

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        include_directories: Optional[List[str]] = None,
        agent_version: Optional[str] = None,
        **kwargs,
    ):
        """Initialize Codex framework.

        Args:
            api_key: API key. If not provided, uses UNIFIED_API_KEY env var.
            base_url: Base URL for API. If not provided, uses UNIFIED_BASE_URL env var.
            reasoning_effort: Reasoning effort level ("low", "medium", "high").
                             Controls how much the model "thinks" before responding.
                             Passed to Codex CLI via model_reasoning_effort.
            include_directories: Extra directories to pass to codex (currently unused,
                                 accepted for interface compatibility with other frameworks).
            agent_version: Exact Codex CLI version to pin, or ``latest``.
        """
        self.api_key = api_key or os.environ.get("UNIFIED_API_KEY")
        self.base_url = base_url or os.environ.get("UNIFIED_BASE_URL")
        self.reasoning_effort = reasoning_effort or "xhigh"
        self._agent_version = validate_agent_cli_version(agent_version, agent_label="Codex")
        self._codex_auth_file = Path.home() / ".codex" / "auth.json"
        self._codex_config_file = Path.home() / ".codex" / "config.toml"

    def get_requested_version(self) -> Optional[str]:
        return self._agent_version

    def get_version_command(self) -> List[str]:
        return ["codex", "--version"]

    def parse_version_output(self, output: str) -> Optional[str]:
        match = re.search(r"\b(\d+\.\d+\.\d+)\b", output or "")
        return match.group(1) if match else None

    def version_matches_request(self, actual_version: str) -> bool:
        if self._agent_version in (None, "latest"):
            return True
        return actual_version == self._agent_version

    def get_effective_reasoning_effort(self) -> Optional[str]:
        """Return effective reasoning effort (default: xhigh)."""
        return self.reasoning_effort

    def _build_reasoning_effort_args(self) -> List[str]:
        """Return Codex CLI overrides for reasoning effort and safety knobs.

        Codex CLI reads model reasoning strength from `model_reasoning_effort`.
        Using `reasoning_effort` here does not reliably override config.toml in
        the containerized harness environment.

        Also forces `web_search="disabled"`: the Responses-API web_search tool
        runs server-side at the LLM provider and bypasses container iptables,
        which lets agents fetch upstream code from github/etc and invalidates
        benchmark results. Kept strictly off at CLI layer as a belt-and-braces
        backup to the in-container config.toml.
        """
        args: List[str] = ["-c", 'web_search="disabled"']
        if self.reasoning_effort and self.reasoning_effort in self.VALID_REASONING_EFFORTS:
            args.extend(["-c", f'model_reasoning_effort="{self.reasoning_effort}"'])
        return args

    def _resolve_model(self, model: str) -> str:
        """Resolve model name."""
        return model if model else self.DEFAULT_MODEL

    def _passthrough_base_url(self, model: str) -> str | None:
        """Return the passthrough base URL if model needs it, else None.

        For models in PASSTHROUGH_MODELS when using a litellm proxy
        (self.base_url is set), returns the /openai_passthrough/v1 URL
        that forwards requests directly to OpenAI without modifying
        the SSE stream.
        """
        if self.base_url and model in self.PASSTHROUGH_MODELS:
            return self.base_url.rstrip("/") + "/openai_passthrough/v1"
        return None

    def get_container_mounts(self) -> List[str]:
        """Return Docker volume mount arguments for Codex.

        Auth mode priority:
        1. API key mode when UNIFIED_API_KEY is set
        2. OAuth file mode when ~/.codex/auth.json exists

        Returns:
            List of -v arguments for docker run
        """
        mounts: List[str] = []

        if not self.api_key:
            # Minimal OAuth credential mount (API mode passes the key via env).
            if self._codex_auth_file.exists():
                mounts.extend(["-v", f"{self._codex_auth_file}:/tmp/host-codex/auth.json:ro"])
                if self._codex_config_file.exists():
                    mounts.extend(["-v", f"{self._codex_config_file}:/tmp/host-codex/config.toml:ro"])
            else:
                logger.warning("No API key and no ~/.codex/auth.json found - Codex authentication may fail")

        # Quarantine mode: offline pip wheelhouse (shared base helper).
        mounts.extend(self.get_quarantine_mounts())
        return mounts

    def get_container_env_vars(self) -> List[str]:
        """Return Docker environment variable arguments.

        Maps UNIFIED_* env vars to Codex-specific env vars:
        - CODEX_API_KEY: Required for `codex exec` non-interactive mode
        - OPENAI_BASE_URL: For custom API endpoints/proxies

        Returns:
            List of -e arguments for docker run
        """
        env_vars = []
        if self.api_key:
            # CODEX_API_KEY is required for `codex exec` mode
            env_vars.extend(["-e", f"CODEX_API_KEY={self.api_key}"])
        if self.base_url:
            env_vars.extend(["-e", f"OPENAI_BASE_URL={self.base_url}"])
        # Quarantine mode: force pip offline to the wheelhouse (shared base helper).
        env_vars.extend(self.get_quarantine_env_vars())
        return env_vars

    def get_container_init_script(self, agent_name: str) -> str:
        """Return Python init script for Codex setup.

        The script:
        1. Installs Codex CLI via npm (if not present)
        2. Copies OAuth credentials from mounted host files (when available)
        3. Verifies installation

        Args:
            agent_name: Git user name for agent commits

        Returns:
            Python script as a string
        """
        script = """
# === Codex: Install Codex CLI ===
try:
    import subprocess
    import shutil

    def run_cmd(cmd, shell=False):
        try:
            result = subprocess.run(
                cmd,
                shell=shell,
                capture_output=True,
                text=True,
                timeout=300
            )
            return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
        except Exception as e:
            return False, '', str(e)

    # Check current Node.js version
    success, node_version, _ = run_cmd(['node', '--version'])
    if success:
        print(f"Current Node.js version: {node_version}")
        try:
            major = int(node_version.lstrip('v').split('.')[0])
            need_upgrade = major < 20
        except:
            need_upgrade = True
    else:
        print("Node.js not found")
        need_upgrade = True

    # Install Node.js 20 if needed
    if need_upgrade:
        print("Installing Node.js 20 via NodeSource...")

        # Ensure curl is available
        if not shutil.which('curl'):
            print("Installing curl...")
            run_cmd(['apt-get', 'update'])
            run_cmd(['apt-get', 'install', '-y', 'curl', 'ca-certificates'])

        # Add NodeSource repository and install Node.js 20
        success, stdout, stderr = run_cmd(
            'curl -fsSL https://deb.nodesource.com/setup_20.x | bash -',
            shell=True
        )
        if not success:
            print(f"Warning: NodeSource setup output: {stderr}")

        success, stdout, stderr = run_cmd(['apt-get', 'install', '-y', 'nodejs'])
        if success:
            success, node_version, _ = run_cmd(['node', '--version'])
            print(f"Node.js installed: {node_version}")
        else:
            print(f"Failed to install Node.js: {stderr}")
            raise Exception("Node.js installation failed")

    # Check if codex is already installed. An exact requested version can be
    # reused only when it matches; 'latest' always re-resolves via npm.
    import re as _re
    requested_version = __CODEX_AGENT_VERSION__
    result = subprocess.run(['which', 'codex'], capture_output=True, text=True)
    needs_install = result.returncode != 0
    if requested_version == 'latest':
        needs_install = True
    elif requested_version and not needs_install:
        ok, ver_out, _ = run_cmd(['codex', '--version'])
        m = _re.search(r'\\b(\\d+\\.\\d+\\.\\d+)\\b', ver_out) if ok else None
        if not m or m.group(1) != requested_version:
            needs_install = True

    if not needs_install:
        print(f"Codex already installed at: {result.stdout.strip()}")
    else:
        pkg = '@openai/codex' + (f'@{requested_version}' if requested_version else '')
        print(f"Installing Codex CLI via npm ({pkg})...")
        install_result = subprocess.run(
            ['npm', 'i', '-g', pkg],
            capture_output=True,
            text=True
        )
        if install_result.returncode == 0:
            print("Codex CLI installed successfully")
        else:
            print(f"Failed to install Codex: {install_result.stderr}")

    # Verify installation
    version_result = subprocess.run(['codex', '--version'], capture_output=True, text=True)
    if version_result.returncode == 0:
        print(f"Codex version: {version_result.stdout.strip()}")
    else:
        print("Warning: Could not verify Codex installation")

except Exception as e:
    print(f"Error setting up Codex: {e}")

# === Codex: Setup OAuth credentials ===
try:
    import os
    import pwd
    import shutil
    from pathlib import Path

    codex_dir = Path('/home/fakeroot/.codex')
    codex_dir.mkdir(parents=True, exist_ok=True)

    fake_user = pwd.getpwnam('fakeroot')
    uid, gid = fake_user.pw_uid, fake_user.pw_gid
    os.chown(codex_dir, uid, gid)
    os.chmod(codex_dir, 0o700)

    auth_src = Path('/tmp/host-codex/auth.json')
    auth_dst = codex_dir / 'auth.json'
    if auth_src.exists():
        shutil.copy2(auth_src, auth_dst)
        os.chown(auth_dst, uid, gid)
        os.chmod(auth_dst, 0o600)
        print(f"Copied Codex auth file to {auth_dst}")

    # Generate a clean config.toml with only the settings we need.
    # Do NOT copy the host config.toml — it may contain wrong model defaults,
    # MCP tokens, and other settings that interfere with the evaluation.
    config_dst = codex_dir / 'config.toml'
    base_url = os.environ.get('OPENAI_BASE_URL', '')
    # Always disable the Responses-API `web_search` tool: it runs server-side
    # at the LLM provider and bypasses the container iptables whitelist,
    # letting the agent fetch upstream code from github/etc even when the
    # repo's own network path is blocked. Kept strictly off for benchmark
    # integrity (valid values: disabled, cached, live).
    config_lines = ['web_search = "disabled"']
    if base_url:
        config_lines.append(f'openai_base_url = "{base_url}"')
    with open(config_dst, 'w') as f:
        f.write('\\n'.join(config_lines) + '\\n')
    os.chown(config_dst, uid, gid)
    os.chmod(config_dst, 0o600)
    print(f"Generated clean config.toml (openai_base_url={base_url or 'default'})")

except Exception as e:
    print(f"Error setting up Codex OAuth files: {e}")
"""
        return script.replace(
            "__CODEX_AGENT_VERSION__", repr(self._agent_version)
        )

    def build_run_command(
        self,
        model: str,
        session_id: str,
        prompt_path: str,
    ) -> str:
        """Build the Codex CLI command for running the agent.

        Uses `codex exec` for non-interactive execution.
        Note: Codex generates its own thread_id, the session_id param is ignored.
        The actual thread_id must be extracted from stdout JSON output.

        Args:
            model: Model identifier (e.g., "gpt-5.2-codex")
            session_id: Ignored - Codex generates its own thread_id
            prompt_path: Path to prompt file inside container

        Returns:
            Shell command string
        """
        actual_model = self._resolve_model(model)

        cmd_parts = [
            "codex",
            "exec",
            "--model",
            actual_model,
            "--json",  # JSON output for parsing (includes thread_id)
            "--dangerously-bypass-approvals-and-sandbox",  # Bypass all restrictions
        ]

        cmd_parts.extend(self._build_reasoning_effort_args())

        # Add prompt from file
        cmd_parts.append(f'"$(cat {prompt_path})"')

        cmd = " ".join(cmd_parts)

        # For passthrough models, override OPENAI_BASE_URL inline
        pt_url = self._passthrough_base_url(actual_model)
        if pt_url:
            cmd = f"OPENAI_BASE_URL={pt_url} {cmd}"

        return cmd

    def build_resume_command(
        self,
        model: str,
        session_id: str,
        message_path: str,
    ) -> str:
        """Build the Codex CLI command for resuming a session.

        Uses `codex exec resume <thread_id>` for session resumption.
        The session_id must be the thread_id from a previous Codex run.

        Args:
            model: Model identifier
            session_id: Codex thread_id from previous run (from stdout JSON)
            message_path: Path to message file inside container

        Returns:
            Shell command string
        """
        actual_model = self._resolve_model(model)

        cmd_parts = [
            "codex",
            "exec",
            "resume",
            session_id,  # This must be the thread_id from Codex output
            "--model",
            actual_model,
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",  # Bypass all restrictions
        ]

        cmd_parts.extend(self._build_reasoning_effort_args())

        # Add message from file
        cmd_parts.append(f'"$(cat {message_path})"')

        cmd = " ".join(cmd_parts)

        # For passthrough models, override OPENAI_BASE_URL inline
        pt_url = self._passthrough_base_url(actual_model)
        if pt_url:
            cmd = f"OPENAI_BASE_URL={pt_url} {cmd}"

        return cmd

    def extract_session_id_from_container(self, container_name: str) -> Optional[str]:
        """Extract the latest thread_id from Codex rollout files inside the container.

        Codex stores session files at:
          ~/.codex/sessions/{year}/{month}/{day}/rollout-{timestamp}-{thread_id}.jsonl

        We find the latest rollout file (by sorted filename) and extract the
        thread_id from the filename.

        Args:
            container_name: Name of the Docker container

        Returns:
            thread_id from the latest rollout file, or None
        """
        sessions_dir = "/home/fakeroot/.codex/sessions"
        try:
            result = subprocess.run(
                ["docker", "exec", container_name, "find", sessions_dir, "-name", "*.jsonl", "-type", "f"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None

            files = sorted(result.stdout.strip().split("\n"))
            if not files:
                return None

            # Latest file is last after sorting (paths contain date components + timestamp)
            latest_file = files[-1]
            filename = Path(latest_file).name

            # Filename format: rollout-{timestamp}-{thread_id}.jsonl
            # timestamp also contains hyphens, so match the UUID at the end
            match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", filename)
            if match:
                return match.group(1)
        except (subprocess.TimeoutExpired, Exception) as e:
            logger.warning(f"Failed to extract session from Codex container: {e}")

        return None

    @staticmethod
    def extract_thread_id(stdout_content: str) -> Optional[str]:
        """Extract thread_id from Codex JSON output.

        Codex outputs: {"type":"thread.started","thread_id":"xxx"}

        Returns the LAST thread_id found, since stdout is append-only across
        resumes and earlier entries may be stale.

        Args:
            stdout_content: Content of agent_stdout.txt

        Returns:
            thread_id if found, None otherwise
        """
        import json

        latest_thread_id = None
        for line in stdout_content.strip().split("\n"):
            if not line:
                continue
            try:
                event = json.loads(line)
                if event.get("type") == "thread.started":
                    thread_id = event.get("thread_id")
                    if thread_id:
                        latest_thread_id = thread_id
            except json.JSONDecodeError:
                continue

        return latest_thread_id
