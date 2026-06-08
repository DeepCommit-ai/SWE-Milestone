"""Container setup utilities for agent execution.

This module provides shared container initialization logic used by both
run_milestone.py (single milestone mode) and orchestrator.py (E2E mode).
"""

import logging
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

from harness.e2e.agents import AgentFramework, get_agent_framework

logger = logging.getLogger("e2e.container_setup")

# Whitelist of domains the agent container is allowed to reach.
# Based on Codex Cloud "Common dependencies" preset, with all code hosting
# sites (github.com, gitlab.com, etc.) deliberately removed.
WHITELISTED_DOMAINS = [
    # === LLM API endpoints ===
    "llm-proxy.eval.all-hands.dev",
    "api.anthropic.com",
    "statsig.anthropic.com",
    "claude.ai",
    "sentry.io",
    "api.openai.com",
    "generativelanguage.googleapis.com",
    # Vertex AI direct (gemini-cli + claude-code native Vertex): the aiplatform
    # endpoint + OAuth token refresh for ADC. Only reachable when EVOCLAW_VERTEX
    # puts ADC in-container.
    "aiplatform.googleapis.com",
    "oauth2.googleapis.com",
    "open.bigmodel.cn",
    "api.kimi.com",
    "api.moonshot.ai",
    "api.fireworks.ai",
    # === Host services: EvoHarness Gitea coordination substrate (multi-role harness) ===
    # Resolves via --add-host=host.docker.internal:host-gateway to the host gateway IP, so the
    # in-container multi-role agents can push/pull/PR against the host Gitea. NOTE: this whitelists
    # the host gateway IP (no per-port iptables here), so it opens the host generally — acceptable
    # for the benchmark: code hosting (github/gitlab) + the repo's own source registry stay blocked.
    "host.docker.internal",
    # === Go module proxy (replaces direct github.com) ===
    "proxy.golang.org",
    "sum.golang.org",
    # NOTE: storage.googleapis.com / dl.google.com intentionally NOT whitelisted.
    # They are arbitrary public-object hosts (any bucket / GCS-hosted pip index)
    # usable as a generic "fetch the answer" channel, and are not needed by the
    # agent at runtime. (Inherited from the AgentBench sync; removed here.)
    "golang.org",
    "pkg.go.dev",
    "goproxy.io",
    "goproxy.cn",
    "go.dev",
    # === npm / yarn ===
    "registry.npmjs.org",
    "registry.yarnpkg.com",
    # === pip ===
    "pypi.org",
    "files.pythonhosted.org",
    # === Rust / cargo ===
    "crates.io",
    "static.crates.io",
    "index.crates.io",
    "rustup.rs",
    # === Maven / Java ===
    "repo1.maven.org",
    "repo.maven.apache.org",
    "central.sonatype.com",
    "spring.io",
    # === Documentation / Info Sites ===
    "docs.rs",
    "docs.spring.io",
    "javadoc.io",
    "en.wikipedia.org",
    "dubbo.apache.org",
    "docs.python.org",
    "nodejs.org",
    "developer.mozilla.org",
    # === Ruby ===
    "rubygems.org",
    # === Debian apt (all containers are Debian-based) ===
    "deb.debian.org",
    "security.debian.org",
    "cdn-fastly.deb.debian.org",
    "apt.llvm.org",
    # === Build tools & runtimes ===
    "nodejs.org",
    "deb.nodesource.com",
    "gradle.org",
    "plugins.gradle.org",
    "apache.org",
    # === Container registries (tools only, NOT ghcr.io) ===
    "docker.com",
    "docker.io",
    "gcr.io",
    "mcr.microsoft.com",
    "quay.io",
]

# Code hosting domains to poison in /etc/hosts (defense-in-depth).
CODE_HOSTING_DOMAINS = [
    "github.com",
    "www.github.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "gist.githubusercontent.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
    "render.githubusercontent.com",
    "gitlab.com",
    "www.gitlab.com",
    "bitbucket.org",
    "www.bitbucket.org",
    "codeberg.org",
    "sr.ht",
    "gitea.com",
    "gitee.com",
    "sourceforge.net",
    "ghfast.top",
    "ghproxy.com",
    "gitclone.com",
]

# Well-known CDN CIDR ranges to handle IP rotation during long trials.
CDN_CIDR_RANGES = [
    "151.101.0.0/16",  # Fastly
    "146.75.0.0/16",  # Fastly
    "104.16.0.0/13",  # Cloudflare
    "142.250.0.0/15",  # Google
    "216.239.32.0/19",  # Google
]


