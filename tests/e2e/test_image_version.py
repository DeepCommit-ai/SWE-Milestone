"""Tests for the single naming authority (image_version.py)."""
import pytest

from harness.e2e.image_version import (
    DEFAULT_IMAGE_TAG,
    PREFIX,
    SEP,
    hub_ref,
    hub_to_local,
    local_ref,
    local_to_hub,
    parse_local_ref,
    validate_component,
)

# 全部 7 个真实 repo_full + 刁钻 milestone 取样
REAL_CASES = [
    ("navidrome_navidrome_v0.57.0_v0.58.0", "milestone_006"),
    ("navidrome_navidrome_v0.57.0_v0.58.0", "milestone_003_sub-01"),
    ("apache_dubbo_dubbo-3.3.3_dubbo-3.3.6", "m001.1"),
    ("burntsushi_ripgrep_14.1.1_15.0.0", "milestone_seed_119407d_1_sub-02"),
    ("zeromicro_go-zero_v1.6.0_v1.9.3", "m007.1"),
    ("nushell_nushell_0.106.0_0.108.0", "milestone_core_development.4"),
    ("element-hq_element-web_v1.11.95_v1.11.97", "milestone_seed_e9a3625_1_sub-03"),
    ("scikit-learn_scikit-learn_1.5.2_1.6.0", "m12.5"),
    ("navidrome_navidrome_v0.57.0_v0.58.0", "base"),
    ("zeromicro_go-zero_v1.6.0_v1.9.3", "base-offline"),
]


class TestConstruct:
    def test_default_tag_is_v1_0(self):
        assert DEFAULT_IMAGE_TAG == "v1.0"

    def test_local_ref_navidrome(self):
        assert (
            local_ref("navidrome_navidrome_v0.57.0_v0.58.0", "milestone_006", "v1.0")
            == "swe-milestone/navidrome_navidrome_v0.57.0_v0.58.0__milestone_006:v1.0"
        )

    def test_local_ref_without_tag(self):
        assert (
            local_ref("navidrome_navidrome_v0.57.0_v0.58.0", "base")
            == "swe-milestone/navidrome_navidrome_v0.57.0_v0.58.0__base"
        )

    def test_local_ref_lowercases_input(self):
        # evaluator 侧历史行为:输入可能带原始大小写(BurntSushi)
        assert (
            local_ref("BurntSushi_ripgrep_14.1.1_15.0.0", "base", "v1.0")
            == "swe-milestone/burntsushi_ripgrep_14.1.1_15.0.0__base:v1.0"
        )

    def test_hub_ref_go_zero(self):
        assert (
            hub_ref("hyd2apse", "zeromicro_go-zero_v1.6.0_v1.9.3", "m007.1", "v1.0")
            == "hyd2apse/swe-milestone__zeromicro_go-zero_v1.6.0_v1.9.3__m007.1:v1.0"
        )


class TestRoundTrip:
    @pytest.mark.parametrize("rf,ms", REAL_CASES)
    def test_local_hub_local(self, rf, ms):
        local = local_ref(rf, ms, "v1.0")
        hub = local_to_hub(local, "hyd2apse")
        assert hub == hub_ref("hyd2apse", rf, ms, "v1.0")
        assert hub_to_local(hub) == local

    @pytest.mark.parametrize("rf,ms", REAL_CASES)
    def test_parse_new_format(self, rf, ms):
        assert parse_local_ref(local_ref(rf, ms, "v1.0")) == (rf, ms)

    def test_parse_new_format_untagged(self):
        assert parse_local_ref("swe-milestone/a_b__c") == ("a_b", "c")


class TestLegacyParse:
    """resume 旧 trial 的唯一兼容分支:首段 ≠ PREFIX → 首段即 repo_full。"""

    def test_legacy_base_latest(self):
        rf, rest = parse_local_ref(
            "navidrome_navidrome_v0.57.0_v0.58.0/base:latest"
        )
        assert rf == "navidrome_navidrome_v0.57.0_v0.58.0"
        assert rest == "base"

    def test_legacy_base_offline(self):
        rf, rest = parse_local_ref(
            "apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/base-offline:latest"
        )
        assert rf == "apache_dubbo_dubbo-3.3.3_dubbo-3.3.6"
        assert rest == "base-offline"

    def test_legacy_3part_agentbench(self):
        rf, rest = parse_local_ref(
            "apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/baseline_rerun_stage4_002_fix2_v2/base:latest"
        )
        assert rf == "apache_dubbo_dubbo-3.3.3_dubbo-3.3.6"
        assert rest == "baseline_rerun_stage4_002_fix2_v2/base"


