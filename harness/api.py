"""harness/api.py — stable integration surface for external training / eval stacks.

DeepCommit-RL's Polar RL pipeline imports ONLY this module from EvoClaw. Internal
refactors of e2e/ are free as long as these signatures hold semantically. See the
consumer-side contract doc: docs/design/task_source_integration_zh.md.

Layering: EvoClaw owns ALL domain knowledge (prompting, container env, leak
masking, grading). The training stack owns execution (Polar concurrency/trace/
on-policy serving) and optimization (verl GRPO). This module is the seam.

Two-state contract: TaskRecord is the single cross-layer structure — its fields
mirror the parquet row the training stack persists offline; in-process it is the
same dict reconstructed. One schema, two states.

Heavy e2e modules (docker, container surgery) are imported lazily inside the
functions that need them, so importing this module is cheap and side-effect free.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

# 1.2: realigned with main's v1.0.2 snapshot/grading contract.
#  - EvalResult gained `scoring_blocked` (1.1) and the infra verdict fields
#    (`infra_invalid`, `infra_invalid_reason`, `infrastructure_failure`,
#    `eval_status`). Both mark a cell as unscoreable; consumers must drop or
#    retry it instead of feeding `resolved` to a reward.
#  - extract_snapshot now writes the `<tar>.integrity.json` sidecar the official
#    evaluator requires, and captures build manifests through the overlay.
#  - harden_container() truncates the work container's git history (anti-leak).
#  - iter_task_records emits the released image ref pinned to BENCHMARK_VERSION.
# 1.3: consumer-built sandboxes get the full pre-agent protection set.
#  - harden_container() restores /testbed's ownership after the truncation, so it
#    can run after the consumer created its agent user (a root-owned .git/logs/HEAD
#    otherwise fails the agent's first commit with EACCES).
#  - verify_masking() re-derives the masked set and checks every file is still
#    root:000 (Rust: inline #[cfg(test)] gone). Call it LAST before the agent
#    starts: any recursive chown of /testbed after mask_tests silently undoes it.
#  - quarantine_container() applies the repo's anti-cheat network policy to a
#    consumer-built container (iptables allowlist + registry denies + offline
#    package managers) and verifies it; allow_endpoints admits the policy server.
#  - run_trial() is the CTE entry point: the official run_e2e per repo, official
#    aggregation, one TrialResult. session_key() / repo_image() / discover_repos()
#    are its companions, so a consumer never has to reach past this module.
#  - Seam env names follow the harness rename: SWE_MILESTONE_DATA_ROOT,
#    SWE_MILESTONE_EXEC_USER / _HOME. Any EVOCLAW_* variable is a hard error.
API_VERSION = "1.3"
PROMPT_DIR = Path(__file__).parent / "e2e" / "prompt"
# The node runtime Claude Code needs. go/java/rust testbed images ship none;
# runtime_spec() bootstraps this static build when npm is absent.
NODE_VERSION = "v22.21.1"
CLAUDE_CODE_PKG = "@anthropic-ai/claude-code@2.1.111"


# ───────────────────────────── data contracts ──────────────────────────────
@dataclass
class TaskRecord:
    """One milestone task. Field set == the parquet `extra_info` contract."""
    instance_id: str
    docker_image: str
    problem_statement: str
    fail_to_pass: list = field(default_factory=list)
    pass_to_pass: list = field(default_factory=list)
    framework: str = "ginkgo"
    test_cmd: str = ""
    test_configs: list = field(default_factory=list)
    fail_to_pass_by_framework: dict = field(default_factory=dict)
    pass_to_pass_by_framework: dict = field(default_factory=dict)
    # source-private; the training stack carries it through opaquely. Holds e.g.
    # new_tests (for masking), filter_list (flaky exclusion), repo_config
    # (src/test dirs), quarantine policy name.
    source_spec: dict = field(default_factory=dict)

    @classmethod
    def from_row(cls, ei: dict) -> "TaskRecord":
        """Build from a parquet extra_info dict (numpy-array fields tolerated)."""
        def _list(v):
            # numpy-safe: parquet round-trips list fields as ndarray, whose
            # truthiness raises — never use `if v` on these.
            if v is None:
                return []
            if hasattr(v, "tolist"):
                v = v.tolist()
            return list(v)

        def _plain(v):
            # recursively coerce numpy arrays/scalars to plain python so
            # downstream `x or []` truthiness is safe everywhere (parquet
            # round-trips nested lists as ndarray).
            if v is None or isinstance(v, (str, int, float, bool)):
                return v
            if hasattr(v, "item") and not hasattr(v, "__len__"):
                return v.item()
            if hasattr(v, "tolist") and not isinstance(v, (str, bytes)):
                return _plain(v.tolist())
            if isinstance(v, dict):
                return {str(k): _plain(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [_plain(x) for x in v]
            return v

        def _dict(v):
            v = _plain(v)
            return v if isinstance(v, dict) else {}

        return cls(
            instance_id=str(ei.get("instance_id", "")),
            docker_image=str(ei["docker_image"]),
            problem_statement=str(ei.get("problem_statement", "")),
            fail_to_pass=_list(ei.get("FAIL_TO_PASS") if "FAIL_TO_PASS" in ei else ei.get("fail_to_pass")),
            pass_to_pass=_list(ei.get("PASS_TO_PASS") if "PASS_TO_PASS" in ei else ei.get("pass_to_pass")),
            framework=str(ei.get("framework") or "ginkgo"),
            test_cmd=str(ei.get("test_cmd") or ""),
            test_configs=_list(ei.get("test_configs")),
            fail_to_pass_by_framework=_dict(ei.get("fail_to_pass_by_framework")),
            pass_to_pass_by_framework=_dict(ei.get("pass_to_pass_by_framework")),
            source_spec=_dict(ei.get("source_spec")),
        )


@dataclass
class RuntimeSpec:
    """How to prepare the work container (the consumer maps this onto its fabric)."""
    prepare: list[str]                      # idempotent shell steps
    artifact: dict = field(default_factory=dict)      # {mode, baseline, excludes}
    requirements: dict = field(default_factory=dict)  # {network, privileged, mounts}


@dataclass
class AgentSessionSpec:
    """How the agent runs inside the container (the 'driver's manual')."""
    run_as: str = "fakeroot"
    cli_args: list[str] = field(default_factory=list)
    env: dict = field(default_factory=dict)
    completion: dict = field(default_factory=dict)    # {signal_cmd, nudge_prompt, max_nudges}


@dataclass
class MaskReport:
    masked_test_files: int = 0
    masked_src_files: int = 0
    skipped: bool = False
    reason: str = ""
    # 1.3: the masked set itself, so verify_masking can check exactly what was
    # applied (a Rust file whose inline tests were removed no longer maps back
    # from its test names, so re-derivation alone under-checks).
    masked_files: list = field(default_factory=list)        # [{path, kind}] kind: test | rust_src
    failed_files: list = field(default_factory=list)        # mask attempted, failed
    unmapped_tests: list = field(default_factory=list)      # unknown format, never masked
    file_not_found_tests: list = field(default_factory=list)  # file absent in container (no leak)


# ───────────────────────────── prompt / instruction ─────────────────────────
def get_prompt_template(version: str = "milestone_v1") -> str:
    """Raw template text ({srs_content}/{milestone_id} placeholders)."""
    p = PROMPT_DIR / f"{version}.md"
    if not p.exists():
        raise FileNotFoundError(f"prompt template not found: {p}")
    return p.read_text(encoding="utf-8")


def build_instruction(task: TaskRecord, version: str = "milestone_v1") -> str:
    """Final rendered instruction. .replace (not .format): SRS bodies contain braces."""
    return (get_prompt_template(version)
            .replace("{srs_content}", task.problem_statement)
            .replace("{milestone_id}", _milestone_id(task)))


# ───────────────────────────── container runtime ────────────────────────────
def node_bootstrap_command() -> str:
    """Idempotent: install node iff absent (go/java/rust images ship none).
    Static .tar.gz (dubbo lacks xz); every milestone image has curl."""
    return (
        "command -v npm >/dev/null 2>&1 || { echo '[evoclaw] image lacks node - installing'; "
        f"curl -fsSL https://nodejs.org/dist/{NODE_VERSION}/node-{NODE_VERSION}-linux-x64.tar.gz "
        "| tar -xz -C /usr/local --strip-components=1; }"
    )


def runtime_spec(task: TaskRecord, *, agent: str = "claude-code",
                 baseline_tag: str = "polar-baseline") -> RuntimeSpec:
    """Work-container recipe. The solution-artifact is a git-archive SNAPSHOT
    (official PatchEvaluator consumes a tar, not a diff) taken against the
    baseline tag set here. quarantine (if any) is declared, not executed."""
    prepare = [
        node_bootstrap_command(),
        f"npm install -g {CLAUDE_CODE_PKG}",
        ("cd /testbed && git config user.email evoclaw@test && git config user.name EvoClaw && "
         "git add -A && git commit -qm baseline 2>/dev/null; "
         f"git tag -f {baseline_tag}; true"),
    ]
    requirements = {"network": "host"}
    q = task.source_spec.get("quarantine")
    if q:
        # declarative: the fabric satisfies it (bridge + iptables allowlist).
        requirements["network"] = f"allowlist:{q.get('name', task.instance_id)}"
        requirements["quarantine"] = q
    return RuntimeSpec(
        prepare=prepare,
        artifact={"mode": "snapshot", "baseline": baseline_tag,
                  "excludes": [".claude/**", "**/.claude/**", "node_modules/**", "**/node_modules/**"]},
        requirements=requirements,
    )


def agent_session_spec(task: TaskRecord, *, agent: str = "claude-code") -> AgentSessionSpec:
    """Declarative session 'driver's manual'. The consumer's harness (Polar)
    stays the session driver (transport/trace); this supplies the knobs that are
    EvoClaw domain knowledge: completion signal + the official nudge."""
    if agent != "claude-code":
        raise NotImplementedError(f"agent_session_spec: only claude-code wired, got {agent!r}")
    mid = _milestone_id(task)
    return AgentSessionSpec(
        run_as="fakeroot",
        cli_args=["--dangerously-skip-permissions"],
        env={},  # training-side overlays CLAUDE_CODE_* knobs; source sets none by default
        completion={
            "signal_cmd": f"cd /testbed && git tag -l agent-impl-{mid}",
            "nudge_prompt": (
                "You have not created the submission tag yet. Please commit your "
                f"changes and create the tag:\n```bash\ngit add .\ngit commit -m "
                f'"Implement {mid}"\ngit tag agent-impl-{mid}\n```\n\n'
                f"**IMPORTANT**: The `git tag agent-impl-{mid}` command signals task completion."
            ),
            "max_nudges": 1,
        },
    )


# ───────────────────────────── leak masking ────────────────────────────────
def _src_filter_for(task: TaskRecord):
    """Build a SrcFileFilter from repo_config in source_spec (src/test/exclude
    dirs, carried from the EvoClaw-data config/<repo>.yaml).

    Passes ALL FIVE pattern sets. Dropping generated_patterns / modifiable_test_patterns
    (as an earlier version did) makes should_include_in_snapshot() never include generated
    code (e.g. *.pb.go, wire_gen.go) or agent-modifiable test files — so codegen-heavy repos
    (Go) lose required files from the snapshot, fail to compile under grading, and get
    misjudged. mask_tests and extract_snapshot share this filter, so both need the full set."""
    from harness.utils.src_filter import SrcFileFilter  # noqa: PLC0415
    rc = task.source_spec.get("repo_config") or {}
    return SrcFileFilter(
        src_dirs=rc.get("src_dirs") or [],
        test_dirs=rc.get("test_dirs") or [],
        exclude_patterns=rc.get("exclude_patterns"),
        generated_patterns=rc.get("generated_patterns"),
        modifiable_test_patterns=rc.get("modifiable_test_patterns"),
    )


def mask_tests(container_name: str, task: TaskRecord, *, workdir: str = "/testbed",
               strict: bool = False) -> MaskReport:
    """Pre-session leak/tamper guard: hide the milestone's graded + new tests
    (chmod 000; rust inline #[cfg(test)] removal) so the agent can neither read
    expected assertions nor overwrite them. Runs host-side against the live
    container — the consumer wires this as a pre-agent hook.

    test_names = fail_to_pass + new_tests (from source_spec). strict=False keeps
    a parse failure from aborting the whole session (logged in the report)."""
    from harness.e2e.test_masking import mask_tests_by_names, TestMappingError  # noqa: PLC0415
    test_names = [str(t) for t in task.fail_to_pass]
    for nt in (task.source_spec.get("new_tests") or []):
        test_names.append(nt.get("test_id") if isinstance(nt, dict) else str(nt))
    test_names = [t for t in test_names if t]
    if not test_names:
        return MaskReport(skipped=True, reason="no fail_to_pass/new_tests to mask")
    try:
        r = mask_tests_by_names(container_name=container_name, test_names=test_names,
                                src_filter=_src_filter_for(task), workdir=workdir, strict=strict)
    except TestMappingError as e:
        return MaskReport(skipped=True, reason=f"unmapped tests (new framework?): {e}")
    failed = list(r.get("failed_files") or [])
    masked_files = [{"path": path, "kind": kind}
                    for path, kind in sorted((r.get("file_types") or {}).items())
                    if path not in failed]
    return MaskReport(masked_test_files=r.get("masked_test_files", 0),
                      masked_src_files=r.get("masked_src_files", 0),
                      masked_files=masked_files, failed_files=failed,
                      unmapped_tests=list(r.get("unmapped_tests") or []),
                      file_not_found_tests=list(r.get("file_not_found_tests") or []))


@dataclass
class MaskVerifyReport:
    ok: bool
    checked: int = 0
    violations: list = field(default_factory=list)   # [{path, kind, mode, owner, reason}]
    skipped: bool = False
    reason: str = ""


def verify_masking(container_name: str, task: TaskRecord, *, report: Optional[MaskReport] = None,
                   workdir: str = "/testbed") -> MaskVerifyReport:
    """Post-condition check for mask_tests, to be run LAST before the agent starts.

    mask_tests is `chown root:root` + `chmod 000` on the graded test files, and an
    in-place removal of the root-level inline test regions of Rust sources. Any
    later recursive chown of /testbed silently undoes the first kind: the file
    becomes agent-owned with mode 000 and the agent can `chmod +r` it. A consumer
    whose sandbox setup chowns /testbed (slime's ensure_agent_user does) must call
    this after the last such step and abort on ok == False.

    Pass the MaskReport that mask_tests returned: its masked_files is the exact
    applied set, and its failed/unmapped tests are reported as violations (they
    were never masked). Without a report the set is re-derived from the task with
    the same mapping mask_tests uses; that path cannot see a Rust file whose
    inline tests are already gone (nothing to map), so it under-checks Rust.

    Checks: test files must be root-owned with mode 000; Rust sources must have
    no root-level test regions left according to the official detector."""
    import subprocess  # noqa: PLC0415
    from harness.e2e.test_masking import _map_tests_to_files, detect_file_type  # noqa: PLC0415
    from harness.utils.rust_test_filter import (  # noqa: PLC0415
        RustTestFilterError, _read_file_from_container, find_test_ranges_from_content)

    violations: list = []
    if report is not None:
        if report.skipped:
            return MaskVerifyReport(ok=True, skipped=True, reason=report.reason or "mask_tests skipped")
        expected = [(f["path"], f["kind"]) for f in report.masked_files]
        for path in report.failed_files:
            violations.append({"path": path, "kind": "", "mode": "", "owner": "",
                               "reason": "mask_tests failed on this file"})
        for t in report.unmapped_tests:
            violations.append({"path": "", "kind": "test", "mode": "", "owner": "",
                               "reason": f"unmapped test never masked: {t}"})
    else:
        test_names = [str(t) for t in task.fail_to_pass]
        for nt in (task.source_spec.get("new_tests") or []):
            test_names.append(nt.get("test_id") if isinstance(nt, dict) else str(nt))
        test_names = [t for t in test_names if t]
        if not test_names:
            return MaskVerifyReport(ok=True, skipped=True, reason="no fail_to_pass/new_tests to mask")
        src_filter = _src_filter_for(task)
        file_to_tests, unmapped, _not_found, _methods = _map_tests_to_files(
            container_name, test_names, src_filter, workdir)
        expected = [(path, detect_file_type(path, src_filter)) for path in sorted(file_to_tests)]
        for t in unmapped:
            violations.append({"path": "", "kind": "test", "mode": "", "owner": "",
                               "reason": f"unmapped test never masked: {t}"})

    for path, kind in expected:
        if kind == "rust_src":
            content = _read_file_from_container(container_name, path)
            if content is None:
                violations.append({"path": path, "kind": kind, "mode": "", "owner": "",
                                   "reason": "cannot read masked Rust source"})
                continue
            try:
                ranges = find_test_ranges_from_content(content, path, only_root_level=True,
                                                       reject_nested=False)
            except RustTestFilterError as exc:
                violations.append({"path": path, "kind": kind, "mode": "", "owner": "",
                                   "reason": f"test detection failed: {exc}"})
                continue
            if ranges:
                violations.append({"path": path, "kind": kind, "mode": "", "owner": "",
                                   "reason": f"{len(ranges)} inline test region(s) still present"})
            continue
        r = subprocess.run(["docker", "exec", "-w", workdir, container_name,
                            "stat", "-c", "%a %U", path],
                           capture_output=True, text=True)
        if r.returncode != 0:
            violations.append({"path": path, "kind": kind, "mode": "", "owner": "",
                               "reason": f"stat failed: {(r.stderr or r.stdout).strip()}"})
            continue
        parts = (r.stdout or "").split()
        mode, owner = (parts + ["", ""])[:2]
        if mode != "0" or owner != "root":
            violations.append({"path": path, "kind": kind, "mode": mode, "owner": owner,
                               "reason": "expected root:000 (mask undone or never applied)"})
    ok = not violations
    if not ok:
        logger.error("verify_masking: %d violation(s) on %s: %s", len(violations), container_name,
                     "; ".join(f"{v['path'] or v['reason']}" for v in violations[:5]))
    return MaskVerifyReport(ok=ok, checked=len(expected), violations=violations)




# ───────────────────────────── grading ─────────────────────────────────────
# The SWE-Milestone-data tree (classification / test_config / metadata / config /
# filter_list) lives on the box; grading reads it directly so ALL judging logic
# stays official — no reimplementation, no materialization. Point at it with
# SWE_MILESTONE_DATA_ROOT (default ~/workspace/SWE-Milestone-data). The consumer is
# responsible for a version self-check (tree commit ↔ parquet source_commit).
#
# Naming rule: the harness renamed EVOCLAW_* to SWE_MILESTONE_* on 2026-07-08 as a
# clean break (harness.e2e.env_guard hard-exits run_e2e/run_all on any legacy
# name). The seam applies the same rule at call time: a legacy name raises with
# the rename hint, it is never aliased silently.
DATA_ROOT_ENV = "SWE_MILESTONE_DATA_ROOT"
DEFAULT_DATA_ROOT = str(Path.home() / "workspace" / "SWE-Milestone-data")
EXEC_USER_ENV = "SWE_MILESTONE_EXEC_USER"
EXEC_HOME_ENV = "SWE_MILESTONE_EXEC_HOME"
_LEGACY_ENV_PREFIX = "EVOCLAW_"


def reject_legacy_env() -> None:
    """Raise if any legacy EVOCLAW_* variable is set (same rule as harness.e2e.env_guard)."""
    legacy = sorted(k for k in os.environ if k.startswith(_LEGACY_ENV_PREFIX))
    if legacy:
        hint = ", ".join(f"{k} -> SWE_MILESTONE_{k[len(_LEGACY_ENV_PREFIX):]}" for k in legacy)
        raise RuntimeError(
            "legacy EVOCLAW_* environment variables are not supported by harness.api "
            f"(renamed to SWE_MILESTONE_* on 2026-07-08, no silent aliasing): {hint}")


def resolve_data_root() -> Path:
    """The data tree root: SWE_MILESTONE_DATA_ROOT, else the default path."""
    reject_legacy_env()
    return Path(os.environ.get(DATA_ROOT_ENV) or DEFAULT_DATA_ROOT)


def exec_user() -> str:
    """The in-container user the snapshot/verify paths exec as (default fakeroot)."""
    reject_legacy_env()
    return os.environ.get(EXEC_USER_ENV) or "fakeroot"


def exec_home() -> str:
    reject_legacy_env()
    return os.environ.get(EXEC_HOME_ENV) or f"/home/{exec_user()}"


def _repo_dir(task: TaskRecord, root: Optional[Path] = None) -> str:
    """Resolve the EvoClaw-data subdir for this task, CASE-INSENSITIVELY against
    `root`. Docker image names are lowercased per OCI (e.g. burntsushi_ripgrep)
    but the data dir may be CamelCase (BurntSushi_ripgrep) — a plain
    docker-image-derived name then misses the tree and every milestone of that
    repo infra-fails. Candidates, correct-case first: source_spec.repo, the
    instance_id prefix (`<repo>__<milestone>` — carries the canonical case), then
    the repo half of docker_image; resolve by exact dir match, then case-insensitive."""
    cands: list[str] = []
    if task.source_spec.get("repo"):
        cands.append(str(task.source_spec["repo"]))
    if "__" in task.instance_id:
        cands.append(task.instance_id.split("__")[0])
    img = (task.docker_image or "").split(":")[0]
    if "/" in img:
        cands.append(img.split("/")[0])
    if not cands:
        cands.append(task.instance_id)
    if root is not None and root.is_dir():
        for c in cands:                       # exact match wins
            if (root / c).is_dir():
                return c
        actual = {d.name.lower(): d.name for d in root.iterdir() if d.is_dir()}
        for c in cands:                       # case-insensitive fallback
            if c.lower() in actual:
                return actual[c.lower()]
    return cands[0]


def _milestone_id(task: TaskRecord) -> str:
    """Tree milestone id (e.g. 'M023'): source_spec.milestone_id, else the suffix
    of instance_id (`<repo>__<milestone>`), else instance_id itself."""
    return str(task.source_spec.get("milestone_id")
               or (task.instance_id.split("__")[-1] if "__" in task.instance_id else task.instance_id))


def _normalize_eval(d: dict) -> dict:
    """Official EvaluationResult.to_dict() -> cross-source EvalResult primitives
    (§4). reward formula stays a training-side config; we only pass counts.

    `scoring_blocked` is the harness's fail-closed verdict (v1.0.2): when filter-list
    validation fails, the official evaluator stamps the RAW result and produces no
    filtered derivative. The raw numbers are still present and still parsed here, but
    they are NOT a valid score — the required set was never reduced by the waivers it
    should have been. Consumers must treat a blocked result as unscoreable (drop the
    sample) rather than feeding `resolved` into a reward."""
    ts = d.get("tests_status", {}) or {}
    f2p = ts.get("FAIL_TO_PASS", {}) or {}
    p2p = ts.get("PASS_TO_PASS", {}) or {}
    summ = d.get("test_summary", {}) or {}
    n_fixed = len(f2p.get("success", []) or [])
    blocked = bool(d.get("scoring_blocked", False))
    if blocked:
        logger.error(
            "scoring_blocked: filter-list validation failed for this cell; the raw "
            "numbers below are NOT a valid score (see harness.e2e.evaluator."
            "generate_filtered_evaluation). Drop this sample."
        )
    # Infra verdict. The official consumer (orchestrator._require_scoreable) raises
    # InfrastructureFailureError on these and RETRIES the cell instead of recording
    # it. Without them a docker/OOM/network-poisoned cell that ran zero tests is
    # indistinguishable from an honest "agent did not solve it" and poisons the
    # reward with a false negative.
    infra_invalid = bool(d.get("infra_invalid", False))
    infra_reason = str(d.get("infra_invalid_reason") or "")
    infra_failure = str(d.get("infrastructure_failure") or "")
    eval_status = str(d.get("eval_status") or "")
    if infra_invalid or infra_failure or eval_status == "infra-invalid":
        logger.error(
            "infrastructure failure: this cell is not safe to score (status=%r, "
            "signature=%r, reason=%r). Retry it; do not feed it to the reward.",
            eval_status, infra_failure, infra_reason,
        )
    return {
        "scoring_blocked": blocked,
        "infra_invalid": infra_invalid,
        "infra_invalid_reason": infra_reason,
        "infrastructure_failure": infra_failure,
        "eval_status": eval_status,
        "scored_failure_reason": str(d.get("scored_failure_reason") or ""),
        "error": str(d.get("error") or ""),
        "resolved": bool(d.get("resolved", False)),
        "n_f2p_fixed": n_fixed,
        "n_f2p_inscope": n_fixed + len(f2p.get("failure", []) or []),
        "n_p2p_broken": len(p2p.get("failure", []) or []),
        "n_p2p_inscope": int(summ.get("pass_to_pass_required", 0) or 0),
        "failed_apply_patch": not bool(d.get("patch_successfully_applied", True)),
        "total_tests": int(summ.get("total", 0) or 0),
        "passed_tests": int(summ.get("passed", 0) or 0),
    }


def _zero_report_cell(milestone: str, message: str) -> dict:
    """The cell dict for a run that produced no test report, classified by the OFFICIAL
    rule (collect_results.is_zero_test_build_failure on the runner's diagnostic):
    build-failure evidence -> a scored failure (0, stays in the denominator);
    no evidence -> infra-invalid (unknown score, retry). Same shape the CTE
    orchestrator's `error` cell takes after collect_results annotates it."""
    from harness.e2e.collect_results import is_zero_test_build_failure  # noqa: PLC0415
    cell = {
        "milestone_id": milestone,
        "resolved": False,
        "eval_status": "error",
        "error": message,
        "error_message": message,
        "patch_successfully_applied": True,
        "test_summary": {"total": 0, "passed": 0},
        "tests_status": {},
        "scoring_blocked": False,
    }
    if is_zero_test_build_failure(cell):
        cell["scored_failure_reason"] = "build-failure-with-zero-tests"
        cell["eval_status"] = "failed"
        cell["infra_invalid"] = False
        cell["infra_invalid_reason"] = ""
        cell["patch_status"] = {"compilation_success": False}
        logger.warning("evaluate: %s produced no test report; build-failure evidence found -> "
                       "scored failure (0)", milestone)
    else:
        cell["infra_invalid"] = True
        cell["infra_invalid_reason"] = "zero-tests-without-build-evidence"
        cell["eval_status"] = "infra-invalid"
        logger.error("evaluate: %s produced no test report and no build-failure evidence -> "
                     "infra-invalid (retry)", milestone)
    return cell


def evaluate(task: TaskRecord, artifact: Path, *, scratch: Path,
             timeout_s: float = 1500.0, data_root: Optional[str] = None) -> dict:
    """Official grading. Points the official PatchEvaluator + the official
    flaky-filter pass at the on-box EvoClaw-data tree — we reimplement NOTHING:
    the per-test timeout, test command, baseline classification and filter_list
    are all read by official code from the tree. `timeout_s` is advisory (the
    real per-test timeout is the tree's metadata `pytest_timeout`). Judging runs
    in a FRESH milestone container (clean isolation). Concurrency-safe: every
    output lands under the per-call `scratch`."""
    import json  # noqa: PLC0415
    from harness.e2e.evaluator import PatchEvaluator, generate_filtered_evaluation  # noqa: PLC0415

    root = Path(data_root) if data_root else resolve_data_root()
    repo = _repo_dir(task, root)
    milestone = _milestone_id(task)
    ws = root / repo
    classification = ws / "test_results" / milestone / f"{milestone}_classification.json"
    if not classification.exists():
        raise FileNotFoundError(
            f"classification not found (EvoClaw-data tree missing or version-mismatched?): {classification}")

    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    ev = PatchEvaluator(workspace_root=ws, milestone_id=milestone,
                        patch_file=Path(artifact), baseline_classification=classification,
                        output_dir=scratch)
    raw_path = scratch / "evaluation_result.json"
    try:
        result = ev.evaluate()                  # official: container -> apply -> tests -> compare
    except RuntimeError as exc:
        # The official test runner raises when a submission produced NO test report at
        # all ("No valid test report files generated"; typically the graded tests do
        # not compile against the agent's tree). The CTE orchestrator records that as
        # an `error` cell and collect_results scores it 0 in the denominator when the
        # message carries build-failure evidence, or flags it infra-invalid otherwise.
        # Mirror that here instead of aborting the sample: an agent that broke the build
        # did not solve the milestone, and it must not be retried as if docker had died.
        if not str(exc).startswith("No valid test report files generated"):
            raise
        raw_path.write_text(json.dumps(_zero_report_cell(milestone, str(exc))))
        return _normalize_eval(json.loads(raw_path.read_text()))
    raw_path.write_text(json.dumps(result.to_dict()))
    filtered = generate_filtered_evaluation(raw_path, ws, milestone)   # official flaky filter_list pass
    # None has two meanings and the raw file distinguishes them: benign (this milestone
    # has no filter_list -> raw IS the score) vs fail-closed (validation failed -> the
    # official code stamped `scoring_blocked` on the raw file). Re-read raw either way;
    # _normalize_eval surfaces the stamp so the caller can drop blocked samples.
    final = json.loads((filtered or raw_path).read_text())
    return _normalize_eval(final)


# ───────────────────────────── snapshot extraction ─────────────────────────
# Public extraction of the OFFICIAL git-archive snapshot logic (was private in
# e2e/run_milestone.py: _extract_snapshot + _extract_snapshot_from_workdir). Pure
# host-side `docker exec` against the live work container — no orchestrator / managed-
# container coupling, so the training stack can call it directly after the agent run.
def _fakeroot_exec(container_name: str) -> list[str]:
    """The `docker exec` prefix the official snapshot path uses: git as the agent user in
    /testbed. The user is `fakeroot` on harness-built containers; a consumer whose sandbox
    creates a different user sets SWE_MILESTONE_EXEC_USER (and _HOME) instead of patching."""
    return ["docker", "exec", "--user", exec_user(), "-e", f"HOME={exec_home()}",
            "-w", "/testbed", container_name]


def harden_container(container_name: str, *, main_branch: str = "main") -> str:
    """Pre-agent anti-leak hook: truncate /testbed's git history, return the baseline commit.

    The milestone images ship the FULL upstream history plus the generator's own
    commits — "Add test code for <milestone>" and "End state for <milestone>" are
    reachable from tags/remotes, so an agent that runs `git log --all` / `git show`
    reads both the graded tests and the reference solution. `mask_tests` does not
    help: it chmods the working tree, while the objects stay in .git. The official
    harness closes this at container setup (container_setup.truncate_git_history,
    "prevent agent from seeing future commits"); a consumer that builds its own
    sandbox must call this instead, BEFORE the agent starts.

    Returns the post-truncation HEAD, i.e. the pre-agent baseline commit — pass it
    to extract_snapshot(baseline=...) so the manifest overlay diffs against the
    real BASE rather than an inferred one.

    Ownership: the truncation runs as root and rewrites .git (index, HEAD, config,
    packed-refs, logs, packs). If the consumer already created its agent user and
    chowned /testbed to it, those root-owned files break the agent's first commit
    ("unable to append to '.git/logs/HEAD': Permission denied"). So the owner of
    /testbed is recorded first and restored on .git afterwards; the call order
    relative to the agent-user creation then does not matter. It never chowns the
    working tree, so a mask applied before it (root:000) survives — but keep the
    documented order anyway: harden, then mask_tests, then quarantine, then
    verify_masking, and no chown after that.

    Raises RuntimeError on any failure: a container whose history was not truncated
    must not run an agent."""
    import subprocess  # noqa: PLC0415
    from harness.e2e.container_setup import ContainerSetup  # noqa: PLC0415

    owner = subprocess.run(_root_exec(container_name) + ["stat", "-c", "%u:%g", "/testbed"],
                           capture_output=True, text=True)
    if owner.returncode != 0 or not owner.stdout.strip():
        detail = (owner.stderr or owner.stdout or "stat failed").strip()
        raise RuntimeError(f"harden_container: cannot stat /testbed on {container_name}: {detail}")
    owner_ids = owner.stdout.strip()

    script = ContainerSetup.truncate_history_script(main_branch=main_branch)
    r = subprocess.run(_root_exec(container_name) + ["/bin/sh", "-c", script],
                       capture_output=True, text=True)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "unknown git error").strip()
        raise RuntimeError(f"harden_container: history truncation failed on "
                           f"{container_name}: {detail}")
    head = subprocess.run(_root_exec(container_name) + ["git", "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    baseline = head.stdout.strip()
    if head.returncode != 0 or not baseline:
        detail = (head.stderr or head.stdout or "no HEAD").strip()
        raise RuntimeError(f"harden_container: could not read the baseline commit on "
                           f"{container_name}: {detail}")
    # Restore ownership of everything the truncation touched (.git only; the
    # working tree is never written by the script).
    restore = subprocess.run(_root_exec(container_name) + ["chown", "-R", owner_ids, "/testbed/.git"],
                             capture_output=True, text=True)
    if restore.returncode != 0:
        detail = (restore.stderr or restore.stdout or "chown failed").strip()
        raise RuntimeError(f"harden_container: could not restore ownership {owner_ids} on "
                           f"{container_name}:/testbed/.git: {detail}")
    logger.info("harden_container: %s history truncated; baseline=%s; .git owner restored to %s",
                container_name, baseline, owner_ids)
    return baseline


@dataclass
class QuarantineReport:
    ok: bool
    repo: str = ""
    mode: str = ""                    # protected | unprotected | absent
    policy_sha256: str = ""
    denied_hosts: list = field(default_factory=list)      # registry hosts verified unreachable
    allowed_endpoints: list = field(default_factory=list)  # consumer endpoints verified reachable
    env: dict = field(default_factory=dict)                # the agent-process env to carry (quarantine_agent_env)
    reason: str = ""


def quarantine_container(container_name: str, task: TaskRecord, *,
                         allow_endpoints: Optional[list] = None,
                         project_root: Optional[Path] = None,
                         unprotected: bool = False) -> QuarantineReport:
    """Anti-cheat network lockdown for a consumer-built work container.

    Applies the repo's runtime policy (quarantine_configs/<repo>.yaml) exactly as
    the official launcher does: iptables allowlist (loopback, established, the
    container's resolver, the harness whitelist minus the policy's denied
    registries and CIDRs, plus `allow_endpoints` port-scoped), /etc/hosts
    poisoning of code hosting and mirror domains, package managers forced
    offline against the image-baked closures, sudo revoked, then the official
    verification (OUTPUT policy DROP, github.com blocked, every denied registry
    unreachable, every allowed endpoint reachable). Also runs the official
    runtime-environment gate (sealed Go toolchain/proxy, cache access, Maven
    offline smoke) so a broken closure fails here, not as a silent zero.

    Requirements: the container was started with `--cap-add=NET_ADMIN`; the
    agent user (SWE_MILESTONE_EXEC_USER, default fakeroot) exists and owns
    /testbed; harden_container ran before this; mask_tests may run before or
    after (this call never chowns the working tree); verify_masking runs LAST.

    `allow_endpoints`: `host:port` / `ip:port` / URL entries the agent must
    reach (the policy server, e.g. 172.17.0.1:18001). Refused if one resolves
    into a denied CIDR. `unprotected=True` is the explicit escape for a repo
    without a policy (scores may be tainted; recorded in the report).

    Returns the report; raises RuntimeError on any failure (fail closed). The
    returned `env` is the environment the AGENT PROCESS must carry (the offline
    switches); inject it into the agent's session, it is not applied here."""
    import subprocess  # noqa: PLC0415
    from harness.e2e.agents.base import quarantine_agent_env  # noqa: PLC0415
    from harness.e2e.container_setup import ContainerSetup  # noqa: PLC0415
    from harness.e2e.runtime_policy_binding import (  # noqa: PLC0415
        RUNTIME_POLICY_ENV_KEYS, RUNTIME_POLICY_MODE_PROTECTED, RUNTIME_POLICY_MODE_UNPROTECTED,
        resolve_runtime_policy, runtime_policy_coverage_errors)

    reject_legacy_env()
    root = Path(project_root) if project_root else Path(__file__).resolve().parent.parent
    repo = _repo_dir(task, resolve_data_root())
    policy = resolve_runtime_policy(repo, root, unprotected=unprotected)
    errors = runtime_policy_coverage_errors(policy)
    if policy.mode != RUNTIME_POLICY_MODE_UNPROTECTED and (errors or policy.mode != RUNTIME_POLICY_MODE_PROTECTED):
        detail = "; ".join(errors) if errors else f"no quarantine_configs/{repo}.yaml"
        raise RuntimeError(
            f"quarantine_container: refusing to prepare {repo} without an anti-cheat policy "
            f"({detail}). Add the policy (docs/quarantine.md) or pass unprotected=True.")
    if policy.mode == RUNTIME_POLICY_MODE_UNPROTECTED:
        logger.warning("quarantine_container: %s runs UNPROTECTED (explicit); scores may be tainted", repo)

    image = subprocess.run(["docker", "inspect", "-f", "{{.Config.Image}}", container_name],
                           capture_output=True, text=True)
    if image.returncode != 0 or not image.stdout.strip():
        raise RuntimeError(f"quarantine_container: cannot inspect {container_name}: "
                           f"{(image.stderr or image.stdout).strip()}")

    endpoints = [str(e) for e in (allow_endpoints or []) if str(e).strip()]
    # The policy env is process-global for the harness code paths; scope it to
    # this call and restore afterwards so a consumer process can prepare
    # containers for different repos back to back.
    saved = {k: os.environ.get(k) for k in list(RUNTIME_POLICY_ENV_KEYS) + ["SWE_MILESTONE_UNPROTECTED"]}
    try:
        for k in RUNTIME_POLICY_ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.pop("SWE_MILESTONE_UNPROTECTED", None)
        os.environ.update(policy.env)
        if policy.mode == RUNTIME_POLICY_MODE_UNPROTECTED:
            os.environ["SWE_MILESTONE_UNPROTECTED"] = "1"
        setup = ContainerSetup(container_name=container_name, image_name=image.stdout.strip(),
                               repo_name=repo, agent_user=exec_user())
        setup.allow_endpoints = endpoints
        if policy.mode == RUNTIME_POLICY_MODE_PROTECTED:
            setup.verify_runtime_environment()   # sealed Go, cache access, Maven smoke (official gate)
        setup.lock_network()                     # includes verify_network_lockdown (fail closed)
        denied = [d.strip() for d in os.environ.get("SWE_MILESTONE_DENY_DOMAINS", "").split(",") if d.strip()]
        agent_env = quarantine_agent_env(exec_home())
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    logger.info("quarantine_container: %s locked (%s, policy %s); %d denied host(s), %d allowed endpoint(s)",
                container_name, policy.mode, policy.sha256[:12], len(denied), len(endpoints))
    return QuarantineReport(ok=True, repo=repo, mode=policy.mode, policy_sha256=policy.sha256,
                            denied_hosts=denied, allowed_endpoints=endpoints, env=agent_env)


def _snapshot_sidecar_payload(*, tag: str, snapshot_file: Path, manifest_overlay,
                              capture_filter: dict, agent_base_image_id: Optional[str] = None,
                              agent_tag_commit: Optional[str] = None) -> dict:
    """The capture sidecar the evaluator validates before it will grade a tar.

    Deliberately omits `repo_config_binding` / `runtime_policy_binding`: declaring
    either forces the evaluator into trial-pinned mode, which this seam cannot
    satisfy (it passes no frozen config path + sha256). Go repos additionally
    require agent_base_image_id / agent_tag_commit / capture_filter for exact
    module replay, so pass them whenever they are known."""
    from harness.utils.snapshot import make_snapshot_metadata  # noqa: PLC0415
    extra: dict = {"tag": tag, "ok": True, "capture_filter": capture_filter}
    if agent_base_image_id:
        extra["agent_base_image_id"] = agent_base_image_id
    if agent_tag_commit:
        extra["agent_tag_commit"] = agent_tag_commit
    return make_snapshot_metadata(tag=tag, snapshot_file=Path(snapshot_file),
                                  manifest_overlay=manifest_overlay, extra=extra)


def _root_exec(container_name: str) -> list[str]:
    """`docker exec` as root in /testbed (history truncation predates the agent user)."""
    return ["docker", "exec", "-w", "/testbed", container_name]


def _tag_exists(container_name: str, tag: str) -> bool:
    import subprocess  # noqa: PLC0415
    r = subprocess.run(_fakeroot_exec(container_name) + ["git", "tag", "-l", tag],
                       capture_output=True, text=True)
    return r.returncode == 0 and tag in r.stdout.split()


def _existing_src_dirs_git(container_name: str, src_dirs: list, tag: str) -> list:
    """Subset of src_dirs that exist at the tag (git ls-tree -d), order preserved."""
    import subprocess  # noqa: PLC0415
    existing = []
    for d in src_dirs:
        r = subprocess.run(_fakeroot_exec(container_name) + ["git", "ls-tree", "-d", tag, d.rstrip("/")],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            existing.append(d)
    return existing


def _existing_root_files_git(container_name: str, files: list, tag: str) -> set:
    import subprocess  # noqa: PLC0415
    if not files:
        return set()
    r = subprocess.run(
        _fakeroot_exec(container_name) + ["git", "ls-tree", "--name-only", tag, "--"] + list(files),
        capture_output=True, text=True)
    if r.returncode != 0:
        return set()
    return {ln for ln in r.stdout.strip().split("\n") if ln}


def _existing_workdir_dirs(container_name: str, src_dirs: list) -> list:
    import subprocess  # noqa: PLC0415
    existing = []
    for d in src_dirs:
        r = subprocess.run(_fakeroot_exec(container_name) + ["test", "-d", d.rstrip("/")], capture_output=True)
        if r.returncode == 0:
            existing.append(d)
    return existing


def _existing_root_files_workdir(container_name: str, files: list) -> set:
    import subprocess  # noqa: PLC0415
    if not files:
        return set()
    script = "; ".join(f'[ -f "{f}" ] && echo "{f}"' for f in files)
    r = subprocess.run(_fakeroot_exec(container_name) + ["sh", "-c", script], capture_output=True, text=True)
    return {ln for ln in r.stdout.strip().split("\n") if ln}


def _filter_snapshot_tar(tar_path: Path, src_filter, extra_build_manifests=None) -> int:
    """Drop tar members the official snapshot policy rejects, keeping src + generated
    + modifiable-test files and exactly the build manifests the capture overlay upserts.

    Mirrors run_milestone._filter_tar_archive. Two things this must NOT do (both were
    bugs here): skip the pass when a repo declares no test/exclude patterns — the pass
    also strips unchanged build manifests, so it is mandatory — and call
    SrcFileFilter.should_include_in_snapshot directly, which lets a pom.xml nested
    under a broad source dir through and overwrites the evaluator's END manifest
    (main's stale-POM pollution bug)."""
    import tarfile  # noqa: PLC0415
    from harness.utils.snapshot import should_include_snapshot_file  # noqa: PLC0415
    explicit = set(extra_build_manifests or set())
    n = 0
    tmp = tar_path.with_suffix(".filtered.tar")
    with tarfile.open(tar_path, "r") as src, tarfile.open(tmp, "w") as dst:
        for m in src.getmembers():
            if not m.isfile():
                dst.addfile(m)
                continue
            if should_include_snapshot_file(m.name, src_filter, extra_build_manifests=explicit):
                fo = src.extractfile(m)
                if fo:
                    dst.addfile(m, fo)
            else:
                n += 1
    tmp.replace(tar_path)
    return n


def _git_out(container_name: str, *args: str) -> str:
    """Run git in the container, returning stdout; raise with git's own message."""
    import subprocess  # noqa: PLC0415
    r = subprocess.run(_fakeroot_exec(container_name) + ["git", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return r.stdout


def _resolve_baseline(container_name: str, tag: str, baseline: Optional[str]) -> str:
    """The pre-agent BASE the manifest overlay diffs against.

    Explicit wins (harden_container returns it). Otherwise reuse the official legacy
    rule (run_milestone._infer_legacy_snapshot_baseline): the parent of the earliest
    reachable agent tag."""
    if baseline:
        return str(baseline).strip()
    reachable = []
    for cand in (t.strip() for t in _git_out(container_name, "tag", "-l", "agent-impl-*").splitlines()):
        if not cand:
            continue
        import subprocess  # noqa: PLC0415
        anc = subprocess.run(_fakeroot_exec(container_name)
                             + ["git", "merge-base", "--is-ancestor", cand, tag],
                             capture_output=True, text=True)
        if anc.returncode != 0:
            continue
        n = _git_out(container_name, "rev-list", "--count", f"{cand}..{tag}").strip()
        reachable.append((int(n or 0), cand))
    earliest = max(reachable, default=(0, tag))[1]
    base = _git_out(container_name, "rev-parse", f"{earliest}^").strip()
    if not base:
        raise RuntimeError(f"extract_snapshot: could not infer a pre-agent BASE for {tag}")
    logger.warning("extract_snapshot: no baseline passed; inferred %s from %s^ "
                   "(pass harden_container()'s return value for an exact BASE)", base, earliest)
    return base


def _manifest_overlay(container_name: str, *, baseline: str, rev: Optional[str],
                      src_dirs: list, src_filter):
    """The agent-authoritative build-manifest delta, expanded to the Go projection.

    Mirrors run_milestone._get_build_manifest_overlay_in_git / _in_workdir: upserts are
    ACMT-changed manifests, deletes are D-removed ones, then scoped Go manifests are
    projected exactly (unchanged Go metadata is captured too, so a prepared END manifest
    cannot supply a dependency the agent never declared). ``rev=None`` diffs the worktree."""
    from harness.utils.snapshot import (  # noqa: PLC0415
        ManifestOverlay, expand_atomic_manifest_overlay, find_build_manifests,
    )
    diff_target = [baseline, rev, "--"] if rev else [baseline, "--"]

    def _names(diff_filter: str) -> list:
        out = _git_out(container_name, "-c", "core.quotePath=false", "diff", "--no-renames",
                       "--name-only", "-z", f"--diff-filter={diff_filter}", *diff_target)
        return out.split("\0")

    inventory = _git_out(container_name, "-c", "core.quotePath=false", "ls-tree", "-r", "-z",
                         "--name-only", rev).split("\0") if rev else \
        _git_out(container_name, "-c", "core.quotePath=false", "ls-files", "-z").split("\0")
    overlay = ManifestOverlay.create(baseline,
                                     find_build_manifests(_names("ACMT"), src_filter),
                                     find_build_manifests(_names("D"), src_filter))
    return expand_atomic_manifest_overlay(
        overlay, find_build_manifests(inventory, src_filter), src_dirs)


def extract_snapshot(container_name: str, task: TaskRecord, *, dest: Path,
                     baseline: Optional[str] = None) -> Path:
    """Extract the gradeable source snapshot from the live work container into ``dest`` (a .tar),
    plus the ``<dest>.integrity.json`` sidecar the official evaluator requires.

    OFFICIAL logic, two paths: if the agent created the completion tag
    ``agent-impl-<milestone>`` → ``git archive`` that tag; otherwise fall back to taring the
    working dir. Both compute the build-manifest overlay against ``baseline`` (pass
    harden_container()'s return value; inferred from the earliest agent tag otherwise),
    archive only the source dirs that EXIST plus exactly the manifests that overlay upserts,
    filter the tar under the official snapshot policy, and write the integrity sidecar —
    without it PatchEvaluator refuses the tar ("Snapshot metadata sidecar is missing").

    The TaskRecord must carry ``source_spec.repo_config`` (built by iter_task_records). Raises
    RuntimeError on infra failure so the consumer can turn it into an abort."""
    from harness.utils.snapshot import ROOT_BUILD_FILES, get_snapshot_paths  # noqa: PLC0415
    import json  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    mid = _milestone_id(task)
    tag = f"agent-impl-{mid}"
    rc = task.source_spec.get("repo_config") or {}
    src_dirs = list(rc.get("src_dirs") or [])
    if not src_dirs:
        raise RuntimeError("extract_snapshot: no src_dirs in source_spec.repo_config "
                           "(build the TaskRecord via iter_task_records)")
    src_filter = _src_filter_for(task)
    tagged = _tag_exists(container_name, tag)
    base = _resolve_baseline(container_name, tag if tagged else "HEAD", baseline)
    overlay = _manifest_overlay(container_name, baseline=base, rev=tag if tagged else None,
                                src_dirs=src_dirs, src_filter=src_filter)
    manifests = set(overlay.upserts)

    if tagged:
        existing = _existing_src_dirs_git(container_name, src_dirs, tag)
        if not existing:
            raise RuntimeError(f"extract_snapshot: no source directories found at {tag}")
        root_files = _existing_root_files_git(container_name, ROOT_BUILD_FILES, tag)
        paths = get_snapshot_paths(existing, existing_root_files=root_files,
                                   extra_build_manifests=manifests)
        cmd = _fakeroot_exec(container_name) + ["git", "archive", "--format=tar", tag] + paths
        sidecar_tag = tag
        logger.info("extract_snapshot: git archive %s (%d/%d src dirs, %d manifests)",
                    tag, len(existing), len(src_dirs), len(manifests))
    else:
        existing = _existing_workdir_dirs(container_name, src_dirs)
        if not existing:
            raise RuntimeError("extract_snapshot: no source directories in container workdir (no tag, fallback)")
        root_files = _existing_root_files_workdir(container_name, ROOT_BUILD_FILES)
        paths = get_snapshot_paths(existing, existing_root_files=root_files,
                                   extra_build_manifests=manifests)
        tar_cmd = "tar -cf - --ignore-failed-read " + " ".join(paths) + " 2>/dev/null"
        cmd = _fakeroot_exec(container_name) + ["sh", "-c", tar_cmd]
        sidecar_tag = f"agent-workdir-{mid}"
        logger.info("extract_snapshot: workdir tar fallback (no %s); %d/%d src dirs, %d manifests",
                    tag, len(existing), len(src_dirs), len(manifests))

    with open(dest, "wb") as f:
        r = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE)
        if r.returncode != 0:
            raise RuntimeError(f"extract_snapshot: archive failed: {r.stderr.decode(errors='replace')}")

    dropped = _filter_snapshot_tar(dest, src_filter, extra_build_manifests=manifests)
    if dropped:
        logger.info("extract_snapshot: filtered out %d test/excluded files", dropped)

    # Sidecar last: snapshot_sha256 must bind the FINAL tar.
    sidecar = dest.parent / (dest.stem + ".integrity.json")
    sidecar.write_text(json.dumps(_snapshot_sidecar_payload(
        tag=sidecar_tag, snapshot_file=dest, manifest_overlay=overlay,
        capture_filter=_capture_filter(src_filter),
        agent_base_image_id=_container_image_id(container_name),
        agent_tag_commit=_git_out(container_name, "rev-parse",
                                  tag if tagged else "HEAD").strip(),
    ), indent=2))
    return dest


# ───────────────────────────── CTE: the official trial per repo ─────────────────────────────
def run_trial(repos=None, **kwargs):
    """Continuous Task Evaluation for an external policy: run the OFFICIAL trial
    (harness.e2e.run_e2e) for each repo in parallel against `base_url`, then
    aggregate with the official collect_results code into one TrialResult.

    Keyword arguments (see harness.e2e.run_trial.run_trial): data_root (a
    WRITABLE checkout at BENCHMARK_VERSION), trial_name, base_url, model
    (default slime-actor), agent_version (default 2.1.193), agent_env (extra
    container variables applied LAST), timeout_s (default 18000), milestones
    (dependency-closed prefix), parallel, out_root, reasoning_effort,
    unprotected, dry_run, aggregate_only.

    The result carries per-milestone raw counters (test_summary), per-repo
    counts with one name per meaning (n_milestones / n_selected / n_graded /
    n_evaluated / n_submitted / n_unfinished), the harness's own summary for
    audit, macro and micro aggregates, and provenance (benchmark_version,
    harness sha, data commit, agent_env)."""
    from harness.e2e.run_trial import run_trial as _impl  # noqa: PLC0415
    return _impl(repos, **kwargs)


def session_key(trial_name: str, repo_name: str) -> str:
    """The per-repo API key a CTE trial gives its containers, which is also the session id the
    policy endpoint sees (claude-code sends it as `x-api-key`). A consumer that wants to
    pre-register those sessions needs the exact string; do not re-derive the format."""
    from harness.e2e.run_trial import session_key as _impl  # noqa: PLC0415
    return _impl(trial_name, repo_name)


def repo_image(repo: str, *, unprotected: bool = False, project_root=None) -> str:
    """The image the harness boots for `repo`: the repo-level offline closure under a protected
    runtime policy, the plain base image otherwise. Ask this rather than formatting the name, so
    a policy change cannot silently leave a consumer pre-pulling the wrong image."""
    from harness.e2e.runtime_policy_binding import (  # noqa: PLC0415
        image_for_runtime_policy, resolve_runtime_policy)
    root = Path(project_root) if project_root else Path(__file__).resolve().parent.parent
    return image_for_runtime_policy(resolve_runtime_policy(repo, root, unprotected=unprotected))


def discover_repos(data_root, repos=None) -> list:
    """Repo directory names under `data_root`, filtered by substring the way the launcher does
    (`scripts/run_all.py --repos`). Returns names, not paths."""
    from harness.e2e.run_trial import _load_run_all  # noqa: PLC0415
    return [p.name for p in _load_run_all().discover_repos(Path(data_root), list(repos) if repos else None)]


def __getattr__(name: str):
    # Lazy re-exports of the CTE result contract (keeps `import harness.api` cheap).
    if name in ("TrialResult", "RepoResult", "MilestoneResult"):
        from harness.e2e import run_trial as _rt  # noqa: PLC0415
        return getattr(_rt, name)
    raise AttributeError(f"module 'harness.api' has no attribute {name!r}")


def _capture_filter(src_filter) -> dict:
    """The filter config the sidecar records (Go exact-replay provenance)."""
    return {
        "src_dirs": list(getattr(src_filter, "src_dirs", []) or []),
        "test_dirs": list(getattr(src_filter, "test_dirs", []) or []),
        "exclude_patterns": list(getattr(src_filter, "exclude_patterns", []) or []),
        "generated_patterns": list(getattr(src_filter, "generated_patterns", []) or []),
        "modifiable_test_patterns": list(getattr(src_filter, "modifiable_test_patterns", []) or []),
    }


def _container_image_id(container_name: str) -> Optional[str]:
    """The 64-hex image id backing the work container (Go replay provenance)."""
    import subprocess  # noqa: PLC0415
    r = subprocess.run(["docker", "inspect", "-f", "{{.Image}}", container_name],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return (r.stdout.strip().split(":")[-1] or None)


# ───────────────────────────── offline data build ──────────────────────────
# Headless enumeration of the EvoClaw-data tree into TaskRecords / milestone ids for
# the training stack's OFFLINE dataset build (not the rollout hot path). Reproduces what
# the harness reads from disk: the milestone DAG (milestone_selection), repo_config
# (metadata.json + config/<repo>.yaml), per-milestone classification + SRS. No docker,
# no orchestrator coupling. NOTE: this is glue specific to the on-disk EvoClaw-data layout;
# convert.py/enrich_source_spec.py from the legacy stack are NOT vendored here.
def _load_repo_config(data_root: Path, repo: str) -> tuple:
    """(repo_config with all 5 SrcFileFilter pattern sets, framework) for a repo.

    metadata.json (repo_src_dirs -> src_dirs, test_dirs, ...) merged with
    config/<repo>.yaml (generated/modifiable patterns + test_framework). The
    repo_src_dirs -> src_dirs rename is REQUIRED — _src_filter_for/extract_snapshot
    read repo_config["src_dirs"]; leaving it as repo_src_dirs yields an empty snapshot."""
    import json  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    ws = Path(data_root) / repo
    md = json.loads((ws / "metadata.json").read_text(encoding="utf-8"))
    cfg_path = Path(data_root) / "config" / f"{repo}.yaml"
    cfg = (yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}) if cfg_path.exists() else {}
    repo_config = {
        "src_dirs": md.get("repo_src_dirs") or [],
        "test_dirs": md.get("test_dirs") or [],
        "exclude_patterns": md.get("exclude_patterns") or cfg.get("exclude_patterns") or [],
        "generated_patterns": md.get("generated_patterns") or cfg.get("generated_patterns") or [],
        "modifiable_test_patterns": (md.get("modifiable_test_patterns")
                                     or cfg.get("modifiable_test_patterns") or []),
    }
    framework = str(cfg.get("test_framework") or md.get("test_framework") or md.get("framework") or "ginkgo")
    return repo_config, framework


def _read_classification(ws: Path, mid: str, *, f2p_strict: bool) -> tuple:
    """(fail_to_pass, pass_to_pass, new_tests[{test_id}]) from
    test_results/<mid>/<mid>_classification.json. Supports the flat and nested
    (stable_classification) formats. f2p_strict=True requires the flaky-filtered
    stable_classification (raises if absent); False falls back to the raw baseline.
    new_tests (the hidden set mask_tests must hide) = none_to_pass (+ any explicit new_tests)."""
    import json  # noqa: PLC0415

    def _tid(x):
        return x.get("test_id") if isinstance(x, dict) else str(x)

    path = ws / "test_results" / mid / f"{mid}_classification.json"
    baseline = json.loads(path.read_text(encoding="utf-8"))
    stable = baseline.get("stable_classification")
    if isinstance(stable, dict):
        cls = stable
    elif f2p_strict:
        raise ValueError(f"{path}: f2p_strict set but no stable_classification present")
    else:
        cls = baseline

    fail_to_pass = [t for t in (_tid(x) for x in (cls.get("fail_to_pass") or [])) if t]
    pass_to_pass = [t for t in (_tid(x) for x in (cls.get("pass_to_pass") or [])) if t]
    none_to_pass = [t for t in (_tid(x) for x in (cls.get("none_to_pass") or [])) if t]
    new_ids = list(none_to_pass)
    for x in (cls.get("new_tests") or baseline.get("new_tests") or []):
        t = _tid(x)
        if t and t not in new_ids:
            new_ids.append(t)
    return fail_to_pass, pass_to_pass, [{"test_id": t} for t in new_ids]


def list_milestones(data_root, repo_dir: str, *, milestone_ids=None, curriculum: bool = False) -> list:
    """Milestone IDs for one repo under data_root, scoped by selected_milestone_ids.txt.

    curriculum=True -> dependency-closed topological order (milestone_selection); else the
    sorted id set. ``milestone_ids`` (if given) intersects/filters the result."""
    from harness.e2e.milestone_selection import load_graph, topological_order, read_base_ids  # noqa: PLC0415
    ws = Path(data_root) / repo_dir
    deps = ws / "dependencies.csv"
    mcsv = ws / "milestones.csv"
    base_ids = read_base_ids(ws / "selected_milestone_ids.txt")
    if deps.exists():
        nodes, edges = load_graph(deps, mcsv if mcsv.exists() else None, base_ids)
        ids = topological_order(nodes, edges) if curriculum else sorted(nodes)
    else:
        import csv  # noqa: PLC0415
        nodes = set()
        if mcsv.exists():
            with open(mcsv, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    m = (row.get("id") or "").strip()
                    if m:
                        nodes.add(m)
        if base_ids is not None:
            nodes &= base_ids
        ids = sorted(nodes)
    if milestone_ids is not None:
        want = set(milestone_ids)
        ids = [m for m in ids if m in want]
    return ids


def iter_task_records(data_root, repos=None, *, framework=None, f2p_strict: bool = False,
                      include_source_spec: bool = True, curriculum: bool = False,
                      on_error: str = "skip") -> Iterator[TaskRecord]:
    """Yield a TaskRecord per (repo, milestone) under data_root.

    repos=None -> every subdir with a metadata.json (sorted). framework filters to repos
    whose config test_framework matches. include_source_spec=False skips the (heavier)
    repo_config/new_tests/filter_list population — listing only (masking/snapshot then won't
    work). on_error='skip' logs and skips a malformed repo/milestone; anything else re-raises."""
    import json  # noqa: PLC0415
    from harness.e2e.image_version import DEFAULT_IMAGE_TAG, local_ref  # noqa: PLC0415
    root = Path(data_root)
    if not root.is_dir():
        raise FileNotFoundError(f"iter_task_records: data_root not found: {root}")
    if repos:
        repo_list = list(repos)
    else:
        repo_list = sorted(d.name for d in root.iterdir()
                           if d.is_dir() and d.name != "config" and (d / "metadata.json").exists())

    for repo in repo_list:
        ws = root / repo
        try:
            repo_config, repo_framework = _load_repo_config(root, repo)
        except Exception as e:
            if on_error == "skip":
                logger.warning("iter_task_records: skip repo %s (%s)", repo, e)
                continue
            raise
        if framework and repo_framework != framework:
            continue
        for mid in list_milestones(root, repo, curriculum=curriculum):
            try:
                fail_to_pass, pass_to_pass, new_tests = _read_classification(ws, mid, f2p_strict=f2p_strict)
                srs = ws / "srs" / mid / "SRS.md"
                problem = srs.read_text(encoding="utf-8") if srs.exists() else ""
                # The released naming scheme, pinned to the benchmark data version
                # the tree belongs to. A hand-rolled "<repo>/<mid>:latest" matches no
                # published image and silently drifts off the released set.
                image = local_ref(repo, mid, DEFAULT_IMAGE_TAG)
                ei = {
                    "instance_id": f"{repo}__{mid}",
                    "docker_image": image,
                    "problem_statement": problem,
                    "fail_to_pass": fail_to_pass,
                    "pass_to_pass": pass_to_pass,
                    "framework": repo_framework,
                }
                if include_source_spec:
                    ss = {"repo": repo, "milestone_id": mid,
                          "repo_config": repo_config, "new_tests": new_tests}
                    fl = ws / "test_results" / mid / f"{mid}_filter_list.json"
                    if fl.exists():
                        ss["filter_list"] = json.loads(fl.read_text(encoding="utf-8"))
                    ei["source_spec"] = ss
                yield TaskRecord.from_row(ei)
            except Exception as e:
                if on_error == "skip":
                    logger.warning("iter_task_records: skip %s/%s (%s)", repo, mid, e)
                    continue
                raise


if __name__ == "__main__":
    import sys as _sys
    if _sys.argv[1:2] == ["run-trial"]:
        from harness.e2e.run_trial import main as _main
        _sys.exit(_main(_sys.argv[2:]))
    _sys.exit("usage: python -m harness.api run-trial [--config <yaml>] [--data-root ..] [--trial-name ..] [--base-url ..]")
