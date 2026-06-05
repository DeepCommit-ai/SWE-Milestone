#!/usr/bin/env python3
"""Launch EvoClaw E2E trials across repos as detached processes.

Reads a trial_config.yaml, resolves the final trial_name based on flags +
existing trial dirs, and spawns one detached run_e2e per repo. Exits
immediately — workers continue in their own session (no nohup needed).
Use ./scripts/monitor.sh to track progress.

Usage:
    python scripts/run_all.py --config trial_config.yaml
    python scripts/run_all.py --config trial_config.yaml --repos navidrome ripgrep
    python scripts/run_all.py --config trial_config.yaml --force
    python scripts/run_all.py --config trial_config.yaml --new
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Make the project root importable so `from harness.e2e...` works regardless of
# where run_all.py is invoked from (sys.path[0] would otherwise be scripts/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml


def _adc_project() -> str | None:
    """Read quota_project_id from the host ADC file (Vertex project default)."""
    cfg = os.environ.get("CLOUDSDK_CONFIG") or os.path.expanduser("~/.config/gcloud")
    try:
        return json.loads((Path(cfg) / "application_default_credentials.json").read_text()).get("quota_project_id")
    except Exception:
        return None


def _load_dotenv_files() -> None:
    """Load host config from .env (committed template) then .env_private
    (gitignored, your real paths) at the project root into os.environ.

    Set host-specific paths ONCE in .env_private and they persist across shells
    — no re-exporting every run. Precedence: a real shell-exported var wins over
    .env_private, which wins over .env. Minimal parser: KEY=VALUE, '#' comments,
    optional `export `, surrounding quotes stripped. See README.
    """
    project_root = Path(__file__).resolve().parent.parent
    merged: dict[str, str] = {}
    for fname in (".env", ".env_private"):  # .env_private overrides .env
        path = project_root / fname
        if not path.exists():
            continue
        try:
            for raw in path.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key:
                    merged[key] = val
        except Exception as e:
            print(f"Warning: failed to parse {path}: {e}", file=sys.stderr)
    for key, val in merged.items():
        os.environ.setdefault(key, val)  # a real shell-exported var wins


def _assert_wheelhouse_excludes(wheelhouse: str, forbid: list[str]) -> None:
    """Fail closed if the offline quarantine wheelhouse contains an artifact
    whose distribution name matches a forbidden prefix (the repo-under-test's own
    package). Without this, an un-audited wheelhouse could silently serve the
    answer offline via PIP_FIND_LINKS, defeating the network deny. See
    docs/quarantine.md.
    """
    norm_forbid = [f.strip().lower().replace("_", "-") for f in forbid if f.strip()]
    if not norm_forbid:
        return
    offending = []
    for name in os.listdir(wheelhouse):
        low = name.lower().replace("_", "-")
        if not low.endswith((".whl", ".tar.gz", ".zip")):
            continue
        if any(low.startswith(pref + "-") for pref in norm_forbid):
            offending.append(name)
    if offending:
        print(
            f"Error: quarantine wheelhouse {wheelhouse} contains forbidden "
            f"artifact(s) {sorted(offending)} matching wheelhouse_forbid={forbid}. "
            f"Refusing to run — this would serve the repo's own target source "
            f"offline. Rebuild the wheelhouse with scripts/build_quarantine_wheelhouse.py.",
            file=sys.stderr,
        )
        sys.exit(1)


def load_quarantine_env(repo_name: str, project_root: Path) -> dict:
    """Per-repo anti-cheat ("quarantine") policy → container env vars.

    Quarantine prevents an agent from fetching the repo-under-test's own
    target-version source (the answer) over the network: it denies the registry
    serving that source and forces the package manager offline against a vetted
    wheelhouse. The policy is **repo-intrinsic** (scikit denies PyPI, go-zero the
    Go proxy, …), so it lives once per repo in `quarantine_configs/<repo>.yaml`.

    Auto-on: presence of the file IS the switch (no trial-config flag). Returns
    {} (quarantine off) if the file is absent. Applied only to THIS repo's
    container — not globally to the whole trial. Fails closed (sys.exit) on a
    malformed policy or a wheelhouse that ships the repo's own package.
    See docs/quarantine.md.
    """
    conf_path = project_root / "quarantine_configs" / f"{repo_name}.yaml"
    if not conf_path.exists():
        return {}
    try:
        with open(conf_path) as f:
            q = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"Error: failed to read quarantine config {conf_path}: {e}", file=sys.stderr)
        sys.exit(1)

    env: dict[str, str] = {}
    dd = q.get("deny_domains")
    dc = q.get("deny_cidrs")
    wh = q.get("pip_wheelhouse")
    if dd:
        env["EVOCLAW_DENY_DOMAINS"] = ",".join(dd) if isinstance(dd, list) else str(dd)
    if dc:
        env["EVOCLAW_DENY_CIDRS"] = ",".join(dc) if isinstance(dc, list) else str(dc)
    if wh:
        # Expand ${EVOCLAW_WHEELHOUSE_DIR} etc. so the policy file carries no
        # host-specific absolute path (set the base once in .env_private).
        wh = str(Path(os.path.expandvars(str(wh))).expanduser().resolve())
        if not Path(wh).is_dir():
            print(f"Error: {conf_path}: pip_wheelhouse not found: {wh} "
                  f"(is EVOCLAW_WHEELHOUSE_DIR set in .env_private?)", file=sys.stderr)
            sys.exit(1)
        # Fail closed if the wheelhouse ships the repo's own package — an
        # un-audited wheelhouse must not be able to serve the answer offline.
        forbid = q.get("wheelhouse_forbid") or []
        if isinstance(forbid, str):
            forbid = [forbid]
        _assert_wheelhouse_excludes(wh, forbid)
        if not forbid:
            print(
                f"Warning: {conf_path}: pip_wheelhouse set without wheelhouse_forbid "
                f"— cannot assert the wheelhouse excludes the repo's own package "
                f"(see docs/quarantine.md).",
                file=sys.stderr,
            )
        else:
            env["EVOCLAW_WHEELHOUSE_FORBID"] = ",".join(forbid)
        env["EVOCLAW_PIP_WHEELHOUSE"] = wh
    return env


def discover_repos(data_root: Path, repo_filters: list[str] | None = None) -> list[Path]:
    """Find all repo directories in data_root that contain metadata.json."""
    repos = []
    for d in sorted(data_root.iterdir()):
        if not d.is_dir():
            continue
        if not (d / "metadata.json").exists():
            continue
        if repo_filters:
            # Substring match: "navidrome" matches "navidrome_navidrome_v0.57.0_v0.58.0"
            if not any(f in d.name for f in repo_filters):
                continue
        repos.append(d)
    return repos


def get_image_name(repo_name: str) -> str:
    """Derive Docker image name from repo directory name (lowercase)."""
    return f"{repo_name.lower()}/base:latest"


def generate_collect_config(
    config_dir: Path,
    trial_name: str,
    data_root: Path,
    repos: list[Path],
) -> Path:
    """Generate a collect_results config file for monitoring."""
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / f"{trial_name}_collect.py"

    mapping_lines = [f'    "{repo.name}": {{"path": "{repo.name}"}},' for repo in repos]

    content = f'''# Auto-generated by run_all.py for trial: {trial_name}
# Usage: python -m harness.e2e.collect_results --multi-repo --config {config_file}

DATA_ROOT = "{data_root}"

WORKSPACE_MAPPING = {{
{chr(10).join(mapping_lines)}
}}

E2E_TRIAL_NAMES = ["{trial_name}"]
'''
    config_file.write_text(content)
    return config_file


def find_max_suffix(repos: list[Path], base_name: str) -> int:
    """Max _NNN suffix existing across all repos for base_name. 0 if none."""
    max_suffix = 0
    pattern = re.compile(rf"^{re.escape(base_name)}_(\d{{3}})$")
    for repo in repos:
        e2e = repo / "e2e_trial"
        if not e2e.exists():
            continue
        for d in e2e.iterdir():
            if not d.is_dir():
                continue
            m = pattern.match(d.name)
            if m:
                max_suffix = max(max_suffix, int(m.group(1)))
    return max_suffix


def resolve_trial_name(yaml_name: str, repos: list[Path], force: bool, new: bool) -> str:
    """Resolve final trial_name based on yaml suffix + flags + existing dirs.

    Matrix (yaml without _NNN suffix):
        (none flag)  → latest existing _NNN, or _001 if none  (resume default)
        --force      → latest existing _NNN, or _001 if none  (wipe & restart)
        --new        → latest existing _NNN + 1                (always fresh)

    yaml WITH _NNN suffix is used as-is regardless of flags.
    """
    if re.match(r".*_\d{3}$", yaml_name):
        return yaml_name

    max_suffix = find_max_suffix(repos, yaml_name)
    if new:
        return f"{yaml_name}_{max_suffix + 1:03d}"
    return f"{yaml_name}_{max(max_suffix, 1):03d}"


def is_trial_completed(trial_dir: Path) -> bool:
    """True if trial summary shows all milestones completed."""
    summary = trial_dir / "evaluation" / "summary.json"
    if not summary.exists():
        return False
    try:
        s = json.loads(summary.read_text())
        completed = set(s.get("resume_state", {}).get("completed_milestones", []))
        total = s.get("total_milestones", 0)
        return total > 0 and len(completed) >= total
    except Exception:
        return False


def build_cmd(
    repo: Path,
    agent: str,
    model: str,
    timeout: int,
    trial_name: str,
    reasoning_effort: str | None,
    api_router: bool,
    force: bool,
) -> tuple[list[str], str]:
    """Build the run_e2e command for one repo. Returns (cmd, mode_label)."""
    repo_name = repo.name
    trial_dir = repo / "e2e_trial" / trial_name
    metadata_path = trial_dir / "trial_metadata.json"

    if not force and trial_dir.exists() and metadata_path.exists():
        return (
            [sys.executable, "-m", "harness.e2e.run_e2e", "--resume-trial", str(trial_dir)],
            "resume",
        )

    cmd = [
        sys.executable, "-m", "harness.e2e.run_e2e",
        "--repo-name", repo_name,
        "--image", get_image_name(repo_name),
        "--srs-root", str(repo / "srs"),
        "--workspace-root", str(repo),
        "--agent", agent,
        "--model", model,
        "--timeout", str(timeout),
        "--trial-name", trial_name,
    ]
    if reasoning_effort:
        cmd.extend(["--reasoning-effort", reasoning_effort])
    if api_router:
        cmd.append("--api-router")
    if force:
        cmd.append("--force")
    return cmd, ("force" if force else "fresh")


def main():
    parser = argparse.ArgumentParser(
        description="Launch EvoClaw trials (detached, fire-and-forget)",
    )
    parser.add_argument("--config", type=Path, required=True, help="Path to trial_config.yaml")
    parser.add_argument("--repos", nargs="+", default=None, help="Override repo filters (substring match)")
    parser.add_argument(
        "--force", action="store_true",
        help="Wipe & restart the latest matching trial. Kills any active worker via flock + SIGTERM, "
             "removes its container, rmtrees the trial dir, and starts fresh under the same _NNN.",
    )
    parser.add_argument(
        "--new", action="store_true",
        help="Create a new trial with the next available _NNN suffix (max+1).",
    )
    args = parser.parse_args()

    if args.new and args.force:
        print("Error: --new and --force are mutually exclusive", file=sys.stderr)
        sys.exit(1)

    # Load host paths from .env / .env_private (once-configured, persists).
    _load_dotenv_files()

    # Load config
    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    # data_root: from the trial config, or EVOCLAW_DATA_ROOT (.env_private).
    # Supports ${EVOCLAW_DATA_ROOT} expansion so trial configs need no host path.
    _dr = cfg.get("data_root") or os.environ.get("EVOCLAW_DATA_ROOT")
    if not _dr:
        print("Error: data_root not set. Put 'data_root:' in the trial config "
              "or set EVOCLAW_DATA_ROOT in .env_private (see README).", file=sys.stderr)
        sys.exit(1)
    data_root = Path(os.path.expandvars(str(_dr))).expanduser().resolve()
    yaml_trial_name = cfg["trial_name"]
    agent = cfg.get("agent", "claude-code")
    model = cfg.get("model", "claude-sonnet-4-5-20250929")
    timeout = cfg.get("timeout", 18000)
    reasoning_effort = cfg.get("reasoning_effort", None)
    api_router = cfg.get("api_router", cfg.get("drop_params", False))
    default_haiku_model = cfg.get("default_haiku_model", None)
    repo_filters = args.repos or cfg.get("repos", None)

    # Anti-cheat ("quarantine") is now PER-REPO and auto-on: each repo's policy
    # lives in quarantine_configs/<repo>.yaml and is applied only to that repo's
    # container at spawn time (load_quarantine_env). No trial-config block. Warn
    # if a deprecated trial-level secure_eval block is still present.
    if cfg.get("secure_eval") is not None:
        print(
            "Warning: 'secure_eval' in the trial config is deprecated and IGNORED. "
            "Quarantine is now per-repo via quarantine_configs/<repo>.yaml (auto-on "
            "when the file exists). See docs/quarantine.md.",
            file=sys.stderr,
        )

    # Vertex AI mode: a single yaml flag (vertex_ai: true) routes the agent to
    # Google Vertex AI using the agent's OWN native Vertex support — gemini-cli
    # (Gemini models) and claude-code (Claude models) both talk to Vertex
    # directly via ADC (no proxy/bridge). Auth is ADC, configured once on the
    # host — no UNIFIED_API_KEY / UNIFIED_BASE_URL to pass. See docs/vertex-ai.md.
    vertex_ai = cfg.get("vertex_ai", False)
    vertex_location = cfg.get("vertex_location", "global")
    vertex_project = cfg.get("vertex_project", None)
    if vertex_ai:
        # No Anthropic↔OpenAI router in Vertex mode. For claude-code, route all
        # of Claude Code's class-based model slots to this same Vertex model so
        # background/subagent calls don't fall back to the hard-coded Anthropic
        # defaults (which may not be enabled on the Vertex project).
        api_router = False
        if not default_haiku_model:
            default_haiku_model = model

    # Propagate default_haiku_model to child processes via env var
    # (ClaudeCodeFramework reads UNIFIED_DEFAULT_HAIKU_MODEL)
    if default_haiku_model:
        os.environ["UNIFIED_DEFAULT_HAIKU_MODEL"] = default_haiku_model

    # Validate
    if not data_root.exists():
        print(f"Error: data_root not found: {data_root}", file=sys.stderr)
        sys.exit(1)
    if not vertex_ai and not os.environ.get("UNIFIED_API_KEY"):
        print("Warning: UNIFIED_API_KEY not set. Agents may fail to authenticate.", file=sys.stderr)

    # Discover repos
    repos = discover_repos(data_root, repo_filters)
    if not repos:
        print(f"Error: no repos found in {data_root}", file=sys.stderr)
        sys.exit(1)

    # Resolve trial name based on yaml + flags + existing trial dirs
    trial_name = resolve_trial_name(yaml_trial_name, repos, args.force, args.new)

    # Vertex AI wiring (before spawning workers; env is inherited by workers).
    # Each agent uses its OWN native Vertex support via ADC copied into the
    # container — no proxy/bridge:
    #   gemini-cli  → Gemini models      (harness/e2e/agents/gemini.py)
    #   claude-code → Claude models via CLAUDE_CODE_USE_VERTEX (claude_code.py)
    # Other agents (codex, openhands) have no Vertex path here and are rejected.
    vertex_info = None
    if vertex_ai:
        if agent not in ("gemini-cli", "claude-code"):
            print(f"Error: vertex_ai is only supported with agent: gemini-cli "
                  f"or claude-code (got '{agent}').", file=sys.stderr)
            sys.exit(1)
        proj = vertex_project or _adc_project()
        if not proj:
            print("Error: set vertex_project (no ADC quota_project_id found)", file=sys.stderr)
            sys.exit(1)
        os.environ["EVOCLAW_VERTEX"] = "1"
        os.environ["EVOCLAW_VERTEX_PROJECT"] = proj
        os.environ["EVOCLAW_VERTEX_LOCATION"] = vertex_location
        vertex_info = {"project": proj}

    mode_label = (
        "--force (wipe & restart)" if args.force
        else "--new (fresh next suffix)" if args.new
        else "default (resume latest)"
    )

    print("=" * 60)
    print("  EvoClaw Run All  (fire-and-forget)")
    print("=" * 60)
    print(f"  Data root:    {data_root}")
    print(f"  Trial name:   {trial_name}")
    print(f"  Agent:        {agent}")
    print(f"  Model:        {model}")
    if vertex_info:
        print(f"  Vertex AI:    {vertex_location} direct/ADC, project={vertex_info['project']}")
    print(f"  Timeout:      {timeout}s")
    print(f"  Repos:        {len(repos)}")
    print(f"  Mode:         {mode_label}")
    print("=" * 60)

    # Generate collect config for monitor.sh
    project_root = Path(__file__).resolve().parent.parent
    log_dir = project_root / ".evoclaw"
    log_dir.mkdir(parents=True, exist_ok=True)
    collect_config = generate_collect_config(
        config_dir=log_dir,
        trial_name=trial_name,
        data_root=data_root,
        repos=repos,
    )

    # Launch each repo detached
    launched = 0
    skipped = 0
    for repo in repos:
        trial_dir = repo / "e2e_trial" / trial_name
        if not args.force and is_trial_completed(trial_dir):
            print(f"\033[0;34m[SKIP]\033[0m       {repo.name:<50}  (already completed)")
            skipped += 1
            continue

        cmd, mode = build_cmd(
            repo, agent, model, timeout, trial_name,
            reasoning_effort, api_router, args.force,
        )
        # Per-repo quarantine: apply this repo's anti-cheat policy (if any) only
        # to its own container, via the worker subprocess env (not global).
        q_env = load_quarantine_env(repo.name, project_root)
        worker_env = {**os.environ, **q_env}
        log_path = log_dir / f"{repo.name}.log"
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "ab") as logf:
            logf.write(f"\n\n===== launched at {ts} ({mode}) =====\n".encode())
            logf.flush()
            proc = subprocess.Popen(
                cmd,
                cwd=str(project_root),
                stdout=logf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # detach: survive shell exit
                env=worker_env,
            )
        q_marker = "  🔒 quarantine" if q_env else ""
        print(f"\033[0;32m[LAUNCHED]\033[0m  {repo.name:<50}  PID={proc.pid}  ({mode}){q_marker}")
        launched += 1

    print()
    print(f"  {launched} launched, {skipped} skipped")
    print()
    print(f"Monitor:       ./scripts/monitor.sh {trial_name}")
    print(f"Per-repo logs: {log_dir}/<repo>.log")
    print(f"Collect cfg:   {collect_config}")
    print()


if __name__ == "__main__":
    main()
