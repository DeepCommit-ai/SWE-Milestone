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

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

API_VERSION = "1.0"
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
            .replace("{milestone_id}", task.instance_id))


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
    mid = task.instance_id
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
    dirs, carried from the EvoClaw-data config/<repo>.yaml)."""
    from harness.utils.src_filter import SrcFileFilter  # noqa: PLC0415
    rc = task.source_spec.get("repo_config") or {}
    return SrcFileFilter(
        src_dirs=rc.get("src_dirs") or [],
        test_dirs=rc.get("test_dirs") or [],
        exclude_patterns=rc.get("exclude_patterns"),
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
    return MaskReport(masked_test_files=r.get("masked_test_files", 0),
                      masked_src_files=r.get("masked_src_files", 0))


# ───────────────────────────── grading ─────────────────────────────────────
# The EvoClaw-data tree (classification / test_config / metadata / config /
# filter_list) lives on the box; grading reads it directly so ALL judging logic
# stays official — no reimplementation, no materialization. Point at it with
# EVOCLAW_DATA_ROOT (default ~/worksapce/EvoClaw-data). The consumer is
# responsible for a version self-check (tree commit ↔ parquet source_commit).
EVOCLAW_DATA_ROOT = os.environ.get("EVOCLAW_DATA_ROOT") or str(Path.home() / "worksapce" / "EvoClaw-data")


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
    (§4). reward formula stays a training-side config; we only pass counts."""
    ts = d.get("tests_status", {}) or {}
    f2p = ts.get("FAIL_TO_PASS", {}) or {}
    p2p = ts.get("PASS_TO_PASS", {}) or {}
    summ = d.get("test_summary", {}) or {}
    n_fixed = len(f2p.get("success", []) or [])
    return {
        "resolved": bool(d.get("resolved", False)),
        "n_f2p_fixed": n_fixed,
        "n_f2p_inscope": n_fixed + len(f2p.get("failure", []) or []),
        "n_p2p_broken": len(p2p.get("failure", []) or []),
        "n_p2p_inscope": int(summ.get("pass_to_pass_required", 0) or 0),
        "failed_apply_patch": not bool(d.get("patch_successfully_applied", True)),
        "total_tests": int(summ.get("total", 0) or 0),
        "passed_tests": int(summ.get("passed", 0) or 0),
    }


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

    root = Path(data_root or EVOCLAW_DATA_ROOT)
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
    result = ev.evaluate()                      # official: container -> apply -> tests -> compare

    raw_path = scratch / "evaluation_result.json"
    raw_path.write_text(json.dumps(result.to_dict()))
    filtered = generate_filtered_evaluation(raw_path, ws, milestone)   # official flaky filter_list pass
    final = json.loads((filtered or raw_path).read_text())
    return _normalize_eval(final)