class ContainerSetup:
    """Docker container initialization with fakeroot user and Claude credentials."""

    # Port used by the in-container API router
    _PROXY_PORT = 8181

    # Vendored Python router copied wholesale into container at /opt/ccr.
    _ROUTER_VENDOR_DIR = Path(__file__).parent.parent.parent / "vendor" / "claude-code-router-py"

    def __init__(
        self,
        container_name: str,
        image_name: str,
        workdir: str = "/testbed",
        agent_name: str = "claude-code",
        e2e_workspace_path: Optional[Path] = None,
        agent_framework_name: str = "claude-code",
        drop_params: bool = False,
        api_router: bool = False,
        reasoning_effort: Optional[str] = None,
    ):
        """Initialize container setup.

        Args:
            container_name: Name for the Docker container
            image_name: Docker image to use
            workdir: Working directory inside container (default: /testbed)
            agent_name: Git user name for agent commits (default: claude)
            e2e_workspace_path: Path to mount as /e2e_workspace (for E2E mode)
            agent_framework_name: Agent framework to use (default: claude-code)
            drop_params: Deprecated, use api_router instead.
            api_router: If True and agent is claude-code, deploy the vendored
                claude-code-router-py inside the container. Translates
                Anthropic Messages API to OpenAI chat/completions format so
                Claude Code can work with OpenAI-compatible backends.
        """
        self.container_name = container_name
        self.image_name = image_name
        self.workdir = workdir
        self.agent_name = agent_name
        self.e2e_workspace_path = Path(e2e_workspace_path) if e2e_workspace_path else None
        # Pass reasoning_effort so the framework can inject CLAUDE_CODE_EFFORT_LEVEL
        # into the container env (workaround for claude-code issue #41028 where
        # the --effort CLI flag is parsed but not propagated to the API request).
        framework_kwargs = {}
        if reasoning_effort:
            framework_kwargs["reasoning_effort"] = reasoning_effort
        self._framework: AgentFramework = get_agent_framework(agent_framework_name, **framework_kwargs)
        self.api_router = api_router or drop_params
        self._agent_framework_name = agent_framework_name

    def get_agent_mounts(self) -> list[str]:
        """Return Docker volume mount arguments for the agent.

        Delegates to the agent framework for agent-specific mounts.

        Returns:
            List of -v arguments for docker run
        """
        return self._framework.get_container_mounts()

    def _should_use_router(self) -> bool:
        """Check if the API router should be enabled.

        The router only applies to claude-code agent framework.
        """
        return self.api_router and self._agent_framework_name == "claude-code"

    def get_agent_env_vars(self) -> list[str]:
        """Return Docker environment variable arguments for the agent.

        Delegates to the agent framework for agent-specific env vars.
        When api_router is enabled for claude-code, intercepts *_BASE_URL
        variables and redirects them to the in-container router.

        Returns:
            List of -e arguments for docker run
        """
        env_vars = self._framework.get_container_env_vars()
        if not self._should_use_router():
            return env_vars

        # Intercept BASE_URL env vars: save original as upstream, redirect to router
        result = []
        proxy_url = f"http://localhost:{self._PROXY_PORT}"
        i = 0
        while i < len(env_vars):
            if env_vars[i] == "-e" and i + 1 < len(env_vars):
                kv = env_vars[i + 1]
                if "_BASE_URL=" in kv:
                    key, _, value = kv.partition("=")
                    result.extend(["-e", f"API_PROXY_UPSTREAM={value}"])
                    result.extend(["-e", f"{key}={proxy_url}"])
                    logger.info(f"  api_router: {key} redirected to in-container router (upstream: {value})")
                    i += 2
                    continue
            result.append(env_vars[i])
            i += 1
        return result

    # Backward compatibility alias
    def get_claude_mounts(self) -> list[str]:
        """Return Docker volume mount arguments for Claude credentials.

        Deprecated: Use get_agent_mounts() instead.

        Returns:
            List of -v arguments for docker run
        """
        return self.get_agent_mounts()

    def _get_base_init_script(self) -> str:
        """Return the base Python init script for container setup.

        This sets up common infrastructure:
        1. Installs sudo
        2. Creates fakeroot user
        3. Sets ownership for /testbed and other directories
        4. Configures git

        Returns:
            Python script as a string
        """
        return f'''
import os
import pwd
import stat
import shutil
from pathlib import Path
import subprocess

# === Step 1: Install sudo ===
try:
    result = subprocess.run(['which', 'sudo'], capture_output=True)
    if result.returncode != 0:
        # Try apt-get first (Debian/Ubuntu)
        apt_result = subprocess.run(['apt-get', 'update'], capture_output=True)
        if apt_result.returncode == 0:
            subprocess.run(['apt-get', 'install', '-y', '-qq', 'sudo'], capture_output=True)
        else:
            # Try apk (Alpine)
            subprocess.run(['apk', 'add', '--no-cache', 'sudo'], capture_output=True)
except Exception as e:
    print(f"Warning: Could not install sudo: {{e}}")

# === Step 2: Create fakeroot user ===
try:
    try:
        pwd.getpwnam('fakeroot')
        print("fakeroot user already exists")
    except KeyError:
        # Find next available UID >= 1000
        existing_uids = [u.pw_uid for u in pwd.getpwall()]
        uid = 1000
        while uid in existing_uids:
            uid += 1

        # Add to /etc/passwd (use GID 0 = root group for more permissions)
        with open('/etc/passwd', 'a') as f:
            f.write(f'fakeroot:x:{{uid}}:0:Fakeroot User:/home/fakeroot:/bin/bash\\n')

        # Also create a fakeroot group for compatibility
        with open('/etc/group', 'a') as f:
            f.write(f'fakeroot:x:{{uid}}:\\n')

        # Add fakeroot to root group (GID 0) explicitly
        # Read current /etc/group and add fakeroot to root group
        with open('/etc/group', 'r') as f:
            group_content = f.read()

        # Add fakeroot to root group if not already there
        lines = group_content.split('\\n')
        new_lines = []
        for line in lines:
            if line.startswith('root:'):
                parts = line.split(':')
                if len(parts) >= 4:
                    members = parts[3].split(',') if parts[3] else []
                    if 'fakeroot' not in members:
                        members.append('fakeroot')
                        parts[3] = ','.join(m for m in members if m)
                    line = ':'.join(parts)
            new_lines.append(line)

        with open('/etc/group', 'w') as f:
            f.write('\\n'.join(new_lines))
        print("Added fakeroot to root group (GID 0)")

        # Create home directory
        os.makedirs('/home/fakeroot', exist_ok=True)
        os.chown('/home/fakeroot', uid, 0)  # GID 0 = root group
        os.chmod('/home/fakeroot', 0o755)

        print(f"Created fakeroot user with UID={{uid}}, GID=0 (root group)")

        # Setup sudo access
        if os.path.isdir('/etc/sudoers.d'):
            with open('/etc/sudoers.d/fakeroot', 'w') as f:
                f.write('fakeroot ALL=(ALL) NOPASSWD:ALL\\n')
            os.chmod('/etc/sudoers.d/fakeroot', 0o440)
            print("Configured sudo access for fakeroot")
except Exception as e:
    print(f"Error creating fakeroot user: {{e}}")

# === Step 3: Set ownership ===
try:
    fake_user = pwd.getpwnam('fakeroot')
    uid, gid = fake_user.pw_uid, fake_user.pw_gid

    # Set ownership for home directory
    for root, dirs, files in os.walk('/home/fakeroot'):
        os.chown(root, uid, gid)
        os.chmod(root, os.stat(root).st_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        for f in files:
            filepath = os.path.join(root, f)
            os.chown(filepath, uid, gid)
            os.chmod(filepath, os.stat(filepath).st_mode | stat.S_IRUSR | stat.S_IWUSR)

    # Set ownership for /testbed
    if os.path.exists('/testbed'):
        print(f"Setting ownership of /testbed to fakeroot (uid={{uid}}, gid={{gid}})")
        result = subprocess.run(['chown', '-R', f'{{uid}}:{{gid}}', '/testbed'], capture_output=True, text=True)
        if result.returncode == 0:
            print("Successfully set /testbed ownership to fakeroot")
        else:
            print(f"chown failed: {{result.stderr}}")

    # Set ownership for /e2e_workspace if exists
    if os.path.exists('/e2e_workspace'):
        result = subprocess.run(['chown', '-R', f'{{uid}}:{{gid}}', '/e2e_workspace'], capture_output=True, text=True)
        if result.returncode == 0:
            print("Successfully set /e2e_workspace ownership to fakeroot")

    # === Fix toolchain directories permissions (Cargo, Rustup, npm, etc.) ===
    # Give fakeroot full access to these directories
    toolchain_dirs = [
        '/usr/local/cargo',      # Cargo home
        '/usr/local/rustup',     # Rustup home
        '/root/.cargo',          # Alternative cargo location
        '/root/.rustup',         # Alternative rustup location
        '/usr/local/go',         # Go installation
        '/go',                   # Go workspace (GOPATH default in many images)
        '/root/go',              # Go workspace (alternative)
        '/usr/local/lib/node_modules',  # Global npm modules
        '/root/.npm',            # npm cache
        '/root/.cache',          # General cache (pip, etc.)
    ]

    for toolchain_dir in toolchain_dirs:
        if os.path.exists(toolchain_dir):
            # Option 1: Change ownership to fakeroot (most permissive)
            result = subprocess.run(['chown', '-R', f'{{uid}}:0', toolchain_dir], capture_output=True, text=True)
            if result.returncode == 0:
                print(f"Changed ownership of {{toolchain_dir}} to fakeroot")
            else:
                # Option 2: If chown fails, at least make it group-writable for root group
                result2 = subprocess.run(['chmod', '-R', 'g+rwX', toolchain_dir], capture_output=True, text=True)
                if result2.returncode == 0:
                    print(f"Made {{toolchain_dir}} group-writable")
                else:
                    print(f"Failed to fix permissions for {{toolchain_dir}}")

    # Ensure /tmp has correct permissions (some tools need it)
    if os.path.exists('/tmp'):
        os.chmod('/tmp', 0o1777)
        print("Set /tmp to 1777")
except Exception as e:
    print(f"Error setting ownership: {{e}}")

# === Step 4: Configure git ===
try:
    fake_user = pwd.getpwnam('fakeroot')
    uid, gid = fake_user.pw_uid, fake_user.pw_gid

    # Create gitconfig for fakeroot user
    gitconfig_path = '/home/fakeroot/.gitconfig'
    gitconfig_content = """[core]
\\tattributesFile = /home/fakeroot/.config/git/attributes
[user]
\\tname = {self.agent_name}
\\temail = agent@example.com
[safe]
\\tdirectory = /testbed
"""

    with open(gitconfig_path, 'w') as f:
        f.write(gitconfig_content)

    os.chown(gitconfig_path, uid, gid)
    os.chmod(gitconfig_path, 0o644)

    # Create .config/git directory
    git_config_dir = '/home/fakeroot/.config/git'
    os.makedirs(git_config_dir, exist_ok=True)
    os.chown(git_config_dir, uid, gid)
    os.chmod(git_config_dir, 0o755)

    # Create empty attributes file
    attributes_path = os.path.join(git_config_dir, 'attributes')
    with open(attributes_path, 'w') as f:
        pass
    os.chown(attributes_path, uid, gid)
    os.chmod(attributes_path, 0o644)

    print("Configured git for fakeroot user")
except Exception as e:
    print(f"Error configuring git: {{e}}")

print("Base container initialization complete!")
'''

    def _install_router_in_container(self) -> None:
        """Copy vendored claude-code-router-py into the container + pip install."""
        vendor_dir = self._ROUTER_VENDOR_DIR
        if not vendor_dir.exists():
            raise RuntimeError(f"Router vendor directory not found: {vendor_dir}")

        # Create target directory
        subprocess.run(
            ["docker", "exec", self.container_name, "mkdir", "-p", "/opt/ccr"],
            check=True, capture_output=True, text=True,
        )

        # Copy vendored files into container
        subprocess.run(
            ["docker", "cp", f"{vendor_dir}/.", f"{self.container_name}:/opt/ccr/"],
            check=True, capture_output=True, text=True,
        )

        # Ensure pip is available, then install dependencies (before network lockdown)
        subprocess.run(
            ["docker", "exec", self.container_name,
             "sh", "-c",
             "python3 -m pip --version >/dev/null 2>&1 || "
             "(apt-get update -qq && apt-get install -y -qq python3-pip) 2>/dev/null || "
             "python3 -m ensurepip --default-pip 2>/dev/null || true"],
            capture_output=True, text=True, timeout=120,
        )
        # Try with --break-system-packages first (PEP 668), fall back without
        result = subprocess.run(
            ["docker", "exec", self.container_name,
             "python3", "-m", "pip", "install", "-q",
             "--break-system-packages",
             "-r", "/opt/ccr/requirements.txt"],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0 and "no such option" in (result.stderr or ""):
            result = subprocess.run(
                ["docker", "exec", self.container_name,
                 "python3", "-m", "pip", "install", "-q",
                 "-r", "/opt/ccr/requirements.txt"],
                capture_output=True, text=True, timeout=600,
            )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to install router dependencies: {result.stderr}")

        logger.info("API router (claude-code-router-py) installed in container")

    def _get_router_init_script(self) -> str:
        """Return init script that starts the vendored Python router daemon.

        The router translates Anthropic Messages API requests to OpenAI
        chat/completions format, enabling Claude Code to work with any
        OpenAI-compatible model backend.

        Reads API_PROXY_UPSTREAM and ANTHROPIC_API_KEY env vars.
        Listens on localhost:{_PROXY_PORT}.
        """
        return f'''
# === API Router (claude-code-router-py) ===
try:
    import json, os, subprocess, time

    upstream = os.environ.get("API_PROXY_UPSTREAM", "")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    # Ensure upstream ends with /chat/completions for OpenAI format
    base = upstream.rstrip("/")
    if base.endswith("/chat/completions"):
        api_base_url = base
    else:
        api_base_url = base + "/v1/chat/completions"

    config = {{
        "PORT": {self._PROXY_PORT},
        "HOST": "127.0.0.1",
        "API_TIMEOUT_MS": 600000,
        "LOG_LEVEL": "info",
        "Providers": [{{
            "name": "upstream",
            "api_base_url": api_base_url,
            "api_key": api_key,
            "max_retries": 3,
            "params": {{
                "max_tokens": 65536,
            }}
        }}],
        "Router": {{
            "default": "upstream,/model"
        }}
    }}

    config_path = "/opt/ccr/config.json"
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)

    # Start router as background daemon
    subprocess.Popen(
        ["python3", "/opt/ccr/main.py", "--config", config_path,
         "--host", "127.0.0.1", "--port", str({self._PROXY_PORT})],
        stdout=open("/tmp/api_router.log", "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    # Wait and verify it started
    time.sleep(3)
    import urllib.request
    for attempt in range(5):
        try:
            urllib.request.urlopen("http://127.0.0.1:{self._PROXY_PORT}/health", timeout=2)
            break
        except Exception:
            time.sleep(1)
    print("API router (claude-code-router-py) started on port {self._PROXY_PORT}")
except Exception as e:
    print(f"Warning: Failed to start API router: {{e}}")
'''

    def get_init_script(self) -> str:
        """Return Python init script for container setup.

        Combines base initialization with agent-specific initialization.
        The base script sets up fakeroot user, sudo, git config.
        The agent-specific script sets up credentials, tools, etc.
        Optionally includes the API router for claude-code with non-native models.

        Returns:
            Combined Python script as a string
        """
        base_script = self._get_base_init_script()
        agent_script = self._framework.get_container_init_script(self.agent_name)
        router_script = self._get_router_init_script() if self._should_use_router() else ""

        return f"""{base_script}

# === Agent-specific initialization ===
{agent_script}
{router_script}
print("Container initialization complete!")
"""

    def start_container(self, extra_mounts: Optional[list[str]] = None, force: bool = False) -> None:
        """Start Docker container with proper initialization.

        Args:
            extra_mounts: Additional -v mount arguments
            force: If True, remove existing container first
        """
        # Check for existing container
        if self.container_exists():
            if force:
                logger.info(f"Removing existing container {self.container_name}...")
                subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True)
            else:
                if self.is_running():
                    logger.info(f"Container {self.container_name} already running")
                    return
                else:
                    logger.info(f"Starting existing container {self.container_name}...")
                    subprocess.run(["docker", "start", self.container_name], check=True)
                    return

        # Verify image exists
        result = subprocess.run(
            ["docker", "image", "inspect", self.image_name],
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Docker image not found: {self.image_name}")

        logger.info(f"Launching container {self.container_name} from {self.image_name}...")

        # Build docker run command
        # Use --init to properly reap zombie child processes (e.g., plugin processes)
        # --cap-add=NET_ADMIN: required for iptables-based network lockdown
        # --sysctl net.ipv6.conf.all.disable_ipv6=1: prevent IPv6 bypass of iptables rules
        docker_options = [
            "docker",
            "run",
            "-d",
            "--init",
            "--cap-add=NET_ADMIN",
            "--sysctl",
            "net.ipv6.conf.all.disable_ipv6=1",
            "--add-host=host.docker.internal:host-gateway",
            "--name",
            self.container_name,
            "--ulimit",
            "nofile=65535:65535",
            "-w",
            self.workdir,
            "-e",
            "HOME=/root",  # Start as root for setup
        ]

        # Add agent mounts (credentials, binaries, etc.)
        docker_options.extend(self.get_agent_mounts())

        # Add agent environment variables (API keys, etc.)
        docker_options.extend(self.get_agent_env_vars())

        # Add e2e_workspace mount if specified
        if self.e2e_workspace_path:
            self.e2e_workspace_path.mkdir(parents=True, exist_ok=True)
            docker_options.extend(["-v", f"{self.e2e_workspace_path.resolve()}:/e2e_workspace"])

        # Add extra mounts
        if extra_mounts:
            docker_options.extend(extra_mounts)

        # Add image and command
        cmd = docker_options + [self.image_name, "tail", "-f", "/dev/null"]

        logger.debug(f"Docker run command: {' '.join(cmd)}")
        subprocess.run(cmd, check=True)

        # Ensure Python3 is available for init script
        self._ensure_python3()

        # Install API router if needed (before init script, before network lockdown)
        if self._should_use_router():
            self._install_router_in_container()

        # Run initialization script
        logger.info("Running container initialization...")
        init_script = self.get_init_script()
        result = subprocess.run(
            ["docker", "exec", self.container_name, "python3", "-c", init_script],
            capture_output=True,
            text=True,
        )

        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                logger.info(f"  {line}")
        if result.stderr:
            for line in result.stderr.strip().split("\n"):
                if line.strip():
                    logger.warning(f"  {line}")

        # Wait for fakeroot user
        self._wait_for_fakeroot()

        logger.info(f"Container {self.container_name} launched and initialized.")

    def _ensure_python3(self) -> None:
        """Ensure Python3 is available in the container.

        If Python3 is not found, attempts to install it using the container's
        package manager (apt-get, apk, or yum).
        """
        # Check if python3 exists
        result = subprocess.run(
            ["docker", "exec", self.container_name, "which", "python3"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            logger.info("Python3 already available in container")
            return

        logger.info("Python3 not found, attempting to install...")

        # Try apt-get (Debian/Ubuntu) - preserve stderr for debugging.
        # Some hosts/datacenters block outbound port 80; rewrite Debian/Ubuntu
        # apt sources to HTTPS so apt-get reaches the mirror via 443 instead.
        install_script = """
if command -v apt-get >/dev/null 2>&1; then
    # Rewrite http://*.ubuntu.com / *.debian.org to https:// — port 443 is
    # commonly reachable when 80 is blocked. Idempotent (sed -i in place).
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
        [ -f "$f" ] || continue
        sed -i -E 's@http://(archive\\.ubuntu\\.com|security\\.ubuntu\\.com|[a-z0-9.-]*\\.archive\\.ubuntu\\.com|deb\\.debian\\.org|security\\.debian\\.org)@https://\\1@g' "$f" 2>/dev/null || true
    done
    apt-get update -qq && apt-get install -y -qq python3-minimal
    exit $?
elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache python3
    exit $?
elif command -v yum >/dev/null 2>&1; then
    yum install -y -q python3
    exit $?
else
    echo "No supported package manager found" >&2
    exit 1
fi
"""
        # Retry up to 3 times with exponential backoff
        max_retries = 3
        last_error = ""
        for attempt in range(max_retries):
            if attempt > 0:
                wait_time = 2**attempt  # 2, 4 seconds
                logger.info(
                    f"Retrying Python3 installation (attempt {attempt + 1}/{max_retries}) after {wait_time}s..."
                )
                time.sleep(wait_time)

            result = subprocess.run(
                ["docker", "exec", self.container_name, "/bin/sh", "-c", install_script],
                capture_output=True,
                text=True,
                timeout=600,  # 10 minute timeout for package installation
            )

            if result.returncode == 0:
                logger.info("Successfully installed Python3")
                return
            else:
                last_error = result.stderr.strip() if result.stderr else "Unknown error"
                logger.warning(f"Python3 installation attempt {attempt + 1} failed: {last_error}")

        # Final verification after all retries failed
        verify = subprocess.run(
            ["docker", "exec", self.container_name, "which", "python3"],
            capture_output=True,
            text=True,
        )
        if verify.returncode == 0:
            logger.info("Python3 is available despite installation errors")
            return

        raise RuntimeError(f"Python3 is required but could not be installed in the container: {last_error}")

    def _wait_for_fakeroot(self, max_wait: int = 10) -> bool:
        """Wait for fakeroot user to be created.

        Args:
            max_wait: Maximum seconds to wait

        Returns:
            True if fakeroot user is ready
        """
        logger.info("Waiting for fakeroot user...")
        for i in range(max_wait):
            time.sleep(1)
            result = subprocess.run(
                ["docker", "exec", self.container_name, "id", "fakeroot"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                logger.info("fakeroot user created successfully")
                return True
            if i == max_wait - 1:
                logger.warning(f"Timeout waiting for fakeroot user (waited {max_wait}s)")
        return False

    def truncate_git_history(self, main_branch: str = "main") -> None:
        """Truncate git history to prevent agent from seeing future commits.

        This removes all tags, branches (except main), remotes, reflog,
        and runs garbage collection to remove unreachable objects.

        Args:
            main_branch: Name of the main branch to keep
        """
        logger.info(f"Truncating git history (main_branch={main_branch})...")

        truncate_script = f"""
set -e
cd /testbed

# Ensure git trusts this directory (avoid "dubious ownership" error)
git config --global --add safe.directory /testbed 2>/dev/null || true

MAIN_BRANCH="{main_branch}"

echo "=== Git History Truncation ==="
echo "Current HEAD: $(git rev-parse HEAD)"
echo "Current branch: $(git branch --show-current 2>/dev/null || echo 'detached')"
echo "Target main branch: $MAIN_BRANCH"

# Step 1: Delete all tags
echo ""
echo "Step 1: Deleting all tags..."
TAG_COUNT=$(git tag -l | wc -l)
if [ "$TAG_COUNT" -gt 0 ]; then
    git tag -l | xargs git tag -d
    echo "  Deleted $TAG_COUNT tags"
else
    echo "  No tags to delete"
fi

# Step 2: Reset main branch to HEAD
echo ""
echo "Step 2: Resetting $MAIN_BRANCH branch to current HEAD..."
CURRENT_HEAD=$(git rev-parse HEAD)

# Delete all branches
BRANCHES=$(git for-each-ref --format='%(refname:short)' refs/heads/)
for branch in $BRANCHES; do
    git branch -D "$branch" 2>/dev/null && echo "  Deleted branch: $branch" || true
done

# Create/reset main branch at current HEAD
git checkout -B "$MAIN_BRANCH" $CURRENT_HEAD 2>/dev/null
echo "  Created $MAIN_BRANCH branch at HEAD ($CURRENT_HEAD)"

# Step 3: Delete all remote tracking branches (fast method)
echo ""
echo "Step 3: Deleting remote tracking branches..."
REMOTE_BRANCHES=$(git branch -r 2>/dev/null | wc -l)
if [ "$REMOTE_BRANCHES" -gt 0 ]; then
    # Fast deletion: remove refs directory and packed-refs entries directly
    rm -rf .git/refs/remotes 2>/dev/null || true
    # Remove remote refs from packed-refs file if it exists
    if [ -f .git/packed-refs ]; then
        grep -v 'refs/remotes/' .git/packed-refs > .git/packed-refs.tmp 2>/dev/null || true
        mv .git/packed-refs.tmp .git/packed-refs 2>/dev/null || true
    fi
    # Remove remote config entries
    git config --remove-section remote.origin 2>/dev/null || true
    echo "  Removed all remotes ($REMOTE_BRANCHES tracking branches)"
else
    echo "  No remote branches"
fi

# Step 4: Clear reflog
echo ""
echo "Step 4: Clearing reflog..."
git reflog expire --expire=now --all 2>/dev/null || true
echo "  Reflog cleared"

# Step 5: Garbage collect
echo ""
echo "Step 5: Running garbage collection..."
git gc --prune=now --aggressive 2>/dev/null || git gc --prune=now || true
echo "  GC completed"

# Step 6: Verify
echo ""
echo "=== Verification ==="
echo "Tags remaining: $(git tag -l | wc -l)"
echo "Branches remaining: $(git branch | wc -l)"
echo "Remote branches: $(git branch -r 2>/dev/null | wc -l || echo 0)"
echo "HEAD: $(git rev-parse --short HEAD)"
echo "Current branch: $(git branch --show-current)"

echo ""
echo "Git history truncated successfully"
"""

        result = subprocess.run(
            [
                "docker",
                "exec",
                "--user",
                "fakeroot",
                "-e",
                "HOME=/home/fakeroot",
                "-w",
                "/testbed",
                self.container_name,
                "/bin/sh",
                "-c",
                truncate_script,
            ],
            capture_output=True,
            text=True,
        )

        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                logger.info(f"  {line}")
        if result.stderr:
            for line in result.stderr.strip().split("\n"):
                if line.strip():
                    logger.warning(f"  {line}")

        if result.returncode != 0:
            logger.warning(f"Git history truncation returned non-zero exit code: {result.returncode}")
        else:
            logger.info("Git history truncation completed")

    def _resolve_whitelisted_ips(self) -> set[str]:
        """Resolve all WHITELISTED_DOMAINS to IP addresses from the host.

        Performs multiple resolution attempts per domain to capture CDN rotation.

        Returns:
            Set of unique IP address strings.
        """
        ips: set[str] = set()
        # Quarantine: domains in EVOCLAW_DENY_DOMAINS are NOT resolved/accepted,
        # so the agent cannot reach that registry (e.g. PyPI) to fetch the
        # repo-under-test's own target-version source. Pairs with EVOCLAW_DENY_CIDRS
        # below (needed because a registry's IPs ride a shared CDN range).
        _deny = {d.strip() for d in os.environ.get("EVOCLAW_DENY_DOMAINS", "").split(",") if d.strip()}
        if _deny:
            logger.warning(f"EVOCLAW_DENY_DOMAINS active — excluding from whitelist: {sorted(_deny)}")
        # Vertex regional endpoints: a non-"global" location routes to
        # "{LOC}-aiplatform.googleapis.com" (the bare aiplatform.googleapis.com
        # host in WHITELISTED_DOMAINS only covers the `global` endpoint). Resolve
        # the regional host too, else the documented region-switch (e.g.
        # us-east5 once quota lands) dies under the always-on network lockdown.
        domains = list(WHITELISTED_DOMAINS)
        _vloc = os.environ.get("EVOCLAW_VERTEX_LOCATION", "").strip()
        if _vloc and _vloc != "global":
            domains.append(f"{_vloc}-aiplatform.googleapis.com")
            logger.info(f"  Vertex location={_vloc}: whitelisting {_vloc}-aiplatform.googleapis.com")
        for domain in domains:
            if domain in _deny:
                continue
            for _attempt in range(3):
                try:
                    results = socket.getaddrinfo(domain, None, socket.AF_INET)
                    for _family, _type, _proto, _canonname, sockaddr in results:
                        ips.add(sockaddr[0])
                except socket.gaierror:
                    pass  # domain may not resolve — that's fine
        # Quarantine: drop any resolved IP that falls inside a denied CIDR. A
        # shared CDN (e.g. Fastly) serves the blocked registry from its WHOLE IP
        # range via SNI, so an allowed Fastly-fronted domain (deb.debian.org)
        # would otherwise re-admit IPs the agent can `curl --resolve` the registry
        # through. Removing them here closes that SNI-routing hole.
        _deny_cidrs = [c.strip() for c in os.environ.get("EVOCLAW_DENY_CIDRS", "").split(",") if c.strip()]
        if _deny_cidrs:
            import ipaddress
            nets = []
            for c in _deny_cidrs:
                try:
                    nets.append(ipaddress.ip_network(c, strict=False))
                except ValueError:
                    pass
            before = len(ips)
            ips = {ip for ip in ips
                   if not any(ipaddress.ip_address(ip) in n for n in nets)}
            if before != len(ips):
                logger.warning(f"EVOCLAW_DENY_CIDRS pruned {before - len(ips)} resolved IPs in denied ranges")
        return ips

    def lock_network(self) -> None:
        """Apply whitelist-based network lockdown inside the container.

        Must be called AFTER start_container() and truncate_git_history(), but
        BEFORE handing control to the agent. Runs as root inside the container.

        Steps:
          1. Install iptables (fatal if fails)
          2. Resolve WHITELISTED_DOMAINS → IP set (+ CDN CIDRs)
          3. Build iptables rules: loopback → established → DNS → whitelist → DROP
          4. Poison /etc/hosts with CODE_HOSTING_DOMAINS
          5. Set Go env vars (GOPROXY, GONOSUMCHECK, etc.)
          6. Remove sudoers so fakeroot cannot flush iptables
          7. Verify lockdown

        Raises:
            RuntimeError: If iptables installation or rule application fails.
        """
        logger.info("Applying network lockdown to container...")

        # --- Step 1: Install iptables ---
        # Same HTTPS-rewrite trick as _ensure_python3 (port 80 may be blocked).
        install_result = subprocess.run(
            [
                "docker",
                "exec",
                self.container_name,
                "/bin/sh",
                "-c",
                (
                    "for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do "
                    "[ -f \"$f\" ] || continue; "
                    "sed -i -E 's@http://(archive\\.ubuntu\\.com|security\\.ubuntu\\.com|[a-z0-9.-]*\\.archive\\.ubuntu\\.com|deb\\.debian\\.org|security\\.debian\\.org)@https://\\1@g' \"$f\" 2>/dev/null || true; "
                    "done; "
                    "apt-get update -qq && apt-get install -y -qq iptables"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if install_result.returncode != 0:
            raise RuntimeError(f"Failed to install iptables in container: {install_result.stderr}")
        logger.info("  iptables installed")

        # --- Step 2: Resolve whitelisted IPs ---
        whitelisted_ips = self._resolve_whitelisted_ips()
        logger.info(f"  Resolved {len(whitelisted_ips)} unique IPs from {len(WHITELISTED_DOMAINS)} domains")

        # --- Step 3: Build iptables script ---
        # Combine resolved IPs with well-known CDN CIDR ranges
        accept_lines = []
        for ip in sorted(whitelisted_ips):
            accept_lines.append(f"iptables -A OUTPUT -d {ip} -j ACCEPT")
        # Quarantine mode: CIDRs in EVOCLAW_DENY_CIDRS are NOT accepted, so a
        # registry fronted by that CDN becomes unreachable even via raw curl.
        # Needed because EVOCLAW_DENY_DOMAINS only drops DNS-resolved IPs, while
        # registries like PyPI ride a shared CDN range (Fastly 151.101.0.0/16)
        # that CDN_CIDR_RANGES would otherwise accept wholesale. Keep Google
        # (Vertex) and Cloudflare (claude.ai) ranges so the LLM path survives.
        _deny_cidrs = {c.strip() for c in os.environ.get("EVOCLAW_DENY_CIDRS", "").split(",") if c.strip()}
        if _deny_cidrs:
            logger.warning(f"EVOCLAW_DENY_CIDRS active — excluding CDN ranges: {sorted(_deny_cidrs)}")
        for cidr in CDN_CIDR_RANGES:
            if cidr in _deny_cidrs:
                continue
            accept_lines.append(f"iptables -A OUTPUT -d {cidr} -j ACCEPT")

        accept_block = "\n".join(accept_lines)

        iptables_script = f"""set -e

# Flush existing rules
iptables -F OUTPUT

# Allow loopback
iptables -A OUTPUT -o lo -j ACCEPT

# Allow established/related connections
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# Allow DNS only to the container's configured resolver(s), not arbitrary DNS
# servers (blocks DNS-over-:53 exfil / recursive lookups to outside resolvers).
# The Docker embedded resolver (127.0.0.11) rides loopback, already accepted.
_ns=$(awk '/^nameserver/ {{print $2}}' /etc/resolv.conf 2>/dev/null)
if [ -n "$_ns" ]; then
  for ns in $_ns; do
    iptables -A OUTPUT -p udp -d "$ns" --dport 53 -j ACCEPT
    iptables -A OUTPUT -p tcp -d "$ns" --dport 53 -j ACCEPT
  done
else
  # No resolver parsed — keep DNS open so resolution still works (fail-open).
  iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
  iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT
fi

# Allow whitelisted IPs and CDN CIDRs
{accept_block}

# Default policy: DROP everything else
iptables -P OUTPUT DROP

echo "iptables rules applied successfully"
"""

        iptables_result = subprocess.run(
            ["docker", "exec", self.container_name, "/bin/sh", "-c", iptables_script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if iptables_result.returncode != 0:
            raise RuntimeError(f"Failed to apply iptables rules: {iptables_result.stderr}")
        logger.info("  iptables rules applied")

        # --- Step 4: Poison /etc/hosts ---
        hosts_lines = "\n".join(f"0.0.0.0 {d}" for d in CODE_HOSTING_DOMAINS)
        hosts_script = f"""
# Append code-hosting blocks to /etc/hosts
cat >> /etc/hosts << 'HOSTS_EOF'

# === Network lockdown: code hosting sites blocked ===
{hosts_lines}
HOSTS_EOF

# Lock permissions so non-root cannot edit
chmod 644 /etc/hosts
echo "/etc/hosts poisoned with {len(CODE_HOSTING_DOMAINS)} domains"
"""
        subprocess.run(
            ["docker", "exec", self.container_name, "/bin/sh", "-c", hosts_script],
            capture_output=True,
            text=True,
        )
        logger.info(f"  /etc/hosts poisoned ({len(CODE_HOSTING_DOMAINS)} domains)")

        # --- Step 5: Set Go env vars ---
        go_env_script = """
# Configure Go to use module proxy instead of direct VCS
cat >> /etc/environment << 'EOF'
GOPROXY=https://proxy.golang.org,direct
GONOSUMCHECK=*
GONOSUMDB=*
EOF

# Also set for fakeroot's shell profile
mkdir -p /home/fakeroot
cat >> /home/fakeroot/.bashrc << 'EOF'
export GOPROXY=https://proxy.golang.org,direct
export GONOSUMCHECK=*
export GONOSUMDB=*
EOF
echo "Go env vars configured"
"""
        subprocess.run(
            ["docker", "exec", self.container_name, "/bin/sh", "-c", go_env_script],
            capture_output=True,
            text=True,
        )
        logger.info("  Go proxy env vars set")

        # --- Step 6: Remove sudoers to prevent iptables bypass ---
        sudo_result = subprocess.run(
            [
                "docker",
                "exec",
                self.container_name,
                "/bin/sh",
                "-c",
                "rm -f /etc/sudoers.d/fakeroot && echo 'sudoers removed'",
            ],
            capture_output=True,
            text=True,
        )
        logger.info(f"  {sudo_result.stdout.strip()}")

        # --- Step 7: Verify lockdown ---
        self.verify_network_lockdown()

        logger.info("Network lockdown applied successfully")

    def verify_network_lockdown(self) -> bool:
        """Verify that network lockdown is active in the container.

        Tests that a blocked domain (github.com) cannot be reached and that
        iptables OUTPUT policy is DROP.

        Returns:
            True if lockdown is verified.

        Raises:
            RuntimeError: If lockdown verification fails.
        """
        # Check iptables OUTPUT policy is DROP
        policy_result = subprocess.run(
            [
                "docker",
                "exec",
                self.container_name,
                "iptables",
                "-L",
                "OUTPUT",
                "-n",
            ],
            capture_output=True,
            text=True,
        )
        if "policy DROP" not in policy_result.stdout:
            raise RuntimeError(
                "Network lockdown verification failed: OUTPUT policy is not DROP. "
                f"iptables output: {policy_result.stdout}"
            )

        # Verify a blocked domain is unreachable (as fakeroot, 3s timeout)
        curl_result = subprocess.run(
            [
                "docker",
                "exec",
                "--user",
                "fakeroot",
                "-e",
                "HOME=/home/fakeroot",
                self.container_name,
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--connect-timeout",
                "3",
                "--max-time",
                "5",
                "https://github.com",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if curl_result.returncode == 0 and curl_result.stdout.strip().startswith("2"):
            raise RuntimeError("Network lockdown verification failed: github.com is reachable")

        # Quarantine: assert the denied registry hosts are actually unreachable
        # — the whole point of EVOCLAW_DENY_*. The github.com probe above only
        # covers code hosting; a typo'd deny CIDR/domain would otherwise pass
        # verification while leaving the exact cheat channel (e.g. PyPI) open.
        _deny_domains = [d.strip() for d in os.environ.get("EVOCLAW_DENY_DOMAINS", "").split(",") if d.strip()]
        for host in _deny_domains:
            probe = subprocess.run(
                [
                    "docker", "exec", "--user", "fakeroot",
                    "-e", "HOME=/home/fakeroot", self.container_name,
                    "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                    "--connect-timeout", "3", "--max-time", "5",
                    f"https://{host}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if probe.returncode == 0 and probe.stdout.strip().startswith("2"):
                raise RuntimeError(
                    f"Quarantine verification failed: denied host '{host}' is still "
                    f"reachable — EVOCLAW_DENY_DOMAINS/EVOCLAW_DENY_CIDRS not effective"
                )
        if _deny_domains:
            logger.info(f"  Quarantine verified: {len(_deny_domains)} denied host(s) unreachable")

        # Verify sudo is revoked
        sudo_result = subprocess.run(
            [
                "docker",
                "exec",
                "--user",
                "fakeroot",
                "-e",
                "HOME=/home/fakeroot",
                self.container_name,
                "sudo",
                "-n",
                "true",
            ],
            capture_output=True,
            text=True,
        )
        if sudo_result.returncode == 0:
            raise RuntimeError("Network lockdown verification failed: fakeroot still has sudo access")

        logger.info("  Lockdown verified: github.com blocked, sudo revoked, OUTPUT policy DROP")
        return True

    def docker_exec(
        self,
        cmd: list[str],
        user: str = "fakeroot",
        check: bool = True,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess:
        """Execute command in container.

        Args:
            cmd: Command to execute
            user: User to run as (default: fakeroot)
            check: If True, raise on non-zero exit
            capture_output: If True, capture stdout/stderr

        Returns:
            CompletedProcess result
        """
        docker_cmd = [
            "docker",
            "exec",
            "--user",
            user,
            "-e",
            f"HOME=/home/{user}" if user != "root" else "HOME=/root",
            "-w",
            self.workdir,
            self.container_name,
        ] + cmd

        return subprocess.run(docker_cmd, capture_output=capture_output, text=True, check=check)

    def docker_exec_git(self, *git_args) -> subprocess.CompletedProcess:
        """Execute git command in container as fakeroot user.

        Args:
            *git_args: Git command arguments

        Returns:
            CompletedProcess result
        """
        # Use -c safe.directory to avoid ownership warnings when running as fakeroot
        return self.docker_exec(["git", "-c", f"safe.directory={self.workdir}", *git_args], check=False)

    def container_exists(self) -> bool:
        """Check if container exists (running or stopped)."""
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", f"name=^{self.container_name}$"],
            capture_output=True,
            text=True,
        )
        return self.container_name in result.stdout

    def is_running(self) -> bool:
        """Check if container is currently running."""
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", self.container_name],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() == "true"

    def cleanup(self, remove: bool = True) -> None:
        """Cleanup container.

        Args:
            remove: If True, remove container; otherwise just stop it
        """
        if not self.container_exists():
            return

        if remove:
            logger.info(f"Removing container {self.container_name}...")
            subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True)
        else:
            logger.info(f"Stopping container {self.container_name}...")
            subprocess.run(["docker", "stop", self.container_name], capture_output=True)
