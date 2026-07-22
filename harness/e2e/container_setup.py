"""Container setup utilities for agent execution.

This module provides shared container initialization logic used by both
run_milestone.py (single milestone mode) and orchestrator.py (E2E mode).
"""

import json
import logging
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

from harness.e2e.agents import AgentFramework, get_agent_framework
from harness.e2e.image_version import parse_local_ref
from harness.e2e.quarantine import (
    GO_OFFLINE_FILE_PROXY,
    GO_OFFLINE_SHELL_ENV,
    FIREWALL_EXEMPTABLE_DOMAINS,
    QUARANTINE_MIRROR_DOMAINS,
    cidr_overlaps_any,
    goproxy_value,
    load_quarantine_env,
    normalize_maven_plugin_probes,
)
from harness.e2e import sni_tunnel as sni_tunnel_module
from harness.e2e.sni_tunnel import tunnel_plan

# Port the SNI-tunnel sidecar listens on. 443 so the agent container can reach
# it at endpoint:443 (mapped to the sidecar IP in /etc/hosts) with no port
# rewrite — the sidecar runs as root in its own container, so binding 443 is
# free. See _ensure_sni_sidecar + harness/e2e/sni_tunnel.py.
SNI_SIDECAR_PORT = 443
# The sidecar reuses the repo's agent image when it ships python3; images
# without it (Java/Rust bases) fall back to this stdlib-only python image.
SNI_SIDECAR_FALLBACK_IMAGE = "python:3.12-slim"
from harness.e2e.runtime_policy_binding import (
    RUNTIME_POLICY_ENV_KEYS,
    RuntimePolicyBinding,
    RuntimePolicyBindingError,
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
    "chatgpt.com",  # Codex ChatGPT OAuth model endpoint
    "generativelanguage.googleapis.com",
    # Vertex AI direct (gemini-cli + claude-code native Vertex): the aiplatform
    # endpoint + OAuth token refresh for ADC. Only reachable when SWE_MILESTONE_VERTEX
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


def _configured_cache_paths() -> list[str]:
    """Return validated, de-duplicated quarantine cache paths from the env.

    ``closure.cache_paths`` is exported as JSON by ``load_quarantine_env``.
    Keep the Maven-specific variable as a backward-compatible fallback for
    workers launched with an older policy env.  Invalid or dangerous paths fail
    closed: container setup must never recursively adjust ``/`` or a relative
    host-controlled path.
    """
    raw = os.environ.get("SWE_MILESTONE_CACHE_PATHS", "").strip()
    paths: list[str] = []
    if raw:
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "SWE_MILESTONE_CACHE_PATHS must be a JSON list of absolute paths"
            ) from exc
        if not isinstance(decoded, list) or not all(isinstance(path, str) for path in decoded):
            raise RuntimeError(
                "SWE_MILESTONE_CACHE_PATHS must be a JSON list of absolute paths"
            )
        paths.extend(decoded)

    maven_repo = os.environ.get("SWE_MILESTONE_MAVEN_REPO_LOCAL", "").strip()
    if maven_repo:
        paths.append(maven_repo)

    normalized: list[str] = []
    for path in paths:
        clean = os.path.normpath(path.strip())
        if not clean.startswith("/") or clean == "/":
            raise RuntimeError(
                f"Invalid quarantine cache path {path!r}: expected an absolute path below /"
            )
        if clean not in normalized:
            normalized.append(clean)
    return normalized


