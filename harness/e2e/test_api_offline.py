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
    assert m1.docker_image == "myrepo/m001:latest"
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