class TestValidation:
    @pytest.mark.parametrize(
        "bad", ["has__double", "has/slash", "has:colon", ""]
    )
    def test_validate_rejects(self, bad):
        with pytest.raises(ValueError):
            validate_component(bad)

    def test_validate_lowercases(self):
        assert validate_component("BurntSushi_ripgrep") == "burntsushi_ripgrep"

    def test_hub_to_local_rejects_wrong_prefix(self):
        with pytest.raises(ValueError):
            hub_to_local("hyd2apse/other-prefix__a__b:v1.0")

    def test_hub_to_local_rejects_wrong_segments(self):
        with pytest.raises(ValueError):
            hub_to_local("hyd2apse/swe-milestone__only-one-segment:v1.0")
        with pytest.raises(ValueError):
            hub_to_local("hyd2apse/swe-milestone__a__b__c:v1.0")

    def test_parse_rejects_no_slash(self):
        with pytest.raises(ValueError):
            parse_local_ref("no-slash-at-all:v1.0")


import subprocess as sp
import sys
from pathlib import Path

from harness.e2e.image_version import default_manifest_path, load_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]

_D = "sha256:" + "ab" * 32
SAMPLE_TSV = f"""\
# comment line
swe-milestone/navidrome_navidrome_v0.57.0_v0.58.0__base:v1.0\thyd2apse/swe-milestone__navidrome_navidrome_v0.57.0_v0.58.0__base@{_D}
swe-milestone/navidrome_navidrome_v0.57.0_v0.58.0__milestone_006:v1.0\thyd2apse/swe-milestone__navidrome_navidrome_v0.57.0_v0.58.0__milestone_006@{_D}
swe-milestone/zeromicro_go-zero_v1.6.0_v1.9.3__m007.1:v1.0\thyd2apse/swe-milestone__zeromicro_go-zero_v1.6.0_v1.9.3__m007.1@{_D}
"""


class TestManifest:
    def test_load_manifest(self, tmp_path):
        p = tmp_path / "m.tsv"
        p.write_text(SAMPLE_TSV)
        rows = load_manifest(p)
        assert [(r[0], r[1], r[2]) for r in rows] == [
            ("navidrome", "navidrome_navidrome_v0.57.0_v0.58.0", "base"),
            ("navidrome", "navidrome_navidrome_v0.57.0_v0.58.0", "milestone_006"),
            ("go-zero", "zeromicro_go-zero_v1.6.0_v1.9.3", "m007.1"),
        ]
        assert all(r[3].endswith(_D) for r in rows)

    def test_load_manifest_rejects_bad_row(self, tmp_path):
        p = tmp_path / "m.tsv"
        p.write_text("swe-milestone/x__y:v1.0\tno-digest-here\n")
        with pytest.raises(ValueError):
            load_manifest(p)

    def test_default_manifest_path(self):
        assert default_manifest_path("v1.0") == (
            REPO_ROOT / "manifests" / "digests-v1.0.tsv"
        )

    def test_shipped_v1_manifest_loads_and_validates(self):
        rows = load_manifest(default_manifest_path("v1.0"))
        assert len(rows) == 115
        shorts = {r[0] for r in rows}
        assert shorts == {
            "navidrome", "dubbo", "ripgrep", "scikit-learn",
            "go-zero", "element-web", "nushell",
        }
        # 每行都能构造合法名字(即隐含 __/大小写不变量成立)
        for _, rf, ms, hub in rows:
            local_ref(rf, ms, "v1.0")
            assert "@sha256:" in hub


def _run_cli(*argv, env_extra=None):
    import os as _os
    env = dict(_os.environ)
    env.pop("SWE_MILESTONE_IMAGE_TAG", None)
    if env_extra:
        env.update(env_extra)
    return sp.run(
        [sys.executable, "-m", "harness.e2e.image_version", *argv],
        capture_output=True, text=True, cwd=REPO_ROOT, env=env,
    )