def inspect_docker_image_id(image_or_container: str, *, container: bool = False) -> str:
    """Return a normalized full Docker image ID or fail closed.

    Image tags are mutable; callers persist this digest and use it to bind a
    trial/snapshot to the exact bytes that actually backed the container.
    """
    if container:
        command = ["docker", "container", "inspect", image_or_container, "--format", "{{.Image}}"]
    else:
        command = ["docker", "image", "inspect", image_or_container, "--format", "{{.Id}}"]
    result = subprocess.run(command, capture_output=True, text=True)
    value = (result.stdout or "").strip().removeprefix("sha256:")
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{64}", value):
        detail = (result.stderr or result.stdout or "missing image ID").strip()
        kind = "container" if container else "image"
        raise RuntimeError(f"Cannot inspect {kind} image ID for {image_or_container}: {detail}")
    return value


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
        agent_version: Optional[str] = None,
        repo_name: Optional[str] = None,
        runtime_policy_binding: Optional[RuntimePolicyBinding] = None,
    ):
        """Initialize container setup.

        Args:
            container_name: Name for the Docker container
            image_name: Docker image to use
            workdir: Working directory inside container (default: /testbed)
            agent_name: Git user name for agent commits (default: claude)
            e2e_workspace_path: Path to mount as /e2e_workspace (for E2E mode)
            agent_framework_name: Agent framework to use (default: claude-code)
            agent_version: Optional agent CLI version selector.
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
        if agent_version:
            framework_kwargs["agent_version"] = agent_version
        self._framework: AgentFramework = get_agent_framework(agent_framework_name, **framework_kwargs)
        self.repo_name = repo_name
        self.runtime_policy_binding = runtime_policy_binding
        self.resolved_image_id: Optional[str] = None
        # SNI-pinned tunnel sidecar (anti-cheat method A). Started when this
        # repo's quarantine would CIDR-block the trial's LLM endpoint (a
        # Cloudflare-fronted host it shares with a denied registry). Both stay
        # None when not needed. See harness/e2e/sni_tunnel.py + docs/quarantine.md.
        self._sni_tunnel_host: Optional[str] = None
        self._sni_tunnel_sidecar_ip: Optional[str] = None

        if (
            self.runtime_policy_binding is not None
            and self.runtime_policy_binding.repo_name != self.repo_name
        ):
            raise RuntimePolicyBindingError(
                "container runtime policy binding mismatch: "
                f"expected {self.repo_name!r}, got "
                f"{self.runtime_policy_binding.repo_name!r}"
            )

        # F2: run_all injects the quarantine env into the worker subprocess; a
        # direct run_e2e --resume-trial or a manual run_milestone does NOT.
        # Recover it from the repo's on-disk policy HERE (in __init__), BEFORE
        # start_container reads it for the offline -e flags and before
        # lock_network/verify read it. Skip when SWE_MILESTONE_QUARANTINE is already
        # present (run_all path — leave it so a canary env override still applies)
        # or when SWE_MILESTONE_UNPROTECTED is set (operator explicitly wants an open
        # baseline). Uses the authoritative repo_name, not a fragile image parse.
        if self.runtime_policy_binding is not None:
            self._verify_bound_runtime_policy_env()
        elif not os.environ.get("SWE_MILESTONE_QUARANTINE") and not os.environ.get("SWE_MILESTONE_UNPROTECTED"):
            _recovered = _recover_quarantine_env(repo_name, image_name)
            if _recovered:
                os.environ.update(_recovered)
                logger.info(
                    f"Quarantine env recovered from policy for "
                    f"'{repo_name or _repo_from_image(image_name)}' (env-less launch path)"
                )

    def _verify_bound_runtime_policy_env(self) -> None:
        """Fail closed if process env no longer matches the trial binding."""
        binding = getattr(self, "runtime_policy_binding", None)
        if binding is None:
            return
        expected = dict(binding.env)
        actual = {
            key: os.environ[key]
            for key in RUNTIME_POLICY_ENV_KEYS
            if key in os.environ
        }
        if actual != expected:
            raise RuntimePolicyBindingError(
                "process runtime policy environment drifted from trial binding: "
                f"expected={expected}, actual={actual}"
            )
        unprotected = bool(os.environ.get("SWE_MILESTONE_UNPROTECTED"))
        if unprotected != (binding.mode == "unprotected"):
            raise RuntimePolicyBindingError(
                "SWE_MILESTONE_UNPROTECTED disagrees with runtime policy mode "
                f"{binding.mode!r}"
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
        return self._framework.get_effective_container_env_vars()

    def get_agent_version(self, *, verify_requested: bool = False) -> Optional[str]:
        """Read and normalize the agent CLI version inside the container.

        When ``verify_requested`` is true, an unavailable or mismatched exact
        version is fatal. Channel selectors such as ``stable`` and ``latest``
        are validated by the installer and accept the numeric version it chose.
        """
        command = self._framework.get_version_command()
        requested = self._framework.get_requested_version()
        if not command:
            if verify_requested and requested:
                raise RuntimeError(
                    f"Agent framework {self._framework.FRAMEWORK_NAME!r} does not support version detection"
                )
            return None

        result = subprocess.run(
            ["docker", "exec", self.container_name, *command],
            capture_output=True,
            text=True,
        )
        output = (result.stdout or result.stderr or "").strip()
        actual = self._framework.parse_version_output(output) if result.returncode == 0 else None

        if verify_requested and requested:
            if not actual:
                raise RuntimeError(
                    f"Could not detect {self._framework.FRAMEWORK_NAME} version after requesting "
                    f"{requested!r}: {output or f'exit code {result.returncode}'}"
                )
            if not self._framework.version_matches_request(actual):
                raise RuntimeError(
                    f"{self._framework.FRAMEWORK_NAME} version mismatch: "
                    f"requested {requested!r}, found {actual!r}"
                )
        return actual

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
        configured_cache_paths = _configured_cache_paths()
        go_offline = bool(os.environ.get("SWE_MILESTONE_GO_OFFLINE"))
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
        '/usr/local/lib/node_modules',  # Global npm modules
        '/root/.npm',            # npm cache
        '/root/.cache',          # General cache (pip, etc.)
        '/root/.m2',             # Maven local repo (fakeroot needs rw under the quarantine maven.repo.local redirect)
    ]
    go_offline = {go_offline!r}
    if not go_offline:
        # Legacy/open Go workflows may still need to install into GOPATH. In a
        # sealed Go trial the toolchain and canonical proxy are immutable and
        # writable build/module caches live under fakeroot's home instead.
        toolchain_dirs.extend(['/usr/local/go', '/go', '/root/go'])

    # The policy's closure.cache_paths are the authoritative caches actually
    # consumed under quarantine.  A cache can itself be readable while an
    # ancestor (notably /root at 0700) blocks fakeroot.  Grant only traverse on
    # ancestors; the declared cache/toolchain directory itself also gets read.
    # Never add ancestor read/write. Apply this to all known toolchain dirs too,
    # so a future /root-based Cargo/npm cache cannot repeat Maven's failure.
    configured_cache_paths = {configured_cache_paths!r}
    access_paths = list(dict.fromkeys(toolchain_dirs + configured_cache_paths))
    fake_groups = set(os.getgrouplist(fake_user.pw_name, gid))
    adjusted_ancestors = set()
    for access_path in access_paths:
        path = Path(access_path)
        if not path.exists():
            continue
        candidates = [path] + list(path.parents)
        for candidate in candidates:
            if candidate == Path('/') or not candidate.is_dir():
                continue
            st = candidate.stat()
            mode = stat.S_IMODE(st.st_mode)
            if st.st_uid == uid:
                required = stat.S_IXUSR
                if candidate == path:
                    required |= stat.S_IRUSR
            elif st.st_gid in fake_groups:
                required = stat.S_IXGRP
                if candidate == path:
                    required |= stat.S_IRGRP
            else:
                required = stat.S_IXOTH
                if candidate == path:
                    required |= stat.S_IROTH
            if (mode & required) != required:
                os.chmod(candidate, mode | required)
                adjusted_ancestors.add(str(candidate))
    if adjusted_ancestors:
        print(
            "Granted fakeroot traverse-only access to cache/toolchain ancestors: "
            + ", ".join(sorted(adjusted_ancestors))
        )

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

    if go_offline:
        immutable_go_paths = [
            Path('/usr/local/go'),
            Path('/go/pkg/mod/cache/download'),
        ]
        for immutable in immutable_go_paths:
            if not immutable.exists():
                raise RuntimeError(f"sealed Go path is missing: {{immutable}}")
            subprocess.run(
                ['chown', '-R', '0:0', str(immutable)], check=True,
                capture_output=True, text=True,
            )
            # Canonical toolchain/proxy bytes must survive every model and
            # milestone unchanged. Go writes locks/extracted modules only to
            # the separate fakeroot-owned GOMODCACHE below.
            for root, dirs, files in os.walk(immutable):
                root_mode = stat.S_IMODE(os.stat(root).st_mode)
                os.chmod(root, (root_mode | 0o555) & ~0o222)
                for name in files:
                    item = os.path.join(root, name)
                    if not os.path.islink(item):
                        item_mode = stat.S_IMODE(os.stat(item).st_mode)
                        os.chmod(item, (item_mode | 0o444) & ~0o222)
        for parent in [
            Path('/go'), Path('/go/pkg'), Path('/go/pkg/mod'), Path('/go/pkg/mod/cache')
        ]:
            if parent.exists():
                os.chown(parent, 0, 0)
                os.chmod(parent, 0o755)

        for writable in [
            Path('/home/fakeroot/.cache/evoclaw-gomodcache'),
            Path('/home/fakeroot/.cache/go-build'),
            Path('/home/fakeroot/.cache/evoclaw-goenv'),
            Path('/home/fakeroot/go/bin'),
        ]:
            writable.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ['chown', '-R', f'{{uid}}:{{gid}}', str(writable)], check=True,
                capture_output=True, text=True,
            )
            os.chmod(writable, 0o755)
        marker = Path('/var/lib/evoclaw/go-runtime-sealed-v1')
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('immutable=/usr/local/go,/go/pkg/mod/cache/download\\n')
        os.chown(marker, 0, 0)
        os.chmod(marker, 0o444)
        print("Sealed Go toolchain/file proxy; initialized writable per-agent caches")

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
        self._verify_bound_runtime_policy_env()

        # Check for existing container
        if self.container_exists():
            if force:
                logger.info(f"Removing existing container {self.container_name}...")
                subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True)
            else:
                if self.is_running():
                    logger.info(f"Container {self.container_name} already running")
                    self.resolved_image_id = inspect_docker_image_id(
                        self.container_name, container=True
                    )
                    self.verify_runtime_environment()
                    return
                else:
                    logger.info(f"Starting existing container {self.container_name}...")
                    subprocess.run(["docker", "start", self.container_name], check=True)
                    self.resolved_image_id = inspect_docker_image_id(
                        self.container_name, container=True
                    )
                    self.verify_runtime_environment()
                    return

        # Verify image exists
        self.resolved_image_id = inspect_docker_image_id(self.image_name)
        immutable_image = f"sha256:{self.resolved_image_id}"

        logger.info(f"Launching container {self.container_name} from {self.image_name}...")

        # Build docker run command
        # Use --init to properly reap zombie child processes (e.g., plugin processes)
        # --cap-add=NET_ADMIN: required for iptables-based network lockdown
        # --sysctl net.ipv6.conf.all.disable_ipv6=1: prevent IPv6 bypass of iptables rules
        docker_options = [
            "docker",
            "run",
            "--pull=never",  # hermetic eval: image must already be local (docs/versioning.md)
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
        # Resolve the mutable user-facing tag once, then run the immutable
        # digest. This closes the inspect -> docker run retag race and makes the
        # actual agent base independently auditable.
        cmd = docker_options + [immutable_image, "tail", "-f", "/dev/null"]

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

        # Fresh and resume paths share the same data-driven runtime gate.
        self.verify_runtime_environment()

        logger.info(f"Container {self.container_name} launched and initialized.")

    def _repair_existing_quarantine_cache_access(self) -> None:
        """Repair cache permissions when resuming a pre-fix container.

        Existing containers bypass the full initialization script.  Without a
        targeted repair, an old /root=0700 container would remain broken forever
        even when resumed with the fixed harness.  Only policy-declared cache
        paths are touched; Maven's local repository additionally needs recursive
        ownership/write because reactor builds install artifacts into it.
        """
        cache_paths = _configured_cache_paths()
        if not cache_paths:
            return
        maven_repo = os.environ.get("SWE_MILESTONE_MAVEN_REPO_LOCAL", "").strip()
        script = f'''
import os
import pwd
import stat
import subprocess
from pathlib import Path

cache_paths = {cache_paths!r}
maven_repo = {maven_repo!r}
fake_user = pwd.getpwnam("fakeroot")
uid, gid = fake_user.pw_uid, fake_user.pw_gid
fake_groups = set(os.getgrouplist(fake_user.pw_name, gid))

if maven_repo and Path(maven_repo).is_dir():
    subprocess.run(["chown", "-R", f"{{uid}}:{{gid}}", maven_repo], check=True)
    subprocess.run(["chmod", "-R", "u+rwX", maven_repo], check=True)

adjusted = []
for raw_path in cache_paths:
    path = Path(raw_path)
    if not path.exists():
        continue
    for candidate in [path] + list(path.parents):
        if candidate == Path("/") or not candidate.is_dir():
            continue
        st = candidate.stat()
        mode = stat.S_IMODE(st.st_mode)
        if st.st_uid == uid:
            required = stat.S_IXUSR
            if candidate == path:
                required |= stat.S_IRUSR
        elif st.st_gid in fake_groups:
            required = stat.S_IXGRP
            if candidate == path:
                required |= stat.S_IRGRP
        else:
            required = stat.S_IXOTH
            if candidate == path:
                required |= stat.S_IROTH
        if (mode & required) != required:
            os.chmod(candidate, mode | required)
            adjusted.append(str(candidate))
print("cache permission repair complete: " + ", ".join(sorted(set(adjusted))))
'''
        result = subprocess.run(
            ["docker", "exec", self.container_name, "python3", "-c", script],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown permission error"
            raise RuntimeError(
                f"Failed to repair quarantine cache permissions: {detail}"
            )
        logger.info(result.stdout.strip() or "Quarantine cache permission repair complete")

    def _harden_go_offline_runtime(self) -> None:
        """Seal canonical Go input while leaving per-agent caches writable."""
        if not os.environ.get("SWE_MILESTONE_GO_OFFLINE"):
            return
        marker = subprocess.run(
            [
                "docker", "exec", self.container_name, "test", "-f",
                "/var/lib/evoclaw/go-runtime-sealed-v1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if marker.returncode != 0:
            # Legacy containers made these trees writable before the immutable
            # split existed. Never seal unknown bytes in place: compare every
            # canonical regular file/symlink to a pristine container launched
            # from this trial's exact image ID, then migrate permissions.
            digest_script = r'''
set -eu
for root in /usr/local/go /go/pkg/mod/cache/download; do
  find "$root" \( -type f -o -type l \) -print0
done | sort -z | while IFS= read -r -d '' path; do
  # Go's old shared GOMODCACHE may have left zero-byte download locks.
  # They contain no dependency bytes and are ignored by module resolution.
  # Any symlink or non-empty .lock remains part of the digest and fails shut.
  if test -f "$path" && test ! -L "$path" && test ! -s "$path" &&
     case "$path" in *.lock) true;; *) false;; esac; then
    continue
  elif test -L "$path"; then
    printf 'link\t%s\t%s\n' "$path" "$(readlink "$path")"
  else
    printf 'file\t%s\t' "$path"
    sha256sum "$path" | awk '{print $1}'
  fi
done | sha256sum | awk '{print $1}'
'''
            current = subprocess.run(
                [
                    "docker", "exec", self.container_name, "bash", "-c",
                    digest_script,
                ],
                capture_output=True,
                text=True,
                timeout=900,
            )
            image_id = inspect_docker_image_id(self.container_name, container=True)
            pristine = subprocess.run(
                [
                    "docker", "run", "--rm", "--pull=never", "--network", "none",
                    "--entrypoint", "bash", f"sha256:{image_id}", "-c",
                    digest_script,
                ],
                capture_output=True,
                text=True,
                timeout=900,
            )
            current_digest = (current.stdout or "").strip()
            pristine_digest = (pristine.stdout or "").strip()
            if (
                current.returncode != 0
                or pristine.returncode != 0
                or not re.fullmatch(r"[0-9a-f]{64}", current_digest)
                or current_digest != pristine_digest
            ):
                detail = "\n".join(
                    part
                    for part in (
                        current.stderr,
                        pristine.stderr,
                        f"container={current_digest or '?'} pristine={pristine_digest or '?'}",
                    )
                    if part
                )
                raise RuntimeError(
                    "Legacy Go runtime/cache differs from its pinned base image; "
                    "refusing resume instead of blessing possibly model-mutated bytes:\n"
                    + detail[-4000:]
                )
        script = r'''
set -eu
marker=/var/lib/evoclaw/go-runtime-sealed-v1
if test ! -f "$marker"; then
  for path in /usr/local/go /go/pkg/mod/cache/download; do
    test -d "$path"
    chown -R 0:0 "$path"
    chmod -R a+rX,a-w "$path"
  done
  for parent in /go /go/pkg /go/pkg/mod /go/pkg/mod/cache; do
    test -d "$parent"
    chown root:root "$parent"
    chmod 0755 "$parent"
  done
  install -d -o fakeroot -g 0 -m 0755 \
    /home/fakeroot/.cache/evoclaw-gomodcache \
    /home/fakeroot/.cache/go-build \
    /home/fakeroot/.cache/evoclaw-goenv \
    /home/fakeroot/go/bin
  install -d -o root -g root -m 0755 /var/lib/evoclaw
  printf '%s\n' 'immutable=/usr/local/go,/go/pkg/mod/cache/download' > "$marker"
  chown root:root "$marker"
  chmod 0444 "$marker"
fi
install -d -o root -g root -m 0755 /etc/evoclaw
cat > __GO_SHELL_ENV__ <<'EVOCLAW_GO_RUNTIME'
export GOPROXY=__GO_FILE_PROXY__
export GONOPROXY=none
export GOSUMDB=off
export GOTOOLCHAIN=local
export GOFLAGS=-buildvcs=false
export GOENV=/home/fakeroot/.cache/evoclaw-goenv/env
export GOMODCACHE=/home/fakeroot/.cache/evoclaw-gomodcache
export GOCACHE=/home/fakeroot/.cache/go-build
export GOBIN=/home/fakeroot/go/bin
export PATH=/home/fakeroot/go/bin:/usr/local/go/bin:/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export BASH_ENV=__GO_SHELL_ENV__
EVOCLAW_GO_RUNTIME
chown root:root __GO_SHELL_ENV__
chmod 0444 __GO_SHELL_ENV__
'''
        script = script.replace("__GO_FILE_PROXY__", GO_OFFLINE_FILE_PROXY)
        script = script.replace("__GO_SHELL_ENV__", GO_OFFLINE_SHELL_ENV)
        result = subprocess.run(
            ["docker", "exec", self.container_name, "sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "hardening failed").strip()
            raise RuntimeError(f"Failed to seal Go runtime/cache: {detail}")

    def _prepare_go_disposable_dirs(self, *, reset_module_cache: bool) -> None:
        """Symlink-safely restore model-owned Go cache/output directories.

        ``reset_module_cache`` is kept as the public compatibility switch, but
        a requested reset covers every disposable Go output and user GOENV.
        Leaving GOCACHE, GOBIN, or ``go env -w`` state populated would otherwise
        leak model-produced state into the next invocation just as surely as
        leaving GOMODCACHE populated.
        """
        if not os.environ.get("SWE_MILESTONE_GO_OFFLINE"):
            return
        reset = "1" if reset_module_cache else "0"
        script = r'''
set -eu
reset_disposable=$1
home=/home/fakeroot
module=/home/fakeroot/.cache/evoclaw-gomodcache
if test -L "$home" || test ! -d "$home"; then
  echo "fakeroot home is missing or a symlink: $home" >&2
  exit 1
fi
# The model owns these immediate parents and may replace either with a symlink.
# Repair the parent before touching a child path; checking only the child would
# let e.g. /home/fakeroot/go -> /usr/local/go redirect a root cleanup into the
# sealed toolchain.
for parent in "$home/.cache" "$home/go"; do
  if test -L "$parent" || test ! -d "$parent"; then
    rm -rf -- "$parent"
    install -d -o fakeroot -g 0 -m 0755 "$parent"
  else
    chown fakeroot:0 "$parent"
    chmod 0755 "$parent"
  fi
done
for path in "$module" /home/fakeroot/.cache/go-build \
  /home/fakeroot/.cache/evoclaw-goenv /home/fakeroot/go/bin; do
  if test -L "$path" || test ! -d "$path"; then
    rm -rf -- "$path"
    install -d -o fakeroot -g 0 -m 0755 "$path"
  else
    chown fakeroot:0 "$path"
    chmod 0755 "$path"
  fi
  if test "$reset_disposable" = 1; then
    find "$path" -xdev -mindepth 1 -delete
  fi
done
'''
        result = subprocess.run(
            [
                "docker", "exec", self.container_name, "sh", "-c", script,
                "evoclaw-go-disposable", reset,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "cache repair failed").strip()
            raise RuntimeError(f"Cannot prepare disposable Go directories: {detail}")

    def _verify_go_offline_runtime(self) -> None:
        """Fail closed on toolchain/proxy drift or an unusable writable COW cache."""
        if not os.environ.get("SWE_MILESTONE_GO_OFFLINE"):
            return
        expected = os.environ.get("SWE_MILESTONE_GO_TOOLCHAIN", "").removeprefix("go")
        script = r'''
set -eu
printf 'executable=%s\n' "$(command -v go)"
printf 'version=%s\n' "$(go version)"
printf 'goroot=%s\n' "$(go env GOROOT)"
printf 'gomodcache=%s\n' "$(go env GOMODCACHE)"
printf 'gocache=%s\n' "$(go env GOCACHE)"
printf 'goproxy=%s\n' "$(go env GOPROXY)"
printf 'gotoolchain=%s\n' "$(go env GOTOOLCHAIN)"
printf 'goflags=%s\n' "$(go env GOFLAGS)"
printf 'goenv=%s\n' "$(go env GOENV)"
printf 'bash_env=%s\n' "${BASH_ENV:-}"
printf 'bash_go=%s\n' "$(bash -lc 'command -v go')"
printf 'bash_goproxy=%s\n' "$(bash -lc 'go env GOPROXY')"
printf 'bash_goenv=%s\n' "$(bash -lc 'go env GOENV')"
printf 'bash_goflags=%s\n' "$(bash -lc 'go env GOFLAGS')"
printf 'golang_version=%s\n' "${GOLANG_VERSION:-}"
test -r /var/lib/evoclaw/go-runtime-sealed-v1
test ! -w /usr/local/go
test ! -w /usr/local/go/bin/go
test ! -w /go/pkg/mod/cache/download
test ! -w /go/pkg/mod/cache
test ! -w /go/pkg/mod
test -z "$(find /usr/local/go /go/pkg/mod/cache/download -type d ! -executable -print -quit)"
test -z "$(find /usr/local/go /go/pkg/mod/cache/download -type f ! -readable -print -quit)"
test -w /home/fakeroot/.cache/evoclaw-gomodcache
test -w /home/fakeroot/.cache/go-build
test -w /home/fakeroot/.cache/evoclaw-goenv
test -w /home/fakeroot/go/bin
test -r __GO_SHELL_ENV__
test ! -w __GO_SHELL_ENV__
'''
        script = script.replace("__GO_SHELL_ENV__", GO_OFFLINE_SHELL_ENV)
        env = [
            "-e", "HOME=/home/fakeroot",
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
            "-e", (
                "PATH=/home/fakeroot/go/bin:/usr/local/go/bin:/go/bin:"
                "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            ),
        ]
        if expected:
            # Legacy images may carry stale Config.Env metadata even though the
            # sealed toolchain bytes are correct. Every agent framework and this
            # verifier explicitly bind the policy version for each docker exec.
            env.extend(["-e", f"GOLANG_VERSION={expected}"])
        result = subprocess.run(
            [
                "docker", "exec", "--user", "fakeroot", *env,
                self.container_name, "sh", "-c", script,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = "\n".join(
            part for part in (result.stdout, result.stderr) if part
        ).strip()
        version_match = re.search(
            r"^version=go version go([0-9.]+)\s", output, re.MULTILINE
        )
        actual = version_match.group(1) if version_match else ""
        required = {
            "executable=/usr/local/go/bin/go",
            "goroot=/usr/local/go",
            "gomodcache=/home/fakeroot/.cache/evoclaw-gomodcache",
            "gocache=/home/fakeroot/.cache/go-build",
            f"goproxy={GO_OFFLINE_FILE_PROXY}",
            "gotoolchain=local",
            "goflags=-buildvcs=false",
            "goenv=/home/fakeroot/.cache/evoclaw-goenv/env",
            f"bash_env={GO_OFFLINE_SHELL_ENV}",
            "bash_go=/usr/local/go/bin/go",
            f"bash_goproxy={GO_OFFLINE_FILE_PROXY}",
            "bash_goenv=/home/fakeroot/.cache/evoclaw-goenv/env",
            "bash_goflags=-buildvcs=false",
        }
        missing = sorted(item for item in required if item not in output.splitlines())
        if expected and f"golang_version={expected}" not in output.splitlines():
            missing.append(f"golang_version={expected}")
        if result.returncode != 0 or missing or (expected and actual != expected):
            raise RuntimeError(
                "Sealed Go runtime verification failed"
                + (f" (expected go{expected}, found go{actual or '?'})" if expected else "")
                + (f"; missing probes: {missing}" if missing else "")
                + f":\n{output}"
            )
        logger.info("Sealed Go runtime verified: go%s, immutable local proxy", actual)

    def verify_runtime_environment(self) -> None:
        """Shared fresh/resume gate for all quarantine runtime prerequisites."""
        self._verify_bound_runtime_policy_env()
        self._repair_existing_quarantine_cache_access()
        self._harden_go_offline_runtime()
        self._prepare_go_disposable_dirs(reset_module_cache=False)
        self._verify_quarantine_cache_access()
        self._verify_maven_offline_smoke()
        self._verify_go_offline_runtime()

    def prepare_agent_invocation(self) -> None:
        """Verify shared inputs and reset disposable Go state before a model turn."""
        self._verify_bound_runtime_policy_env()
        # A model may legitimately run ``go clean -modcache`` or remove its
        # disposable caches. Repair/reset them before the verifier demands
        # writability; immutable toolchain/proxy inputs are checked afterward.
        self._prepare_go_disposable_dirs(reset_module_cache=True)
        self.verify_runtime_environment()
        if not os.environ.get("SWE_MILESTONE_GO_OFFLINE"):
            return
        logger.info("Disposable Go module cache reset before agent invocation")

    def _verify_quarantine_cache_access(self) -> None:
        """Fail fast when fakeroot cannot consume an image-baked cache."""
        cache_paths = _configured_cache_paths()
        maven_repo = os.environ.get("SWE_MILESTONE_MAVEN_REPO_LOCAL", "").strip()
        go_offline = bool(os.environ.get("SWE_MILESTONE_GO_OFFLINE"))
        for cache_path in cache_paths:
            # All caches must be non-empty, readable and traversable. Maven also
            # installs reactor artifacts into its local repo, so it must be
            # writable. The first-file probe catches a path that exists but is an
            # empty/wrong cache (the original failure otherwise looked like an
            # ordinary offline dependency miss).
            if cache_path == maven_repo:
                writable_check = ' && test -w "$1"'
            elif go_offline and cache_path.endswith("/cache/download"):
                writable_check = ' && test ! -w "$1"'
            else:
                writable_check = ""
            probe = (
                'test -d "$1" && test -r "$1" && test -x "$1"'
                + writable_check
                + ' && first=$(find "$1" -type f -print -quit 2>/dev/null)'
                + ' && test -n "$first" && test -r "$first"'
            )
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "--user",
                    "fakeroot",
                    "-e",
                    "HOME=/home/fakeroot",
                    self.container_name,
                    "/bin/sh",
                    "-c",
                    probe,
                    "sh",
                    cache_path,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                detail = (
                    result.stderr.strip()
                    or result.stdout.strip()
                    or "path missing, empty, or permission denied"
                )
                raise RuntimeError(
                    "Configured offline cache is not usable by fakeroot: "
                    f"{cache_path} ({detail})"
                )
            logger.info(f"Offline cache is usable by fakeroot: {cache_path}")

    def _verify_maven_offline_smoke(self) -> None:
        """Load Maven extensions and config-selected plugin engines offline."""
        repo = os.environ.get("SWE_MILESTONE_MAVEN_REPO_LOCAL", "").strip()
        if not repo:
            return

        raw = os.environ.get("SWE_MILESTONE_MAVEN_PLUGIN_PROBES", "").strip()
        try:
            configured = json.loads(raw) if raw else [
                {"pom": "pom.xml", "goal": "spotless:check", "timeout_seconds": 120}
            ]
            probes = normalize_maven_plugin_probes(configured)
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(f"Invalid Maven plugin probe configuration: {exc}") from exc
        if not probes:
            raise RuntimeError("Maven offline mode has no configured plugin probes")

        for probe in probes:
            command = [
                "docker",
                "exec",
                "--user",
                "fakeroot",
                "-e",
                "HOME=/home/fakeroot",
                "-w",
                self.workdir,
                self.container_name,
                "mvn",
                "-q",
                "-o",
                f"-Dmaven.repo.local={repo}",
                "-N",
                "-f",
                probe["pom"],
                probe["goal"],
                "-Dspotless.check.skip=false",
                "-Dcheckstyle.skip=true",
                "-Drat.skip=true",
                "-Dmaven.gitcommitid.skip=true",
            ]
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=probe["timeout_seconds"],
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    "Maven offline cache smoke test timed out for "
                    f"{probe['pom']} {probe['goal']}"
                ) from exc
            if result.returncode != 0:
                detail = (result.stderr.strip() or result.stdout.strip() or "unknown Maven error")[-4000:]
                raise RuntimeError(
                    "Maven offline cache smoke test failed for "
                    f"{probe['pom']} {probe['goal']} ({repo}): {detail}"
                )
            logger.info(
                "Maven offline cache smoke test passed: %s %s (%s)",
                probe["pom"],
                probe["goal"],
                repo,
            )

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

# Some release images intentionally contain only the prepared source tree and
# omit .git.  Submission tags are the evaluator's source of truth, so create a
# single baseline commit before handing the tree to the agent.  Do this in the
# harness instead of relying on an agent to notice and repair the environment.
if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "No Git repository found; creating a single baseline commit..."
    git init -q
    git config user.name "SWE-Milestone Harness"
    git config user.email "harness@swe-milestone.local"
    git add -A
    git commit -q -m "Initial baseline"
    echo "  Baseline repository initialized"
fi

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
            detail = (result.stderr or result.stdout or "unknown Git error").strip()
            raise RuntimeError(
                f"Git baseline initialization/history truncation failed "
                f"(exit {result.returncode}): {detail}"
            )
        logger.info("Git history truncation completed")

    def _resolve_whitelisted_ips(self) -> set[str]:
        """Resolve all WHITELISTED_DOMAINS to IP addresses from the host.

        Performs multiple resolution attempts per domain to capture CDN rotation.

        Returns:
            Set of unique IP address strings.
        """
        ips: set[str] = set()
        # Quarantine: domains in SWE_MILESTONE_DENY_DOMAINS are NOT resolved/accepted,
        # so the agent cannot reach that registry (e.g. PyPI) to fetch the
        # repo-under-test's own target-version source. Pairs with SWE_MILESTONE_DENY_CIDRS
        # below (needed because a registry's IPs ride a shared CDN range).
        _deny = {d.strip() for d in os.environ.get("SWE_MILESTONE_DENY_DOMAINS", "").split(",") if d.strip()}
        if _deny:
            logger.warning(f"SWE_MILESTONE_DENY_DOMAINS active — excluding from whitelist: {sorted(_deny)}")
        # Vertex regional endpoints: a non-"global" location routes to
        # "{LOC}-aiplatform.googleapis.com" (the bare aiplatform.googleapis.com
        # host in WHITELISTED_DOMAINS only covers the `global` endpoint). Resolve
        # the regional host too, else the documented region-switch (e.g.
        # us-east5 once quota lands) dies under the always-on network lockdown.
        domains = list(WHITELISTED_DOMAINS)
        _vloc = os.environ.get("SWE_MILESTONE_VERTEX_LOCATION", "").strip()
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
        _deny_cidrs = [c.strip() for c in os.environ.get("SWE_MILESTONE_DENY_CIDRS", "").split(",") if c.strip()]
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
                logger.warning(f"SWE_MILESTONE_DENY_CIDRS pruned {before - len(ips)} resolved IPs in denied ranges")
        return ips

    def _sni_sidecar_name(self) -> str:
        return f"{self.container_name}-snitun"

    def _sni_sidecar_ip(self) -> Optional[str]:
        result = subprocess.run(
            [
                "docker", "inspect", "-f",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                self._sni_sidecar_name(),
            ],
            capture_output=True, text=True,
        )
        ip = result.stdout.strip()
        return ip or None

    def _ensure_sni_sidecar(self) -> Optional[tuple[str, str]]:
        """Ensure the SNI-tunnel sidecar is up and wired into the agent container.

        Returns (endpoint_host, sidecar_ip) when a tunnel is active, else None.
        Idempotent and used on both fresh-lock and resume.

        A tunnel is required only when quarantine denies a CIDR the trial's LLM
        endpoint (UNIFIED_BASE_URL host) resolves into AND that host is in the
        code-level SNI_TUNNELABLE_DOMAINS allowlist — i.e. the endpoint shares a
        CDN range with a denied registry and would otherwise be blocked with it.

        The forwarder runs in a SEPARATE container (not on the host: this host
        blocks container->host traffic, and not in the agent container: the
        agent controls it). The agent container reaches the sidecar over the
        Docker bridge (container->container), maps the endpoint to the sidecar
        in /etc/hosts, and ACCEPTs only the sidecar IP on :443. The sidecar
        relays ONLY the pinned SNI, so the registry that shares the denied CDN
        range stays unreachable. See harness/e2e/sni_tunnel.py.
        """
        deny_cidrs = [
            c.strip()
            for c in os.environ.get("SWE_MILESTONE_DENY_CIDRS", "").split(",")
            if c.strip()
        ]
        endpoint_url = self._framework.get_network_endpoint_url() or ""
        host = tunnel_plan(endpoint_url, deny_cidrs)
        if not host:
            return None

        name = self._sni_sidecar_name()
        running = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True, text=True,
        ).stdout.strip()
        if running != "true":
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            sni_src = str(Path(sni_tunnel_module.__file__).resolve())
            # The tunnel is stdlib-only python3. Probe the repo image for it at
            # runtime (Java/Rust bases don't ship python3) instead of trusting
            # the image; fall back to a plain python image when absent.
            sidecar_image = self.image_name
            probe = subprocess.run(
                ["docker", "run", "--rm", "--entrypoint", "python3",
                 sidecar_image, "--version"],
                capture_output=True, text=True,
            )
            if probe.returncode != 0:
                logger.warning(
                    f"  Image {sidecar_image} has no usable python3 "
                    f"({probe.stderr.strip().splitlines()[-1] if probe.stderr.strip() else 'probe failed'}); "
                    f"using {SNI_SIDECAR_FALLBACK_IMAGE} for the SNI sidecar"
                )
                sidecar_image = SNI_SIDECAR_FALLBACK_IMAGE
            # --entrypoint python3 bypasses the image's own ENTRYPOINT (e.g. the
            # node image's docker-entrypoint.sh) so the tunnel runs directly.
            launch = subprocess.run(
                [
                    "docker", "run", "-d", "--restart", "no", "--user", "0",
                    "--name", name,
                    "-v", f"{sni_src}:/sni_tunnel.py:ro",
                    "--entrypoint", "python3",
                    sidecar_image,
                    "/sni_tunnel.py",
                    "--pin", host,
                    "--listen", f"0.0.0.0:{SNI_SIDECAR_PORT}",
                    "--upstream", f"{host}:443",
                ],
                capture_output=True, text=True,
            )
            if launch.returncode != 0:
                raise RuntimeError(
                    f"Failed to start SNI tunnel sidecar {name}: {launch.stderr.strip()}"
                )
            logger.info(f"  SNI tunnel sidecar {name} started (pinned to {host})")

        sidecar_ip = self._sni_sidecar_ip()
        if not sidecar_ip:
            raise RuntimeError(
                f"SNI tunnel sidecar {name} has no IP — cannot wire the tunnel"
            )

        # Wire the agent container: map endpoint -> sidecar and ACCEPT the
        # sidecar IP on the tunnel port. Idempotent (drop any prior mapping /
        # duplicate ACCEPT first) so resume and re-lock converge cleanly.
        wire = (
            f"grep -v ' {host}$' /etc/hosts > /etc/hosts.new || true; "
            f"printf '%s %s\\n' '{sidecar_ip}' '{host}' >> /etc/hosts.new; "
            f"cat /etc/hosts.new > /etc/hosts; rm -f /etc/hosts.new; "
            f"chmod 644 /etc/hosts; "
            f"iptables -C OUTPUT -d {sidecar_ip} -p tcp --dport {SNI_SIDECAR_PORT} -j ACCEPT 2>/dev/null "
            f"|| iptables -I OUTPUT 1 -d {sidecar_ip} -p tcp --dport {SNI_SIDECAR_PORT} -j ACCEPT"
        )
        wired = subprocess.run(
            ["docker", "exec", self.container_name, "/bin/sh", "-c", wire],
            capture_output=True, text=True,
        )
        if wired.returncode != 0:
            raise RuntimeError(
                f"Failed to wire SNI tunnel into {self.container_name}: {wired.stderr.strip()}"
            )

        self._sni_tunnel_host = host
        self._sni_tunnel_sidecar_ip = sidecar_ip
        logger.info(
            "  SNI tunnel active: %s -> sidecar %s:%d, pinned to %s (quarantine "
            "CIDR-blocks its CDN range; registry stays denied)",
            host, sidecar_ip, SNI_SIDECAR_PORT, host,
        )
        return (host, sidecar_ip)

    def stop_sni_tunnel(self) -> None:
        subprocess.run(
            ["docker", "rm", "-f", self._sni_sidecar_name()], capture_output=True
        )

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
        # Quarantine mode: CIDRs in SWE_MILESTONE_DENY_CIDRS are NOT accepted, so a
        # registry fronted by that CDN becomes unreachable even via raw curl.
        # Needed because SWE_MILESTONE_DENY_DOMAINS only drops DNS-resolved IPs, while
        # registries like PyPI ride a shared CDN range (Fastly 151.101.0.0/16)
        # that CDN_CIDR_RANGES would otherwise accept wholesale. Overlap
        # matching (not string equality): the builtin Cloudflare accept is
        # 104.16.0.0/13 while a policy may deny 104.16.0.0/12 — equality would
        # leave the /13 accepted and the registry reachable. LLM paths survive
        # on other ranges (Vertex = Google, api.anthropic.com = Anthropic ASN).
        _deny_cidrs = [c.strip() for c in os.environ.get("SWE_MILESTONE_DENY_CIDRS", "").split(",") if c.strip()]
        if _deny_cidrs:
            logger.warning(f"SWE_MILESTONE_DENY_CIDRS active — excluding CDN ranges overlapping: {sorted(_deny_cidrs)}")
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
        _quarantine = bool(os.environ.get("SWE_MILESTONE_QUARANTINE"))
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
        # Go quarantine uses the image-baked local file proxy; other quarantine
        # ecosystems use GOPROXY=off because the public mirror domains are
        # hosts-poisoned above. A non-quarantine container keeps the sanctioned
        # public proxy. Shell profiles override docker -e, so this MUST be
        # written here (#4).
        _goproxy = goproxy_value(
            go_offline=bool(os.environ.get("SWE_MILESTONE_GO_OFFLINE")),
            quarantine_active=_quarantine,
        )
        _go_offline = bool(os.environ.get("SWE_MILESTONE_GO_OFFLINE"))
        if _go_offline and _goproxy != GO_OFFLINE_FILE_PROXY:
            raise RuntimeError("Go-offline policy did not resolve to the local file proxy")
        _go_extra = ""
        _go_shell_extra = ""
        if _go_offline:
            _go_extra = """
