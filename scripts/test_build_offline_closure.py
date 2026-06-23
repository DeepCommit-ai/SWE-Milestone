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
    # cwd is the FIRST milestone testbed, vendor syncs the rest PLUS the base
    # A-baseline manifest (/testbed/Cargo.toml, present in the FROM-base stage) so
    # the vendor spans A→B and the agent's A-start `cargo build` resolves.
    assert "cd /tb/m0 &&" in df
    assert "cargo vendor" in df
    # m0 is cwd (not --sync'd); m1..mN are --sync'd; the base A-baseline is one more.
    assert df.count("--sync") == (len(ms) - 1) + 1
    assert "--sync /tb/m1/Cargo.toml" in df
    assert "--sync /tb/m2/Cargo.toml" in df
    assert "--sync /tb/m0/Cargo.toml" not in df
    # the base image's own /testbed (the A-baseline) is synced alongside milestones.
    assert "--sync /testbed/Cargo.toml" in df
    # final stage copies the vendor dir out of the builder
    assert "COPY --from=vendor_builder /opt/vendor /opt/vendor" in df
    # config written to $CARGO_HOME, never /testbed/.cargo
    assert "/usr/local/cargo/config.toml" in df
    assert "/testbed/.cargo" not in df
    assert 'directory = "/opt/vendor"' in df


def test_assemble_cargo_dockerfile_single_milestone():
    """One milestone: it is the cwd, so the only --sync is the base A-baseline
    manifest (/testbed/Cargo.toml) — the milestone itself is m0/cwd, not --sync'd."""
    df = boc.assemble_cargo_dockerfile("r/x", ["r/x/m00:latest"])
    assert "cd /tb/m0 &&" in df
    assert "cargo vendor" in df
    # exactly one --sync: the base A-baseline manifest (vendor must span A→B).
    assert df.count("--sync") == 1
    assert "--sync /testbed/Cargo.toml" in df
    assert "COPY --from=r/x/m00:latest /testbed /tb/m0" in df


def test_assemble_cargo_dockerfile_syncs_base_a_baseline():
    """Closure gap fix: the vendor MUST span A→B. The vendor_builder stage is
    `FROM <repo>/base:latest`, so its OWN /testbed is the A-baseline (A Cargo.lock,
    e.g. bstr 1.10.0). Assert the base A-baseline manifest (/testbed/Cargo.toml) is
    in the --sync set alongside the milestone (B) testbeds, so `cargo vendor
    --versioned-dirs` holds BOTH the A and B versions of each crate. Without this,
    an agent starting from the A-baseline fails its first `cargo build` (the vendor
    only has B-version crates)."""
    for ms in (["r/x/m00:latest"],
               ["r/x/m00:latest", "r/x/m01:latest", "r/x/m02:latest"]):
        df = boc.assemble_cargo_dockerfile("r/x", ms)
        # The base stage's own /testbed (A-baseline) is synced — NOT a /tb/m* copy.
        assert "--sync /testbed/Cargo.toml" in df
        # cwd stays the first milestone workspace; the A-baseline is just one more
        # --sync (cargo vendor needs a workspace manifest as cwd, m0 provides it).
        assert "cd /tb/m0 &&" in df


def test_assemble_cargo_dockerfile_vendor_to_opt():
    """Vendor dir is /opt/vendor (image path), never under /testbed."""
    df = boc.assemble_cargo_dockerfile("r/x", ["r/x/m00:latest", "r/x/m01:latest"])
    assert "/opt/vendor" in df
    # the cargo vendor invocation must target /opt/vendor as its positional arg
    assert "/opt/vendor\n" in df or "/opt/vendor " in df


def test_assemble_cargo_dockerfile_installs_rust_toolchain_when_set():
    """toolchain.rust set (nushell → 1.88.0) ⇒ a build-time online rustup install
    in the FINAL stage, with `rustup default` so cargo uses it outside /testbed."""
    df = boc.assemble_cargo_dockerfile(
        "r/x", ["r/x/m00:latest"], toolchain={"rust": "1.88.0", "install_online": True})
    assert "rustup toolchain install 1.88.0 --profile minimal" in df
    assert "rustup default 1.88.0" in df
    # the install must land in the FINAL stage (after `FROM ... AS final`), not the
    # throwaway vendor_builder — otherwise the published image lacks the toolchain.
    final_idx = df.index("FROM r/x/base:latest AS final")
    assert df.index("rustup toolchain install 1.88.0") > final_idx


def test_assemble_cargo_dockerfile_no_toolchain_no_rust_install():
    """No toolchain key (ripgrep) ⇒ NO rustup-install line is emitted (unchanged).
    Cover both the default (None) and an empty/`{}`/no-`rust` dict."""
    for tc in (None, {}, {"install_online": True}):
        df = boc.assemble_cargo_dockerfile("r/x", ["r/x/m00:latest"], toolchain=tc)
        assert "rustup toolchain install" not in df
        assert "rustup default" not in df


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


# ---- cargo A-baseline offline gate (agent-start closure-gap guard) ------------

def test_cargo_abaseline_gate_pass_no_injection(monkeypatch):
    """EXIT 0 → returns (None) and runs against the STAGING image's OWN /testbed
    (the A-baseline) with --network none — NO docker-create/cp injection."""
    calls = []
    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        return _R(0, "")
    monkeypatch.setattr(boc.subprocess, "run", fake_run)
    assert boc.run_cargo_abaseline_gate(
        "r/x/base-offline:staging", "cargo build --offline") is None
    # exactly one docker run; no create/cp (the staging /testbed IS the A-baseline).
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[:2] == ["docker", "run"]
    assert "--network" in cmd and "none" in cmd
    assert "r/x/base-offline:staging" in cmd
    joined = " ".join(cmd)
    assert "cd /testbed && cargo build --offline" in joined
    assert "docker create" not in joined and "docker cp" not in joined


def test_cargo_abaseline_gate_select_version_is_gap(monkeypatch):
    """`failed to select a version` (vendor lacks the A-version a Cargo.lock pins)
    → real CLOSURE GAP → sys.exit(1). This is the exact glm-5.2 failure."""
    GAP = ("error: failed to select a version for the requirement `bstr = \"^1.10\"`\n"
           "candidate versions found which didn't match: 1.12.0\n")
    monkeypatch.setattr(boc.subprocess, "run", lambda *a, **k: _R(101, "", GAP))
    with pytest.raises(SystemExit) as e:
        boc.run_cargo_abaseline_gate("r/x/base-offline:staging", "cargo build --offline")
    assert e.value.code == 1


def test_cargo_abaseline_gate_not_in_vendored_is_gap(monkeypatch):
    """`not found in vendored sources` is also treated as a closure gap → exit 1."""
    GAP = "error: the crate `bstr v1.10.0` was not found in vendored sources\n"
    monkeypatch.setattr(boc.subprocess, "run", lambda *a, **k: _R(101, GAP, ""))
    with pytest.raises(SystemExit) as e:
        boc.run_cargo_abaseline_gate("r/x/base-offline:staging", "cargo build --offline")
    assert e.value.code == 1


