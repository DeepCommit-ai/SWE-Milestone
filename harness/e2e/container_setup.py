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
from harness.e2e.image_version import parse_local_ref
from harness.e2e.quarantine import (
    FIREWALL_EXEMPTABLE_DOMAINS,
    QUARANTINE_MIRROR_DOMAINS,
    cidr_overlaps_any,
    goproxy_value,
    load_quarantine_env,
)

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
    "static.rust-lang.org",  # official rust toolchain binary source (rustc/cargo/std); safe — cannot serve a repo's @B crate (those ride crates.io, still denied under quarantine)
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
    # NOTE: public module-proxy mirror domains (proxy.golang.org, goproxy.cn, …)
    # are NOT here — they are a cross-ecosystem answer channel poisoned only in
    # quarantine containers via QUARANTINE_MIRROR_DOMAINS (quarantine.py) +
    # _poison_domain_list, so non-quarantine baselines keep working go fetches.
]

# Well-known CDN CIDR ranges to handle IP rotation during long trials.
CDN_CIDR_RANGES = [
    "151.101.0.0/16",  # Fastly
    "146.75.0.0/16",  # Fastly
    "104.16.0.0/13",  # Cloudflare
    "142.250.0.0/15",  # Google
    "216.239.32.0/19",  # Google
]


def _poison_domain_list(quarantine_active: bool) -> list[str]:
    """Domains to blackhole in /etc/hosts for this container.

    Code-hosting sites are poisoned in every container (defense in depth). The
    public module-proxy mirror domains (QUARANTINE_MIRROR_DOMAINS) are a
    cross-ecosystem answer channel and are added ONLY under quarantine, so a
    non-quarantine/baseline container keeps working go module fetches (#4).
    """
    return list(CODE_HOSTING_DOMAINS) + (
        list(QUARANTINE_MIRROR_DOMAINS) if quarantine_active else []
    )


def _interpret_probe(returncode: int, stdout: str) -> bool:
    """Interpret an in-container reachability probe's output.

    True (reachable) on a REACH marker, False (blocked) on BLOCK. A result with
    neither marker means the probe itself failed to run (python3 missing, docker
    exec error) — INDETERMINATE, not 'blocked', so raise rather than let a
    broken probe silently pass verification (fail-open) (#11).
    """
    if "REACH" in stdout:
        return True
    if "BLOCK" in stdout:
        return False
    raise RuntimeError(
        f"network probe did not run (rc={returncode}, stdout={stdout!r}) — "
        f"cannot determine reachability"
    )


def _repo_from_image(image_name: str) -> str:
    """Best-effort repo_full for log messages (never raises)."""
    try:
        return parse_local_ref((image_name or "").strip())[0]
    except ValueError:
        return image_name or "?"


def _quarantine_env_from_image(image_name: str, project_root=None) -> dict:
    """Recover the full quarantine env for the repo this image belongs to, from
    the on-disk policy file.

    Used when the process env lacks the quarantine vars — a direct `run_e2e
    --resume-trial` or a manual `run_milestone` don't inject q_env the way
    run_all does, and without this the mirror-domain poison + registry deny would
    silently not apply (F2 de-harden). The signal is a DISK FACT (does the
    image's repo have a quarantine_configs/<repo>.yaml), not a propagated env
    var, so it survives env loss. A repo with no config recovers {} and stays
    unprotected (parity preserved). Docker repo names are lowercase while config
    filenames may not be (e.g. BurntSushi), so match case-insensitively.
    Handles both the swe-milestone/ scheme and legacy pre-v1.0 names (resumed
    old trials replay recorded image names verbatim).
    """
    try:
        repo_lower, _ = parse_local_ref((image_name or "").strip())
    except ValueError:
        return {}
    repo_lower = repo_lower.lower()
    root = Path(project_root) if project_root else Path(__file__).resolve().parent.parent.parent
    conf_dir = root / "quarantine_configs"
    if not conf_dir.is_dir():
        return {}
    match = next(
        (p.stem for p in sorted(conf_dir.glob("*.yaml")) if p.stem.lower() == repo_lower),
        None,
    )
    if not match:
        return {}
    return load_quarantine_env(match, root)


