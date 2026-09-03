"""Offline (no-docker) tests for the harness.api additions:
list_milestones / iter_task_records (data-tree enumeration) and the _src_filter_for
fix that threads all 5 SrcFileFilter pattern sets into should_include_in_snapshot.

extract_snapshot's docker-exec paths need a live container -> integration-only; here we
cover the filter logic it relies on. Run: pytest harness/e2e/test_api_offline.py
"""
import json

from harness import api
from harness.api import TaskRecord


# ───────────────────────── synthetic EvoClaw-data tree ─────────────────────────
def _make_tree(root):
    """A tiny data_root: one repo 'myrepo', milestones M001 (nested stable
    classification) + M002 (flat classification), config yaml, SRS."""
    repo = root / "myrepo"
    (root / "config").mkdir(parents=True)
    (repo / "srs" / "M001").mkdir(parents=True)
    (repo / "srs" / "M002").mkdir(parents=True)
    (repo / "test_results" / "M001").mkdir(parents=True)
    (repo / "test_results" / "M002").mkdir(parents=True)

    (repo / "metadata.json").write_text(json.dumps({
        "repo_src_dirs": ["src/"],
        "test_dirs": ["**/*_test.go"],
        "exclude_patterns": ["**/*.pb.go"],
    }))
    # generated/modifiable live in config yaml (metadata lacks them -> must be merged in)
    (root / "config" / "myrepo.yaml").write_text(
        "test_framework: go_test\n"
        "generated_patterns:\n  - '**/*.pb.go'\n"
        "modifiable_test_patterns:\n  - '**/special_test.go'\n"
    )
    # DAG: M002 depends on M001 (edge source=M001 -> target=M002)
    (repo / "dependencies.csv").write_text(
        "source_id,target_id,type,strength,rationale,confidence_score\n"
        "M001,M002,FUNC,Strong,x,0.9\n"
    )
    (repo / "milestones.csv").write_text("id\nM001\nM002\n")
    (repo / "selected_milestone_ids.txt").write_text("M001\nM002\n")
    (repo / "srs" / "M001" / "SRS.md").write_text("Problem one {keep braces}")
    (repo / "srs" / "M002" / "SRS.md").write_text("Problem two")
    # M001: nested stable_classification; M002: flat
    (repo / "test_results" / "M001" / "M001_classification.json").write_text(json.dumps(
        {"stable_classification": {"fail_to_pass": ["t1"], "pass_to_pass": ["t2"], "none_to_pass": ["t3"]}}))
    (repo / "test_results" / "M002" / "M002_classification.json").write_text(json.dumps(
        {"fail_to_pass": ["u1"], "pass_to_pass": ["u2"], "none_to_pass": ["u3"]}))
    return root


# ───────────────────────────── list_milestones ─────────────────────────────
def test_list_milestones_curriculum_and_filter(tmp_path):
    root = _make_tree(tmp_path)
    assert api.list_milestones(root, "myrepo", curriculum=True) == ["M001", "M002"]   # topo: prereq first
    assert api.list_milestones(root, "myrepo", curriculum=False) == ["M001", "M002"]  # sorted
    assert api.list_milestones(root, "myrepo", milestone_ids=["M002"]) == ["M002"]
    assert api.list_milestones(root, "myrepo", milestone_ids=["Mzzz"]) == []


def test_list_milestones_no_dependencies_csv(tmp_path):
    root = _make_tree(tmp_path)
    (root / "myrepo" / "dependencies.csv").unlink()  # fall back to milestones.csv id column
    assert api.list_milestones(root, "myrepo") == ["M001", "M002"]


