# scripts/test_build_offline_closure.py
import sys, types
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_offline_closure as boc

def test_load_closure_config_missing_exits(tmp_path):
    (tmp_path / "quarantine_configs").mkdir()
    (tmp_path / "quarantine_configs" / "foo.yaml").write_text("ecosystem: [pip]\n")
    with pytest.raises(SystemExit):
        boc.load_closure_config("foo", tmp_path)

def test_load_closure_config_returns_block(tmp_path):
    (tmp_path / "quarantine_configs").mkdir()
    (tmp_path / "quarantine_configs" / "foo.yaml").write_text(
        "ecosystem: [cargo]\nclosure:\n  cache_paths: ['/c']\n  offline_build: 'cargo build --offline'\n")
    cfg = boc.load_closure_config("foo", tmp_path)
    assert cfg["cache_paths"] == ["/c"]
    assert cfg["offline_build"] == "cargo build --offline"

def test_discover_excludes_base_and_dedups():
    fake = ("burntsushi_ripgrep_14.1.1_15.0.0/base:latest\n"
            "burntsushi_ripgrep_14.1.1_15.0.0/base-offline:latest\n"
            "burntsushi_ripgrep_14.1.1_15.0.0/m01:latest\n"
            "burntsushi_ripgrep_14.1.1_15.0.0/m01:v0.9\n"
            "other_repo/m01:latest\n")
    got = boc.discover_milestone_images("burntsushi_ripgrep_14.1.1_15.0.0", _docker_images=fake)
    assert got == ["burntsushi_ripgrep_14.1.1_15.0.0/m01:latest"]