GONOPROXY=none
GOSUMDB=off
GOTOOLCHAIN=local
GOFLAGS=-buildvcs=false
GOENV=/home/fakeroot/.cache/evoclaw-goenv/env
BASH_ENV={GO_OFFLINE_SHELL_ENV}
GOMODCACHE=/home/fakeroot/.cache/evoclaw-gomodcache
GOCACHE=/home/fakeroot/.cache/go-build
GOBIN=/home/fakeroot/go/bin
"""
            _go_shell_extra = """
export GONOPROXY=none
export GOSUMDB=off
export GOTOOLCHAIN=local
export GOFLAGS=-buildvcs=false
export GOENV=/home/fakeroot/.cache/evoclaw-goenv/env
export BASH_ENV={GO_OFFLINE_SHELL_ENV}
export GOMODCACHE=/home/fakeroot/.cache/evoclaw-gomodcache
export GOCACHE=/home/fakeroot/.cache/go-build
export GOBIN=/home/fakeroot/go/bin
export PATH=/home/fakeroot/go/bin:/usr/local/go/bin:/go/bin:$PATH
"""
        go_env_script = f"""
# Configure Go module fetching (quarantine-aware)
cat >> /etc/environment << 'EOF'
GOPROXY={_goproxy}
GONOSUMCHECK=*
GONOSUMDB=*
{_go_extra}
EOF

