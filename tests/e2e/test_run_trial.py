"""Offline tests for harness.e2e.run_trial (the CTE seam): aggregation on a
synthetic trial tree, the explicit worker environment, and the result contract."""
import json
from pathlib import Path

import pytest

from harness.e2e import run_trial as rt


def _make_repo(root: Path, name: str = "org_repo_v1_v2") -> Path:
    ws = root / name
    (ws / "e2e_trial" / "t_001" / "evaluation").mkdir(parents=True)
    (ws / "e2e_trial" / "t_001" / "log" / "claude_code").mkdir(parents=True)
    (ws / "metadata.json").write_text(json.dumps({"repo_src_dirs": ["src/"], "test_dirs": ["tests/"]}))
    (ws / "milestones.csv").write_text("id,name,description,commits\nM1,a,,x\nM2,b,,y\nM3,c,,z\nM4,d,,w\n")
    (ws / "selected_milestone_ids.txt").write_text("M1\nM2\nM3\n")
    (ws / "non-graded_milestone_ids.txt").write_text("M3\n")
    return ws


def _result(resolved, *, total=10, f2p_req=2, f2p_ach=2, p2p_req=8, p2p_ach=8, p2p_failed=0, infra=False, blocked=False):
    d = {
        "milestone_id": "x", "resolved": resolved, "eval_status": "passed" if resolved else "failed",
        "test_summary": {"total": total, "fail_to_pass_required": f2p_req, "fail_to_pass_achieved": f2p_ach,
                         "none_to_pass_required": 0, "none_to_pass_achieved": 0,
                         "pass_to_pass_required": p2p_req, "pass_to_pass_achieved": p2p_ach,
                         "pass_to_pass_failed": p2p_failed, "pass_to_pass_missing": 0},
        "infra_invalid": infra, "infra_invalid_reason": "docker died" if infra else "",
        "infrastructure_failure": None, "scoring_blocked": blocked,
    }
    if infra:
        d["eval_status"] = "infra-invalid"
        d["test_summary"] = {"total": 0, "fail_to_pass_required": 2, "fail_to_pass_achieved": 0,
                             "pass_to_pass_required": 8, "pass_to_pass_achieved": 0, "pass_to_pass_failed": 0,
                             "pass_to_pass_missing": 8, "none_to_pass_required": 0, "none_to_pass_achieved": 0}
    return d


def _write_trial(ws: Path, cells: dict, *, status_submitted=()):
    tdir = ws / "e2e_trial" / "t_001"
    ev = tdir / "evaluation"
    results = {}
    for mid, res in cells.items():
        (ev / mid).mkdir(exist_ok=True)
        (ev / mid / "evaluation_result.json").write_text(json.dumps(dict(res, milestone_id=mid)))
        results[mid] = {"dag_status": "unlocked", "eval_status": res["eval_status"], "attempt": 0,
                        "result_dir": str(ev / mid)}
    summary = {"results": results, "total_milestones": 3,
               "milestone_status": {"passed": [m for m, r in cells.items() if r["resolved"]],
                                    "failed": [m for m, r in cells.items() if not r["resolved"]],
                                    "submitted": list(status_submitted)},
               "resume_state": {"dag": {"completed": list(cells), "failed": [], "skipped": []}}}
    (ev / "summary.json").write_text(json.dumps(summary))
    (tdir / "trial_metadata.json").write_text(json.dumps({"image": "swe-milestone/org_repo_v1_v2__base-offline:v1.0.2"}))
    (tdir / "agent_stats.json").write_text(json.dumps({
        "model": "slime-actor", "agent_framework": "claude-code",
        "summary": {"total_turns": 120, "total_cost_usd": 0.0, "duration_ms": 3600000, "wall_clock_ms": 3700000},
        "milestone_stats": {
            "M1": {"turns": 50, "duration_ms": 1200000, "cost_usd": 0.0,
                   "start_time": "2026-09-01T10:00:00Z", "end_time": "2026-09-01T10:20:00Z"},
            "M2": {"turns": 70, "duration_ms": 2400000, "cost_usd": 0.0,
                   "start_time": "2026-09-01T10:20:00Z", "end_time": "2026-09-01T11:00:00Z"}},
        "modelUsage": {}}))
    sess = tdir / "log" / "claude_code" / "abc.jsonl"
    lines = [
        {"type": "system", "subtype": "compact_boundary", "uuid": "c1", "timestamp": "2026-09-01T10:05:00Z"},
        {"type": "system", "subtype": "compact_boundary", "uuid": "c2", "timestamp": "2026-09-01T10:30:00Z"},
        {"type": "system", "subtype": "compact_boundary", "uuid": "c3", "timestamp": "2026-09-01T10:45:00Z"},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-09-01T10:46:00Z", "message": "compact_boundary mentioned"},
    ]
    sess.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return tdir