def test_cargo_abaseline_gate_any_nonzero_is_gap(monkeypatch):
    """The A-baseline is a clean compilable state, so ANY non-zero offline build is
    fail-closed (a real gap), even without a recognised vendored-source signature —
    unlike the per-milestone gate, there is no source-state escape hatch here."""
    monkeypatch.setattr(boc.subprocess, "run",
                        lambda *a, **k: _R(101, "", "error: some other build failure\n"))
    with pytest.raises(SystemExit) as e:
        boc.run_cargo_abaseline_gate("r/x/base-offline:staging", "cargo build --offline")
    assert e.value.code == 1


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


# ---- Task 4.4: maven ecosystem assembly + self@B removal ----------------------

# The dubbo cache_forbid_globs (top-level in the quarantine yaml). These ARE the
# self@B patterns: the milestone `.m2` caches hold dubbo's own
# org/apache/dubbo/<mod>/3.3.6-SNAPSHOT/* jars — the answer — and these globs both
# audit AND (now) drive the rm. The rm targets MUST be byte-identical to them so
# the post-rm audit (same globs) matches nothing.
_DUBBO_FORBID = [
    "/root/.m2/repository/org/apache/dubbo/*/3.3.[4-9]*",
    "/root/.m2/repository/org/apache/dubbo/*/3.[4-9]*",
]


def test_maven_rm_self_at_b_cmd_targets_match_forbid_globs():
    """The self@B rm RUN must delete EXACTLY the config's cache_forbid_globs (so the
    generic audit, which runs the same globs, then finds nothing)."""
    line = boc.maven_rm_self_at_b_cmd(_DUBBO_FORBID)
    assert line.startswith("RUN rm -rf ")
    # every forbid glob appears verbatim as an rm target
    for g in _DUBBO_FORBID:
        assert g in line
    # rm'd targets == forbid globs, in order (no extra/missing pattern → audit clean)
    rm_targets = line[len("RUN rm -rf "):].split(" 2>/dev/null", 1)[0].split()
    assert rm_targets == _DUBBO_FORBID
    # errors suppressed + exit 0 so an unmatched glob (e.g. the 3.[4-9]* one) is fine
    assert "2>/dev/null; true" in line


def test_maven_rm_self_at_b_cmd_empty_is_noop():
    """No forbid globs → a harmless `RUN true` (well-formed, audit also a no-op)."""
    assert boc.maven_rm_self_at_b_cmd([]) == "RUN true\n"
    assert boc.maven_rm_self_at_b_cmd(None) == "RUN true\n"


def test_maven_online_fetch_cmd_goes_offline_and_test_scope():
    """The per-milestone fetch body runs BOTH a broad `dependency:go-offline`
    (plugins + compile/runtime graph) AND an ONLINE `test-compile` (the tightest
    match to the offline `mvn -o test-compile` gate — it downloads the exact
    versions/jars incl. test-scope deps that go-offline alone misses, e.g.
    bcprov-ext-jdk18on jar and otel-sdk-testing 1.50.0) into the SHARED
    maven.repo.local, cd'd into the milestone's reactor, each `|| true`-guarded, lint
    plugins skipped, and ONLINE (no `-o`)."""
    body = boc.maven_online_fetch_cmd("/tb/m3", "/root/.m2/repository")
    assert body.startswith("cd /tb/m3 && (")
    # the cheap broad first pass: plugins + compile/runtime graph
    assert "mvn -q dependency:go-offline" in body
    # the workhorse: ONLINE test-compile downloads exactly what the gate needs,
    # incl. all test-scope deps; -fae so a non-compiling module doesn't starve the
    # cache, -DskipTests so only test-SOURCES compile (still resolves test deps).
    assert "mvn -q -fae test-compile -DskipTests" in body
    # both write into the SHARED local repo
    assert body.count("-Dmaven.repo.local=/root/.m2/repository") == 2
    # ONLINE fetch stage — must NOT pass -o (that would defeat the whole point)
    assert " -o " not in f" {body} "
    # each goal guarded so one milestone's source-state never aborts the build
    assert body.count("|| true") == 2
    # lint plugins skipped so a spotless/rat violation can't abort the reactor
    for skip in ("-Dspotless.check.skip=true", "-Dcheckstyle.skip=true",
                 "-Drat.skip=true"):
        assert skip in body


def test_assemble_maven_dockerfile_online_fetch_union_plus_self_at_b_rm():
    """The maven branch ONLINE-fetches the union of each milestone's declared deps
    (go-offline + resolve test-scope) into the shared `.m2`, then COPYs it forward
    and rm's self@B last — like the npm/pip/cargo 'fetch the union online' pattern,
    NOT the old raw-cache rsync union."""
    ms = ["r/x/m01:latest", "r/x/m02:latest"]
    caches = ["/root/.m2/repository"]
    df = boc.assemble_maven_dockerfile("r/x", ms, caches, _DUBBO_FORBID)
    # two stages: an online fetch_builder (FROM base) + a final (FROM base)
    assert "FROM r/x/base:latest AS fetch_builder" in df
    assert "FROM r/x/base:latest AS final" in df
    # NO raw-cache rsync union anymore (that was the incomplete approach)
    assert "rsync -a /milestone_" not in df
    # the FULL /testbed of every milestone is COPY'd (reactor needs all module poms)
    assert "COPY --from=r/x/m01:latest /testbed /tb/m0" in df
    assert "COPY --from=r/x/m02:latest /testbed /tb/m1" in df
    # one online fetch RUN per milestone, each warming the shared local repo
    assert "RUN cd /tb/m0 && ( mvn -q dependency:go-offline" in df
    assert "RUN cd /tb/m1 && ( mvn -q dependency:go-offline" in df
    assert df.count("mvn -q -fae test-compile -DskipTests") == len(ms)
    # the warmed cache is COPY'd into the final stage, then self@B is rm'd from it
    assert "COPY --from=fetch_builder /root/.m2/repository /root/.m2/repository" in df
    assert "RUN rm -rf /root/.m2/repository/org/apache/dubbo/*/3.3.[4-9]*" in df
    # ordering: every milestone fetch BEFORE the final COPY BEFORE the self@B rm
    assert df.index("RUN cd /tb/m1 && (") < \
        df.index("COPY --from=fetch_builder /root/.m2/repository") < \
        df.index("RUN rm -rf /root/.m2/repository/org/apache/dubbo")


def test_assemble_maven_dockerfile_rm_after_fetch_and_copy_so_audit_clean():
    """The rm of self@B must be the LAST instruction (after the online fetch and the
    final-stage cache COPY), so what the generic audit later greps for — including
    any 3.3.6 jar the online fetch may have pulled — has already been deleted."""
    df = boc.assemble_maven_dockerfile(
        "r/x", ["r/x/m01:latest"], ["/root/.m2/repository"], _DUBBO_FORBID)
    # the self@B rm is the final instruction in the Dockerfile
    last = [ln for ln in df.strip().splitlines() if ln.strip()][-1]
    assert last.startswith("RUN rm -rf /root/.m2/repository/org/apache/dubbo")


def test_assemble_maven_dockerfile_empty_milestones_errors():
    """No milestones → fail-closed (can't build a closure with nothing to fetch)."""
    with pytest.raises(SystemExit):
        boc.assemble_maven_dockerfile("r/x", [], ["/root/.m2/repository"],
                                      _DUBBO_FORBID)