# Also set for fakeroot's shell profile
mkdir -p /home/fakeroot
cat >> /home/fakeroot/.bashrc << 'EOF'
export GOPROXY={_goproxy}
export GONOSUMCHECK=*
export GONOSUMDB=*
{_go_shell_extra}
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

        # --- Step 6b: SNI-pinned tunnel sidecar (if the LLM endpoint is
        # CIDR-blocked). Adds the endpoint->sidecar /etc/hosts mapping and the
        # sidecar-IP ACCEPT to the just-applied lockdown. Same code path is used
        # on resume, so it must run after the base lockdown is in place.
        self._ensure_sni_sidecar()

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

    def _tls_handshake_reachable_in_container(
        self, connect_host: str, port: int, sni: str, user: str = "fakeroot"
    ) -> bool:
        """True if a TLS handshake to connect_host:port with `sni` completes.

        Connects (respecting /etc/hosts) and drives a real TLS handshake with
        the given SNI. Used to assert both directions of the SNI tunnel: the
        pinned endpoint handshakes (relayed to the real upstream), while a
        registry SNI does NOT (the forwarder drops it). Certs are not verified —
        a completed ServerHello is proof the connection was relayed.
        """
        probe = (
            "import sys, socket, ssl\n"
            "host, port, sni = sys.argv[1], int(sys.argv[2]), sys.argv[3]\n"
            "ctx = ssl.create_default_context()\n"
            "ctx.check_hostname = False\n"
            "ctx.verify_mode = ssl.CERT_NONE\n"
            "try:\n"
            "    raw = socket.create_connection((host, port), timeout=8)\n"
            "    s = ctx.wrap_socket(raw, server_hostname=sni)\n"
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
                    self.container_name, "python3", "-c", probe,
                    connect_host, str(port), sni,
                ],
                capture_output=True, text=True, timeout=20,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"SNI tunnel probe timed out for {connect_host}:{port} sni={sni}"
            ) from e
        return result.stdout.strip().endswith("REACH")

    def _verify_sni_tunnel(self) -> None:
        """Assert the SNI tunnel relays the pinned endpoint and blocks detours.

        No-op unless a tunnel was started for this container. Two invariants:
          (a) the pinned endpoint completes a TLS handshake through the tunnel
              (LLM path works), and
          (b) a registry SNI to the same gateway is refused (the answer-fetch
              detour the tunnel could otherwise open stays closed).
        """
        host = self._sni_tunnel_host
        sidecar_ip = self._sni_tunnel_sidecar_ip
        if not host or not sidecar_ip:
            return
        # (a) endpoint handshakes through the sidecar (endpoint -> sidecar via
        #     /etc/hosts -> relayed to the real upstream).
        if not self._tls_handshake_reachable_in_container(host, SNI_SIDECAR_PORT, host):
            raise RuntimeError(
                f"SNI tunnel verification failed: pinned endpoint {host} did not "
                f"complete a TLS handshake through the sidecar — LLM path broken"
            )
        # (b) a registry SNI straight to the sidecar IP must be refused.
        if self._tls_handshake_reachable_in_container(
            sidecar_ip, SNI_SIDECAR_PORT, "registry.npmjs.org"
        ):
            raise RuntimeError(
                f"SNI tunnel verification failed: a registry SNI (registry.npmjs.org) "
                f"was relayed through sidecar {sidecar_ip} — the answer-fetch detour is OPEN"
            )
        logger.info(
            f"  SNI tunnel verified: {host} handshakes through the sidecar; "
            f"registry SNI detour blocked"
        )

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
        # — the whole point of SWE_MILESTONE_DENY_*. The github.com probe above only
        # covers code hosting; a typo'd deny CIDR/domain would otherwise pass
        # verification while leaving the exact cheat channel (e.g. PyPI) open.
        _deny_domains = [d.strip() for d in os.environ.get("SWE_MILESTONE_DENY_DOMAINS", "").split(",") if d.strip()]
        # A denied registry is exempt from the reachability assertion ONLY if the
        # policy DECLARES it un-CIDR-blockable (SWE_MILESTONE_FIREWALL_EXEMPT — e.g.
        # proxy.golang.org shares Vertex's Google range, defended by /etc/hosts
        # poison + local-only GOPROXY). Everything else MUST verify unreachable. The old
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
            for d in os.environ.get("SWE_MILESTONE_FIREWALL_EXEMPT", "").split(",")
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
                    f"reachable — SWE_MILESTONE_DENY_DOMAINS/SWE_MILESTONE_DENY_CIDRS not effective"
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
        _verify_urls = [u.strip() for u in os.environ.get("SWE_MILESTONE_VERIFY_FETCH_URLS", "").split(",") if u.strip()]
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
        _forbid_globs = [g.strip() for g in os.environ.get("SWE_MILESTONE_CACHE_FORBID_GLOBS", "").split(",") if g.strip()]
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

        # SNI tunnel (if active): endpoint relays, registry-SNI detour blocked.
        self._verify_sni_tunnel()

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
        # Stop the host-side SNI tunnel thread first (harmless if none).
        self.stop_sni_tunnel()

        if not self.container_exists():
            return

        if remove:
            logger.info(f"Removing container {self.container_name}...")
            subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True)
        else:
            logger.info(f"Stopping container {self.container_name}...")
            subprocess.run(["docker", "stop", self.container_name], capture_output=True)