def test_aggregate_repo_counts_and_scores(tmp_path):
    ws = _make_repo(tmp_path)
    _write_trial(ws, {"M1": _result(True), "M2": _result(False, f2p_ach=0, p2p_ach=6, p2p_failed=2)})
    r = rt.aggregate_repo(ws, "t_001", worker_rc=0)
    assert (r.n_milestones, r.n_selected, r.n_graded) == (4, 3, 2)      # M3 is non-graded, M4 unselected
    assert (r.n_evaluated, r.n_submitted, r.n_unfinished) == (2, 2, 0)
    assert r.n_resolved == 1 and r.resolve_rate == pytest.approx(0.5)
    m = {x.id: x for x in r.milestones}
    assert m["M1"].status == "passed" and m["M1"].score == pytest.approx(1.0)
    assert m["M2"].status == "failed" and m["M2"].recall == 0.0 and 0.0 < m["M2"].precision < 1.0
    # headline == official compute_repo_summary (never a private formula)
    assert r.score == pytest.approx(r.official_summary["score_reliable"] / 100.0)
    assert r.official_summary["graded"] == 2
    # compactions are attributed by milestone window
    assert (m["M1"].n_compactions, m["M2"].n_compactions) == (1, 2)
    assert m["M1"].n_turns == 50 and m["M2"].wall_seconds == pytest.approx(2400.0)
    assert m["M1"].test_summary["fail_to_pass_achieved"] == 2
    assert r.agent_exit["reason"] == "completed" and r.agent_exit["turns"] == 120
    assert r.image_ref.endswith("__base-offline:v1.0.2")


def test_aggregate_repo_unsubmitted_and_infra_stay_in_denominator(tmp_path):
    ws = _make_repo(tmp_path)
    _write_trial(ws, {"M1": _result(False, infra=True)})     # M2 never submitted
    r = rt.aggregate_repo(ws, "t_001", worker_rc=0)
    m = {x.id: x for x in r.milestones}
    assert m["M1"].status == "infra-invalid" and m["M1"].infra_invalid_reason == "docker died"
    assert m["M2"].status == "not-submitted" and not m["M2"].submitted and m["M2"].score == 0.0
    assert r.n_graded == 2 and r.n_infra_invalid == 1 and r.n_unfinished == 1
    assert r.score == 0.0 and r.resolve_rate == 0.0
    assert r.agent_exit["reason"] == "incomplete"                # rc 0 but DAG not done


def test_aggregate_repo_scoring_blocked_and_timeout(tmp_path):
    ws = _make_repo(tmp_path)
    tdir = _write_trial(ws, {"M1": _result(True, blocked=True), "M2": _result(True)})
    (tdir / "log" / "session_history.jsonl").write_text(
        json.dumps({"event": "resume_failure", "reason": "timeout", "timeout_ms": 5}) + "\n")
    r = rt.aggregate_repo(ws, "t_001", worker_rc=1)
    m = {x.id: x for x in r.milestones}
    assert m["M1"].status == "scoring-blocked" and m["M1"].scoring_blocked
    assert r.n_scoring_blocked == 1
    assert r.agent_exit["reason"] == "timeout" and r.agent_exit["worker_exit_code"] == 1


def test_aggregate_macro_micro():
    a = rt.RepoResult(repo="a", image_ref="", trial_dir="", n_milestones=2, n_selected=2, n_graded=2, n_evaluated=2,
                      n_submitted=2, n_unfinished=0, n_infra_invalid=0, n_scoring_blocked=0, n_resolved=2,
                      resolve_rate=1.0, score=1.0, recall=1.0, precision=1.0,
                      milestones=[rt.MilestoneResult("M1", "passed", True, "passed", True, 1.0, 1.0, 1.0),
                                  rt.MilestoneResult("M2", "passed", True, "passed", True, 1.0, 1.0, 1.0)])
    b = rt.RepoResult(repo="b", image_ref="", trial_dir="", n_milestones=6, n_selected=6, n_graded=6, n_evaluated=3,
                      n_submitted=3, n_unfinished=3, n_infra_invalid=1, n_scoring_blocked=0, n_resolved=0,
                      resolve_rate=0.0, score=0.0, recall=0.0, precision=0.0,
                      milestones=[rt.MilestoneResult(f"M{i}", "failed", i < 4, "failed", False, 0.0, 0.0, 0.0)
                                  for i in range(1, 7)])
    macro, micro = rt.aggregate([a, b])
    assert macro["score"] == pytest.approx(0.5) and macro["resolve_rate"] == pytest.approx(0.5)
    assert micro["n_graded"] == 8 and micro["score"] == pytest.approx(2 / 8) and micro["resolve_rate"] == pytest.approx(2 / 8)
    assert micro["unfinished_ratio"] == pytest.approx(3 / 8) and micro["n_infra_invalid"] == 1