def test_build_closure_maven_branch_wires_union_rm_audit_gate(monkeypatch, tmp_path):
    """End-to-end-ish (all docker mocked): the maven branch in build_closure unions
    the `.m2`, builds the staging image WITH the self@B rm (forbid globs), runs the
    generic audit + per-milestone offline gate, then tags :latest. Asserts call
    ORDER and that the built Dockerfile carries the self@B rm."""
    (tmp_path / "quarantine_configs").mkdir()
    (tmp_path / "quarantine_configs" / "dub.yaml").write_text(
        "ecosystem: [maven]\n"
        "cache_forbid_globs:\n"
        "  - /root/.m2/repository/org/apache/dubbo/*/3.3.[4-9]*\n"
        "  - /root/.m2/repository/org/apache/dubbo/*/3.[4-9]*\n"
        "closure:\n"
        "  cache_paths:\n"
        "    - /root/.m2/repository\n"
        "  offline_build: 'git clean -xfd && mvn -o test-compile'\n")

    events = []
    monkeypatch.setattr(boc, "discover_milestone_images",
                        lambda repo: ["dub/m01:latest", "dub/m02:latest"])
    built = {}
    def fake_build(df, tag, root):
        events.append(("build", tag))
        built["df"] = df
        built["tag"] = tag
    monkeypatch.setattr(boc, "_docker_build", fake_build)
    monkeypatch.setattr(boc, "audit_staging_image",
                        lambda tag, globs: events.append(("audit", tag, tuple(globs))))
    monkeypatch.setattr(boc, "run_offline_gate",
                        lambda tag, m, ob, **k: events.append(("gate", m)) or "PASS")
    def fake_sub_run(cmd, *a, **k):
        if cmd[:2] == ["docker", "tag"]:
            events.append(("tag", cmd[2], cmd[3]))
        return _R(0, "")
    monkeypatch.setattr(boc.subprocess, "run", fake_sub_run)

    boc.build_closure("dub", tmp_path, push=False, keep=True)

    kinds = [e[0] for e in events]
    # build BEFORE audit BEFORE (per-milestone) gate BEFORE tag
    assert kinds.index("build") < kinds.index("audit") < kinds.index("gate") \
        < kinds.index("tag")
    assert built["tag"] == "dub/base-offline:staging"
    assert ("tag", "dub/base-offline:staging", "dub/base-offline:latest") in events
    # the staging Dockerfile ONLINE-fetches each milestone's declared deps (go-offline
    # + resolve test-scope) into the shared `.m2` and rm's self@B (the forbid globs)
    assert "rsync -a /milestone_" not in built["df"]
    assert "RUN cd /tb/m0 && ( mvn -q dependency:go-offline" in built["df"]
    assert "mvn -q -fae test-compile -DskipTests" in built["df"]
    assert "RUN rm -rf /root/.m2/repository/org/apache/dubbo/*/3.3.[4-9]*" in built["df"]
    # the generic audit got the SAME forbid globs the rm deleted (post-rm: clean)
    audit_ev = next(e for e in events if e[0] == "audit")
    assert audit_ev[2] == (
        "/root/.m2/repository/org/apache/dubbo/*/3.3.[4-9]*",
        "/root/.m2/repository/org/apache/dubbo/*/3.[4-9]*")
    # one offline gate per milestone (B-source mvn -o test-compile)
    assert [e[1] for e in events if e[0] == "gate"] == ["dub/m01:latest", "dub/m02:latest"]


# ---- maven offline-build failure classifier (self@B vs real closure gap) ------
# A maven offline build that cannot resolve a dependency emits
# `Could not resolve dependencies ... Cannot access <repo> in offline mode and the
# artifact <g>:<a>:<type>:<ver> has not been downloaded from it before`. The go
# classifier doesn't recognise these strings, so maven needs its own: an unresolved
# artifact that IS the repo's own self@B (org.apache.dubbo:*:3.3.6-SNAPSHOT — the
# sibling reactor modules we deliberately removed) is EXPECTED (the agent builds
# them from the reactor) → source_state; an unresolved THIRD-PARTY artifact is a
# real closure gap → BLOCK. The self@B patterns are derived from the SAME
# cache_forbid_globs that drove the rm.

_MVN_OFFLINE_SELF = (
    "[ERROR] Failed to execute goal on project dubbo: Could not resolve "
    "dependencies for project org.apache.dubbo:dubbo:jar:3.3.6-SNAPSHOT\n"
    "[ERROR] dependency: org.apache.dubbo:dubbo-spring6-security:jar:3.3.6-SNAPSHOT "
    "(compile?)\n"
    "[ERROR] \tCannot access apache.snapshots (https://repository.apache.org/"
    "snapshots) in offline mode and the artifact "
    "org.apache.dubbo:dubbo-spring6-security:jar:3.3.6-SNAPSHOT has not been "
    "downloaded from it before.\n")

_MVN_OFFLINE_THIRDPARTY = (
    "[ERROR] Failed to execute goal on project dubbo-common: Could not resolve "
    "dependencies for project org.apache.dubbo:dubbo-common:jar:3.3.6-SNAPSHOT\n"
    "[ERROR] \tCannot access central (https://repo.maven.apache.org/maven2) in "
    "offline mode and the artifact io.smallrye.reactive:mutiny:jar:2.9.0 has not "
    "been downloaded from it before.\n")


def test_maven_coord_self_at_b_matches_dubbo_snapshot():
    """org.apache.dubbo:*:3.3.6-SNAPSHOT is self@B (matches the forbid globs)."""
    assert boc._maven_coord_is_self_at_b(
        "org.apache.dubbo:dubbo-spring6-security:jar:3.3.6-SNAPSHOT", _DUBBO_FORBID)
    # a 3.4.x major bump is also self@B (second forbid glob /3.[4-9]*)
    assert boc._maven_coord_is_self_at_b(
        "org.apache.dubbo:dubbo-common:jar:3.4.0", _DUBBO_FORBID)


def test_maven_coord_third_party_is_not_self_at_b():
    """A non-dubbo artifact (mutiny) is NOT self@B → a real gap if unresolved."""
    assert not boc._maven_coord_is_self_at_b(
        "io.smallrye.reactive:mutiny:jar:2.9.0", _DUBBO_FORBID)
    # the A-baseline dubbo (3.3.3-SNAPSHOT) is NOT forbidden → not self@B
    assert not boc._maven_coord_is_self_at_b(
        "org.apache.dubbo:dubbo-common:jar:3.3.3-SNAPSHOT", _DUBBO_FORBID)


def test_classify_maven_self_at_b_unresolved_is_source_state():
    """An offline build that can't resolve a self@B sibling (dubbo-spring6-security
    :3.3.6-SNAPSHOT — removed on purpose) is SOURCE-STATE, not a closure gap: at
    eval the agent builds the sibling from the reactor; we excluded the answer jar
    by design. No docker probe needed (the classifier is pure-text for maven)."""
    classify = boc.classify_maven_offline_build_failure(_DUBBO_FORBID)
    kind, detail = classify("r/x/base-offline:staging", _MVN_OFFLINE_SELF)
    assert kind == "source_state"
    assert "dubbo-spring6-security" in detail or "self@B" in detail


def test_classify_maven_third_party_unresolved_is_closure_gap():
    """An offline build that can't resolve a THIRD-PARTY artifact (mutiny:2.9.0) is
    a REAL closure gap → must BLOCK. This is the fail-OPEN the go classifier would
    have missed (it doesn't recognise maven's 'Cannot access ... offline' string)."""
    classify = boc.classify_maven_offline_build_failure(_DUBBO_FORBID)
    kind, detail = classify("r/x/base-offline:staging", _MVN_OFFLINE_THIRDPARTY)
    assert kind == "closure_gap"
    assert "mutiny" in detail