# ───────────────────────────── iter_task_records ───────────────────────────
def test_iter_task_records_fields(tmp_path):
    root = _make_tree(tmp_path)
    recs = {r.instance_id: r for r in api.iter_task_records(root)}
    assert set(recs) == {"myrepo__M001", "myrepo__M002"}

    m1 = recs["myrepo__M001"]
    from harness.e2e.image_version import DEFAULT_IMAGE_TAG, local_ref
    assert m1.docker_image == local_ref("myrepo", "M001", DEFAULT_IMAGE_TAG)
    assert m1.problem_statement == "Problem one {keep braces}"
    assert m1.fail_to_pass == ["t1"] and m1.pass_to_pass == ["t2"]
    assert m1.framework == "go_test"
    rc = m1.source_spec["repo_config"]
    assert rc["src_dirs"] == ["src/"]                                  # repo_src_dirs -> src_dirs rename
    assert rc["generated_patterns"] == ["**/*.pb.go"]                  # merged from config yaml
    assert rc["modifiable_test_patterns"] == ["**/special_test.go"]
    assert m1.source_spec["new_tests"] == [{"test_id": "t3"}]          # none_to_pass -> new_tests
    assert m1.source_spec["repo"] == "myrepo" and m1.source_spec["milestone_id"] == "M001"

    m2 = recs["myrepo__M002"]                                          # flat classification format
    assert m2.fail_to_pass == ["u1"] and m2.source_spec["new_tests"] == [{"test_id": "u3"}]


def test_iter_task_records_framework_filter(tmp_path):
    root = _make_tree(tmp_path)
    assert list(api.iter_task_records(root, framework="pytest")) == []           # repo is go_test
    assert len(list(api.iter_task_records(root, framework="go_test"))) == 2


def test_iter_task_records_f2p_strict_skips_flat(tmp_path):
    root = _make_tree(tmp_path)
    # strict requires stable_classification: M002 (flat) is skipped, M001 (nested) survives
    strict = [r.instance_id for r in api.iter_task_records(root, f2p_strict=True, on_error="skip")]
    assert strict == ["myrepo__M001"]
    # non-strict keeps both
    assert len(list(api.iter_task_records(root, f2p_strict=False))) == 2


def test_iter_task_records_on_error_raise(tmp_path):
    root = _make_tree(tmp_path)
    (root / "myrepo" / "test_results" / "M002" / "M002_classification.json").unlink()
    # skip -> only M001; raise -> propagates
    assert [r.instance_id for r in api.iter_task_records(root, on_error="skip")] == ["myrepo__M001"]
    import pytest
    with pytest.raises(Exception):
        list(api.iter_task_records(root, on_error="raise"))


def test_iter_task_records_include_source_spec_false(tmp_path):
    root = _make_tree(tmp_path)
    recs = list(api.iter_task_records(root, include_source_spec=False))
    assert recs and all(r.source_spec == {} for r in recs)


# ───────────────────────── _src_filter_for (§8 fix) ─────────────────────────
def _filter_task():
    return TaskRecord.from_row({
        "docker_image": "x/y:latest",
        "source_spec": {"repo_config": {
            "src_dirs": ["src/"],
            "test_dirs": ["**/*_test.go"],
            "exclude_patterns": ["**/*.pb.go"],          # generated code also excluded from agent edits
            "generated_patterns": ["**/*.pb.go"],        # ...but must stay in the snapshot
            "modifiable_test_patterns": ["**/special_test.go"],
        }},
    })


def test_src_filter_for_threads_all_five_patterns():
    f = api._src_filter_for(_filter_task())
    assert f.generated_patterns == ["**/*.pb.go"]
    assert f.modifiable_test_patterns == ["**/special_test.go"]


def test_should_include_in_snapshot_keeps_generated_and_modifiable():
    f = api._src_filter_for(_filter_task())
    # plain source: included
    assert f.should_include_in_snapshot("src/app.go") is True
    # generated code excluded-as-src BUT re-included for compilation (the §8 bug, now fixed)
    assert f.should_include_in_snapshot("src/api.pb.go") is True
    # modifiable test (matches test_dirs) re-included
    assert f.should_include_in_snapshot("src/special_test.go") is True
    # ordinary test: dropped
    assert f.should_include_in_snapshot("src/app_test.go") is False
    # outside src dirs: dropped
    assert f.should_include_in_snapshot("docs/readme.md") is False


# ───────────────────── tag derivation consistency ──────────────────────────
def test_tag_derivation_uses_milestone_id_consistently():
    # instance_id is unique (<repo>__<mid>) but the completion tag / prompt placeholder must be
    # the BARE milestone id, so build_instruction (what the agent is told), agent_session_spec
    # (the completion check) and extract_snapshot (what it archives) all agree on agent-impl-<mid>.
    tr = TaskRecord.from_row({"docker_image": "r/m1:latest", "problem_statement": "do it",
                              "instance_id": "r__M001", "source_spec": {"milestone_id": "M001"}})
    assert api._milestone_id(tr) == "M001"
    instr = api.build_instruction(tr)
    assert "agent-impl-M001" in instr and "r__M001" not in instr
    assert "agent-impl-M001" in api.agent_session_spec(tr).completion["signal_cmd"]


