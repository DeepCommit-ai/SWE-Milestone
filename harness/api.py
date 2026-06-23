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


# ───────────────────────────── snapshot extraction ─────────────────────────
# Public extraction of the OFFICIAL git-archive snapshot logic (was private in
# e2e/run_milestone.py: _extract_snapshot + _extract_snapshot_from_workdir). Pure
# host-side `docker exec` against the live work container — no orchestrator / managed-
# container coupling, so the training stack can call it directly after the agent run.
def _fakeroot_exec(container_name: str) -> list[str]:
    """The `docker exec` prefix the official snapshot path uses (git as fakeroot in /testbed)."""
    return ["docker", "exec", "--user", "fakeroot", "-e", "HOME=/home/fakeroot",
            "-w", "/testbed", container_name]


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


def _filter_snapshot_tar(tar_path: Path, src_filter) -> int:
    """Drop tar members should_include_in_snapshot() rejects (test/excluded files),
    keeping src + generated + modifiable-test files. No-op when no test/exclude patterns
    are defined. Mirrors run_milestone._filter_tar_archive."""
    import tarfile  # noqa: PLC0415
    if not src_filter.test_dirs and not src_filter.exclude_patterns:
        return 0
    n = 0
    tmp = tar_path.with_suffix(".filtered.tar")
    with tarfile.open(tar_path, "r") as src, tarfile.open(tmp, "w") as dst:
        for m in src.getmembers():
            if not m.isfile():
                dst.addfile(m)
                continue
            if src_filter.should_include_in_snapshot(m.name):
                fo = src.extractfile(m)
                if fo:
                    dst.addfile(m, fo)
            else:
                n += 1
    tmp.replace(tar_path)
    return n


def extract_snapshot(container_name: str, task: TaskRecord, *, dest: Path) -> Path:
    """Extract the gradeable source snapshot from the live work container into ``dest`` (a .tar).

    OFFICIAL logic, two paths: if the agent created the completion tag
    ``agent-impl-<milestone>`` → ``git archive`` that tag; otherwise fall back to taring the
    working dir (regardless of git state). In both cases only the source dirs that EXIST plus
    ROOT_BUILD_FILES are archived, then the tar is filtered by ``should_include_in_snapshot``
    (keeps generated code + modifiable tests, drops other tests/excludes — see _src_filter_for).
    The TaskRecord must carry ``source_spec.repo_config`` (built by iter_task_records). Raises
    RuntimeError on infra failure so the consumer can turn it into an abort."""
    from harness.utils.snapshot import ROOT_BUILD_FILES, get_snapshot_paths  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tag = f"agent-impl-{_milestone_id(task)}"
    rc = task.source_spec.get("repo_config") or {}
    src_dirs = list(rc.get("src_dirs") or [])
    if not src_dirs:
        raise RuntimeError("extract_snapshot: no src_dirs in source_spec.repo_config "
                           "(build the TaskRecord via iter_task_records)")
    src_filter = _src_filter_for(task)

    if _tag_exists(container_name, tag):
        existing = _existing_src_dirs_git(container_name, src_dirs, tag)
        if not existing:
            raise RuntimeError(f"extract_snapshot: no source directories found at {tag}")
        root_files = _existing_root_files_git(container_name, ROOT_BUILD_FILES, tag)
        paths = get_snapshot_paths(existing, existing_root_files=root_files)
        cmd = _fakeroot_exec(container_name) + ["git", "archive", "--format=tar", tag] + paths
        logger.info("extract_snapshot: git archive %s (%d/%d src dirs)", tag, len(existing), len(src_dirs))
    else:
        existing = _existing_workdir_dirs(container_name, src_dirs)
        if not existing:
            raise RuntimeError("extract_snapshot: no source directories in container workdir (no tag, fallback)")
        root_files = _existing_root_files_workdir(container_name, ROOT_BUILD_FILES)
        paths = get_snapshot_paths(existing, existing_root_files=root_files)
        tar_cmd = "tar -cf - --ignore-failed-read " + " ".join(paths) + " 2>/dev/null"
        cmd = _fakeroot_exec(container_name) + ["sh", "-c", tar_cmd]
        logger.info("extract_snapshot: workdir tar fallback (no %s); %d/%d src dirs", tag, len(existing), len(src_dirs))

    with open(dest, "wb") as f:
        r = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE)
        if r.returncode != 0:
            raise RuntimeError(f"extract_snapshot: archive failed: {r.stderr.decode(errors='replace')}")

    dropped = _filter_snapshot_tar(dest, src_filter)
    if dropped:
        logger.info("extract_snapshot: filtered out %d test/excluded files", dropped)
    return dest


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
                image = f"{repo.lower()}/{mid.lower()}"
                if ":" not in image:
                    image += ":latest"
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
