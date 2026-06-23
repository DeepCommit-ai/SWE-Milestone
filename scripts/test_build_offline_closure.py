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
    assert "rsync -a /milestone_" in df
    assert "COPY --from=builder /staging" in df

def test_render_union_dockerfile_empty_cache_paths():
    df = boc.render_union_dockerfile("r/x", ["r/x/m01:latest"], [])
    assert "AS builder" in df
    assert "AS final" in df
    assert "COPY --from=r/x/m01" not in df
    assert "RUN mkdir -p /staging" in df

def test_render_union_dockerfile_rsync_mkpath_and_trailing_slash():
    df = boc.render_union_dockerfile("r/x", ["r/x/m01:latest"], ["/usr/local/cargo/registry/cache"])
    assert "mkdir -p /staging/usr/local/cargo/registry/cache && rsync -a /milestone_0_0/usr/local/cargo/registry/cache/ /staging/usr/local/cargo/registry/cache/" in df

def test_cargo_vendor_sync_one_shot():
    cmd = boc.cargo_vendor_cmd(["/tb1/Cargo.toml", "/tb2/Cargo.toml"], "/opt/vendor")
    assert cmd.startswith("cargo vendor --versioned-dirs")
    assert cmd.count("--sync") == 2     # one call, multiple --sync (not a loop)
    assert cmd.rstrip().endswith("/opt/vendor")

def test_cargo_config_points_to_vendor():
    cfg = boc.cargo_config_toml("/opt/vendor")
    assert '[source.crates-io]' in cfg
    assert 'replace-with = "vendored-sources"' in cfg
    assert 'directory = "/opt/vendor"' in cfg

def test_pip_union_drops_self_and_editable():
    f1 = "numpy==2.4.1\n-e /testbed\narray-api-compat==1.13.0\n"
    f2 = "# scikit-learn==1.6.dev0\nnumpy==2.4.1\nscikit_learn==1.6.0\n"
    reqs = boc.pip_union_requirements([f1, f2], ["scikit-learn", "scikit_learn", "sklearn"])
    assert "array-api-compat==1.13.0" in reqs
    assert "numpy==2.4.1" in reqs
    assert not any("scikit" in r or "sklearn" in r for r in reqs)
    assert not any(r.startswith("-e") or r.startswith("#") for r in reqs)

def test_pip_multi_version_raises():
    with pytest.raises(SystemExit):
        boc.assert_single_version_or_explain(["foo==1.0", "foo==2.0"])

def test_offline_gate_cmd_is_network_none():
    cmd = boc.offline_gate_cmd("r/x/base-offline:staging-1", "r/x/m01:latest", "cargo build --offline")
    assert "--network" in cmd and "none" in cmd
    assert "cargo build --offline" in " ".join(cmd)
