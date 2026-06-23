# scripts/test_build_offline_closure.py
import sys, types
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_offline_closure as boc


def _R(returncode=0, stdout="", stderr=""):
    """A stand-in for subprocess.CompletedProcess in monkeypatched tests."""
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

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


# ---- Task 4.2: cargo-vendor assembly ---------------------------------------

def test_assemble_cargo_dockerfile_structure():
    ms = ["r/x/m00:latest", "r/x/m01:latest", "r/x/m02:latest"]
    df = boc.assemble_cargo_dockerfile("r/x", ms)
    # two-stage build off base:latest
    assert "FROM r/x/base:latest AS vendor_builder" in df
    assert "FROM r/x/base:latest AS final" in df
    # one COPY --from per milestone, into /tb/m<i>
    for i, m in enumerate(ms):
        assert f"COPY --from={m} /testbed /tb/m{i}" in df
    # cwd is the FIRST milestone testbed, vendor syncs the rest
    assert "cd /tb/m0 &&" in df
    assert "cargo vendor" in df
    assert df.count("--sync") == len(ms) - 1   # m0 is cwd, not --sync'd
    assert "--sync /tb/m1/Cargo.toml" in df
    assert "--sync /tb/m2/Cargo.toml" in df
    assert "--sync /tb/m0/Cargo.toml" not in df
    # final stage copies the vendor dir out of the builder
    assert "COPY --from=vendor_builder /opt/vendor /opt/vendor" in df
    # config written to $CARGO_HOME, never /testbed/.cargo
    assert "/usr/local/cargo/config.toml" in df
    assert "/testbed/.cargo" not in df
    assert 'directory = "/opt/vendor"' in df


def test_assemble_cargo_dockerfile_single_milestone():
    """One milestone: it is the cwd, so there are zero --sync flags."""
    df = boc.assemble_cargo_dockerfile("r/x", ["r/x/m00:latest"])
    assert "cd /tb/m0 &&" in df
    assert "cargo vendor" in df
    assert "--sync" not in df
    assert "COPY --from=r/x/m00:latest /testbed /tb/m0" in df


def test_assemble_cargo_dockerfile_vendor_to_opt():
    """Vendor dir is /opt/vendor (image path), never under /testbed."""
    df = boc.assemble_cargo_dockerfile("r/x", ["r/x/m00:latest", "r/x/m01:latest"])
    assert "/opt/vendor" in df
    # the cargo vendor invocation must target /opt/vendor as its positional arg
    assert "/opt/vendor\n" in df or "/opt/vendor " in df


def test_resolve_config_path_case_insensitive(tmp_path):
    """--repo is lowercased for image tags, but the yaml file may be MixedCase."""
    (tmp_path / "quarantine_configs").mkdir()
    (tmp_path / "quarantine_configs" / "BurntSushi_ripgrep_14.1.1_15.0.0.yaml").write_text(
        "ecosystem: [cargo]\nclosure:\n  cache_paths: ['/c']\n  offline_build: 'cargo build --offline'\n")
    p = boc._resolve_config_path("burntsushi_ripgrep_14.1.1_15.0.0", tmp_path)
    assert p is not None and p.name == "BurntSushi_ripgrep_14.1.1_15.0.0.yaml"


def test_load_closure_config_case_insensitive(tmp_path):
    (tmp_path / "quarantine_configs").mkdir()
    (tmp_path / "quarantine_configs" / "BurntSushi_X.yaml").write_text(
        "ecosystem: [cargo]\nclosure:\n  cache_paths: ['/c']\n  offline_build: 'cargo build --offline'\n")
    cfg = boc.load_closure_config("burntsushi_x", tmp_path)
    assert cfg["cache_paths"] == ["/c"]


# ---- Task 4.2 freebie: cargo_vendor_cmd single-milestone has no double space --

def test_cargo_vendor_cmd_no_double_space_single():
    """Empty --sync list (single milestone) must not leave a `--versioned-dirs  /opt`
    double space."""
    cmd = boc.cargo_vendor_cmd([], "/opt/vendor")
    assert "  " not in cmd
    assert cmd == "cargo vendor --versioned-dirs /opt/vendor"


# ---- Task 4.3: in-image audit --------------------------------------------------

def test_audit_staging_image_empty_globs_is_noop(monkeypatch):
    """No forbid globs → no docker run, just return."""
    called = []
    monkeypatch.setattr(boc.subprocess, "run",
                        lambda *a, **k: called.append(a) or _R(0, ""))
    boc.audit_staging_image("r/x/base-offline:staging", [])
    assert called == []   # never shelled out