def test_classify_maven_mixed_self_and_third_party_is_gap():
    """If BOTH a self@B and a third-party artifact are unresolved, the third-party
    one wins → closure_gap (fail-closed: any real missing dep blocks)."""
    classify = boc.classify_maven_offline_build_failure(_DUBBO_FORBID)
    kind, _ = classify("r/x/base-offline:staging",
                       _MVN_OFFLINE_SELF + _MVN_OFFLINE_THIRDPARTY)
    assert kind == "closure_gap"


def test_classify_maven_spotless_failure_is_source_state():
    """A spotless/checkstyle/rat LINT failure (no dependency-resolution string) is
    SOURCE-STATE — it is not a missing dependency, so it must not BLOCK."""
    classify = boc.classify_maven_offline_build_failure(_DUBBO_FORBID)
    kind, _ = classify(
        "r/x/base-offline:staging",
        "[ERROR] The following files had format violations:\n"
        "[ERROR] Run 'mvn spotless:apply' to fix these violations.\n")
    assert kind == "source_state"


def test_classify_maven_compile_error_is_source_state():
    """A pure java compile error (no 'Could not resolve dependencies') is
    SOURCE-STATE (a mid-migration checkpoint), never a closure gap."""
    classify = boc.classify_maven_offline_build_failure(_DUBBO_FORBID)
    kind, _ = classify(
        "r/x/base-offline:staging",
        "[ERROR] /testbed/foo/Bar.java:[12,9] cannot find symbol\n"
        "[ERROR]   symbol:   method baz()\n")
    assert kind == "source_state"


def test_run_offline_gate_uses_injected_classifier(monkeypatch):
    """run_offline_gate must accept a custom `classifier` (maven path) and use it
    instead of the default go classifier. A maven 'Cannot access offline' for a
    third-party artifact → the injected classifier says closure_gap → sys.exit."""
    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["docker", "create"]:
            return _R(0, "cid\n")
        if cmd[:2] == ["docker", "run"]:
            return _R(1, _MVN_OFFLINE_THIRDPARTY)   # the offline build fails
        return _R(0, "")
    monkeypatch.setattr(boc.subprocess, "run", fake_run)
    classify = boc.classify_maven_offline_build_failure(_DUBBO_FORBID)
    with pytest.raises(SystemExit) as e:
        boc.run_offline_gate("r/x/base-offline:staging", "r/x/m01:latest",
                             "git clean -xfd && mvn -o test-compile",
                             classifier=classify)
    assert e.value.code == 1


def test_run_offline_gate_maven_self_at_b_is_source_state(monkeypatch):
    """With the maven classifier injected, a self@B-only unresolved (sibling reactor
    module) → SOURCE_STATE (does not block), returned as the sentinel."""
    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["docker", "create"]:
            return _R(0, "cid\n")
        if cmd[:2] == ["docker", "run"]:
            return _R(1, _MVN_OFFLINE_SELF)
        return _R(0, "")
    monkeypatch.setattr(boc.subprocess, "run", fake_run)
    classify = boc.classify_maven_offline_build_failure(_DUBBO_FORBID)
    got = boc.run_offline_gate("r/x/base-offline:staging", "r/x/m01:latest",
                               "git clean -xfd && mvn -o test-compile",
                               classifier=classify)
    assert got == "SOURCE_STATE"


# ---- Task 4.6: npm ecosystem assembly + yarn classifier (element-web e2e) ------
# element-web (yarn classic) is the simplest cache-COPY ecosystem: the union ADDs
# the milestone yarn caches on top of the base image's own yarn cache (spans A→B,
# like go), with NO toolchain step and NO self@B removal (the app source is not
# published to npm, so cache_forbid_globs is empty → the generic audit is a no-op).
# A yarn `--offline` failure is classified by classify_npm_offline_build_failure: a
# cache-miss / registry-request is a real closure gap (BLOCK); a frozen-lockfile
# integrity mismatch or a webpack/tsc/eslint error is source-state (does not block).

# Representative yarn `--offline` failure outputs.
_YARN_OFFLINE_NO_VERSIONS = (
    "yarn install v1.22.22\n"
    "[1/4] Resolving packages...\n"
    "error Couldn't find any versions for \"matrix-js-sdk\" that match "
    "\"^37.0.0\" in our cache (possible versions are: \"36.1.0\")\n"
    "info Visit https://yarnpkg.com/en/docs/cli/install for documentation.\n")

_YARN_OFFLINE_REGISTRY_REQUEST = (
    "yarn install v1.22.22\n"
    "[2/4] Fetching packages...\n"
    "error An unexpected error occurred: \"request to "
    "https://registry.yarnpkg.com/some-pkg/-/some-pkg-1.2.3.tgz failed, reason: "
    "getaddrinfo ENOTFOUND registry.yarnpkg.com\".\n")

_YARN_OFFLINE_FROZEN_LOCKFILE = (
    "yarn install v1.22.22\n"
    "[1/4] Resolving packages...\n"
    "error Your lockfile needs to be updated, but yarn was run with "
    "`--frozen-lockfile`.\n"
    "info Visit https://yarnpkg.com/en/docs/cli/install for documentation.\n")

_YARN_BUILD_COMPILE_ERROR = (
    "yarn run v1.22.22\n"
    "$ tsc -p tsconfig.json\n"
    "src/components/Foo.tsx:42:13 - error TS2339: Property 'bar' does not exist on "
    "type 'Props'.\n"
    "error Command failed with exit code 2.\n")


def test_classify_npm_no_versions_is_closure_gap():
    """`error Couldn't find any versions for` (yarn cache lacks the version a
    lockfile pins) is a REAL closure gap → BLOCK. Pure-text (no docker probe)."""
    kind, detail = boc.classify_npm_offline_build_failure(
        "r/x/base-offline:staging", _YARN_OFFLINE_NO_VERSIONS)
    assert kind == "closure_gap"
    assert "find any versions" in detail.lower() or "gap" in detail.lower()


_YARN_OFFLINE_CANT_REQUEST = (
    "yarn install v1.22.22\n"
    "[1/5] Validating package.json...\n"
    "[2/5] Resolving packages...\n"
    "[3/5] Fetching packages...\n"
    "info Visit https://yarnpkg.com/en/docs/cli/install for documentation about "
    "this command.\n"
    "error Can't make a request in offline mode "
    "(\"https://registry.yarnpkg.com/caniuse-lite/-/caniuse-lite-1.0.30001701.tgz\")\n")


def test_classify_npm_cant_make_request_offline_is_closure_gap():
    """THE canonical yarn-classic offline cache-miss: `error Can't make a request in
    offline mode ("<registry url>")` — yarn needs a tarball it can't serve from the
    mirror and would have to fetch (e.g. caniuse-lite-1.0.30001701 missing from the
    union) → a real CLOSURE GAP → BLOCK. This is the fail-OPEN the first integration
    run exposed: the string doesn't contain `request to https://registry` so it was
    mis-labelled source_state; the classifier must now catch it."""
    kind, detail = boc.classify_npm_offline_build_failure(
        "r/x/base-offline:staging", _YARN_OFFLINE_CANT_REQUEST)
    assert kind == "closure_gap"
    assert "offline mode" in detail.lower() or "gap" in detail.lower()


