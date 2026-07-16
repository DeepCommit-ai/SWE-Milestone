"""Agent-side masking must degrade per-file, never abort the whole setup.

Regression for the strict-detector rewrite: find_test_ranges_from_content now
raises RustTestFilterError on detector failures; the masking path promises the
historical behavior (skip the file, keep going) and must catch it.
"""

import pytest

from harness.e2e import test_masking
from harness.utils.rust_test_filter import RustTestFilterError


@pytest.fixture()
def _container_file(monkeypatch):
    monkeypatch.setattr(
        test_masking, "_read_file_from_container", lambda *_a, **_k: "fn t() {}"
    )


def test_detector_failure_skips_file_instead_of_raising(monkeypatch, _container_file):
    def _boom(*_args, **_kwargs):
        raise RustTestFilterError("synthetic detector failure")

    monkeypatch.setattr(test_masking, "find_test_ranges_from_content", _boom)
    assert test_masking.mask_inline_tests_in_file("container", "src/lib.rs") is False


def test_no_ranges_still_returns_false(monkeypatch, _container_file):
    monkeypatch.setattr(
        test_masking, "find_test_ranges_from_content", lambda *_a, **_k: []
    )
    assert test_masking.mask_inline_tests_in_file("container", "src/lib.rs") is False