def test_audit_staging_image_clean_passes(monkeypatch):
    """Globs present but empty stdout (nothing matched) → return, no exit."""
    captured = {}
    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return _R(0, "")   # empty stdout = clean
    monkeypatch.setattr(boc.subprocess, "run", fake_run)
    boc.audit_staging_image("r/x/base-offline:staging", ["/c/grep-*.crate"])
    # ran `docker run ... ls -d <glob>` against the staging image
    assert captured["cmd"][:3] == ["docker", "run", "--rm"]
    assert "r/x/base-offline:staging" in captured["cmd"]
    joined = " ".join(captured["cmd"])
    assert "ls -d" in joined and "/c/grep-*.crate" in joined


def test_audit_staging_image_match_exits(monkeypatch):
    """Non-empty stdout (a glob matched → self@B leaked) → sys.exit(1)."""
    monkeypatch.setattr(boc.subprocess, "run",
                        lambda *a, **k: _R(0, "/c/grep-1.0.crate\n"))
    with pytest.raises(SystemExit) as e:
        boc.audit_staging_image("r/x/base-offline:staging", ["/c/grep-*.crate"])
    assert e.value.code == 1


# ---- Task 4.3: per-milestone offline gate -------------------------------------

def test_run_offline_gate_create_cp_run_sequence(monkeypatch):
    """Happy path builds the create→cp→rm→run sequence; exit-0 build → no raise."""
    calls = []
    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        if cmd[:2] == ["docker", "create"]:
            return _R(0, "deadbeefcid\n")
        return _R(0, "")   # cp, rm, run all succeed
    monkeypatch.setattr(boc.subprocess, "run", fake_run)
    boc.run_offline_gate("r/x/base-offline:staging", "r/x/m01:latest",
                         "cargo build --offline")
    kinds = [c[:2] for c in calls]
    assert ["docker", "create"] in kinds
    assert ["docker", "cp"] in kinds
    assert ["docker", "rm"] in kinds
    assert ["docker", "run"] in kinds
    # docker create targets the MILESTONE (B-source), not the staging image
    create = next(c for c in calls if c[:2] == ["docker", "create"])
    assert create[2] == "r/x/m01:latest"
    # the offline build runs against the STAGING image with --network none
    run = next(c for c in calls if c[:2] == ["docker", "run"])
    assert "--network" in run and "none" in run
    assert "r/x/base-offline:staging" in run
    assert "cargo build --offline" in " ".join(run)
    # and bind-mounts the copied milestone /testbed over /testbed
    assert any(isinstance(x, str) and x.endswith("/testbed:/testbed") for x in run)