def test_worker_env_is_explicit_and_per_repo(tmp_path, monkeypatch):
    from harness.e2e.runtime_policy_binding import resolve_runtime_policy
    policy = resolve_runtime_policy("no_such_repo_x", tmp_path, unprotected=True)
    host = {"PATH": "/usr/bin", "HOME": "/home/u", "EVOCLAW_DATA_ROOT": "/stale", "SWE_MILESTONE_AUTO_COMPACT_WINDOW": "200000",
            "UNIFIED_API_KEY": "host-key", "ANTHROPIC_API_KEY": "leak", "PYTHONPATH": "/extra"}
    env = rt.worker_env(data_root=Path("/data"), base_url="http://172.17.0.1:18001", model="slime-actor",
                        trial_name="cte_001", repo_name="navidrome_x", agent_env={"CLAUDE_CODE_MAX_CONTEXT_TOKENS": 75536},
                        policy=policy, host_env=host)
    assert not any(k.startswith("EVOCLAW_") for k in env)
    assert "SWE_MILESTONE_AUTO_COMPACT_WINDOW" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["UNIFIED_API_KEY"] == "cte_001/navidrome_x"
    assert env["UNIFIED_BASE_URL"] == "http://172.17.0.1:18001" and env["UNIFIED_DEFAULT_AGENT_MODEL"] == "slime-actor"
    assert json.loads(env["SWE_MILESTONE_AGENT_ENV"]) == {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "75536"}
    assert env["PYTHONPATH"].split(":")[0] == str(rt.PROJECT_ROOT) and env["PYTHONPATH"].endswith("/extra")
    assert env["SWE_MILESTONE_DATA_ROOT"] == "/data" and env["SWE_MILESTONE_UNPROTECTED"] == "1"
    other = rt.worker_env(data_root=Path("/data"), base_url="http://x:1", model="m", trial_name="cte_001",
                          repo_name="ripgrep_y", agent_env=None, policy=policy, host_env=host)
    assert other["UNIFIED_API_KEY"] != env["UNIFIED_API_KEY"] and "SWE_MILESTONE_AGENT_ENV" not in other


def test_trial_result_json_round_trip(tmp_path):
    t = rt.TrialResult(api_version="1.3", benchmark_version="v1.0.2", harness_sha="abc", data_commit="def",
                       data_root="/d", trial_name="t", model="slime-actor", agent_version="2.1.193",
                       base_url="http://x:1", agent_env={}, started_at="s", finished_at="f")
    p = t.to_json(tmp_path / "out" / "t.trial_result.json")
    d = json.loads(p.read_text())
    assert d["api_version"] == "1.3" and d["repos"] == [] and d["mode"] == "run"


def test_project_root_points_at_the_repo():
    assert (rt.PROJECT_ROOT / "scripts" / "run_all.py").is_file()
    assert (rt.PROJECT_ROOT / "harness" / "api.py").is_file()


def test_api_exports_run_trial_and_types():
    from harness import api
    assert api.run_trial.__doc__.startswith("Continuous Task Evaluation")
    assert api.TrialResult is rt.TrialResult and api.MilestoneResult is rt.MilestoneResult
    with pytest.raises(AttributeError):
        api.__getattr__("NoSuchThing")


# ───────────────────── equivalence knobs (2026-09-03 review) ─────────────────────
def test_official_zero_is_not_replaced_by_the_local_count(tmp_path):
    """A legitimately zero official value must win over the local tally; `or` would not."""
    ws = _make_repo(tmp_path)
    _write_trial(ws, {"M1": _result(False, f2p_ach=0, p2p_ach=6, p2p_failed=2)})
    r = rt.aggregate_repo(ws, "t_001", worker_rc=0)
    assert r.official_summary["resolved"] == 0
    assert r.n_resolved == 0            # not the local count, not a fallback
    assert r.n_infra_invalid == 0


