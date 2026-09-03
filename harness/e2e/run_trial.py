"""CTE entry point for external consumers: run the official SWE-Milestone trial
per repo, aggregate with the official code, return one TrialResult.

CTE (Continuous Task Evaluation) is what the leaderboard measures: one repo is
one agent session in one container started from the repo's ``__base-offline``
image; milestones are released in dependency order through TASK_QUEUE.md; the
agent's tree carries over from milestone to milestone; a watcher evaluates each
``agent-impl-<milestone>`` tag with the official PatchEvaluator; the trial ends
when the DAG is complete or the trial timeout fires.

Nothing here reimplements a score or a launch step:

- the per-repo command is ``scripts/run_all.build_cmd`` (image selection,
  runtime-policy pin, ``--allow-partial-build-reports`` default, resume when a
  ``trial_metadata.json`` already exists) and the worker environment is
  ``runtime_policy_subprocess_env`` on top of an EXPLICIT base (never the
  caller's whole environment: a stray ``EVOCLAW_*`` or a leftover
  ``SWE_MILESTONE_AUTO_COMPACT_WINDOW`` would change the trial silently);
- the headline numbers come from ``collect_results.compute_repo_summary`` and
  the per-milestone numbers from ``collect_results``'s own helpers on the
  authoritative cell (``load_e2e_results``, retry-attempt aware).

The policy under test is a self-served model behind an Anthropic-compatible
endpoint: ``base_url`` is the endpoint the in-container claude-code dials,
``model`` is the id it sends (pinned into every class-based model slot), and
``agent_env`` carries the consumer's context pins into the container LAST so
they win over the harness-derived values (recorded in trial_metadata.agent_env).
Sessions are keyed by the API key the container sends (x-api-key), so every repo
gets its own key value ``<trial_name>/<repo>`` and therefore its own session on
the endpoint.
"""
from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent   # harness/e2e/run_trial.py -> repo root
DEFAULT_MODEL = "slime-actor"
DEFAULT_TIMEOUT_S = 18000          # the leaderboard trial timeout
# Host variables a worker may inherit. Everything else is set explicitly.
_INHERITED_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TZ", "TMPDIR",
    "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT", "XDG_RUNTIME_DIR",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONUNBUFFERED",
    # data/image version knobs are deliberate operator choices and they MOVE THE SCORE
    # (the tag selects which images the trial boots); keep them visible to the worker.
    "SWE_MILESTONE_DATA_VERSION_CHECK", "SWE_MILESTONE_BENCHMARK_VERSION", "SWE_MILESTONE_IMAGE_TAG",
    "SWE_MILESTONE_RESIDUE_PRUNE",   # scoring override read by the evaluator
)


# ───────────────────────────── result contract ─────────────────────────────
@dataclass
class MilestoneResult:
    id: str
    status: str                      # passed | failed | infra-invalid | scoring-blocked | not-submitted
    submitted: bool
    eval_status: str
    resolved: bool
    score: float                     # score_reliable (0..1)
    recall: float
    precision: float
    infra_invalid: bool = False
    infra_invalid_reason: str = ""
    scoring_blocked: bool = False
    test_summary: dict = field(default_factory=dict)
    n_turns: Optional[int] = None
    n_compactions: Optional[int] = None
    wall_seconds: Optional[float] = None
    cost_usd: Optional[float] = None
    session_dir: str = ""
    eval_dir: str = ""


@dataclass
class RepoResult:
    repo: str
    image_ref: str
    trial_dir: str
    n_milestones: int                # rows of milestones.csv
    n_selected: int                  # selected_milestone_ids.txt (else csv)
    n_graded: int                    # selected minus non-graded: THE denominator
    n_evaluated: int
    n_submitted: int
    n_unfinished: int
    n_infra_invalid: int
    n_scoring_blocked: int
    n_resolved: int
    resolve_rate: float
    score: float
    recall: float
    precision: float
    mode: str = "fresh"              # fresh | resume | completed | aggregate
    agent_exit: dict = field(default_factory=dict)   # {reason, wall_seconds, turns, cost_usd, worker_exit_code}
    official_summary: dict = field(default_factory=dict)  # compute_repo_summary verbatim (audit)
    milestones: List[MilestoneResult] = field(default_factory=list)