class TestPlanCLI:
    def test_pull_plan_golden_line(self):
        r = _run_cli("pull-plan", "--repo", "navidrome", "--version", "v1.0")
        assert r.returncode == 0, r.stderr
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 11  # navidrome: base + base-offline + 9 milestones
        hub, local = lines[0].split("\t")
        assert hub.startswith(
            "hyd2apse/swe-milestone__navidrome_navidrome_v0.57.0_v0.58.0__base@sha256:"
        )
        assert local == "swe-milestone/navidrome_navidrome_v0.57.0_v0.58.0__base:v1.0"

    def test_pull_plan_all_repos(self):
        r = _run_cli("pull-plan", "--version", "v1.0")
        assert len(r.stdout.strip().splitlines()) == 115

    def test_version_from_env(self):
        r = _run_cli("pull-plan", "--repo", "navidrome",
                     env_extra={"SWE_MILESTONE_IMAGE_TAG": "v1.0"})
        assert ":v1.0" in r.stdout.splitlines()[0]

    def test_org_flag(self):
        r = _run_cli("pull-plan", "--repo", "navidrome", "--version", "v1.0",
                     "--org", "otherorg")
        assert r.stdout.startswith("otherorg/")

    def test_unknown_repo_fails(self):
        r = _run_cli("pull-plan", "--repo", "nonexistent", "--version", "v1.0")
        assert r.returncode != 0

    def test_retag_plan(self):
        r = _run_cli("retag-plan", "--repo", "navidrome", "--version", "v1.0",
                     "--from-version", "v0.9", "--base-offline-from", "latest")
        lines = r.stdout.strip().splitlines()
        assert (
            "navidrome_navidrome_v0.57.0_v0.58.0/base:v0.9"
            "\tswe-milestone/navidrome_navidrome_v0.57.0_v0.58.0__base:v1.0"
        ) in lines
        assert (
            "navidrome_navidrome_v0.57.0_v0.58.0/base-offline:latest"
            "\tswe-milestone/navidrome_navidrome_v0.57.0_v0.58.0__base-offline:v1.0"
        ) in lines


class TestConsumers:
    def test_container_setup_repo_extraction_both_formats(self):
        from harness.e2e.container_setup import _quarantine_env_from_image  # noqa: F401
        # 直接验证解析函数对两种格式给出相同 repo_full(quarantine 查找的 key)
        new = parse_local_ref(
            "swe-milestone/zeromicro_go-zero_v1.6.0_v1.9.3__base-offline:v1.0"
        )[0]
        old = parse_local_ref(
            "zeromicro_go-zero_v1.6.0_v1.9.3/base-offline:latest"
        )[0]
        assert new == old == "zeromicro_go-zero_v1.6.0_v1.9.3"


class TestPullNeverHardening:
    """spec §3.3a: benchmark 镜像的容器启动必须显式 --pull=never。"""

    @pytest.mark.parametrize("rel", [
        "harness/e2e/evaluator.py",
        "harness/e2e/container_setup.py",
        "scripts/verify_quarantine.py",
    ])
    def test_launch_sites_have_pull_never(self, rel):
        src = (REPO_ROOT / rel).read_text()
        assert '"--pull=never"' in src, f"{rel}: benchmark container launch lacks --pull=never"


class TestEnvGuard:
    """legacy EVOCLAW_* 必须硬报错(防静默忽略,如被无视的 IMAGE_TAG 评错版本)。"""

    def test_clean_env_passes(self, monkeypatch):
        from harness.e2e.env_guard import reject_legacy_env
        for k in list(__import__("os").environ):
            if k.startswith("EVOCLAW_"):
                monkeypatch.delenv(k)
        reject_legacy_env()  # 不抛即过

    def test_legacy_var_rejected_with_mapping(self, monkeypatch):
        from harness.e2e.env_guard import reject_legacy_env
        monkeypatch.setenv("EVOCLAW_IMAGE_TAG", "v0.9")
        with pytest.raises(SystemExit) as ei:
            reject_legacy_env()
        assert "SWE_MILESTONE_IMAGE_TAG" in str(ei.value)

    def test_plan_cli_rejects_legacy(self):
        r = _run_cli("pull-plan", "--repo", "navidrome", "--version", "v1.0",
                     env_extra={"EVOCLAW_DATA_ROOT": "/tmp/x"})
        assert r.returncode != 0
        assert "SWE_MILESTONE_DATA_ROOT" in (r.stderr + r.stdout)
