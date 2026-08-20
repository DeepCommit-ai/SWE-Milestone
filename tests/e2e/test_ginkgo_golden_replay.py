"""Golden replay of a real build-dead Ginkgo cell (issue #22).

The unit tests around this change use synthetic reports, which is exactly
where a scoring regression would hide: hand-written ids match the code's own
assumptions. This test instead drives the real `compare_results` with the
inputs of an actually-recorded evaluation — the cell issue #22 quotes — and
asserts that every scored number and every id list is byte-identical to what
that run recorded, while the new record-only fields are populated.

Fixture: `fixtures/navidrome_ginkgo_build_dead_cell.json.gz`, trimmed from
  SWE-Milestone-log/residue_prune_verify/run1/navidrome_navidrome_v0.57.0_v0.58.0
    /opus-4.7-xhigh/milestone_003_sub-01/prune_off
to the inputs compare_results reads plus the numbers it produced. That run
recorded 660 tests, 603 N2P required / 0 achieved, 271 P2P missing, and an
empty build_failure_policy — the exact shape the issue reports.

Writing this replay is what caught a real defect: driven with `eval.json`
(bare package ids) instead of the `eval_summary.json` the evaluator actually
prefers, nothing matched and every id looked missing. The bug was in the
replay, not the evaluator — but a synthetic-only test suite could not have
told the difference, which is the point.
"""

import gzip
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from harness.e2e.evaluator import PatchEvaluator

FIXTURE = Path(__file__).parent / "fixtures" / "navidrome_ginkgo_build_dead_cell.json.gz"
# The milestone's own test_config.json is what resolves the framework
# (navidrome's repo config sets no test_framework), so the replay needs the
# data root to reproduce that resolution exactly rather than asserting it.
DATA_ROOT = Path("/data2/gangda/SWE-Milestone-data/navidrome_navidrome_v0.57.0_v0.58.0")


def _load():
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as handle:
        return json.load(handle)


class _Meta(dict):
    """`_eval_meta` as a fresh run would have it: only the keys this cell
    actually recorded are set; everything else takes its natural empty value
    instead of forcing the test to enumerate ~60 unrelated fields."""

    def __missing__(self, key):
        if key.endswith(("_paths", "_list", "s")):
            return []
        if any(tok in key for tok in ("error", "sha", "tag", "reason", "source", "mode")):
            return ""
        return 0


def _replay(fixture):
    evaluator = object.__new__(PatchEvaluator)
    evaluator.milestone_id = "milestone_003_sub-01"
    evaluator.patch_file = None
    evaluator.output_dir = Path("/tmp/ginkgo-golden-replay")
    evaluator.workspace_root = DATA_ROOT
    evaluator.repo_config = {}
    evaluator.snapshot_legacy_unverified = False
    evaluator._eval_meta = _Meta()

    with redirect_stdout(io.StringIO()):
        result = evaluator.compare_results(
            fixture["classification"],
            fixture["summary_payload"],
            patch_exists=True,
            patch_applied=True,
        )
    return result.to_dict()


@pytest.fixture(scope="module")
def replayed():
    if not FIXTURE.exists():
        pytest.skip(f"fixture missing: {FIXTURE}")
    if not (DATA_ROOT / "dockerfiles" / "milestone_003_sub-01" / "test_config.json").exists():
        pytest.skip(f"data root not available: {DATA_ROOT}")
    fixture = _load()
    return fixture, _replay(fixture)


class TestScoringIsUnchanged:
    def test_every_scored_number_matches_the_recorded_run(self, replayed):
        fixture, actual = replayed
        expected = fixture["expected"]["test_summary"]

        assert {k: actual["test_summary"][k] for k in expected} == expected

    def test_resolved_matches(self, replayed):
        fixture, actual = replayed

        assert actual["resolved"] == fixture["expected"]["resolved"]

    @pytest.mark.parametrize(
        "category, bucket, key",
        [
            ("FAIL_TO_PASS", "success", "f2p_success"),
            ("NONE_TO_PASS", "success", "n2p_success"),
            ("PASS_TO_PASS", "failure", "p2p_failure"),
        ],
    )
    def test_id_lists_match_test_for_test(self, replayed, category, bucket, key):
        """Aggregate counts can agree while individual verdicts move."""
        fixture, actual = replayed

        assert sorted(actual["tests_status"][category].get(bucket) or []) == (
            fixture["expected"][key]
        )


class TestLostUniverseIsNowRecorded:
    def test_none_to_pass_missing_accounts_for_the_dead_suites(self, replayed):
        """All 603 N2P 'failures' in this cell never produced a result. The
        recorded run could not say so; the count is what makes that visible."""
        fixture, actual = replayed

        assert actual["test_summary"]["none_to_pass_missing"] == (
            fixture["expected"]["test_summary"]["none_to_pass_required"]
        )

    def test_absent_suites_names_go_packages_that_reported_nothing(self, replayed):
        _, actual = replayed
        absent = actual["build_failure_policy"]["absent_suites"]

        assert absent, "the cell's dead packages must be named"
        assert absent == sorted(absent)
        # Every entry is a real suite identity, not a fragment of an id.
        assert all("::" not in suite for suite in absent)
        # The packages issue #22 names as holding the 603 unreported N2P ids.
        assert {
            "github.com/navidrome/navidrome/model",
            "github.com/navidrome/navidrome/model/metadata",
            "github.com/navidrome/navidrome/persistence",
        } <= set(absent)

    def test_partial_test_universe_is_raised(self, replayed):
        """The recorded run left this False with a third of its universe gone."""
        _, actual = replayed

        assert actual["build_failure_policy"]["partial_test_universe"] is True


class TestMixedFrameworkRepo:
    def test_suite_identities_follow_the_ids_not_the_language(self, replayed):
        """navidrome runs ginkgo *and* vitest under one resolved framework
        ("ginkgo", inferred from the first test_config mode). Both dialects
        are `suite::test`, so a vitest file that reported nothing would be
        named the same way a Go package is. Pinned because it is a deliberate
        consequence of keying on the id, not an accident: in this cell the
        frontend suite ran, so nothing non-Go is listed."""
        _, actual = replayed
        absent = actual["build_failure_policy"]["absent_suites"]

        assert [s for s in absent if not s.startswith("github.com/")] == []