def test_worker_env_carries_the_score_moving_knobs(tmp_path):
    from harness.e2e.runtime_policy_binding import resolve_runtime_policy
    policy = resolve_runtime_policy("no_such_repo_x", tmp_path, unprotected=True)
    host = {"PATH": "/usr/bin"}
    env = rt.worker_env(data_root=Path("/data"), base_url="http://x:1", model="m", trial_name="t",
                        repo_name="r", agent_env=None, policy=policy, host_env=host,
                        auto_compact_window=200000, enable_tool_search="false")
    assert env["SWE_MILESTONE_AUTO_COMPACT_WINDOW"] == "200000"
    assert env["SWE_MILESTONE_ENABLE_TOOL_SEARCH"] == "false"
    bare = rt.worker_env(data_root=Path("/data"), base_url="http://x:1", model="m", trial_name="t",
                         repo_name="r", agent_env=None, policy=policy, host_env=host)
    assert "SWE_MILESTONE_AUTO_COMPACT_WINDOW" not in bare
    assert "SWE_MILESTONE_ENABLE_TOOL_SEARCH" not in bare


def test_run_trial_exposes_every_score_moving_knob():
    import inspect
    sig = inspect.signature(rt.run_trial).parameters
    for knob in ("agent", "agent_version", "reasoning_effort", "auto_compact_window",
                 "enable_tool_search", "build_failure_fail_closed", "timeout_s", "milestones"):
        assert knob in sig, knob
    assert sig["agent"].default == "claude-code"
    assert sig["build_failure_fail_closed"].default is False   # run_all's own default


def test_result_path_is_outside_the_data_tree(tmp_path, monkeypatch):
    t = rt.TrialResult(api_version="1.3", benchmark_version="v", harness_sha="", data_commit="",
                       data_root=str(tmp_path), trial_name="t", model="m", agent_version="v",
                       base_url="u", agent_env={}, started_at="s", finished_at="f")
    assert t.result_path == ""
    p = t.to_json(tmp_path / "out" / "t.trial_result.json")
    assert json.loads(p.read_text())["result_path"] == ""


def test_session_key_is_the_endpoint_session_id(tmp_path):
    from harness.e2e.runtime_policy_binding import resolve_runtime_policy
    assert rt.session_key("cte_001", "navidrome_x") == "cte_001/navidrome_x"
    policy = resolve_runtime_policy("no_such_repo_x", tmp_path, unprotected=True)
    env = rt.worker_env(data_root=Path("/d"), base_url="http://x:1", model="m", trial_name="cte_001",
                        repo_name="navidrome_x", agent_env=None, policy=policy,
                        host_env={"PATH": "/usr/bin"})
    assert env["UNIFIED_API_KEY"] == rt.session_key("cte_001", "navidrome_x")


def test_image_tag_is_inherited_by_the_worker():
    """SWE_MILESTONE_IMAGE_TAG selects which images the trial boots, so it must reach the worker."""
    assert "SWE_MILESTONE_IMAGE_TAG" in rt._INHERITED_ENV
    assert "SWE_MILESTONE_DATA_VERSION_CHECK" in rt._INHERITED_ENV
    assert "SWE_MILESTONE_BENCHMARK_VERSION" in rt._INHERITED_ENV


def test_missing_repo_marks_the_result_incomplete():
    """A repo that produced no trial directory is an unknown, not a zero: averaging over the
    survivors would report a plausible score for a trial that never covered its repo set."""
    t = rt.TrialResult(api_version="1.3", benchmark_version="v", harness_sha="", data_commit="",
                       data_root="/d", trial_name="t", model="m", agent_version="v", base_url="u",
                       agent_env={}, started_at="s", finished_at="f",
                       repos_requested=["a", "b"], repos_missing=["b"], complete=False)
    d = t.to_dict()
    assert d["complete"] is False and d["repos_missing"] == ["b"]
    assert d["repos_requested"] == ["a", "b"]
    ok = rt.TrialResult(api_version="1.3", benchmark_version="v", harness_sha="", data_commit="",
                        data_root="/d", trial_name="t", model="m", agent_version="v", base_url="u",
                        agent_env={}, started_at="s", finished_at="f")
    assert ok.complete is True and ok.repos_missing == []


def test_repo_result_records_its_launch_mode(tmp_path):
    ws = _make_repo(tmp_path)
    _write_trial(ws, {"M1": _result(True)})
    assert rt.aggregate_repo(ws, "t_001", worker_rc=0).mode == "fresh"
    assert rt.aggregate_repo(ws, "t_001", worker_rc=None, mode="resume").mode == "resume"


def test_agent_version_is_unpinned_by_default():
    """run_all omits --agent-version when the config omits it; the seam must not invent a pin."""
    import inspect
    assert inspect.signature(rt.run_trial).parameters["agent_version"].default is None
    assert not hasattr(rt, "DEFAULT_AGENT_VERSION")


def test_residue_prune_is_inherited():
    assert "SWE_MILESTONE_RESIDUE_PRUNE" in rt._INHERITED_ENV