@dataclass
class TrialResult:
    api_version: str
    benchmark_version: str
    harness_sha: str
    data_commit: str
    data_root: str
    trial_name: str
    model: str
    agent_version: str
    base_url: str
    agent_env: dict
    started_at: str
    finished_at: str
    repos: List[RepoResult] = field(default_factory=list)
    macro: dict = field(default_factory=dict)
    micro: dict = field(default_factory=dict)
    mode: str = "run"                # run | resume | dry-run | aggregate-only
    repos_requested: List[str] = field(default_factory=list)
    repos_missing: List[str] = field(default_factory=list)   # requested but produced no result
    complete: bool = True            # every requested repo produced a result
    result_path: str = ""            # where this JSON was written
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    def to_json(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=False))
        return path


# ───────────────────────────── helpers ─────────────────────────────
def _load_run_all():
    """scripts/run_all.py is a script, not a package; import it by path so the
    per-repo command and the worker env are built by the leaderboard code."""
    path = PROJECT_ROOT / "scripts" / "run_all.py"
    spec = importlib.util.spec_from_file_location("swe_milestone_run_all", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(path: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def session_key(trial_name: str, repo_name: str) -> str:
    """The per-repo API key, which is also the policy endpoint's session id.

    The worker exports it as UNIFIED_API_KEY; the harness maps that to ANTHROPIC_API_KEY and
    claude-code sends it as the `x-api-key` header, which is what an Anthropic-compatible
    adapter keys its sessions by. Exposed so a consumer can pre-register those sessions with
    its own per-session settings instead of re-deriving the format."""
    return f"{trial_name}/{repo_name}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        v = value.replace("Z", "+00:00")
        d = datetime.fromisoformat(v)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def worker_env(*, data_root: Path, base_url: str, model: str, trial_name: str, repo_name: str,
               agent_env: Optional[Dict[str, str]], policy, host_env: Optional[Dict[str, str]] = None,
               auto_compact_window: Optional[int] = None,
               enable_tool_search: Optional[str] = None) -> Dict[str, str]:
    """The explicit environment of one per-repo worker (see module docstring).

    `auto_compact_window` / `enable_tool_search` are the two score-moving knobs `run_all.py`
    exports from the trial config (SWE_MILESTONE_AUTO_COMPACT_WINDOW / _ENABLE_TOOL_SEARCH).
    They are set here for the same reason: unset means claude-code's own default, which is
    version dependent, so two trials that differ in them are not comparable."""
    from harness.e2e.runtime_policy_binding import runtime_policy_subprocess_env  # noqa: PLC0415
    src = os.environ if host_env is None else host_env
    base: Dict[str, str] = {k: src[k] for k in _INHERITED_ENV if k in src}
    py = str(PROJECT_ROOT)
    if src.get("PYTHONPATH"):
        py = py + os.pathsep + src["PYTHONPATH"]
    base["PYTHONPATH"] = py
    base["SWE_MILESTONE_DATA_ROOT"] = str(data_root)
    base["UNIFIED_BASE_URL"] = base_url
    base["UNIFIED_API_KEY"] = session_key(trial_name, repo_name)   # per-repo session key (x-api-key)
    base["UNIFIED_DEFAULT_AGENT_MODEL"] = model                   # every class-based model slot
    if auto_compact_window:
        base["SWE_MILESTONE_AUTO_COMPACT_WINDOW"] = str(int(auto_compact_window))
    if enable_tool_search is not None:
        base["SWE_MILESTONE_ENABLE_TOOL_SEARCH"] = str(enable_tool_search)
    if agent_env:
        base["SWE_MILESTONE_AGENT_ENV"] = json.dumps({str(k): str(v) for k, v in agent_env.items()})
    assert not any(k.startswith("EVOCLAW_") for k in base)
    return runtime_policy_subprocess_env(policy, base)


def _count_compactions(log_dir: Path, start: Optional[datetime], end: Optional[datetime]) -> Optional[int]:
    """Compaction events (claude-code `compact_boundary`) in the session jsonl
    files under <trial>/log, restricted to [start, end] when both are known."""
    files = sorted(log_dir.glob("claude_code/*.jsonl")) + sorted(log_dir.glob("*.jsonl"))
    if not files:
        return None
    n = 0
    seen = set()
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "compact_boundary" not in line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("subtype") != "compact_boundary":
                        continue
                    key = ev.get("uuid") or (f.name, fh.tell())
                    if key in seen:
                        continue
                    ts = _parse_ts(ev.get("timestamp"))
                    if start and end and ts and not (start <= ts <= end):
                        continue
                    seen.add(key)
                    n += 1
        except OSError:
            continue
    return n


def _agent_exit(trial_dir: Path, worker_rc: Optional[int], graded_ids: List[str], summary: dict, stats: dict) -> dict:
    """Why the repo session ended, with the session-level totals."""
    rs = (summary.get("resume_state") or {}).get("dag") or {}
    done = set(rs.get("completed") or []) | set(rs.get("failed") or []) | set(rs.get("skipped") or [])
    dag_done = bool(graded_ids) and all(m in done for m in graded_ids)
    timed_out = False
    hist = trial_dir / "log" / "session_history.jsonl"
    if hist.exists():
        try:
            with open(hist, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"timeout"' in line and ('"reason"' in line or '"event"' in line):
                        timed_out = True
        except OSError:
            pass
    if timed_out:
        reason = "timeout"
    elif worker_rc == 0 and dag_done:
        reason = "completed"
    elif worker_rc == 0:
        reason = "incomplete"
    elif worker_rc is None:
        reason = "completed" if dag_done else "unknown"
    else:
        reason = "error"
    s = stats.get("summary") or {}
    wall_ms = s.get("wall_clock_ms") or s.get("duration_ms")
    return {
        "reason": reason,
        "wall_seconds": (wall_ms / 1000.0) if wall_ms else None,
        "turns": s.get("total_turns"),
        "cost_usd": stats.get("_cost"),
        "worker_exit_code": worker_rc,
    }


# ───────────────────────────── aggregation ─────────────────────────────
def aggregate_repo(ws: Path, trial_name: str, *, worker_rc: Optional[int] = None,
                   mode: str = "fresh") -> RepoResult:
    """One repo's TrialResult entry from the official aggregation code."""
    from harness.e2e import collect_results as cr  # noqa: PLC0415

    trial_dir = ws / "e2e_trial" / trial_name
    eval_dir = trial_dir / "evaluation"
    selected, _src = cr.load_selected_milestones(ws)
    csv_ids = cr.load_milestones_from_csv(ws) or set()
    non_graded = cr.load_non_graded_milestones(ws)
    universe = set(selected) if selected else set(csv_ids)
    graded_ids = sorted(universe - non_graded, key=cr.sort_milestone_key)

    official = cr.compute_repo_summary(ws, [trial_name], trial_type="e2e", prefer_filtered=True)
    results, _counts = cr.load_e2e_results(ws, trial_name, prefer_filtered=True)
    cells = cr.authoritative_cells(eval_dir, prefer_filtered=True) if eval_dir.exists() else {}
    summary = cr._read_summary_results(eval_dir) if False else {}
    try:
        summary = json.loads((eval_dir / "summary.json").read_text()) if (eval_dir / "summary.json").exists() else {}
    except (OSError, json.JSONDecodeError):
        summary = {}
    status_buckets = summary.get("milestone_status") or {}
    pending_submitted = set(status_buckets.get("submitted") or [])

    stats: dict = {}
    if (trial_dir / "agent_stats.json").exists():
        try:
            stats = json.loads((trial_dir / "agent_stats.json").read_text())
        except (OSError, json.JSONDecodeError):
            stats = {}
    loaded = cr.load_agent_stats(trial_dir)
    stats["_cost"] = loaded.get("cost")
    mstats = stats.get("milestone_stats") or {}

    milestones: List[MilestoneResult] = []
    n_submitted = n_evaluated = n_infra = n_blocked = n_resolved = 0
    for mid in graded_ids:
        r = results.get(mid)
        ms = mstats.get(mid) or {}
        start, end = _parse_ts(ms.get("start_time")), _parse_ts(ms.get("end_time"))
        common = dict(
            n_turns=ms.get("turns"),
            n_compactions=_count_compactions(trial_dir / "log", start, end),
            wall_seconds=(ms["duration_ms"] / 1000.0) if ms.get("duration_ms") else None,
            cost_usd=ms.get("cost_usd"),
            session_dir=str(trial_dir / "log"),
            eval_dir=str(cells[mid]) if mid in cells else "",
        )
        if not r or r.get("eval_status") == "not_run":
            milestones.append(MilestoneResult(
                id=mid, status="not-submitted", submitted=mid in pending_submitted,
                eval_status=(r or {}).get("eval_status", "not_run"), resolved=False,
                score=0.0, recall=0.0, precision=0.0, **common))
            if mid in pending_submitted:
                n_submitted += 1
            continue
        n_submitted += 1
        n_evaluated += 1
        blocked = bool(r.get("scoring_blocked"))
        infra = cr.is_infra_invalid(r)
        resolved = bool(cr.is_resolved(r))
        score = cr.calculate_score_reliable(r)
        prec, rec = cr.calculate_precision_recall(r)
        if blocked:
            status = "scoring-blocked"
            n_blocked += 1
        elif infra:
            status = "infra-invalid"
            n_infra += 1
        elif resolved:
            status = "passed"
            n_resolved += 1
        else:
            status = "failed"
        ts = r.get("test_summary") or {}
        milestones.append(MilestoneResult(
            id=mid, status=status, submitted=True, eval_status=str(r.get("eval_status", "")),
            resolved=resolved, score=float(score or 0.0), recall=float(rec or 0.0), precision=float(prec or 0.0),
            infra_invalid=bool(infra), infra_invalid_reason=str(r.get("infra_invalid_reason") or ""),
            scoring_blocked=blocked,
            test_summary={k: ts.get(k, 0) for k in (
                "fail_to_pass_required", "fail_to_pass_achieved", "none_to_pass_required", "none_to_pass_achieved",
                "pass_to_pass_required", "pass_to_pass_achieved", "pass_to_pass_failed", "pass_to_pass_missing",
                "total")},
            **common))

    def _official(key, fallback):
        """compute_repo_summary's value, or the locally counted one when it is absent.
        `or` would be wrong: a legitimate 0 (no resolved cell, no infra-invalid cell) must not
        fall back to the local count."""
        value = official.get(key)
        return fallback if value is None else int(value)

    n_graded = _official("graded", len(graded_ids))
    image_ref = ""
    meta_path = trial_dir / "trial_metadata.json"
    if meta_path.exists():
        try:
            image_ref = str(json.loads(meta_path.read_text()).get("image") or "")
        except (OSError, json.JSONDecodeError):
            image_ref = ""
    return RepoResult(
        repo=ws.name, image_ref=image_ref, trial_dir=str(trial_dir),
        n_milestones=len(csv_ids) if csv_ids else len(universe),
        n_selected=len(universe), n_graded=n_graded,
        n_evaluated=_official("evaluated", n_evaluated),
        n_submitted=n_submitted, n_unfinished=max(n_graded - n_submitted, 0),
        n_infra_invalid=_official("infra_invalid", n_infra), n_scoring_blocked=n_blocked,
        n_resolved=_official("resolved", n_resolved),
        resolve_rate=float(official.get("resolve_pct") or 0.0) / 100.0,
        score=float(official.get("score_reliable") or 0.0) / 100.0,
        recall=float(official.get("recall") or 0.0) / 100.0,
        precision=float(official.get("precision") or 0.0) / 100.0,
        mode=mode,
        agent_exit=_agent_exit(trial_dir, worker_rc, graded_ids, summary, stats),
        official_summary={k: v for k, v in official.items() if k != "error"},
        milestones=milestones,
    )


def aggregate(repos: List[RepoResult]) -> tuple[dict, dict]:
    """macro = mean of per-repo values; micro = cells pooled over repos."""
    if not repos:
        return {}, {}
    n = len(repos)
    macro = {
        "score": sum(r.score for r in repos) / n,
        "resolve_rate": sum(r.resolve_rate for r in repos) / n,
        "recall": sum(r.recall for r in repos) / n,
        "precision": sum(r.precision for r in repos) / n,
        "n_repos": n,
    }
    graded = sum(r.n_graded for r in repos)
    cells = [m for r in repos for m in r.milestones]
    micro = {
        "score": (sum(m.score for m in cells) / graded) if graded else 0.0,
        "resolve_rate": (sum(r.n_resolved for r in repos) / graded) if graded else 0.0,
        "recall": (sum(m.recall for m in cells) / graded) if graded else 0.0,
        "precision": (sum(m.precision for m in cells) / graded) if graded else 0.0,
        "n_graded": graded,
        "n_evaluated": sum(r.n_evaluated for r in repos),
        "n_submitted": sum(r.n_submitted for r in repos),
        "n_unfinished": sum(r.n_unfinished for r in repos),
        "n_infra_invalid": sum(r.n_infra_invalid for r in repos),
        "n_scoring_blocked": sum(r.n_scoring_blocked for r in repos),
        "unfinished_ratio": (sum(r.n_unfinished for r in repos) / graded) if graded else 0.0,
    }
    return macro, micro


# ───────────────────────────── preflight + launch ─────────────────────────────
def _preflight(data_root: Path, repos: List[Path], policies: dict, *, launch: bool,
               resuming: Optional[Dict[str, bool]] = None) -> tuple[str, str, List[str]]:
    """Fail loud before spending a container. Returns (benchmark_version, data_commit, notes)."""
    from harness.api import reject_legacy_env  # noqa: PLC0415
    from harness.e2e.data_version import check_data_version  # noqa: PLC0415
    from harness.e2e.runtime_policy_binding import image_for_runtime_policy  # noqa: PLC0415
    from harness.prepare_repo.split_test_patches.test_detector import RustTestDetectionError, ensure_ast_grep  # noqa: PLC0415

    reject_legacy_env()
    notes: List[str] = []
    if not data_root.is_dir():
        raise FileNotFoundError(f"run_trial: data_root not found: {data_root}")
    try:
        version_meta = check_data_version(data_root, context="run_trial")
    except SystemExit as exc:                      # the official check refuses via sys.exit
        raise RuntimeError(f"run_trial: data version check refused the tree: {exc}") from exc
    bench = str(version_meta.get("benchmark_version") or "")
    dv = version_meta.get("data_version") or {}
    if dv.get("state") not in ("match", "unchecked"):
        raise RuntimeError(f"run_trial: data tree is not at {bench}: {dv}")
    data_commit = str(dv.get("commit") or _git(data_root, "rev-parse", "HEAD"))
    if launch:
        if shutil.which("docker") is None or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
            raise RuntimeError("run_trial: docker is not reachable from this process")
        try:
            ensure_ast_grep()
        except RustTestDetectionError as exc:      # Rust cells would fail closed hours later
            raise RuntimeError(f"run_trial: {exc}") from exc
        if not os.access(data_root, os.W_OK):
            raise RuntimeError(f"run_trial: data_root must be a WRITABLE checkout (trial artifacts are "
                               f"written under <repo>/e2e_trial/): {data_root}")
        for repo in repos:
            if resuming and resuming.get(repo.name):
                continue     # a resume boots the image frozen in trial_metadata, not this one
            image = image_for_runtime_policy(policies[repo.name])
            if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode != 0:
                raise RuntimeError(f"run_trial: image not present locally: {image} (scripts/pull_images.sh)")
    return bench, data_commit, notes


def run_trial(repos: Optional[List[str]], *, data_root: str, trial_name: str, base_url: str,
              model: str = DEFAULT_MODEL, agent: str = "claude-code",
              agent_version: Optional[str] = None,
              agent_env: Optional[Dict[str, str]] = None, timeout_s: int = DEFAULT_TIMEOUT_S,
              milestones: Optional[str] = None, parallel: int = 7, out_root: Optional[str] = None,
              reasoning_effort: Optional[str] = None, auto_compact_window: Optional[int] = None,
              enable_tool_search: Optional[str] = None, build_failure_fail_closed: bool = False,
              unprotected: bool = False, dry_run: bool = False, aggregate_only: bool = False,
              project_root: Optional[Path] = None) -> TrialResult:
    """Run (or resume) the official trial for each repo and aggregate. See the module docstring.

    repos: substrings matched against the repo dirs under data_root (None = all).
    milestones: optional dependency-closed prefix ('10' or '50%') for smoke runs.
    dry_run: preflight, build every worker command/env, launch nothing.
    aggregate_only: skip launching; aggregate an existing trial directory.

    Every knob a leaderboard trial config carries and that MOVES THE SCORE is exposed here with
    the launcher's own default, so a seam-launched trial can be made argument-identical to a
    `scripts/run_all.py --config` trial: `agent`, `reasoning_effort`, `auto_compact_window`,
    `enable_tool_search`, `build_failure_fail_closed`, `timeout_s`, `milestones`. What remains
    deliberately different is the endpoint, the model id and `agent_env`, i.e. the policy itself.
    """
    from harness import api  # noqa: PLC0415
    from harness.e2e.runtime_policy_binding import (  # noqa: PLC0415
        RUNTIME_POLICY_MODE_UNPROTECTED, image_for_runtime_policy, resolve_runtime_policy,
        runtime_policy_coverage_errors)

    root = Path(project_root) if project_root else PROJECT_ROOT
    run_all = _load_run_all()
    data_root_p = Path(data_root).expanduser().resolve()
    repo_paths: List[Path] = run_all.discover_repos(data_root_p, list(repos) if repos else None)
    if not repo_paths:
        raise RuntimeError(f"run_trial: no repos matched {repos!r} under {data_root_p}")
    if not base_url or "://" not in base_url:
        raise ValueError(f"run_trial: base_url must be a URL, got {base_url!r}")
    agent_env = {str(k): str(v) for k, v in (agent_env or {}).items()}

    # A repo whose trial directory already exists RESUMES, and a resume replays the binding
    # frozen into trial_metadata.json. run_all gates only fresh workers on the live policy for
    # exactly that reason; gating a resume would let a later policy edit strand a running trial.
    resuming = {repo.name: (repo / "e2e_trial" / trial_name / "trial_metadata.json").exists()
                for repo in repo_paths}
    # A trial whose DAG is already complete is not relaunched: run_all skips it and aggregates
    # what is on disk, and relaunching one whose container was since removed fails resume
    # validation for no reason.
    completed = {repo.name: bool(resuming[repo.name])
                 and run_all.is_trial_completed(repo / "e2e_trial" / trial_name)
                 for repo in repo_paths}
    policies = {}
    for repo in repo_paths:
        policy = resolve_runtime_policy(repo.name, root, unprotected=unprotected)
        errors = runtime_policy_coverage_errors(policy)
        if errors and policy.mode != RUNTIME_POLICY_MODE_UNPROTECTED and not resuming[repo.name]:
            raise RuntimeError(f"run_trial: quarantine coverage gate failed for {repo.name}: {errors}. "
                               f"Add quarantine_configs/{repo.name}.yaml or pass unprotected=True.")
        policies[repo.name] = policy

    launch = not (dry_run or aggregate_only)
    bench, data_commit, notes = _preflight(data_root_p, repo_paths, policies, launch=launch,
                                           resuming=resuming)
    started = _now()
    # Never default inside the data checkout: that tree is a pinned git clone and the harness
    # verifies its version, so writing run artifacts into it is a footgun.
    out_dir = Path(out_root).expanduser().resolve() if out_root else Path(
        os.environ.get("TMPDIR", "/tmp")) / "swe-milestone-run-trial"
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
    if launch:
        (out_dir / trial_name).mkdir(parents=True, exist_ok=True)

    worker_rcs: Dict[str, Optional[int]] = {r.name: None for r in repo_paths}
    mode = "aggregate-only" if aggregate_only else "dry-run" if dry_run else "run"
    plans: List[dict] = []
    for repo in repo_paths:
        if completed.get(repo.name):
            notes.append(f"{repo.name}: trial already complete; not relaunched")
            continue
        policy = policies[repo.name]
        cmd, cmd_mode = run_all.build_cmd(
            repo, agent, model, int(timeout_s), trial_name,
            reasoning_effort, agent_version, False,
            milestones, root, bool(build_failure_fail_closed), runtime_policy=policy,
        )
        # On a resume the worker restores the binding frozen in trial_metadata.json, so the LIVE
        # policy must not be injected: run_all passes None for exactly this reason, and a policy
        # edited after launch would otherwise strand a running trial.
        env = worker_env(data_root=data_root_p, base_url=base_url, model=model, trial_name=trial_name,
                         repo_name=repo.name, agent_env=agent_env,
                         policy=None if cmd_mode == "resume" else policy,
                         auto_compact_window=auto_compact_window, enable_tool_search=enable_tool_search)
        plans.append({"repo": repo.name, "cmd": cmd, "mode": cmd_mode, "env": env,
                      "image": image_for_runtime_policy(policy)})
        if cmd_mode == "resume" and launch:
            mode = "resume"

    if launch:
        sem = threading.Semaphore(max(1, int(parallel)))
        threads: List[threading.Thread] = []

        def _work(plan: dict) -> None:
            with sem:
                log_path = out_dir / trial_name / f"{plan['repo']}.log"
                with open(log_path, "ab") as logf:
                    logf.write(f"\n===== run_trial launched at {_now()} ({plan['mode']}) =====\n".encode())
                    logf.write((" ".join(plan["cmd"]) + "\n").encode())
                    logf.flush()
                    proc = subprocess.Popen(plan["cmd"], cwd=str(root), env=plan["env"],
                                            stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                                            start_new_session=True)
                    logger.info("run_trial: %s worker pid=%d (%s)", plan["repo"], proc.pid, plan["mode"])
                    worker_rcs[plan["repo"]] = proc.wait()
                    logger.info("run_trial: %s worker exited rc=%s", plan["repo"], worker_rcs[plan["repo"]])

        for plan in plans:
            t = threading.Thread(target=_work, args=(plan,), name=f"run_trial:{plan['repo']}", daemon=True)
            t.start()
            threads.append(t)
            time.sleep(1.0)   # stagger container boots
        for t in threads:
            t.join()

    repo_results: List[RepoResult] = []
    missing: List[str] = []
    if not dry_run:
        for repo in repo_paths:
            trial_dir = repo / "e2e_trial" / trial_name
            if not trial_dir.exists():
                # A repo that never produced a trial directory is NOT a zero, it is an unknown.
                # Averaging over the survivors would report a plausible score for a trial that
                # never covered its repo set, so it is recorded and the result is marked
                # incomplete; the consumer decides whether to report or retry.
                notes.append(f"{repo.name}: no trial directory {trial_dir} (worker rc="
                             f"{worker_rcs.get(repo.name)})")
                missing.append(repo.name)
                continue
            repo_mode = ("completed" if completed.get(repo.name)
                         else "aggregate" if aggregate_only
                         else "resume" if resuming.get(repo.name) else "fresh")
            repo_results.append(aggregate_repo(repo, trial_name, worker_rc=worker_rcs.get(repo.name),
                                               mode=repo_mode))
    macro, micro = aggregate(repo_results)
    if missing:
        logger.error("run_trial: %d of %d requested repo(s) produced no result: %s. The macro "
                     "average below covers only the rest and is NOT a trial-level score.",
                     len(missing), len(repo_paths), ", ".join(missing))
    result = TrialResult(
        api_version=api.API_VERSION, benchmark_version=bench, harness_sha=_git(root, "rev-parse", "HEAD"),
        data_commit=data_commit, data_root=str(data_root_p), trial_name=trial_name, model=model,
        agent_version=agent_version, base_url=base_url, agent_env=agent_env, started_at=started,
        finished_at=_now(), repos=repo_results, macro=macro, micro=micro, mode=mode, notes=notes,
        repos_requested=[r.name for r in repo_paths], repos_missing=missing, complete=not missing,
    )
    if dry_run:
        result.notes.append("dry-run: nothing launched")
        result.notes.extend(f"{p['repo']}: {' '.join(p['cmd'])}" for p in plans)
        result.notes.extend(f"{p['repo']} env: " + json.dumps(
            {k: v for k, v in p["env"].items() if k.startswith(("SWE_MILESTONE_", "UNIFIED_"))}) for p in plans)
    if not dry_run:
        try:
            out = out_dir / f"{trial_name}.trial_result.json"
            result.to_json(out)
            result.result_path = str(out)
            logger.info("run_trial: wrote %s", out)
        except OSError as exc:
            result.notes.append(f"could not write trial_result.json: {exc}")
    return result


# ───────────────────────────── CLI ─────────────────────────────
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="SWE-Milestone CTE runner for external consumers (harness.api.run_trial)")
    ap.add_argument("--config", type=Path, help="YAML with the run_trial keyword arguments")
    ap.add_argument("--data-root")
    ap.add_argument("--trial-name")
    ap.add_argument("--base-url")
    ap.add_argument("--model")
    ap.add_argument("--agent")
    ap.add_argument("--agent-version")
    ap.add_argument("--reasoning-effort")
    ap.add_argument("--auto-compact-window", type=int)
    ap.add_argument("--enable-tool-search")
    ap.add_argument("--build-failure-fail-closed", action="store_true")
    ap.add_argument("--repos", nargs="*")
    ap.add_argument("--milestones")
    ap.add_argument("--timeout", type=int)
    ap.add_argument("--parallel", type=int)
    ap.add_argument("--out-root")
    ap.add_argument("--unprotected", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--aggregate-only", action="store_true")
    ap.add_argument("--json", action="store_true", help="print the TrialResult JSON to stdout")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    kw: Dict[str, Any] = {}
    if args.config:
        import yaml  # noqa: PLC0415
        kw.update(yaml.safe_load(args.config.read_text()) or {})
    for key, val in (("data_root", args.data_root), ("trial_name", args.trial_name), ("base_url", args.base_url),
                     ("model", args.model), ("agent", args.agent), ("agent_version", args.agent_version),
                     ("milestones", args.milestones), ("reasoning_effort", args.reasoning_effort),
                     ("auto_compact_window", args.auto_compact_window),
                     ("enable_tool_search", args.enable_tool_search),
                     ("timeout_s", args.timeout), ("parallel", args.parallel), ("out_root", args.out_root)):
        if val is not None:
            kw[key] = val
    repos = args.repos if args.repos is not None else kw.pop("repos", None)
    if args.unprotected:
        kw["unprotected"] = True
    if args.build_failure_fail_closed:
        kw["build_failure_fail_closed"] = True
    if args.dry_run:
        kw["dry_run"] = True
    if args.aggregate_only:
        kw["aggregate_only"] = True
    if isinstance(kw.get("data_root"), str):
        kw["data_root"] = os.path.expandvars(kw["data_root"])
    missing = [k for k in ("data_root", "trial_name", "base_url") if not kw.get(k)]
    if missing:
        ap.error(f"missing required settings: {missing}")
    result = run_trial(repos, **kw)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(f"trial {result.trial_name} [{result.mode}] macro score={result.macro.get('score', 0):.4f} "
              f"resolve={result.macro.get('resolve_rate', 0):.4f} over {len(result.repos)} repo(s)")
        for r in result.repos:
            print(f"  {r.repo}: score={r.score:.4f} resolve={r.n_resolved}/{r.n_graded} "
                  f"unfinished={r.n_unfinished} infra_invalid={r.n_infra_invalid} exit={r.agent_exit.get('reason')}")
        for n in result.notes:
            print(f"  note: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
