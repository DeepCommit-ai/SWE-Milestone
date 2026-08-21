"""Repo-level test-id identity check (scripts/check_test_id_identity.py)."""

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_test_id_identity.py"
spec = importlib.util.spec_from_file_location("check_test_id_identity", SCRIPT)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)  # type: ignore[union-attr]

PY_A = "pkg/tests/test_a.py::test_same"
PY_B = "pkg/tests/test_b.py::test_same"
JAVA_A = "module-a::org.example.ParamTest::body [Book@5faeeb56]"
JAVA_B = "module-a::org.example.ParamTest::body [Book@62f11ebb]"


def _universe(data_root: Path, mid: str, classification: dict, framework: str):
    (data_root / "test_results" / mid).mkdir(parents=True)
    (data_root / "test_results" / mid / f"{mid}_classification.json").write_text(
        json.dumps({"stable_classification": classification})
    )
    (data_root / "dockerfiles" / mid).mkdir(parents=True)
    (data_root / "dockerfiles" / mid / "test_config.json").write_text(
        json.dumps([{"name": "default", "test_cmd": "x", "framework": framework}])
    )


def test_clean_universe_passes_and_all_buckets_are_read(tmp_path):
    root = tmp_path / "repo_x"
    _universe(root, "M1", {"pass_to_pass": [PY_A], "skipped_to_skipped": [PY_B], "fail_to_fail": ["pkg/t.py::t"]}, "pytest")
    report = check.check_repo(root, None, None, None)
    assert report["hard_failures"] == 0
    u = report["universes"][0]
    assert u["buckets"] == ["fail_to_fail", "pass_to_pass", "skipped_to_skipped"]
    assert u["ids"] == 3


def test_duplicate_raw_id_across_buckets_is_hard(tmp_path):
    root = tmp_path / "repo_x"
    _universe(root, "M1", {"pass_to_pass": [PY_A], "fail_to_fail": [PY_A]}, "pytest")
    report = check.check_repo(root, None, None, None)
    assert report["hard_failures"] == 1
    assert report["universes"][0]["hard"][0].startswith("duplicate-raw-ids")


def test_unknown_framework_string_is_hard(tmp_path):
    root = tmp_path / "repo_x"
    _universe(root, "M1", {"pass_to_pass": [PY_A]}, "go")  # not a scorer framework
    report = check.check_repo(root, None, None, None)
    assert any(h.startswith("framework-unknown") for h in report["universes"][0]["hard"])


def test_maven_hashcode_merge_is_info_not_hard(tmp_path):
    root = tmp_path / "repo_x"
    _universe(root, "M1", {"pass_to_pass": [JAVA_A, JAVA_B]}, "maven")
    report = check.check_repo(root, None, None, None)
    assert report["hard_failures"] == 0
    assert report["universes"][0]["info"]


def test_ginkgo_merge_is_warning(tmp_path):
    root = tmp_path / "repo_x"
    _universe(root, "M1", {"pass_to_pass": ["pkg-a::X > y", "pkg-b::X > y"]}, "ginkgo")
    report = check.check_repo(root, None, None, None)
    assert report["hard_failures"] == 0 and report["warnings"] == 1


def test_no_universes_is_a_failure(tmp_path):
    root = tmp_path / "repo_x"
    root.mkdir()
    report = check.check_repo(root, None, None, None)
    assert report["hard_failures"] == 1 and "error" in report


def test_runtime_payload_is_checked_against_its_milestone_only(tmp_path):
    root = tmp_path / "repo_x"
    _universe(root, "M1", {"pass_to_pass": [PY_A]}, "pytest")
    _universe(root, "M2", {"pass_to_pass": ["pkg/tests/test_c.py::test_same"]}, "pytest")
    payload = tmp_path / "eval_summary.json"
    payload.write_text(json.dumps({"results": {"passed": [PY_A, PY_B]}, "summary": {}}))
    report = check.check_repo(root, None, payload, "M1")
    # Identity key: PY_A and PY_B are distinct, no merge introduced.
    assert report["hard_failures"] == 0
    report2 = check.check_repo(root, None, payload, "M9")
    assert report2["hard_failures"] == 1 and "error" in report2