def _recover_quarantine_env(repo_name, image_name: str, project_root=None) -> dict:
    """Recover the full quarantine env from the repo's on-disk policy, for the
    env-less launch paths (direct run_e2e --resume-trial / manual run_milestone)
    that don't inject it like run_all does.

    Prefer the AUTHORITATIVE repo_name passed by the caller (a known fact) over
    parsing the image name (a fragile signal that misparses a registry-prefixed
    image and would silently leave a policy'd repo unprotected — F2-c). Match
    case-insensitively (config filenames may be mixed-case, e.g. BurntSushi,
    while docker repos are lowercase). Returns {} for a non-quarantine repo
    (parity preserved).
    """
    root = Path(project_root) if project_root else Path(__file__).resolve().parent.parent.parent
    conf_dir = root / "quarantine_configs"
    if repo_name and conf_dir.is_dir():
        stems = {p.stem.lower(): p.stem for p in conf_dir.glob("*.yaml")}
        match = stems.get(str(repo_name).strip().lower())
        if match:
            return load_quarantine_env(match, root)
    # Fallback: parse the image (only when repo_name is absent or unmatched).
    return _quarantine_env_from_image(image_name, project_root)


class ContainerSetup:
    """Docker container initialization with fakeroot user and Claude credentials."""

    def __init__(
        self,
        container_name: str,
        image_name: str,
        workdir: str = "/testbed",
        agent_name: str = "claude-code",
        e2e_workspace_path: Optional[Path] = None,
        agent_framework_name: str = "claude-code",
        reasoning_effort: Optional[str] = None,
        repo_name: Optional[str] = None,
    ):
        """Initialize container setup.

        Args:
            container_name: Name for the Docker container
            image_name: Docker image to use
            workdir: Working directory inside container (default: /testbed)
            agent_name: Git user name for agent commits (default: claude)
            e2e_workspace_path: Path to mount as /e2e_workspace (for E2E mode)
            agent_framework_name: Agent framework to use (default: claude-code)
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
        self.repo_name = repo_name

        # F2: run_all injects the quarantine env into the worker subprocess; a
        # direct run_e2e --resume-trial or a manual run_milestone does NOT.
        # Recover it from the repo's on-disk policy HERE (in __init__), BEFORE
        # start_container reads it for the offline -e flags and before
        # lock_network/verify read it. Skip when EVOCLAW_QUARANTINE is already
        # present (run_all path — leave it so a canary env override still applies)
        # or when EVOCLAW_UNPROTECTED is set (operator explicitly wants an open
        # baseline). Uses the authoritative repo_name, not a fragile image parse.
        if not os.environ.get("EVOCLAW_QUARANTINE") and not os.environ.get("EVOCLAW_UNPROTECTED"):
            _recovered = _recover_quarantine_env(repo_name, image_name)
            if _recovered:
                os.environ.update(_recovered)
                logger.info(
                    f"Quarantine env recovered from policy for "
                    f"'{repo_name or _repo_from_image(image_name)}' (env-less launch path)"
                )

    def get_agent_mounts(self) -> list[str]:
        """Return Docker volume mount arguments for the agent.

        Delegates to the agent framework for agent-specific mounts.

        Returns:
            List of -v arguments for docker run
        """
        return self._framework.get_container_mounts()

    def get_agent_env_vars(self) -> list[str]:
        """Return Docker environment variable arguments for the agent.

        Delegates to the agent framework for agent-specific env vars.

        Returns:
            List of -e arguments for docker run
        """
        return self._framework.get_container_env_vars()

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
        '/root/.m2',             # Maven local repo (fakeroot needs rw under the quarantine maven.repo.local redirect)
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

    def get_init_script(self) -> str:
        """Return Python init script for container setup.

        Combines base initialization with agent-specific initialization.
        The base script sets up fakeroot user, sudo, git config.
        The agent-specific script sets up credentials, tools, etc.

        Returns:
            Combined Python script as a string
        """
        base_script = self._get_base_init_script()
        agent_script = self._framework.get_container_init_script(self.agent_name)

        return f"""{base_script}

