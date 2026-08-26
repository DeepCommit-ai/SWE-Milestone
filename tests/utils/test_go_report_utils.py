"""Ginkgo report parsing keeps suite-level failure evidence (issue #22).

A suite that fails to compile reports ``SuiteSucceeded: false`` with empty
``SpecReports`` and carries the full compiler output in
``SpecialSuiteFailureReasons``. The parser must retain that evidence instead
of silently treating the suite as an empty package. Record-only: spec counts
and outcomes must not change.
"""

import json

from harness.utils.go_report_utils import (
    convert_ginkgo_report_to_dict,
    parse_ginkgo_json_report,
)

COMPILE_FAILURE_REASON = (
    "Failed to compile plugins:\n\n"
    "# github.com/example/proj/plugins\n"
    "./wasm_base_plugin.go:28:6: loaderFunc redeclared in this block\n"
    "\t./base_capability.go:29:6: other declaration of loaderFunc"
)


def _report(tmp_path):
    healthy_spec = {
        "ContainerHierarchyTexts": ["Utils"],
        "LeafNodeType": "It",
        "LeafNodeText": "does the thing",
        "LeafNodeLocation": {"FileName": "/testbed/utils/x_test.go", "LineNumber": 10},
        "State": "passed",
        "RunTime": 1000000,
    }
    data = [
        {
            "SuitePath": "/testbed/plugins",
            "SuiteDescription": "Plugins Suite",
            "SuiteSucceeded": False,
            "SpecReports": None,
            "SpecialSuiteFailureReasons": [COMPILE_FAILURE_REASON],
            "RunTime": 0,
        },
        {
            "SuitePath": "/testbed/utils",
            "SuiteDescription": "Utils Suite",
            "SuiteSucceeded": True,
            "SpecReports": [healthy_spec],
            "SpecialSuiteFailureReasons": None,
            "RunTime": 2000000,
        },
    ]
    path = tmp_path / "eval_default.json"
    path.write_text(json.dumps(data))
    return path


class TestSuiteFailureFidelity:
    def test_parser_retains_special_suite_failure_reasons(self, tmp_path):
        summary = parse_ginkgo_json_report(_report(tmp_path), "github.com/example/proj")

        by_path = {s.suite_path: s for s in summary.suites}
        broken = by_path["/testbed/plugins"]
        healthy = by_path["/testbed/utils"]

        assert broken.succeeded is False
        assert broken.special_failure_reasons == [COMPILE_FAILURE_REASON]
        assert healthy.succeeded is True
        assert healthy.special_failure_reasons == []

    def test_spec_counts_unchanged_by_fidelity_fields(self, tmp_path):
        summary = parse_ginkgo_json_report(_report(tmp_path), "github.com/example/proj")

        assert (summary.total, summary.passed, summary.failed) == (1, 1, 0)

    def test_convert_to_dict_scores_are_unchanged(self, tmp_path):
        result = convert_ginkgo_report_to_dict(_report(tmp_path), "github.com/example/proj")

        assert result["summary"] == {
            "passed": 1,
            "failed": 0,
            "skipped": 0,
            "error": 0,
            "total": 1,
        }
        assert [t["outcome"] for t in result["tests"]] == ["passed"]
