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