# ─────────────────── fail-closed scoring verdict passthrough ────────────────
# harness v1.0.2 made generate_filtered_evaluation fail-closed: when filter-list
# validation fails it writes NO filtered derivative and stamps `scoring_blocked`
# on the raw result. evaluate() falls back to the raw file in that case, so the
# flag has to survive normalization or the training stack silently rewards a cell
# the harness declared unscoreable.
def test_normalize_eval_passes_through_scoring_blocked():
    raw = {"resolved": True, "scoring_blocked": True,
           "tests_status": {"FAIL_TO_PASS": {"success": ["t1"], "failure": []},
                            "PASS_TO_PASS": {"success": [], "failure": []}},
           "test_summary": {"pass_to_pass_required": 0, "total": 1, "passed": 1}}
    out = api._normalize_eval(raw)
    assert out["scoring_blocked"] is True
    # the raw numbers are still parsed (the caller decides to drop the sample)
    assert out["resolved"] is True and out["n_f2p_fixed"] == 1


def test_normalize_eval_defaults_scoring_blocked_false():
    # benign path: milestone has no filter_list, raw IS the score, no stamp present
    raw = {"resolved": False,
           "tests_status": {"FAIL_TO_PASS": {"success": [], "failure": ["t1"]},
                            "PASS_TO_PASS": {"success": [], "failure": []}},
           "test_summary": {"pass_to_pass_required": 0, "total": 1, "passed": 0}}
    out = api._normalize_eval(raw)
    assert out["scoring_blocked"] is False
    assert out["n_f2p_inscope"] == 1


# ═════════════ post-v1.0.2 harness-contract regressions (merge drift) ═══════════
# The seam was written 208 commits before main's v1.0.2 snapshot/grading contract.
# These lock the four places where main changed behaviour under it.
import tarfile

from harness.utils.snapshot import ManifestOverlay, SNAPSHOT_METADATA_SCHEMA_VERSION


def _tar_with(tmp_path, names):
    p = tmp_path / "snap.tar"
    with tarfile.open(p, "w") as t:
        for n in names:
            f = tmp_path / "payload.txt"
            f.write_text("x")
            t.add(f, arcname=n)
    return p


def _members(p):
    with tarfile.open(p, "r") as t:
        return sorted(m.name for m in t.getmembers() if m.isfile())


# ── S3: the tar pass must use the build-manifest-aware wrapper ──────────────
def test_filter_snapshot_tar_drops_unchanged_manifest_under_src_dir(tmp_path):
    # main's should_include_snapshot_file short-circuits EVERY build manifest: a pom.xml
    # nested under a broad src dir is kept only when the overlay upserts it. Letting it
    # fall through to SrcFileFilter is the stale-POM pollution bug.
    tar = _tar_with(tmp_path, ["src/app.java", "src/pom.xml"])
    f = api._src_filter_for(TaskRecord.from_row({
        "docker_image": "r/m:latest", "problem_statement": "", "instance_id": "r__M001",
        "source_spec": {"repo_config": {"src_dirs": ["src/"], "test_dirs": ["**/*_test.java"],
                                        "exclude_patterns": [], "generated_patterns": [],
                                        "modifiable_test_patterns": []}}}))
    api._filter_snapshot_tar(tar, f, extra_build_manifests=set())
    assert _members(tar) == ["src/app.java"]


def test_filter_snapshot_tar_keeps_manifest_the_overlay_upserts(tmp_path):
    tar = _tar_with(tmp_path, ["src/app.java", "src/pom.xml"])
    f = api._src_filter_for(TaskRecord.from_row({
        "docker_image": "r/m:latest", "problem_statement": "", "instance_id": "r__M001",
        "source_spec": {"repo_config": {"src_dirs": ["src/"], "test_dirs": ["**/*_test.java"],
                                        "exclude_patterns": [], "generated_patterns": [],
                                        "modifiable_test_patterns": []}}}))
    api._filter_snapshot_tar(tar, f, extra_build_manifests={"src/pom.xml"})
    assert _members(tar) == ["src/app.java", "src/pom.xml"]