def test_run_offline_gate_build_fail_closure_gap_exits(monkeypatch):
    """Build fails with a missing-module token whose bytes are ABSENT from the
    closure cache → real CLOSURE GAP → sys.exit(1) (never skip)."""
    GAP = ("core/x.go:1:2: no required module provides package "
           "example.com/totally/absent; to add it:\n\tgo get example.com/totally/absent\n")
    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["docker", "create"]:
            return _R(0, "cid\n")
        if cmd[:2] == ["docker", "run"]:
            # both the offline build AND the cache probe are `docker run`; neither
            # prints HIT here, so the probe reports the module ABSENT → gap.
            return _R(101, GAP)
        return _R(0, "")
    monkeypatch.setattr(boc.subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as e:
        boc.run_offline_gate("r/x/base-offline:staging", "r/x/m01:latest",
                             "go build ./...")
    assert e.value.code == 1


def test_run_offline_gate_source_state_returns_not_exit(monkeypatch):
    """Build fails on a module the closure cache DOES have (go.mod/source
    inconsistency, e.g. START-state checkpoint) → SOURCE_STATE, not a closure
    failure → returns the sentinel instead of exiting."""
    SS = ("core/stores/redis/hook.go:10:2: no required module provides package "
          "github.com/go-redis/redis/v8; to add it:\n\tgo get github.com/go-redis/redis/v8\n")
    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["docker", "create"]:
            return _R(0, "cid\n")
        if cmd[:2] == ["docker", "run"]:
            # The offline-build run (has the -v testbed mount + the build cmd) fails;
            # the cache-probe run (sh -c with a for-loop over cache dirs) reports HIT.
            joined = " ".join(cmd)
            if "cache/download" in joined:        # the cache probe
                return _R(0, "HIT\n")
            return _R(101, SS)                      # the offline build
        return _R(0, "")
    monkeypatch.setattr(boc.subprocess, "run", fake_run)
    got = boc.run_offline_gate("r/x/base-offline:staging", "r/x/m01:latest",
                               "go build ./...")
    assert got == "SOURCE_STATE"


def test_run_offline_gate_pass_returns_pass(monkeypatch):
    """Build exit 0 → returns "PASS"."""
    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["docker", "create"]:
            return _R(0, "cid\n")
        return _R(0, "")
    monkeypatch.setattr(boc.subprocess, "run", fake_run)
    assert boc.run_offline_gate("r/x/base-offline:staging", "r/x/m01:latest",
                                "go build ./...") == "PASS"


def test_classify_compile_error_is_source_state(monkeypatch):
    """A pure compile/type error (no missing-module token) is SOURCE-STATE: it is
    not a missing dependency, so it must not be reported as a closure gap. The
    cache probe is never consulted (no token to probe)."""
    called = {"probe": False}
    def fake_run(cmd, *a, **k):
        called["probe"] = True   # any docker run here would be the probe
        return _R(0, "")
    monkeypatch.setattr(boc.subprocess, "run", fake_run)
    kind, _ = boc.classify_offline_build_failure(
        "r/x/base-offline:staging",
        "core/foo.go:12:9: undefined: tests.MockBar\n")
    assert kind == "source_state"
    assert called["probe"] is False   # no token → no probe call


def test_classify_missing_from_cache_is_gap(monkeypatch):
    """A missing-module token whose bytes are absent from the cache → closure_gap."""
    monkeypatch.setattr(boc.subprocess, "run",
                        lambda *a, **k: _R(0, ""))   # probe: no HIT → absent
    kind, detail = boc.classify_offline_build_failure(
        "r/x/base-offline:staging",
        "x.go:1:2: no required module provides package example.com/gone; to add it:\n")
    assert kind == "closure_gap"
    assert "example.com/gone" in detail


def test_classify_present_in_cache_is_source_state(monkeypatch):
    """A missing-module token whose bytes ARE in the cache → source_state."""
    monkeypatch.setattr(boc.subprocess, "run",
                        lambda *a, **k: _R(0, "HIT\n"))
    kind, _ = boc.classify_offline_build_failure(
        "r/x/base-offline:staging",
        "x.go:1:2: no required module provides package github.com/go-redis/redis/v8;\n")
    assert kind == "source_state"


def test_run_offline_gate_create_fail_exits(monkeypatch):
    """`docker create` failure is fail-closed too."""
    monkeypatch.setattr(boc.subprocess, "run",
                        lambda cmd, *a, **k: _R(1, "", "no such image"))
    with pytest.raises(SystemExit):
        boc.run_offline_gate("r/x/base-offline:staging", "r/x/m01:latest",
                             "cargo build --offline")


def test_run_offline_gate_cleans_tmp(monkeypatch, tmp_path):
    """The host tmp dir is rmtree'd even though the build failed."""
    made = {}
    import tempfile as _t, shutil as _s
    def fake_mkdtemp(*a, **k):
        d = str(tmp_path / "gate")
        Path(d).mkdir()
        made["d"] = d
        return d
    removed = []
    monkeypatch.setattr(_t, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(_s, "rmtree", lambda d, **k: removed.append(d))
    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["docker", "create"]:
            return _R(0, "cid\n")
        if cmd[:2] == ["docker", "run"]:
            # closure-gap error (token absent from cache → probe returns no HIT) so
            # the gate fail-closes; the point of the test is the finally-cleanup.
            return _R(1, "no required module provides package example.com/x")
        return _R(0, "")
    monkeypatch.setattr(boc.subprocess, "run", fake_run)
    with pytest.raises(SystemExit):
        boc.run_offline_gate("r/x/base-offline:staging", "r/x/m01:latest", "go build ./...")
    assert made["d"] in removed


def test_audit_staging_image_docker_failure_exits(monkeypatch):
    """docker run non-zero return (daemon down, image gone) → fail-closed sys.exit(1)."""
    monkeypatch.setattr(boc.subprocess, "run",
                        lambda *a, **k: _R(1, "", "daemon down"))
    with pytest.raises(SystemExit) as e:
        boc.audit_staging_image("r/x/base-offline:staging", ["/c/grep-*.crate"])
    assert e.value.code == 1


# ---- Task 4.4: go ecosystem assembly + toolchain ------------------------------

def test_assemble_go_dockerfile_appends_clean_replace_toolchain():
    """The go branch unions the module cache (render_union_dockerfile) AND appends a
    clean-replace toolchain (`RUN rm -rf /usr/local/go` BEFORE
    `COPY .../usr/local/go`) + `ENV GOTOOLCHAIN=local` to the final stage. No
    `.info`-synth RUN (the build-scoped gate doesn't need it)."""
    ms = ["r/x/m01:latest", "r/x/m02:latest"]
    # fake probe: only the LAST milestone has the target go (B-end), as in go-zero
    probe = lambda img: "go1.21.13" if img == "r/x/m02:latest" else "go1.19.13"
    df = boc.assemble_go_dockerfile(
        "r/x", ms, ["/go/pkg/mod/cache/download"], "1.21.13", _probe=probe)
    # base of the assembly is the raw-cache union (final stage + rsync of the cache)
    assert "FROM r/x/base:latest AS final" in df
    assert "rsync -a /milestone_" in df
    assert "/go/pkg/mod/cache/download" in df
    # toolchain layer appended AFTER the union's final stage, with a clean-replace
    # `rm -rf /usr/local/go` BEFORE the COPY (overlay would mix go1.19/go1.21 stdlib
    # -> `go build` fails "m0 redeclared")
    assert "RUN rm -rf /usr/local/go" in df
    assert "COPY --from=r/x/m02:latest /usr/local/go /usr/local/go" in df
    assert "ENV GOTOOLCHAIN=local" in df
    # the .info-synth RUN was removed when the gate became build-scoped
    assert ".info" not in df and "find /go/pkg/mod/cache/download" not in df
    # ordering: union FROM final -> RUN rm -> COPY toolchain -> ENV GOTOOLCHAIN
    assert df.index("AS final") < df.index("RUN rm -rf /usr/local/go")
    assert df.index("RUN rm -rf /usr/local/go") < df.index("COPY --from=r/x/m02:latest /usr/local/go")
    assert df.index("COPY --from=r/x/m02:latest /usr/local/go") < df.index("ENV GOTOOLCHAIN=local")
    # equals the rendered union + the appended tail (no other mutation)
    union = boc.render_union_dockerfile("r/x", ms, ["/go/pkg/mod/cache/download"])
    assert df == union + (
        "RUN rm -rf /usr/local/go\n"
        "COPY --from=r/x/m02:latest /usr/local/go /usr/local/go\n"
        "ENV GOTOOLCHAIN=local\n")


def test_pick_go_toolchain_milestone_last_when_correct():
    """Last milestone (B-end) has the target go → it is picked (and probed)."""
    ms = ["r/x/m01:latest", "r/x/m02:latest", "r/x/m03:latest"]
    probed = []
    def probe(img):
        probed.append(img)
        return "go1.21.13" if img == "r/x/m03:latest" else "go1.19.13"
    got = boc.pick_go_toolchain_milestone(ms, "1.21.13", _probe=probe)
    assert got == "r/x/m03:latest"
    assert probed[0] == "r/x/m03:latest"   # last-first probing


def test_pick_go_toolchain_milestone_falls_back_when_last_wrong():
    """If the last milestone has the wrong go, an earlier one that matches is used."""
    ms = ["r/x/m01:latest", "r/x/m02:latest", "r/x/m03:latest"]
    # only m02 has the target; m03 (last) regressed/lacks it
    probe = lambda img: "go1.21.13" if img == "r/x/m02:latest" else "go1.19.13"
    got = boc.pick_go_toolchain_milestone(ms, "1.21.13", _probe=probe)
    assert got == "r/x/m02:latest"


def test_pick_go_toolchain_milestone_none_matches_exits():
    """No milestone reports the target go → fail-closed (would break offline gate)."""
    ms = ["r/x/m01:latest", "r/x/m02:latest"]
    probe = lambda img: "go1.19.13"
    with pytest.raises(SystemExit) as e:
        boc.pick_go_toolchain_milestone(ms, "1.21.13", _probe=probe)
    assert e.value.code == 1


def test_pick_go_toolchain_milestone_accepts_go_prefixed_target():
    """target_go may be given as "go1.21.13" or "1.21.13"; both match."""
    ms = ["r/x/m01:latest"]
    probe = lambda img: "go1.21.13"
    assert boc.pick_go_toolchain_milestone(ms, "go1.21.13", _probe=probe) == "r/x/m01:latest"
    assert boc.pick_go_toolchain_milestone(ms, "1.21.13", _probe=probe) == "r/x/m01:latest"


def test_probe_go_version_parses_token(monkeypatch):
    """_probe_go_version extracts the bare go token from `go version` output."""
    monkeypatch.setattr(boc.subprocess, "run",
                        lambda *a, **k: _R(0, "go version go1.21.13 linux/amd64\n"))
    assert boc._probe_go_version("r/x/m01:latest") == "go1.21.13"


def test_probe_go_version_returns_empty_on_failure(monkeypatch):
    """Probe failure (no such image / no go) → "" (picker treats as non-match)."""
    monkeypatch.setattr(boc.subprocess, "run",
                        lambda *a, **k: _R(1, "", "no such image"))
    assert boc._probe_go_version("r/x/nope:latest") == ""


# ---- Fix 1: @v-sound probe —————————————————————————————————————————————————
# These tests pin the CRITICAL soundness fix: _go_cache_has_path must check
# <path>/@v (not just the parent dir). Before the fix, a parent org-dir hit
# (e.g. github.com/go-redis/) would cause a false HIT even when the specific
# module's @v dir was absent.

def test_go_cache_probe_parent_dir_only_is_miss(monkeypatch):
    """Parent org-dir exists but /@v does NOT → probe returns False → closure_gap.

    This is the soundness bug: the old `[ -d "$d" ]` would HIT on the parent dir
    github.com/go-redis/ even when github.com/go-redis/redis/v8/@v is absent.
    The fixed probe uses `[ -d "$d/@v" ]` so the parent-only case is a MISS.
    """
    captured_cmds = []

    def fake_run(cmd, *a, **k):
        captured_cmds.append(cmd)
        if cmd[:2] == ["docker", "run"]:
            # Simulate: parent dirs exist as plain dirs, but none have @v.
            # The fixed probe tests each candidate as "$d/@v" — no HIT returned.
            return _R(0, "")   # empty stdout → no HIT
        return _R(0, "")

    monkeypatch.setattr(boc.subprocess, "run", fake_run)
    result = boc._go_cache_has_path("r/x/base-offline:staging",
                                    "github.com/go-redis/redis/v8")
    assert result is False   # must be False (miss): parent dir ≠ @v present


def test_go_cache_probe_at_v_present_is_hit(monkeypatch):
    """<path>/@v exists in the container → probe returns True → source_state."""

    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["docker", "run"]:
            # The shell for-loop finds a candidate whose /@v dir exists → HIT.
            return _R(0, "HIT\n")
        return _R(0, "")

    monkeypatch.setattr(boc.subprocess, "run", fake_run)
    result = boc._go_cache_has_path("r/x/base-offline:staging",
                                    "github.com/go-redis/redis/v8")
    assert result is True   # @v present → source_state path


def test_go_cache_probe_shell_uses_at_v(monkeypatch):
    """The shell command sent to docker run must test `$d/@v`, not just `$d`."""
    captured = {}

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return _R(0, "")

    monkeypatch.setattr(boc.subprocess, "run", fake_run)
    boc._go_cache_has_path("r/x/base-offline:staging", "github.com/foo/bar")
    sh_cmd = " ".join(captured.get("cmd", []))
    # Must contain the @v suffix in the directory test — NOT just `[ -d "$d" ]`
    assert '[ -d "$d/@v" ]' in sh_cmd
    assert '[ -d "$d" ] ' not in sh_cmd  # old pattern must NOT be present


def test_classify_missing_from_cache_is_gap_at_v(monkeypatch):
    """classify uses _go_cache_has_path; when @v absent → closure_gap (end-to-end)."""
    # Simulate: docker run for probe returns no HIT (the @v dir is absent)
    monkeypatch.setattr(boc.subprocess, "run",
                        lambda *a, **k: _R(0, ""))   # no HIT → miss
    kind, detail = boc.classify_offline_build_failure(
        "r/x/base-offline:staging",
        "x.go:1:2: no required module provides package github.com/go-redis/redis/v8;\n")
    assert kind == "closure_gap"
    assert "github.com/go-redis/redis/v8" in detail


# ---- Fix 2: GOPROXY=off in go gate ————————————————————————————————————————

def test_run_offline_gate_go_sets_goproxy_off(monkeypatch):
    """`goproxy_off=True` must inject `-e GOPROXY=off` into the docker run argv."""
    docker_run_args = []

    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["docker", "create"]:
            return _R(0, "cid\n")
        if cmd[:2] == ["docker", "run"]:
            docker_run_args.extend(cmd)
            return _R(0, "")
        return _R(0, "")

    monkeypatch.setattr(boc.subprocess, "run", fake_run)
    boc.run_offline_gate("r/x/base-offline:staging", "r/x/m01:latest",
                         "go build ./...", goproxy_off=True)
    joined = " ".join(docker_run_args)
    assert "-e" in docker_run_args and "GOPROXY=off" in docker_run_args


def test_run_offline_gate_non_go_no_goproxy(monkeypatch):
    """`goproxy_off=False` (default, non-go) must NOT inject GOPROXY=off."""
    docker_run_args = []

    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["docker", "create"]:
            return _R(0, "cid\n")
        if cmd[:2] == ["docker", "run"]:
            docker_run_args.extend(cmd)
            return _R(0, "")
        return _R(0, "")

    monkeypatch.setattr(boc.subprocess, "run", fake_run)
    boc.run_offline_gate("r/x/base-offline:staging", "r/x/m01:latest",
                         "cargo build --offline")  # no goproxy_off kwarg
    assert "GOPROXY=off" not in docker_run_args


# ---- Fix 3: missing go.sum entry for module providing package regex ————————

def test_missing_gosum_providing_package_extracted(monkeypatch):
    """The 'providing package' variant captures the package path, not 'providing'."""
    # No probe needed: we're testing token extraction only (compile error path).
    monkeypatch.setattr(boc.subprocess, "run",
                        lambda *a, **k: _R(0, "HIT\n"))
    kind, detail = boc.classify_offline_build_failure(
        "r/x/base-offline:staging",
        "verifier/foo.go:5:2: missing go.sum entry for module providing package "
        "github.com/foo/bar/baz; to add:\n\tgo mod tidy\n")
    # Token extracted should be the package path, not the word "providing"
    assert kind == "source_state"   # HIT in cache → source_state
    assert "providing" not in detail or "github.com/foo/bar/baz" in detail


def test_missing_gosum_plain_module_still_works(monkeypatch):
    """The plain 'missing go.sum entry for module <module>' variant still works."""
    monkeypatch.setattr(boc.subprocess, "run",
                        lambda *a, **k: _R(0, "HIT\n"))
    kind, _ = boc.classify_offline_build_failure(
        "r/x/base-offline:staging",
        "missing go.sum entry for module github.com/some/dep\n")
    assert kind == "source_state"


# ---- Task 4.5: pip ecosystem assembly (freeze → union → download) ------------

def test_collect_pip_freezes_runs_each_image(monkeypatch):
    """One `docker run --rm <m> pip freeze` per milestone; returns the texts."""
    seen = []
    def fake_run(cmd, *a, **k):
        # docker run --rm <image> pip freeze
        assert cmd[:3] == ["docker", "run", "--rm"]
        assert cmd[-2:] == ["pip", "freeze"]
        img = cmd[-3]
        seen.append(img)
        return _R(0, f"numpy==2.4.1\n# from {img}\n")
    monkeypatch.setattr(boc.subprocess, "run", fake_run)
    out = boc.collect_pip_freezes(["r/x/m01:latest", "r/x/m06:latest"])
    assert seen == ["r/x/m01:latest", "r/x/m06:latest"]
    assert len(out) == 2
    assert "numpy==2.4.1" in out[0]


def test_collect_pip_freezes_fail_exits(monkeypatch):
    """A `pip freeze` non-zero (bad image / no pip) is fail-closed."""
    monkeypatch.setattr(boc.subprocess, "run",
                        lambda *a, **k: _R(1, "", "no such image"))
    with pytest.raises(SystemExit):
        boc.collect_pip_freezes(["r/x/m01:latest"])


def test_assemble_pip_dockerfile_two_stage_wheel_builder():
    """Multi-stage: wheel_builder does `pip download -r <reqs> -d /wheelhouse`
    off base:latest; final COPYs /wheelhouse + the reqs file back in."""
    df = boc.assemble_pip_dockerfile("r/x", "union_reqs.txt")
    # builder stage off base:latest
    assert "FROM r/x/base:latest AS wheel_builder" in df
    # copies the reqs from the build context and downloads into /wheelhouse (online)
    assert "COPY union_reqs.txt /tmp/union_reqs.txt" in df
    assert "pip download -r /tmp/union_reqs.txt -d /wheelhouse" in df
    # final stage off the SAME base, carrying the wheelhouse + reqs
    assert "FROM r/x/base:latest AS final" in df
    assert "COPY --from=wheel_builder /wheelhouse /wheelhouse" in df
    # the reqs file must also exist in the final image (gate -r reads it)
    assert df.count("COPY union_reqs.txt /tmp/union_reqs.txt") == 2
    # ordering: builder download BEFORE final COPY of the wheelhouse
    assert df.index("pip download") < df.index("COPY --from=wheel_builder")


def test_audit_wheelhouse_self_exclusion_empty_forbid_noop(monkeypatch):
    """No forbid names → no docker run, just return (nothing to audit)."""
    called = []
    monkeypatch.setattr(boc.subprocess, "run",
                        lambda *a, **k: called.append(a) or _R(0, ""))
    boc.audit_wheelhouse_self_exclusion("r/x/base-offline:staging", [])
    assert called == []


def test_audit_wheelhouse_self_exclusion_clean_passes(monkeypatch):
    """Forbid names present but no matching wheel in /wheelhouse → return, no exit.
    Runs an in-image `ls /wheelhouse` against the staging image."""
    captured = {}
    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return _R(0, "")   # empty = no forbidden wheel
    monkeypatch.setattr(boc.subprocess, "run", fake_run)
    boc.audit_wheelhouse_self_exclusion(
        "r/x/base-offline:staging", ["scikit-learn", "scikit_learn", "sklearn"])
    assert captured["cmd"][:3] == ["docker", "run", "--rm"]
    assert "r/x/base-offline:staging" in captured["cmd"]
    assert "/wheelhouse" in " ".join(captured["cmd"])


def test_audit_wheelhouse_self_exclusion_forbidden_wheel_exits(monkeypatch):
    """A forbidden wheel in /wheelhouse (the answer would be served) → sys.exit(1)."""
    monkeypatch.setattr(
        boc.subprocess, "run",
        lambda *a, **k: _R(0, "scikit_learn-1.6.0-cp310-cp310-linux_x86_64.whl\n"))
    with pytest.raises(SystemExit) as e:
        boc.audit_wheelhouse_self_exclusion(
            "r/x/base-offline:staging", ["scikit-learn", "scikit_learn", "sklearn"])
    assert e.value.code == 1


def test_audit_wheelhouse_self_exclusion_full_name_boundary():
    """scikit-image / scikit_image are NOT a forbid match for scikit-learn — the
    matcher must use the wheel's full normalized DIST name, not a prefix. This
    tests the pure name-matcher (no docker)."""
    forbid = ["scikit-learn", "scikit_learn", "sklearn"]
    # forbidden
    assert boc._wheel_is_forbidden("scikit_learn-1.6.0-cp310-cp310-linux_x86_64.whl", forbid)
    assert boc._wheel_is_forbidden("sklearn-0.0-py3-none-any.whl", forbid)
    # NOT forbidden — full-name boundary (scikit_image is a legit third-party dep)
    assert not boc._wheel_is_forbidden("scikit_image-0.26.0-cp310-cp310-linux_x86_64.whl", forbid)
    assert not boc._wheel_is_forbidden("scikit_learn_extra-0.3.0-py3-none-any.whl", forbid)
    assert not boc._wheel_is_forbidden("numpy-2.4.1-cp310-cp310-linux_x86_64.whl", forbid)


def test_audit_wheelhouse_self_exclusion_match_among_many(monkeypatch):
    """Given a /wheelhouse listing with many wheels incl. scikit_image (keep) and
    a scikit_learn (forbidden), the audit must fire on the scikit_learn only."""
    listing = ("array_api_compat-1.13.0-py3-none-any.whl\n"
               "array_api_strict-2.4.1-py3-none-any.whl\n"
               "scikit_image-0.26.0-cp310-cp310-linux_x86_64.whl\n"
               "numpy-2.4.1-cp310-cp310-linux_x86_64.whl\n"
               "scikit_learn-1.6.0-cp310-cp310-linux_x86_64.whl\n")
    monkeypatch.setattr(boc.subprocess, "run", lambda *a, **k: _R(0, listing))
    with pytest.raises(SystemExit):
        boc.audit_wheelhouse_self_exclusion(
            "r/x/base-offline:staging", ["scikit-learn", "scikit_learn", "sklearn"])


def test_run_pip_offline_gate_install_ok_no_exit(monkeypatch):
    """`pip install --no-index` of the union exits 0 → no raise. Runs the staging
    image with --network none (no /testbed injection needed for pip)."""
    captured = {}
    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return _R(0, "Successfully installed numpy-2.4.1\n")
    monkeypatch.setattr(boc.subprocess, "run", fake_run)
    boc.run_pip_offline_gate(
        "r/x/base-offline:staging",
        "pip install --no-index -f /wheelhouse -r /tmp/union_reqs.txt")
    run = captured["cmd"]
    assert run[:3] == ["docker", "run", "--rm"]
    assert "--network" in run and "none" in run
    assert "r/x/base-offline:staging" in run
    joined = " ".join(run)
    assert "pip install --no-index -f /wheelhouse -r /tmp/union_reqs.txt" in joined
    # pip gate must NOT bind-mount a milestone /testbed (unlike the go/cargo gate)
    assert not any(isinstance(x, str) and x.endswith(":/testbed") for x in run)


def test_run_pip_offline_gate_install_fail_exits(monkeypatch):
    """A non-zero `pip install` offline = a needed wheel is missing → sys.exit(1)."""
    monkeypatch.setattr(
        boc.subprocess, "run",
        lambda *a, **k: _R(1, "", "ERROR: No matching distribution found for foo==1.0"))
    with pytest.raises(SystemExit) as e:
        boc.run_pip_offline_gate(
            "r/x/base-offline:staging",
            "pip install --no-index -f /wheelhouse -r /tmp/union_reqs.txt")
    assert e.value.code == 1


def test_build_closure_pip_branch_wires_freeze_union_audit_gate(monkeypatch, tmp_path):
    """End-to-end-ish (all docker mocked): the pip branch in build_closure collects
    freezes, unions+asserts, builds the staging image, runs the wheelhouse audit and
    the pip offline gate, then tags :latest. Asserts the call ORDER and that the
    reqs handed to download exclude scikit-learn but include array-api-compat."""
    # quarantine config: pip ecosystem, forbid sklearn family
    (tmp_path / "quarantine_configs").mkdir()
    (tmp_path / "quarantine_configs" / "sk.yaml").write_text(
        "ecosystem: [pip]\n"
        "wheelhouse_forbid: [scikit-learn, scikit_learn, sklearn]\n"
        "closure:\n"
        "  cache_paths: []\n"
        "  offline_build: 'pip install --no-index -f /wheelhouse -r /tmp/union_reqs.txt'\n")

    events = []
    # milestone discovery → two fake milestones
    monkeypatch.setattr(boc, "discover_milestone_images",
                        lambda repo: ["sk/m01:latest", "sk/m06:latest"])
    # freeze: base-like m01 lacks array-api; m06 has it + the editable comment + scikit
    def fake_collect(images):
        events.append(("freeze", list(images)))
        return [
            "numpy==2.4.1\nscikit-image==0.26.0\n",
            "numpy==2.4.1\narray-api-compat==1.13.0\narray_api_strict==2.4.1\n"
            "scikit-image==0.26.0\n# Editable install scikit-learn==1.6.dev0\n",
        ]
    monkeypatch.setattr(boc, "collect_pip_freezes", fake_collect)

    built = {}
    def fake_build(df, tag, root):
        events.append(("build", tag))
        built["df"] = df
        built["tag"] = tag
        # capture the reqs file the dockerfile references and that it was written
        # to the build context
        ctx_reqs = list(Path(root).glob("union_reqs*.txt"))
        built["reqs_files"] = ctx_reqs
        built["reqs_text"] = ctx_reqs[0].read_text() if ctx_reqs else ""
    monkeypatch.setattr(boc, "_docker_build", fake_build)
    monkeypatch.setattr(boc, "audit_wheelhouse_self_exclusion",
                        lambda tag, forbid: events.append(("audit", tag, tuple(forbid))))
    monkeypatch.setattr(boc, "run_pip_offline_gate",
                        lambda tag, ob: events.append(("gate", tag)))
    # docker tag / rmi
    def fake_sub_run(cmd, *a, **k):
        if cmd[:2] == ["docker", "tag"]:
            events.append(("tag", cmd[2], cmd[3]))
        return _R(0, "")
    monkeypatch.setattr(boc.subprocess, "run", fake_sub_run)

    boc.build_closure("sk", tmp_path, push=False, keep=True)

    kinds = [e[0] for e in events]
    # freeze BEFORE build BEFORE audit BEFORE gate BEFORE tag
    assert kinds.index("freeze") < kinds.index("build") < kinds.index("audit") \
        < kinds.index("gate") < kinds.index("tag")
    # staging built, :latest tagged
    assert built["tag"] == "sk/base-offline:staging"
    assert ("tag", "sk/base-offline:staging", "sk/base-offline:latest") in events
    # the reqs handed to the wheel_builder: array-api-compat IN, scikit-learn OUT
    assert "array-api-compat==1.13.0" in built["reqs_text"]
    assert "array_api_strict==2.4.1" in built["reqs_text"]
    assert "scikit-image==0.26.0" in built["reqs_text"]   # legit dep kept
    assert "scikit-learn" not in built["reqs_text"]
    assert "scikit_learn" not in built["reqs_text"]
    # audit got the forbid list from the yaml
    audit_ev = next(e for e in events if e[0] == "audit")
    assert audit_ev[2] == ("scikit-learn", "scikit_learn", "sklearn")


def test_build_closure_pip_branch_multi_version_exits(monkeypatch, tmp_path):
    """If two milestones freeze the SAME package at different versions, the pip
    branch fail-closes (assert_single_version_or_explain) before building."""
    (tmp_path / "quarantine_configs").mkdir()
    (tmp_path / "quarantine_configs" / "sk.yaml").write_text(
        "ecosystem: [pip]\n"
        "wheelhouse_forbid: [scikit-learn]\n"
        "closure:\n"
        "  cache_paths: []\n"
        "  offline_build: 'pip install --no-index -f /wheelhouse -r /tmp/union_reqs.txt'\n")
    monkeypatch.setattr(boc, "discover_milestone_images",
                        lambda repo: ["sk/m01:latest", "sk/m06:latest"])
    monkeypatch.setattr(boc, "collect_pip_freezes",
                        lambda images: ["numpy==2.4.1\n", "numpy==2.5.0\n"])
    monkeypatch.setattr(boc, "_docker_build",
                        lambda *a, **k: pytest.fail("must not build on version conflict"))
    monkeypatch.setattr(boc.subprocess, "run", lambda *a, **k: _R(0, ""))
    with pytest.raises(SystemExit):
        boc.build_closure("sk", tmp_path, push=False, keep=True)
