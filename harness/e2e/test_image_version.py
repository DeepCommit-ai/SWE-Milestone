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
