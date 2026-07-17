"""Tests for harness/e2e/data_version.py (benchmark data-version pinning).

Mirrors the resolve_image() contract on the data axis:
- default pin (env unset): any non-match is a loud warning, never fatal;
- explicit SWE_MILESTONE_IMAGE_TAG: any non-match refuses the launch;
- SWE_MILESTONE_DATA_VERSION_CHECK=off: recorded as unchecked, never fatal.
"""

import subprocess

import pytest

from harness.e2e.data_version import (
    DATA_VERSION_CHECK_ENV,
    VERSION_ENV,
    check_data_version,
    check_image_tag_consistency,
    expected_benchmark_version,
    inspect_data_version,
)
from harness.e2e.image_version import DEFAULT_IMAGE_TAG


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(cwd),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )


@pytest.fixture()
def data_repo(tmp_path):
    """A git 'data' repo with two commits; HEAD is the second."""
    root = tmp_path / "data"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.txt").write_text("one\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-qm", "one")
    (root / "a.txt").write_text("two\n")
    _git(root, "commit", "-aqm", "two")
    return root


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(VERSION_ENV, raising=False)
    monkeypatch.delenv(DATA_VERSION_CHECK_ENV, raising=False)


def test_expected_version_default():
    assert expected_benchmark_version() == (DEFAULT_IMAGE_TAG, False)


def test_expected_version_explicit(monkeypatch):
    monkeypatch.setenv(VERSION_ENV, "v9.9")
    assert expected_benchmark_version() == ("v9.9", True)


def test_inspect_match(data_repo):
    _git(data_repo, "tag", DEFAULT_IMAGE_TAG)
    info = inspect_data_version(data_repo)
    assert info["state"] == "match"
    assert info["expected_tag"] == DEFAULT_IMAGE_TAG
    assert len(info["commit"]) == 40


def test_inspect_mismatch(data_repo):
    _git(data_repo, "tag", DEFAULT_IMAGE_TAG, "HEAD~1")
    info = inspect_data_version(data_repo)
    assert info["state"] == "mismatch"


def test_inspect_tag_missing(data_repo):
    assert inspect_data_version(data_repo)["state"] == "tag-missing"


def test_inspect_not_a_git_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert inspect_data_version(plain)["state"] == "not-a-git-repo"


def test_inspect_from_subdirectory(data_repo):
    """Verification works when handed a repo subdir (e.g. a workspace_root)."""
    sub = data_repo / "some_repo_dir"
    sub.mkdir()
    _git(data_repo, "tag", DEFAULT_IMAGE_TAG)
    assert inspect_data_version(sub)["state"] == "match"


def test_check_match_returns_metadata(data_repo, capsys):
    _git(data_repo, "tag", DEFAULT_IMAGE_TAG)
    meta = check_data_version(data_repo, context="test")
    assert meta["benchmark_version"] == DEFAULT_IMAGE_TAG
    assert meta["data_version"]["state"] == "match"
    assert meta["data_version"]["checked"] is True
    assert "WARNING" not in capsys.readouterr().out


def test_check_default_pin_also_refuses(data_repo):
    # Policy hardened 2026-07-17: score comparability is the core contract, so
    # the DEFAULT pin refuses exactly like an explicit one (escape hatch:
    # SWE_MILESTONE_DATA_VERSION_CHECK=off, recorded as unchecked).
    with pytest.raises(SystemExit, match="pinned by default"):
        check_data_version(data_repo, context="test")  # tag-missing


def test_check_explicit_pin_refuses_mismatch(data_repo, monkeypatch):
    _git(data_repo, "tag", "v9.9", "HEAD~1")
    monkeypatch.setenv(VERSION_ENV, "v9.9")
    with pytest.raises(SystemExit):
        check_data_version(data_repo, context="test")


def test_check_explicit_pin_refuses_missing_tag(data_repo, monkeypatch):
    monkeypatch.setenv(VERSION_ENV, "v9.9")
    with pytest.raises(SystemExit):
        check_data_version(data_repo, context="test")


def test_check_off_never_fatal(data_repo, monkeypatch):
    monkeypatch.setenv(VERSION_ENV, "v9.9")
    monkeypatch.setenv(DATA_VERSION_CHECK_ENV, "off")
    meta = check_data_version(data_repo, context="test")
    assert meta["data_version"]["checked"] is False


def test_image_tag_consistent(capsys):
    meta = check_image_tag_consistency(
        f"swe-milestone/x__base-offline:{DEFAULT_IMAGE_TAG}", context="test"
    )
    assert meta["state"] == "match"
    assert meta["observed_tag"] == DEFAULT_IMAGE_TAG
    assert "WARNING" not in capsys.readouterr().out


def test_image_tag_mismatch_default_also_refuses():
    with pytest.raises(SystemExit, match="pinned by default"):
        check_image_tag_consistency(
            "swe-milestone/x__base-offline:latest", context="test"
        )


def test_image_tag_mismatch_explicit_refuses(monkeypatch):
    monkeypatch.setenv(VERSION_ENV, "v9.9")
    with pytest.raises(SystemExit):
        check_image_tag_consistency("swe-milestone/x__base-offline:v1.0", context="test")


def test_image_digest_pin_accepted_even_under_explicit_pin(monkeypatch, capsys):
    """A digest is a stronger immutable pin than any tag — never refused."""
    monkeypatch.setenv(VERSION_ENV, "v9.9")
    meta = check_image_tag_consistency(
        "swe-milestone/x__base-offline@sha256:" + "a" * 64, context="test"
    )
    assert meta["state"] == "digest-pinned"
    assert "WARNING" not in capsys.readouterr().out