def test_filter_snapshot_tar_runs_without_test_or_exclude_patterns(tmp_path):
    # main: "This pass also strips unchanged build manifests, so it is mandatory even
    # with no test/exclude patterns." The old early-return skipped it entirely.
    tar = _tar_with(tmp_path, ["src/app.rs", "Cargo.toml"])
    f = api._src_filter_for(TaskRecord.from_row({
        "docker_image": "r/m:latest", "problem_statement": "", "instance_id": "r__M001",
        "source_spec": {"repo_config": {"src_dirs": ["src/"], "test_dirs": [],
                                        "exclude_patterns": [], "generated_patterns": [],
                                        "modifiable_test_patterns": []}}}))
    api._filter_snapshot_tar(tar, f, extra_build_manifests=set())
    assert _members(tar) == ["src/app.rs"]


# ── S1: the capture must emit the integrity sidecar the evaluator demands ───
def test_snapshot_sidecar_payload_satisfies_the_evaluator_contract(tmp_path):
    # evaluator._load_and_validate_snapshot_metadata requires: schema_version, ok is True,
    # tag in {agent-impl-<mid>, agent-workdir-<mid>}, snapshot_sha256 matching the tar,
    # and a well-formed manifest_overlay. Go repos additionally need agent_base_image_id,
    # agent_tag_commit, go_manifest_projection and capture_filter.
    tar = _tar_with(tmp_path, ["src/app.go", "go.mod"])
    overlay = ManifestOverlay.create("a" * 40, upserts={"go.mod"}, deletes=())
    payload = api._snapshot_sidecar_payload(
        tag="agent-impl-M001", snapshot_file=tar, manifest_overlay=overlay,
        capture_filter={"src_dirs": ["src/"]}, agent_base_image_id="b" * 64,
        agent_tag_commit="c" * 40)

    assert payload["schema_version"] == SNAPSHOT_METADATA_SCHEMA_VERSION
    assert payload["ok"] is True
    assert payload["tag"] == "agent-impl-M001"
    from harness.utils.snapshot import snapshot_sha256
    assert payload["snapshot_sha256"] == snapshot_sha256(tar)
    assert ManifestOverlay.from_metadata(payload["manifest_overlay"]).upserts == frozenset({"go.mod"})
    assert payload["go_manifest_projection"]["present"] == ["go.mod"]
    assert payload["agent_base_image_id"] == "b" * 64
    assert payload["agent_tag_commit"] == "c" * 40
    assert isinstance(payload["capture_filter"], dict)
    # binding fields must be ABSENT: declaring them forces the evaluator into
    # trial-pinned mode, which this seam cannot satisfy.
    assert "repo_config_binding" not in payload
    assert "runtime_policy_binding" not in payload


# ── S4: infra-poisoned cells must be distinguishable from honest failures ───
def test_normalize_eval_passes_through_infrastructure_verdict():
    raw = {"resolved": False, "infrastructure_failure": "docker_oom",
           "infra_invalid": True, "infra_invalid_reason": "container died",
           "eval_status": "infra-invalid",
           "tests_status": {"FAIL_TO_PASS": {"success": [], "failure": []},
                            "PASS_TO_PASS": {"success": [], "failure": []}},
           "test_summary": {"pass_to_pass_required": 0, "total": 0, "passed": 0}}
    out = api._normalize_eval(raw)
    assert out["infra_invalid"] is True
    assert out["eval_status"] == "infra-invalid"
    assert out["infrastructure_failure"] == "docker_oom"
    assert out["infra_invalid_reason"] == "container died"


def test_normalize_eval_infra_defaults_are_clean_for_an_honest_failure():
    raw = {"resolved": False,
           "tests_status": {"FAIL_TO_PASS": {"success": [], "failure": ["t1"]},
                            "PASS_TO_PASS": {"success": [], "failure": []}},
           "test_summary": {"pass_to_pass_required": 0, "total": 1, "passed": 0}}
    out = api._normalize_eval(raw)
    assert out["infra_invalid"] is False
    assert out["eval_status"] == ""
    assert out["infrastructure_failure"] == ""


