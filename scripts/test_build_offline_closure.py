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

def test_load_closure_config_missing_offline_build_exits(tmp_path):
    (tmp_path / "quarantine_configs").mkdir()
    (tmp_path / "quarantine_configs" / "foo.yaml").write_text(
        "ecosystem: [pip]\nclosure:\n  cache_paths: []\n")
    with pytest.raises(SystemExit):
        boc.load_closure_config("foo", tmp_path)

def test_load_closure_config_empty_cache_paths_ok(tmp_path):
    """scikit-style empty cache_paths [] must load successfully."""
    (tmp_path / "quarantine_configs").mkdir()
    (tmp_path / "quarantine_configs" / "foo.yaml").write_text(
        "ecosystem: [pip]\nclosure:\n  cache_paths: []\n  offline_build: 'pip install --no-index -f /wheelhouse -r /tmp/union_reqs.txt'\n")
    cfg = boc.load_closure_config("foo", tmp_path)
    assert cfg["cache_paths"] == []
    assert "offline_build" in cfg

def test_load_closure_config_returns_block(tmp_path):
    (tmp_path / "quarantine_configs").mkdir()
    (tmp_path / "quarantine_configs" / "foo.yaml").write_text(
        "ecosystem: [cargo]\nclosure:\n  cache_paths: ['/c']\n  offline_build: 'cargo build --offline'\n")
    cfg = boc.load_closure_config("foo", tmp_path)
    assert cfg["cache_paths"] == ["/c"]
    assert cfg["offline_build"] == "cargo build --offline"

def test_assert_no_self_packages_fires(tmp_path):
    (tmp_path / "org/apache/dubbo/dubbo-common/3.3.6").mkdir(parents=True)
    (tmp_path / "org/apache/dubbo/dubbo-common/3.3.6/dubbo-common-3.3.6.jar").write_text("x")
    with pytest.raises(SystemExit):
        boc.assert_no_self_packages(tmp_path, ["org/apache/dubbo/*/3.3.[4-9]*"])

def test_assert_no_self_packages_clean(tmp_path):
    (tmp_path / "io/smallrye/mutiny/2.9.0").mkdir(parents=True)
    boc.assert_no_self_packages(tmp_path, ["org/apache/dubbo/*/3.3.[4-9]*"])  # no raise

def test_discover_excludes_base_and_dedups():
    fake = ("burntsushi_ripgrep_14.1.1_15.0.0/base:latest\n"
            "burntsushi_ripgrep_14.1.1_15.0.0/base-offline:latest\n"
            "burntsushi_ripgrep_14.1.1_15.0.0/m01:latest\n"
            "burntsushi_ripgrep_14.1.1_15.0.0/m01:v0.9\n"
            "other_repo/m01:latest\n")
    got = boc.discover_milestone_images("burntsushi_ripgrep_14.1.1_15.0.0", _docker_images=fake)
    assert got == ["burntsushi_ripgrep_14.1.1_15.0.0/m01:latest"]

def test_render_union_dockerfile_structure():
    df = boc.render_union_dockerfile(
        "r/x", ["r/x/m01:latest", "r/x/m02:latest"], ["/usr/local/cargo/registry/cache"])
    assert "FROM r/x/base:latest AS final" in df
    assert df.count("COPY --from=r/x/m01:latest") >= 1
    assert df.count("COPY --from=r/x/m02:latest") >= 1
    assert "rsync" in df
    assert "COPY --from=builder /staging" in df