def test_classify_npm_registry_request_is_closure_gap():
    """A `request to https://registry…` under --offline = yarn fell back to the
    network because the bytes weren't in the cache → closure gap → BLOCK. (This is
    the fail-OPEN the go classifier would miss — it doesn't know yarn's strings.)"""
    kind, detail = boc.classify_npm_offline_build_failure(
        "r/x/base-offline:staging", _YARN_OFFLINE_REGISTRY_REQUEST)
    assert kind == "closure_gap"


def test_classify_npm_couldnt_find_package_is_closure_gap():
    """`Couldn't find package` (the package itself is absent from the cache) is a
    closure gap too."""
    kind, _ = boc.classify_npm_offline_build_failure(
        "r/x/base-offline:staging",
        "error Couldn't find package \"@matrix-org/olm@^3.2.15\" required by "
        "\"matrix-react-sdk\" on the \"npm\" registry.\n")
    assert kind == "closure_gap"


def test_classify_npm_frozen_lockfile_is_source_state():
    """A `--frozen-lockfile` integrity mismatch (yarn.lock disagrees with
    package.json/node_modules — a mid-change source state; the cache HAS the bytes)
    is SOURCE-STATE, not a closure gap → must NOT block."""
    kind, _ = boc.classify_npm_offline_build_failure(
        "r/x/base-offline:staging", _YARN_OFFLINE_FROZEN_LOCKFILE)
    assert kind == "source_state"


def test_classify_npm_compile_error_is_source_state():
    """A webpack/tsc compile error (no yarn cache-miss/registry-request signature) is
    SOURCE-STATE — it is not a missing dependency, so it must not BLOCK."""
    kind, _ = boc.classify_npm_offline_build_failure(
        "r/x/base-offline:staging", _YARN_BUILD_COMPILE_ERROR)
    assert kind == "source_state"