# ── image naming must follow the released scheme, not a hand-rolled one ─────
def test_iter_task_records_emits_the_canonical_image_ref(tmp_path):
    # The offline dataset build feeds metadata.docker_image straight into the
    # consumer's `docker pull`. A hand-rolled "<repo>/<mid>:latest" neither exists
    # nor tracks BENCHMARK_VERSION, so training silently drifts off the released
    # image set. Pin the released scheme instead.
    from harness.e2e.image_version import local_ref, DEFAULT_IMAGE_TAG
    root = _make_tree(tmp_path)
    recs = {t.instance_id: t for t in api.iter_task_records(root)}
    assert recs["myrepo__M001"].docker_image == local_ref("myrepo", "M001", DEFAULT_IMAGE_TAG)
    assert recs["myrepo__M001"].docker_image.endswith(f":{DEFAULT_IMAGE_TAG}")


# ── anti-leak: history hardening must exist and must fail loudly ────────────
def test_harden_container_raises_when_the_container_is_unreachable():
    # The official harness truncates git history at container setup ("prevent agent
    # from seeing future commits"); the seam had no equivalent, so an RL work
    # container kept every future commit -- including "End state for <milestone>".
    # A silent failure here would reopen that hole, so the contract is fail-loud.
    import pytest
    with pytest.raises(RuntimeError):
        api.harden_container("swe_milestone_no_such_container_zzz")


# ───────────────────────── api 1.3: seam naming, agent env, quarantine helpers ─────────────────────────
import os
import pytest


def test_api_version_is_1_3():
    assert api.API_VERSION == "1.3"


def test_legacy_evoclaw_env_is_rejected(monkeypatch):
    monkeypatch.setenv("EVOCLAW_DATA_ROOT", "/nowhere")
    with pytest.raises(RuntimeError, match="EVOCLAW_DATA_ROOT -> SWE_MILESTONE_DATA_ROOT"):
        api.resolve_data_root()
    with pytest.raises(RuntimeError):
        api.exec_user()


def test_data_root_and_exec_user_read_new_names(monkeypatch):
    for k in list(os.environ):
        if k.startswith("EVOCLAW_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("SWE_MILESTONE_DATA_ROOT", "/data/tree")
    monkeypatch.setenv("SWE_MILESTONE_EXEC_USER", "agent")
    monkeypatch.delenv("SWE_MILESTONE_EXEC_HOME", raising=False)
    assert str(api.resolve_data_root()) == "/data/tree"
    assert api.exec_user() == "agent"
    assert api.exec_home() == "/home/agent"
    assert api._fakeroot_exec("c1")[:5] == ["docker", "exec", "--user", "agent", "-e"]
    monkeypatch.delenv("SWE_MILESTONE_EXEC_USER")
    assert api.exec_user() == "fakeroot" and api.exec_home() == "/home/fakeroot"


def test_slime_actor_pricing_is_zero():
    from harness.e2e.pricing import resolve_pricing
    p = resolve_pricing("slime-actor")
    assert p["input"] == 0.0 and p["output"] == 0.0 and p["cache_read"] == 0.0


def test_parse_agent_env_and_override_order():
    from harness.e2e.agents.claude_code import apply_agent_env_overrides, parse_agent_env
    assert parse_agent_env(None) == {} and parse_agent_env("  ") == {}
    ov = parse_agent_env('{"CLAUDE_CODE_AUTO_COMPACT_WINDOW": 100000, "ANTHROPIC_MODEL": "slime-actor"}')
    assert ov == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "100000", "ANTHROPIC_MODEL": "slime-actor"}
    base = ["-e", "ANTHROPIC_BASE_URL=http://x", "-e", "CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000"]
    out = apply_agent_env_overrides(base, ov)
    assert out == ["-e", "ANTHROPIC_BASE_URL=http://x", "-e", "CLAUDE_CODE_AUTO_COMPACT_WINDOW=100000",
                   "-e", "ANTHROPIC_MODEL=slime-actor"]
    for bad in ("[1]", '{"lower": 1}', '{"K": [1]}', "{not json"):
        with pytest.raises(ValueError):
            parse_agent_env(bad)