# === Agent-specific initialization ===
{agent_script}
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
            "--pull=never",  # hermetic eval: image must already be local (spec §3.3a)
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
        # that CDN_CIDR_RANGES would otherwise accept wholesale. Overlap
        # matching (not string equality): the builtin Cloudflare accept is
        # 104.16.0.0/13 while a policy may deny 104.16.0.0/12 — equality would
        # leave the /13 accepted and the registry reachable. LLM paths survive
        # on other ranges (Vertex = Google, api.anthropic.com = Anthropic ASN).
        _deny_cidrs = [c.strip() for c in os.environ.get("EVOCLAW_DENY_CIDRS", "").split(",") if c.strip()]
        if _deny_cidrs:
            logger.warning(f"EVOCLAW_DENY_CIDRS active — excluding CDN ranges overlapping: {sorted(_deny_cidrs)}")
        for cidr in CDN_CIDR_RANGES:
            if _deny_cidrs and cidr_overlaps_any(cidr, _deny_cidrs):
                logger.warning(f"  CDN range {cidr} overlaps a denied CIDR — not accepted")
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
        # Mirror domains (go proxies) are poisoned only under quarantine so a
        # non-quarantine/baseline container keeps working go fetches (#4).
        _quarantine = bool(os.environ.get("EVOCLAW_QUARANTINE"))
        _poison = _poison_domain_list(_quarantine)
        hosts_lines = "\n".join(f"0.0.0.0 {d}" for d in _poison)
        hosts_script = f"""
# Append code-hosting blocks to /etc/hosts
cat >> /etc/hosts << 'HOSTS_EOF'

# === Network lockdown: code hosting sites blocked ===
{hosts_lines}
HOSTS_EOF

# Lock permissions so non-root cannot edit
chmod 644 /etc/hosts
echo "/etc/hosts poisoned with {len(_poison)} domains"
"""
        subprocess.run(
            ["docker", "exec", self.container_name, "/bin/sh", "-c", hosts_script],
            capture_output=True,
            text=True,
        )
        logger.info(f"  /etc/hosts poisoned ({len(_poison)} domains)")

        # --- Step 5: Set Go env vars ---
        # GOPROXY=off under go quarantine (the proxy is the answer channel) OR
        # any quarantine (mirror domains are hosts-poisoned above, so a bare
        # proxy URL would resolve to 0.0.0.0 and every fetch would fail). A
        # non-quarantine container keeps the sanctioned proxy. Shell profiles
        # override docker -e, so this MUST be written here (#4).
        _goproxy = goproxy_value(
            go_offline=bool(os.environ.get("EVOCLAW_GO_OFFLINE")),
            quarantine_active=_quarantine,
        )
        go_env_script = f"""
# Configure Go module fetching (quarantine-aware)
cat >> /etc/environment << 'EOF'
GOPROXY={_goproxy}
GONOSUMCHECK=*
GONOSUMDB=*
EOF

# Also set for fakeroot's shell profile
mkdir -p /home/fakeroot
cat >> /home/fakeroot/.bashrc << 'EOF'
export GOPROXY={_goproxy}
export GONOSUMCHECK=*
export GONOSUMDB=*
EOF
echo "Go env vars configured (GOPROXY={_goproxy})"
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

    def _url_reachable_in_container(self, url: str, user: str = "fakeroot") -> bool:
        """True if `url`'s host:port is reachable (TCP) from inside the container.

        Uses python3 (guaranteed present by init) rather than curl, so the
        probe is real even on images without curl (e.g. the scikit base). A
        successful TCP connect proves the channel is open — that is exactly
        what the iptables IP/CIDR deny is meant to prevent, so a full HTTP
        round-trip is unnecessary. Only the first IPv4 address is tried, with
        a short timeout, so a blocked (DROP'd) host fails fast instead of
        walking every CDN anycast IP. Mirrors docs/quarantine.md.
        """
        probe_script = (
            "import sys, socket\n"
            "from urllib.parse import urlparse\n"
            "u = urlparse(sys.argv[1])\n"
            "port = u.port or (443 if u.scheme == 'https' else 80)\n"
            "try:\n"
            "    infos = socket.getaddrinfo(u.hostname, port, socket.AF_INET, socket.SOCK_STREAM)\n"
            "    fam, typ, proto, _, sa = infos[0]\n"
            "    s = socket.socket(fam, typ, proto)\n"
            "    s.settimeout(4)\n"
            "    s.connect(sa)\n"
            "    s.close()\n"
            "    print('REACH')\n"
            "except Exception:\n"
            "    print('BLOCK')\n"
        )
        try:
            result = subprocess.run(
                [
                    "docker", "exec", "--user", user,
                    "-e", f"HOME=/home/{user}" if user != "root" else "HOME=/root",
                    self.container_name, "python3", "-c", probe_script, url,
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired as e:
            # The probe (docker exec + in-container DNS/connect) outran 15s.
            # Treat as indeterminate and fail closed — never read a hung probe
            # as 'blocked' (#11).
            raise RuntimeError(
                f"network probe timed out after 15s for {url} — cannot verify lockdown"
            ) from e
        return _interpret_probe(result.returncode, result.stdout)

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
        # A denied registry is exempt from the reachability assertion ONLY if the
        # policy DECLARES it un-CIDR-blockable (EVOCLAW_FIREWALL_EXEMPT — e.g.
        # proxy.golang.org shares Vertex's Google range, defended by /etc/hosts
        # poison + GOPROXY=off). Everything else MUST verify unreachable. The old
        # runtime "resolved IPs all fall in a still-ACCEPTed CDN range" inference
        # is gone: a typo'd/omitted deny_cidr made a normal registry look exempt
        # and silently pass (fail-open, #2b). A missing deny_cidr is now caught up
        # front by the coverage gate; a wrong one is caught by this assertion.
        # Only honor exempts that are in the code-level whitelist (defense in
        # depth: the gate rejects illegal exempts at launch, but --unprotected
        # bypasses the gate — verify must not skip a CIDR-blockable registry just
        # because the policy mislabeled it exempt) (F1).
        _exempt = {
            d.strip().lower()
            for d in os.environ.get("EVOCLAW_FIREWALL_EXEMPT", "").split(",")
            if d.strip()
        } & FIREWALL_EXEMPTABLE_DOMAINS
        _verified = 0
        for host in _deny_domains:
            if host.lower() in _exempt:
                logger.warning(
                    f"  Quarantine: '{host}' firewall-exempt (declared "
                    f"un-CIDR-blockable) — defended by /etc/hosts poison + "
                    f"offline switch (residual until SNI proxy)."
                )
                continue
            if self._url_reachable_in_container(f"https://{host}"):
                raise RuntimeError(
                    f"Quarantine verification failed: denied host '{host}' is still "
                    f"reachable — EVOCLAW_DENY_DOMAINS/EVOCLAW_DENY_CIDRS not effective"
                )
            _verified += 1
        if _verified:
            logger.info(f"  Quarantine verified: {_verified} denied host(s) unreachable")

        # Quarantine: probe the exact registry URLs of the observed cheats
        # (e.g. the self-crate on static.crates.io, the -sources.jar on Maven
        # Central). ANY successful connect — even an HTTP error response —
        # means the answer-fetch channel is open; the deny must make connects
        # fail. Uses python3 (guaranteed by init), not curl, so the check is
        # real even on images that ship no curl (e.g. the scikit base).
        _verify_urls = [u.strip() for u in os.environ.get("EVOCLAW_VERIFY_FETCH_URLS", "").split(",") if u.strip()]
        for url in _verify_urls:
            if self._url_reachable_in_container(url):
                raise RuntimeError(
                    f"Quarantine verification failed: answer-fetch URL still "
                    f"reachable: {url}"
                )
        if _verify_urls:
            logger.info(f"  Quarantine verified: {len(_verify_urls)} answer-fetch URL(s) blocked")

        # Quarantine: fail closed if the image's pre-baked package cache
        # contains the repo's own artifacts at a post-baseline version (the
        # offline closure must not be able to serve the answer — the cache
        # analogue of the pip wheelhouse_forbid audit).
        _forbid_globs = [g.strip() for g in os.environ.get("EVOCLAW_CACHE_FORBID_GLOBS", "").split(",") if g.strip()]
        for glob_pat in _forbid_globs:
            audit = subprocess.run(
                [
                    "docker", "exec", self.container_name,
                    "/bin/sh", "-c", f"ls -d {glob_pat} 2>/dev/null | head -5",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if audit.stdout.strip():
                raise RuntimeError(
                    f"Quarantine cache audit failed: forbidden artifact(s) in the "
                    f"image cache match '{glob_pat}':\n{audit.stdout.strip()}\n"
                    f"The offline cache could serve the repo's own target-version "
                    f"source. Rebuild/clean the image before running."
                )
        if _forbid_globs:
            logger.info(f"  Quarantine cache audit passed: {len(_forbid_globs)} forbid glob(s) matched nothing")

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