def test_run_offline_gate_uses_npm_classifier_gap_exits(monkeypatch):
    """run_offline_gate with the npm classifier injected: a yarn `Couldn't find any
    versions` offline failure → closure_gap → sys.exit(1)."""
    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["docker", "create"]:
            return _R(0, "cid\n")
        if cmd[:2] == ["docker", "run"]:
            return _R(1, _YARN_OFFLINE_NO_VERSIONS)   # the offline yarn install fails
        return _R(0, "")
    monkeypatch.setattr(boc.subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as e:
        boc.run_offline_gate(
            "r/x/base-offline:staging", "r/x/m01:latest",
            "cd /testbed && yarn install --offline --frozen-lockfile",
            classifier=boc.classify_npm_offline_build_failure)
    assert e.value.code == 1


def test_run_offline_gate_npm_frozen_lockfile_is_source_state(monkeypatch):
    """With the npm classifier injected, a frozen-lockfile mismatch → SOURCE_STATE
    (does not block), returned as the sentinel."""
    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["docker", "create"]:
            return _R(0, "cid\n")
        if cmd[:2] == ["docker", "run"]:
            return _R(1, _YARN_OFFLINE_FROZEN_LOCKFILE)
        return _R(0, "")
    monkeypatch.setattr(boc.subprocess, "run", fake_run)
    got = boc.run_offline_gate(
        "r/x/base-offline:staging", "r/x/m01:latest",
        "cd /testbed && yarn install --offline --frozen-lockfile",
        classifier=boc.classify_npm_offline_build_failure)
    assert got == "SOURCE_STATE"


def test_assemble_npm_dockerfile_fetches_union_into_shared_cache():
    """npm assembly ONLINE-fetches the union of declared deps (like pip/cargo): a
    fetch_builder stage off base:latest COPYs ONLY each milestone's
    package.json+yarn.lock and runs one `yarn install --cache-folder <shared>` per
    milestone (prefer --frozen-lockfile, fall back to non-frozen), then the final
    stage COPYs the warmed cache back. NOT a raw rsync union."""
    ms = ["ew/m01:latest", "ew/m02:latest"]
    df = boc.assemble_npm_dockerfile("ew", ms)
    cache = "/usr/local/share/.cache/yarn/v6"         # the versioned dir yarn fills
    cache_parent = "/usr/local/share/.cache/yarn"     # what --cache-folder must be
    # two stages off base:latest
    assert "FROM ew/base:latest AS fetch_builder" in df
    assert "FROM ew/base:latest AS final" in df
    # ONLY the two manifests are COPY'd per milestone (no whole /testbed)
    assert "COPY --from=ew/m01:latest /testbed/package.json /testbed/yarn.lock /m0/" in df
    assert "COPY --from=ew/m02:latest /testbed/package.json /testbed/yarn.lock /m1/" in df
    assert "/testbed /m" not in df               # never the full /testbed tree
    # one cache-warming RUN per milestone, all writing the SAME shared cache folder
    assert sum(1 for ln in df.splitlines()
               if ln.startswith("RUN cd /m") and "yarn install" in ln) == 2
    # THE --cache-folder GOTCHA: --cache-folder is the PARENT of the versioned dir
    # (yarn re-appends its own /v6), NOT the versioned dir itself — else the deps
    # land in …/yarn/v6/v6/ where the offline gate never looks.
    assert f"--cache-folder {cache_parent} " in df
    assert f"--cache-folder {cache} " not in df       # NOT the doubled-v6 path
    assert "cd /m0 && ( yarn install" in df
    assert "cd /m1 && ( yarn install" in df
    # prefer --frozen-lockfile, fall back to a plain non-frozen install
    assert "--frozen-lockfile ||" in df
    assert "--ignore-scripts" in df and "--non-interactive" in df
    # the warmed cache is baked into the final stage (the VERSIONED dir yarn filled)
    assert f"COPY --from=fetch_builder {cache} {cache}" in df
    # NOT the old raw rsync union (this was the closure gap)
    assert "rsync" not in df and "/milestone_" not in df
    # ordering: fetch (warm) BEFORE final COPY of the cache
    assert df.index("yarn install") < df.index("COPY --from=fetch_builder")


def test_assemble_npm_dockerfile_no_milestones_exits():
    with pytest.raises(SystemExit):
        boc.assemble_npm_dockerfile("ew", [])


def test_build_closure_npm_branch_dispatches_fetch_audit_gate(monkeypatch, tmp_path):
    """End-to-end-ish (all docker mocked): the npm branch in build_closure
    ONLINE-fetches the union of declared deps (assemble_npm_dockerfile — NO
    toolchain, NO self@B rm), builds the staging image, runs the generic audit
    (empty forbid → no-op) + per-milestone offline gate with the npm classifier,
    then tags :latest. Asserts call ORDER, that the built Dockerfile is the
    fetch-assembly (per-milestone `yarn install --cache-folder`, not a raw rsync
    union), and that the gate got the npm classifier."""
    (tmp_path / "quarantine_configs").mkdir()
    (tmp_path / "quarantine_configs" / "ew.yaml").write_text(
        "ecosystem: [npm]\n"
        "closure:\n"
        "  cache_paths:\n"
        "    - /usr/local/share/.cache/yarn/v6\n"
        "  offline_build: 'cd /testbed && yarn install --offline --frozen-lockfile'\n")

    events = []
    monkeypatch.setattr(boc, "discover_milestone_images",
                        lambda repo: ["ew/m01:latest", "ew/m02:latest"])
    built = {}
    def fake_build(df, tag, root):
        events.append(("build", tag))
        built["df"] = df
        built["tag"] = tag
    monkeypatch.setattr(boc, "_docker_build", fake_build)
    monkeypatch.setattr(boc, "audit_staging_image",
                        lambda tag, globs: events.append(("audit", tag, tuple(globs))))
    gate_classifiers = []
    def fake_gate(tag, m, ob, **k):
        gate_classifiers.append(k.get("classifier"))
        events.append(("gate", m))
        return "PASS"
    monkeypatch.setattr(boc, "run_offline_gate", fake_gate)
    def fake_sub_run(cmd, *a, **k):
        if cmd[:2] == ["docker", "tag"]:
            events.append(("tag", cmd[2], cmd[3]))
        return _R(0, "")
    monkeypatch.setattr(boc.subprocess, "run", fake_sub_run)

    boc.build_closure("ew", tmp_path, push=False, keep=True)

    kinds = [e[0] for e in events]
    # build BEFORE audit BEFORE (per-milestone) gate BEFORE tag
    assert kinds.index("build") < kinds.index("audit") < kinds.index("gate") \
        < kinds.index("tag")
    assert built["tag"] == "ew/base-offline:staging"
    assert ("tag", "ew/base-offline:staging", "ew/base-offline:latest") in events
    # the staging Dockerfile is the ONLINE declared-deps FETCH (per-milestone
    # `yarn install --cache-folder <shared>`), NOT a raw rsync cache union, and has
    # no self@B rm and no toolchain step.
    assert "yarn install" in built["df"] and "--cache-folder" in built["df"]
    assert "/usr/local/share/.cache/yarn/v6" in built["df"]
    assert "rsync" not in built["df"] and "/milestone_" not in built["df"]
    assert "rm -rf" not in built["df"]            # no self@B removal for element-web
    assert "GOTOOLCHAIN" not in built["df"] and "rustup" not in built["df"]
    # it equals assemble_npm_dockerfile exactly (the fetch assembly)
    expected = boc.assemble_npm_dockerfile("ew", ["ew/m01:latest", "ew/m02:latest"])
    assert built["df"] == expected
    # the generic audit got the (empty) forbid globs → clean no-op
    audit_ev = next(e for e in events if e[0] == "audit")
    assert audit_ev[2] == ()
    # one offline gate per milestone, each handed the npm/yarn classifier
    assert [e[1] for e in events if e[0] == "gate"] == ["ew/m01:latest", "ew/m02:latest"]
    assert gate_classifiers == [boc.classify_npm_offline_build_failure,
                                boc.classify_npm_offline_build_failure]


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


# ---- DUAL go+npm ecosystem assembly (navidrome e2e) ---------------------------
# navidrome combines a Go backend (raw-cache modcache union + clean-replaced go
# toolchain, like go-zero) with an npm-managed React UI (warm /root/.npm/_cacache
# from each milestone's ui/package-lock.json — npm, NOT yarn). The dual branch
# composes BOTH into ONE staging image and gates with `npm ci --offline && npm run
# build && GOPROXY=off go build ./...`, classified by BOTH the npm and go
# classifiers. No self@B in either cache → no removal, audit is a clean no-op.

def _go_probe_124(target="r/x/m02:latest"):
    """Fake go probe: only `target` reports go1.24.5 (the B-side toolchain bump);
    the rest report go1.24.4 — mirrors navidrome's milestone split."""
    return lambda img: "go1.24.5" if img == target else "go1.24.4"


def test_npm_online_fetch_cmd_npm_not_yarn_into_shared_cacache():
    """The per-milestone UI fetch uses `npm ci` (NOT yarn), prefers a real online
    fetch (--prefer-offline=false), writes into the SHARED _cacache via npm's
    `--cache` = the PARENT of _cacache (npm appends /_cacache itself), ignores
    scripts, and falls back to `npm install` if `npm ci` aborts (mid-change lock)."""
    body = boc.npm_online_fetch_cmd("/ui_m3", "/root/.npm/_cacache")
    assert body.startswith("cd /ui_m3 && (")
    # npm ci preferred, npm install fallback (|| inside the RUN)
    assert "npm ci --cache /root/.npm " in body
    assert "|| npm install --cache /root/.npm " in body
    # NOT yarn
    assert "yarn" not in body
    # --cache is the PARENT of _cacache (npm re-appends /_cacache); NOT _cacache itself
    assert "--cache /root/.npm " in body
    assert "--cache /root/.npm/_cacache" not in body
    # online (force a real fetch), no postinstall, quiet
    assert "--prefer-offline=false" in body
    assert body.count("--ignore-scripts") == 2
    assert "--no-audit" in body and "--no-fund" in body


def test_assemble_go_npm_dockerfile_emits_both_closures():
    """The dual assembly emits BOTH the go union+toolchain AND the npm cacache warm
    in ONE multi-stage Dockerfile, with a single syntax directive and a final stage
    that COPYs the unioned go cache + the go toolchain + the warmed _cacache."""
    ms = ["r/x/m01:latest", "r/x/m02:latest"]
    caches = ["/go/pkg/mod/cache/download", "/root/.npm/_cacache"]
    df = boc.assemble_go_npm_dockerfile(
        "r/x", ms, caches, "1.24.5", _probe=_go_probe_124())
    # exactly ONE syntax directive, at the very top
    assert df.count("# syntax=docker/dockerfile:1") == 1
    assert df.startswith("# syntax=docker/dockerfile:1\n")
    # --- npm part: a npm_fetch stage that COPYs ONLY the UI manifests + warms cacache
    assert "FROM r/x/base:latest AS npm_fetch" in df
    assert ("COPY --from=r/x/m01:latest /testbed/ui/package.json "
            "/testbed/ui/package-lock.json /ui_m0/") in df
    assert ("COPY --from=r/x/m02:latest /testbed/ui/package.json "
            "/testbed/ui/package-lock.json /ui_m1/") in df
    assert "npm ci --cache /root/.npm " in df          # npm, not yarn
    assert "yarn" not in df
    # the WHOLE /testbed is never COPY'd into the npm stage (manifests only)
    assert "/testbed /ui_m" not in df
    # --- go part: raw-cache rsync union of the go modcache (NOT the npm cacache)
    assert "FROM r/x/base:latest AS builder" in df
    assert "FROM r/x/base:latest AS final" in df
    assert "rsync -a /milestone_" in df
    assert "/go/pkg/mod/cache/download" in df
    # the npm _cacache must NOT be rsync-union'd as a go cache path
    assert "rsync -a /milestone_0_0/root/.npm/_cacache" not in df
    assert "/staging/root/.npm/_cacache" not in df
    # --- go toolchain: clean-replace from the 1.24.5 milestone + GOTOOLCHAIN=local
    assert "RUN rm -rf /usr/local/go" in df
    assert "COPY --from=r/x/m02:latest /usr/local/go /usr/local/go" in df
    assert "ENV GOTOOLCHAIN=local" in df
    # --- final stage COPYs the warmed _cacache from the npm_fetch stage
    assert "COPY --from=npm_fetch /root/.npm/_cacache /root/.npm/_cacache" in df
    # ordering: npm_fetch DEFINED before final references it; rm before toolchain
    # COPY before ENV before the cacache COPY
    assert df.index("AS npm_fetch") < df.index("COPY --from=npm_fetch")
    assert df.index("AS final") < df.index("RUN rm -rf /usr/local/go")
    assert df.index("RUN rm -rf /usr/local/go") \
        < df.index("COPY --from=r/x/m02:latest /usr/local/go")
    assert df.index("COPY --from=r/x/m02:latest /usr/local/go") \
        < df.index("ENV GOTOOLCHAIN=local")
    assert df.index("ENV GOTOOLCHAIN=local") \
        < df.index("COPY --from=npm_fetch /root/.npm/_cacache")
    # NO self@B removal for navidrome (no rm -rf of any cache)
    assert "rm -rf /root/.npm" not in df
    assert "rm -rf /go/pkg/mod" not in df


def test_assemble_go_npm_dockerfile_no_milestones_exits():
    with pytest.raises(SystemExit):
        boc.assemble_go_npm_dockerfile(
            "r/x", [], ["/go/pkg/mod/cache/download", "/root/.npm/_cacache"], "1.24.5")


def test_assemble_go_npm_dockerfile_toolchain_fallback_scans():
    """If the LAST milestone lacks the target go, an earlier one that reports it is
    picked for the toolchain COPY (pick_go_toolchain_milestone scan)."""
    ms = ["r/x/m01:latest", "r/x/m02:latest", "r/x/m03:latest"]
    # only m02 has go1.24.5; m03 (last) regressed to go1.24.4
    df = boc.assemble_go_npm_dockerfile(
        "r/x", ms, ["/go/pkg/mod/cache/download", "/root/.npm/_cacache"], "1.24.5",
        _probe=_go_probe_124("r/x/m02:latest"))
    assert "COPY --from=r/x/m02:latest /usr/local/go /usr/local/go" in df


# ---- DUAL go+npm gate classifier ----------------------------------------------

_NPM_CI_OFFLINE_GAP = (
    "npm error code ENOTCACHED\n"
    "npm error request to https://registry.npmjs.org/caniuse-lite/-/"
    "caniuse-lite-1.0.30001701.tgz failed: cache mode is \"only-if-cached\" but no "
    "cached response is available.\n")

_GO_BUILD_GAP = ("core/x.go:1:2: no required module provides package "
                 "example.com/totally/absent; to add it:\n")

_GO_BUILD_SOURCESTATE_COMPILE = "core/foo.go:12:9: undefined: tests.MockBar\n"

_UI_TSC_ERROR = (
    "> ui@ build\n> vite build\n"
    "src/components/Foo.tsx:42:13 - error TS2339: Property 'bar' does not exist.\n"
    "npm error Lifecycle script `build` failed with error code 2\n")


def test_classify_go_npm_npm_cache_miss_is_gap():
    """An `npm ci --offline` cache-miss (ENOTCACHED / only-if-cached) → closure_gap,
    tagged [npm]. The go classifier alone would miss npm's strings (fail-OPEN)."""
    kind, detail = boc.classify_go_npm_offline_build_failure(
        "r/x/base-offline:staging", _NPM_CI_OFFLINE_GAP)
    assert kind == "closure_gap"
    assert "[npm]" in detail


def test_classify_go_npm_go_module_gap_is_gap(monkeypatch):
    """A go build that cites a module ABSENT from the modcache → closure_gap, tagged
    [go]. The npm classifier finds no gap signature, so the go classifier (which
    probes the cache) is consulted and BLOCKs."""
    # probe: no HIT → the module is absent from the cache
    monkeypatch.setattr(boc.subprocess, "run", lambda *a, **k: _R(0, ""))
    kind, detail = boc.classify_go_npm_offline_build_failure(
        "r/x/base-offline:staging", _GO_BUILD_GAP)
    assert kind == "closure_gap"
    assert "[go]" in detail
    assert "example.com/totally/absent" in detail


def test_classify_go_npm_go_compile_error_is_source_state(monkeypatch):
    """A pure go compile error (no missing-module token, no npm signature) →
    source_state (a mid-migration milestone), tagged [go] — must NOT block."""
    called = {"probe": False}
    def fake_run(*a, **k):
        called["probe"] = True
        return _R(0, "")
    monkeypatch.setattr(boc.subprocess, "run", fake_run)
    kind, detail = boc.classify_go_npm_offline_build_failure(
        "r/x/base-offline:staging", _GO_BUILD_SOURCESTATE_COMPILE)
    assert kind == "source_state"
    assert called["probe"] is False     # no token → no cache probe


def test_classify_go_npm_ui_build_error_is_source_state():
    """A UI tsc/vite build error (no npm cache-miss, no go module token) →
    source_state — a compile error is not a missing dependency."""
    kind, _ = boc.classify_go_npm_offline_build_failure(
        "r/x/base-offline:staging", _UI_TSC_ERROR)
    assert kind == "source_state"


def test_classify_go_npm_npm_gap_wins_over_go_token(monkeypatch):
    """If BOTH an npm cache-miss AND a go module token appear, the npm gap is
    returned (fail-closed: any real missing dep blocks; npm is checked first)."""
    monkeypatch.setattr(boc.subprocess, "run", lambda *a, **k: _R(0, "HIT\n"))
    kind, detail = boc.classify_go_npm_offline_build_failure(
        "r/x/base-offline:staging", _NPM_CI_OFFLINE_GAP + _GO_BUILD_GAP)
    assert kind == "closure_gap"
    assert "[npm]" in detail


# ---- _ecosystems_of (multi-aware reader) + dual gate constant -----------------

def test_ecosystems_of_returns_list(tmp_path):
    """`_ecosystems_of` returns the normalized lowercase LIST (order preserved) for
    both a scalar and a list `ecosystem`."""
    (tmp_path / "quarantine_configs").mkdir()
    (tmp_path / "quarantine_configs" / "nav.yaml").write_text(
        "ecosystem: [Go, NPM]\nclosure:\n  cache_paths: ['/a']\n  offline_build: 'x'\n")
    assert boc._ecosystems_of("nav", tmp_path) == ["go", "npm"]
    (tmp_path / "quarantine_configs" / "sk.yaml").write_text(
        "ecosystem: pip\nclosure:\n  cache_paths: []\n  offline_build: 'x'\n")
    assert boc._ecosystems_of("sk", tmp_path) == ["pip"]


def test_ecosystems_of_missing_exits(tmp_path):
    (tmp_path / "quarantine_configs").mkdir()
    (tmp_path / "quarantine_configs" / "no.yaml").write_text(
        "closure:\n  cache_paths: []\n  offline_build: 'x'\n")
    with pytest.raises(SystemExit):
        boc._ecosystems_of("no", tmp_path)


def test_go_npm_offline_gate_constant_shape():
    """The dual gate: npm ci --offline (rebuild node_modules from _cacache) → npm
    run build → GOPROXY=off go build ./... (build-scoped, like go-zero)."""
    g = boc._GO_NPM_OFFLINE_GATE
    assert "cd /testbed/ui && npm ci --offline && npm run build" in g
    assert "cd /testbed && GOPROXY=off go build ./..." in g
    # build-scoped go build, NOT a whole-graph `go mod download`
    assert "go mod download" not in g


# ---- build_closure DUAL dispatch (no longer sys.exit on [go, npm]) ------------

def _write_navidrome_cfg(tmp_path):
    (tmp_path / "quarantine_configs").mkdir(exist_ok=True)
    (tmp_path / "quarantine_configs" / "nav.yaml").write_text(
        "ecosystem: [go, npm]\n"
        "cache_forbid_globs:\n"
        "  - /go/pkg/mod/cache/download/github.com/navidrome/*\n"
        "  - /go/pkg/mod/github.com/navidrome/*\n"
        "closure:\n"
        "  cache_paths:\n"
        "    - /go/pkg/mod/cache/download\n"
        "    - /root/.npm/_cacache\n"
        "  offline_build: 'cd /testbed/ui && npm ci --offline && npm run build && "
        "cd /testbed && GOPROXY=off go build ./...'\n"
        "  toolchain: {go: \"1.24.5\", gotoolchain_local: true}\n")


def test_build_closure_dual_go_npm_dispatches_not_sysexit(monkeypatch, tmp_path):
    """The dual branch is dispatched for `ecosystem: [go, npm]` (NO sys.exit). It
    builds the dual staging image (go union+toolchain + npm cacache warm), runs the
    generic audit + per-milestone DUAL gate (goproxy_off + the go+npm classifier),
    then tags :latest. Asserts call ORDER + the built Dockerfile carries BOTH
    closures + the gate used the dual command and classifier."""
    _write_navidrome_cfg(tmp_path)

    events = []
    monkeypatch.setattr(boc, "discover_milestone_images",
                        lambda repo: ["nav/m01:latest", "nav/m02:latest"])
    # assemble_go_npm_dockerfile picks the toolchain milestone via
    # pick_go_toolchain_milestone(... _probe=<default-bound _probe_go_version>); the
    # default arg is bound at def-time so reassigning boc._probe_go_version won't
    # take. Stub the picker itself (its probe/scan logic is unit-tested separately)
    # so the dispatch test exercises only the dual wiring.
    monkeypatch.setattr(boc, "pick_go_toolchain_milestone",
                        lambda ms, tg, _probe=None: "nav/m02:latest")
    built = {}
    def fake_build(df, tag, root):
        events.append(("build", tag))
        built["df"] = df
        built["tag"] = tag
    monkeypatch.setattr(boc, "_docker_build", fake_build)
    monkeypatch.setattr(boc, "audit_staging_image",
                        lambda tag, globs: events.append(("audit", tag, tuple(globs))))
    gate_calls = []
    def fake_gate(tag, m, ob, **k):
        gate_calls.append((m, ob, k.get("goproxy_off"), k.get("classifier")))
        events.append(("gate", m))
        return "PASS"
    monkeypatch.setattr(boc, "run_offline_gate", fake_gate)
    def fake_sub_run(cmd, *a, **k):
        if cmd[:2] == ["docker", "tag"]:
            events.append(("tag", cmd[2], cmd[3]))
        return _R(0, "")
    monkeypatch.setattr(boc.subprocess, "run", fake_sub_run)

    # must NOT raise SystemExit (the whole point — dual is supported now)
    boc.build_closure("nav", tmp_path, push=False, keep=True)

    kinds = [e[0] for e in events]
    assert kinds.index("build") < kinds.index("audit") < kinds.index("gate") \
        < kinds.index("tag")
    assert built["tag"] == "nav/base-offline:staging"
    assert ("tag", "nav/base-offline:staging", "nav/base-offline:latest") in events
    # the staging Dockerfile carries BOTH the go union+toolchain AND the npm warm
    df = built["df"]
    assert "FROM nav/base:latest AS npm_fetch" in df
    assert "npm ci --cache /root/.npm " in df
    assert "rsync -a /milestone_" in df and "/go/pkg/mod/cache/download" in df
    assert "RUN rm -rf /usr/local/go" in df and "ENV GOTOOLCHAIN=local" in df
    assert "COPY --from=nav/m02:latest /usr/local/go /usr/local/go" in df  # 1.24.5
    assert "COPY --from=npm_fetch /root/.npm/_cacache /root/.npm/_cacache" in df
    # it equals assemble_go_npm_dockerfile exactly (the dual assembly); the picker
    # is stubbed above, so both this and the dispatch use the same toolchain pick.
    expected = boc.assemble_go_npm_dockerfile(
        "nav", ["nav/m01:latest", "nav/m02:latest"],
        ["/go/pkg/mod/cache/download", "/root/.npm/_cacache"], "1.24.5")
    assert df == expected
    # one DUAL gate per milestone: the dual command, goproxy_off=True, dual classifier
    assert [e[1] for e in events if e[0] == "gate"] == ["nav/m01:latest", "nav/m02:latest"]
    for (m, ob, gpo, clf) in gate_calls:
        assert ob == boc._GO_NPM_OFFLINE_GATE
        assert gpo is True
        assert clf is boc.classify_go_npm_offline_build_failure


def test_build_closure_dual_go_npm_gap_blocks_no_tag(monkeypatch, tmp_path):
    """A real closure gap in the dual gate (run_offline_gate sys.exit) fail-closes:
    :latest is NEVER tagged."""
    _write_navidrome_cfg(tmp_path)
    monkeypatch.setattr(boc, "discover_milestone_images",
                        lambda repo: ["nav/m01:latest", "nav/m02:latest"])
    monkeypatch.setattr(boc, "pick_go_toolchain_milestone",
                        lambda ms, tg, _probe=None: "nav/m02:latest")
    monkeypatch.setattr(boc, "_docker_build", lambda df, tag, root: None)
    monkeypatch.setattr(boc, "audit_staging_image", lambda tag, globs: None)
    # the gate fail-closes (a real gap) by sys.exit, as run_offline_gate would
    def fake_gate(tag, m, ob, **k):
        raise SystemExit(1)
    monkeypatch.setattr(boc, "run_offline_gate", fake_gate)
    tagged = []
    def fake_sub_run(cmd, *a, **k):
        if cmd[:2] == ["docker", "tag"]:
            tagged.append(cmd)
        return _R(0, "")
    monkeypatch.setattr(boc.subprocess, "run", fake_sub_run)
    with pytest.raises(SystemExit):
        boc.build_closure("nav", tmp_path, push=False, keep=True)
    assert tagged == []     # :latest never tagged on a closure gap


def test_build_closure_dual_go_npm_source_state_still_tags(monkeypatch, tmp_path):
    """A SOURCE-STATE-only failure (the dual gate returns SOURCE_STATE, not a gap)
    does NOT block: :latest is still tagged (closure has the bytes)."""
    _write_navidrome_cfg(tmp_path)
    monkeypatch.setattr(boc, "discover_milestone_images",
                        lambda repo: ["nav/m01:latest", "nav/m02:latest"])
    monkeypatch.setattr(boc, "pick_go_toolchain_milestone",
                        lambda ms, tg, _probe=None: "nav/m02:latest")
    monkeypatch.setattr(boc, "_docker_build", lambda df, tag, root: None)
    monkeypatch.setattr(boc, "audit_staging_image", lambda tag, globs: None)
    # m01 source-state, m02 pass
    monkeypatch.setattr(
        boc, "run_offline_gate",
        lambda tag, m, ob, **k: "SOURCE_STATE" if m == "nav/m01:latest" else "PASS")
    tagged = []
    def fake_sub_run(cmd, *a, **k):
        if cmd[:2] == ["docker", "tag"]:
            tagged.append((cmd[2], cmd[3]))
        return _R(0, "")
    monkeypatch.setattr(boc.subprocess, "run", fake_sub_run)
    boc.build_closure("nav", tmp_path, push=False, keep=True)
    assert ("nav/base-offline:staging", "nav/base-offline:latest") in tagged