def test_framework_applies_agent_env_last(monkeypatch):
    from harness.e2e.agents.claude_code import ClaudeCodeFramework
    monkeypatch.setenv("UNIFIED_API_KEY", "k")
    monkeypatch.setenv("UNIFIED_BASE_URL", "http://172.17.0.1:18001")
    monkeypatch.setenv("SWE_MILESTONE_AUTO_COMPACT_WINDOW", "200000")
    monkeypatch.setenv("SWE_MILESTONE_AGENT_ENV", '{"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "64000", "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "75536"}')
    ev = ClaudeCodeFramework().get_container_env_vars()
    pairs = dict(x.split("=", 1) for x in ev[1::2])
    assert pairs["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "64000"
    assert pairs["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "75536"
    assert ev[1::2].count("CLAUDE_CODE_AUTO_COMPACT_WINDOW=64000") == 1
    assert not any(x.startswith("CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000") for x in ev[1::2])


def test_quarantine_agent_env_is_keyed_by_home(monkeypatch):
    from harness.e2e.agents.base import quarantine_agent_env
    for k in ("SWE_MILESTONE_PIP_OFFLINE", "SWE_MILESTONE_CARGO_OFFLINE", "SWE_MILESTONE_MAVEN_OFFLINE",
              "SWE_MILESTONE_NPM_OFFLINE", "SWE_MILESTONE_GO_OFFLINE", "SWE_MILESTONE_GO_TOOLCHAIN"):
        monkeypatch.delenv(k, raising=False)
    assert quarantine_agent_env("/home/agent") == {}
    monkeypatch.setenv("SWE_MILESTONE_GO_OFFLINE", "1")
    monkeypatch.setenv("SWE_MILESTONE_GO_TOOLCHAIN", "go1.24.5")
    e = quarantine_agent_env("/home/agent")
    assert e["GOMODCACHE"] == "/home/agent/.cache/evoclaw-gomodcache"
    assert e["GOBIN"] == "/home/agent/go/bin" and e["PATH"].startswith("/home/agent/go/bin:")
    assert e["GOLANG_VERSION"] == "1.24.5" and e["GOTOOLCHAIN"] == "local"
    assert quarantine_agent_env("/home/fakeroot")["GOCACHE"] == "/home/fakeroot/.cache/go-build"


def test_container_setup_agent_user_defaults_and_rekey():
    from harness.e2e.container_setup import ContainerSetup
    cs = ContainerSetup.__new__(ContainerSetup)
    assert cs.agent_user == "fakeroot" and cs.agent_home == "/home/fakeroot" and list(cs.allow_endpoints) == []
    script = "install -d -o fakeroot -g 0 /home/fakeroot/.cache/x\nchown fakeroot:0 /home/fakeroot/go"
    assert cs._for_agent_user(script) == script
    cs.agent_user, cs.agent_home = "agent", "/home/agent"
    assert cs._for_agent_user(script) == "install -d -o agent -g 0 /home/agent/.cache/x\nchown agent:0 /home/agent/go"


def test_endpoint_accept_rules():
    from harness.e2e.container_setup import _split_host_port, endpoint_accept_rules
    assert _split_host_port("172.17.0.1:18001") == ("172.17.0.1", 18001)
    assert _split_host_port("http://172.17.0.1:18001/v1") == ("172.17.0.1", 18001)
    assert _split_host_port("https://api.example.com") == ("api.example.com", 443)
    with pytest.raises(ValueError):
        _split_host_port("nonsense")
    resolver = lambda h: {"policy.internal": ["10.9.8.7"], "cdn.example": ["104.16.1.1"]}[h]
    rules = endpoint_accept_rules(["", "http://172.17.0.1:18001", "policy.internal:9000",
                                   "https://api.anthropic.com"],
                                  deny_cidrs=["104.16.0.0/12"], whitelisted_hosts={"api.anthropic.com"},
                                  resolver=resolver)
    assert rules == [("172.17.0.1", 18001), ("10.9.8.7", 9000)]
    with pytest.raises(RuntimeError, match="denied CIDR"):
        endpoint_accept_rules(["cdn.example:443"], deny_cidrs=["104.16.0.0/12"], whitelisted_hosts=set(),
                              resolver=resolver)
    with pytest.raises(RuntimeError, match="cannot resolve"):
        import socket
        def boom(h):
            raise socket.gaierror("nope")
        endpoint_accept_rules(["missing.host:1"], deny_cidrs=[], whitelisted_hosts=set(), resolver=boom)


def test_mask_report_and_verify_masking_skip(tmp_path):
    rep = api.MaskReport(skipped=True, reason="nothing to mask")
    assert rep.masked_files == [] and rep.failed_files == []
    v = api.verify_masking("no-such-container", TaskRecord(instance_id="r__M1", docker_image="i", problem_statement=""), report=rep)
    assert v.ok and v.skipped and v.checked == 0
    v2 = api.verify_masking("no-such-container", TaskRecord(instance_id="r__M1", docker_image="i", problem_statement=""))
    assert v2.ok and v2.skipped  # no fail_to_pass / new_tests -> nothing to verify


def test_verify_masking_reports_failed_and_unmapped_from_report(monkeypatch):
    rep = api.MaskReport(masked_test_files=0, masked_src_files=0, masked_files=[],
                         failed_files=["tests/a_test.go"], unmapped_tests=["weird::format"])
    v = api.verify_masking("no-such-container", TaskRecord(instance_id="r__M1", docker_image="i", problem_statement="", fail_to_pass=["x"]), report=rep)
    assert not v.ok and len(v.violations) == 2
    reasons = {x["reason"] for x in v.violations}
    assert any("mask_tests failed" in r for r in reasons) and any("unmapped" in r for r in reasons)


def test_quarantine_report_shape():
    q = api.QuarantineReport(ok=True, repo="r", mode="protected", policy_sha256="ab" * 32)
    assert q.denied_hosts == [] and q.allowed_endpoints == [] and q.env == {}


def test_evaluate_zero_report_build_failure_is_scored_zero(tmp_path, monkeypatch):
    """A submission whose graded tests do not compile produces no test report: the
    official runner raises. Mirror the CTE orchestrator: a scored 0, not an abort."""
    import harness.e2e.evaluator as evaluator
    root = tmp_path / "data"
    _make_tree(root)
    (root / "myrepo" / "test_results" / "M001").mkdir(parents=True, exist_ok=True)
    tr = next(t for t in api.iter_task_records(root) if t.instance_id.endswith("__M001"))

    class Boom:
        def __init__(self, **kw):
            pass

        def evaluate(self):
            raise RuntimeError("No valid test report files generated under /x\nFirst fatal error (eval_default.log):\n"
                               "error[E0599]: no method named `min_depth` found for struct `WalkBuilder`")

    monkeypatch.setattr(evaluator, "PatchEvaluator", Boom)
    out = api.evaluate(tr, tmp_path / "a.tar", scratch=tmp_path / "s", data_root=str(root))
    assert out["resolved"] is False and out["total_tests"] == 0
    assert out["infra_invalid"] is False and out["scoring_blocked"] is False
    assert out["scored_failure_reason"] == "build-failure-with-zero-tests" and "min_depth" in out["error"]
    assert json.loads((tmp_path / "s" / "evaluation_result.json").read_text())["eval_status"] == "failed"


def test_evaluate_zero_report_without_evidence_is_infra_invalid(tmp_path, monkeypatch):
    import harness.e2e.evaluator as evaluator
    root = tmp_path / "data"
    _make_tree(root)
    tr = next(t for t in api.iter_task_records(root) if t.instance_id.endswith("__M001"))

    class Boom:
        def __init__(self, **kw):
            pass

        def evaluate(self):
            raise RuntimeError("No valid test report files generated under /x")

    monkeypatch.setattr(evaluator, "PatchEvaluator", Boom)
    out = api.evaluate(tr, tmp_path / "a.tar", scratch=tmp_path / "s", data_root=str(root))
    assert out["infra_invalid"] is True and out["eval_status"] == "infra-invalid"


def test_evaluate_other_runtime_errors_propagate(tmp_path, monkeypatch):
    import harness.e2e.evaluator as evaluator
    root = tmp_path / "data"
    _make_tree(root)
    tr = next(t for t in api.iter_task_records(root) if t.instance_id.endswith("__M001"))

    class Boom:
        def __init__(self, **kw):
            pass

        def evaluate(self):
            raise RuntimeError("docker: Error response from daemon")

    monkeypatch.setattr(evaluator, "PatchEvaluator", Boom)
    with pytest.raises(RuntimeError, match="docker"):
        api.evaluate(tr, tmp_path / "a.tar", scratch=tmp_path / "s", data_root=str(root))
