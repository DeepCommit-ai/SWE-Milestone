"""Tests for evaluator test-framework resolution (fallback + fail-loud).

Regression guard for the 2026-07-12 go-zero incident: re-eval pointed
--workspace-root at a tree without config/, so test_framework read None,
TestIdNormalizer no-op'd, and go-zero N2P required went 17 -> 222. The
evaluator must (1) fall back to inferring the framework from the milestone's
test_config, and (2) fail loudly when the baseline clearly needs go_test
normalization but the framework did not resolve to go_test.
"""
import json

import pytest

from harness.e2e.evaluator import _resolve_test_framework


# go_test random-subtest IDs (t.Run(stringx.Rand())) — normalizer collapses these
GO_RANDOM_IDS = [
    f"github.com/x/y/core/collection/TestTimingWheel_SetAndMoveTwice/{s}"
    for s in ["57711fa31570c1f7", "a9a36459ef734152", "de3714ecdc81ffda",
              "7ffbd05eedfe5c85", "927777a8c9b592c7", "0c0570e4efc955fd"]
]
# Java parameterized IDs carry hashcodes but via a different normalization path
JAVA_IDS = [
    "org.apache.dubbo.rpc.protocol.rest.RestProtocolTest::bean arg [body: Book@5faeeb56]",
    "org.apache.dubbo.rpc.protocol.rest.RestProtocolTest::bean arg [body: Book@62f11ebb]",
]
# playwright / jest IDs have no `Parent/<rand>` shape
PLAYWRIGHT_IDS = [
    "right-panel/file-panel.spec.ts::FilePanel > render > empty state [Chrome]",
    "right-panel/file-panel.spec.ts::FilePanel > render > empty state [Firefox]",
]


def _write_test_config(tmp_path, milestone_id, test_cmd):
    d = tmp_path / "dockerfiles" / milestone_id
    d.mkdir(parents=True)
    (d / "test_config.json").write_text(json.dumps([{"test_cmd": test_cmd}]))


class TestResolveTestFramework:
    def test_explicit_config_wins(self, tmp_path):
        # Explicit config value is authoritative, no inference needed.
        fw = _resolve_test_framework({"test_framework": "go_test"}, tmp_path, "M001", GO_RANDOM_IDS)
        assert fw == "go_test"

    def test_infers_go_test_from_test_config_when_config_empty(self, tmp_path):
        # The go-zero incident: config/ absent, but the milestone test_config
        # carries `go test ...` — inference must recover go_test.
        _write_test_config(tmp_path, "M026", "go test -json ./... 2>&1 | tee out")
        fw = _resolve_test_framework({}, tmp_path, "M026", GO_RANDOM_IDS)
        assert fw == "go_test"

    def test_infers_cargo(self, tmp_path):
        _write_test_config(tmp_path, "M001", "cargo test --workspace")
        fw = _resolve_test_framework({}, tmp_path, "M001", [])
        assert fw == "cargo"

    def test_fail_loud_when_go_test_random_ids_but_unresolved(self, tmp_path):
        # No config, no test_config to infer from, yet the baseline is full of
        # go_test random subtests — silently no-op'ing would crater scores.
        with pytest.raises(ValueError, match="go_test"):
            _resolve_test_framework({}, tmp_path, "M026", GO_RANDOM_IDS)

    def test_no_false_alarm_on_java_hashcodes(self, tmp_path):
        # Java hashcodes go through a different normalization path; they must
        # NOT trip the go_test fail-loud guard.
        fw = _resolve_test_framework({}, tmp_path, "M001", JAVA_IDS)
        assert fw is None

    def test_no_false_alarm_on_playwright(self, tmp_path):
        fw = _resolve_test_framework({}, tmp_path, "M001", PLAYWRIGHT_IDS)
        assert fw is None

    def test_no_baseline_is_silent(self, tmp_path):
        # Without a baseline to probe, cannot fail loud; return None quietly.
        assert _resolve_test_framework({}, tmp_path, "M001", None) is None
