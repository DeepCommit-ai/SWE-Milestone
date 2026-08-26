"""Tests for scripts/verify_env_deps.py (--static, design v3 §3.1).

Mock-based, no Docker, no network, tmp_path-only writes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.verify_env_deps import (  # noqa: E402
    ECOSYSTEM_MAP,
    Entry,
    _fingerprint,
    extract_srs,
    lint_dockerfile,
    main,
    parse_declaration,
    reconcile_overrides,
)


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------

class TestGrammar:
    def test_at_form(self):
        e = parse_declaration("html-react-parser@5.2.2 added", "npm")
        assert e is not None
        assert (e.name, e.version, e.change) == (
            "html-react-parser", "5.2.2", "added")

    def test_at_form_scoped_last_at_split(self):
        e = parse_declaration(
            "@element-hq/element-web-playwright-common@1.1.5 added", "npm")
        assert e is not None
        assert e.name == "@element-hq/element-web-playwright-common"
        assert e.version == "1.1.5"

    def test_space_form(self):
        e = parse_declaration("array-api-compat 1.13.0 added", "pip")
        assert e is not None
        assert (e.name, e.version) == ("array-api-compat", "1.13.0")

    def test_space_form_go_v_prefix(self):
        e = parse_declaration(
            "github.com/jackc/puddle/v2 v2.2.1 added", "go")
        assert e is not None
        assert (e.name, e.version) == ("github.com/jackc/puddle/v2", "v2.2.1")

    def test_space_form_maven_gav(self):
        e = parse_declaration("io.smallrye.reactive:mutiny 2.9.0 added",
                              "maven")
        assert e is not None
        assert e.name == "io.smallrye.reactive:mutiny"

    def test_space_form_non_version_word_rejected(self):
        assert parse_declaration("something went added", "npm") is None

    def test_upgraded_to(self):
        e = parse_declaration("react upgraded to 18.3.1", "npm")
        assert e is not None
        assert (e.change, e.version) == ("upgraded", "18.3.1")

    def test_downgraded_to(self):
        e = parse_declaration("foo downgraded to 1.0.0", "npm")
        assert e is not None
        assert e.change == "downgraded"

    def test_from_to(self):
        e = parse_declaration("bar upgraded from 1.0.0 to 2.0.0", "npm")
        assert e is not None
        assert (e.change, e.version) == ("upgraded", "2.0.0")

    def test_caret_constraint_is_presence_only(self):
        e = parse_declaration("babel-loader upgraded to ^10.0.0", "npm")
        assert e is not None
        assert e.version is None
        assert e.constraint == "^10.0.0"

    def test_at_latest_is_presence_only(self):
        e = parse_declaration("golang.org/x/tools upgraded to @latest", "go")
        assert e is not None
        assert e.version is None
        assert e.constraint == "@latest"

    def test_versionless_add(self):
        e = parse_declaration("wire added", "go")
        assert e is not None
        assert e.version is None and e.constraint is None

    def test_env_var_set_to(self):
        e = parse_declaration("MAVEN_OPTS set to -Xmx4g", "env-var")
        assert e is not None
        assert (e.name, e.change) == ("MAVEN_OPTS", "set")

    def test_env_var_assign(self):
        e = parse_declaration("GOFLAGS=-mod=mod set", "env-var")
        assert e is not None
        assert e.name == "GOFLAGS"

    def test_prose_rejected(self):
        assert parse_declaration(
            "The build requires network access for the first run", "maven"
        ) is None

    def test_arrow_form_rejected(self):
        assert parse_declaration("ureq: 2.12 → =3.0.12", "cargo") is None

    def test_bare_toolchain_line_rejected(self):
        assert parse_declaration("rustc 1.87.0", "toolchain") is None

    def test_wildcard_removal_rejected(self):
        assert parse_declaration(
            "cloud.google.com/* (100+ transitive modules) removed", "go"
        ) is None

    def test_trailing_paren_annotation_stripped(self):
        e = parse_declaration(
            "xvfb added (virtual framebuffer for headless browser testing)",
            "system")
        assert e is not None
        assert (e.name, e.version, e.change) == ("xvfb", None, "added")
        # identity stays on the ORIGINAL text
        assert e.fingerprint == _fingerprint(
            "xvfb added (virtual framebuffer for headless browser testing)")

    def test_trailing_paren_with_version(self):
        e = parse_declaration("Go upgraded to 1.21.13 (from 1.19.13)",
                              "toolchain")
        assert e is not None
        assert (e.name, e.version) == ("Go", "1.21.13")

    def test_paren_then_trailing_text_still_rejected(self):
        assert parse_declaration(
            "Rust upgraded to 1.92.0 (stable) to support edition 2024",
            "toolchain") is None

    def test_smoking_gun_line_parses(self):
        e = parse_declaration(
            "linkify-element 4.1.4 added "
            "(explicit env patch for START state missing dependency)", "npm")
        assert e is not None
        assert (e.name, e.version) == ("linkify-element", "4.1.4")


# ---------------------------------------------------------------------------
# Section extraction
# ---------------------------------------------------------------------------

SRS_CANONICAL = """# Overview
stuff

# Environment Dependency Changes (relative to Base Env)

## Node.js Packages
- html-react-parser@5.2.2 added
- linkify-element@4.2.0 removed

## Environment Variables
- MAVEN_OPTS set to -Xmx4g
"""

SRS_VARIANT_PROSE = """# Overview

## Environment Dependency Changes
This milestone introduces the following dependencies:
1. **Go Generics**: Requires Go 1.18+ for type parameters
2. **cgroup v2**: detection support

## Implementation Notes
irrelevant
"""

SRS_NONE = """# Overview

# Environment Dependency Changes (relative to Base Env)

No changes detected.
"""

SRS_NESTED = """# Environment Dependency Changes (relative to Base Env)

## System Packages
- Playwright browser dependencies added (via `npx playwright install`):
  - libasound2 added
  - libatk1.0-0 added
"""


class TestExtract:
    def test_canonical(self):
        r = extract_srs(SRS_CANONICAL, "m1")
        assert r.section_found and not r.section_variant
        names = {(e.name, e.ecosystem) for e in r.entries}
        assert ("html-react-parser", "npm") in names
        assert ("linkify-element", "npm") in names
        assert ("MAVEN_OPTS", "env-var") in names
        assert r.unparsed == []

    def test_variant_heading_prose_lands_in_unparsed(self):
        r = extract_srs(SRS_VARIANT_PROSE, "M004")
        assert r.section_found and r.section_variant
        assert r.entries == []
        assert len(r.unparsed) >= 2  # prose + numbered items, never dropped
        # body must stop at the next same-level heading
        assert all("irrelevant" not in u.text for u in r.unparsed)

    def test_none_marker(self):
        r = extract_srs(SRS_NONE, "m")
        assert r.none_marker and not r.entries and not r.unparsed

    def test_missing_section_is_warn(self):
        r = extract_srs("# Overview\nnothing here\n", "M006")
        assert not r.section_found
        assert any("no env-deps section" in w for w in r.warnings)

    def test_group_header_children_parse(self):
        r = extract_srs(SRS_NESTED, "m")
        names = {e.name for e in r.entries}
        assert {"libasound2", "libatk1.0-0"} <= names
        # the parent "…:" line is structural — neither entry nor unparsed
        assert all("Playwright" not in u.text for u in r.unparsed)
        assert all("Playwright" not in e.name for e in r.entries)

    def test_unknown_heading_warns_but_parses(self):
        srs = ("# Environment Dependency Changes (relative to Base Env)\n"
               "## Weird Section\n- foo 1.0.0 added\n")
        r = extract_srs(srs, "m")
        assert any("unmapped subsection" in w for w in r.warnings)
        assert r.entries[0].ecosystem == "unknown"

    def test_fingerprint_stable_under_whitespace(self):
        assert _fingerprint("a  b\tc") == _fingerprint("a b c")


# ---------------------------------------------------------------------------
# Overrides reconciliation
# ---------------------------------------------------------------------------

def _mk_report_with_unparsed(mid="m1", text="ureq: 2.12 → =3.0.12"):
    srs = ("# Environment Dependency Changes (relative to Base Env)\n"
           "## Rust Packages\n"
           f"- {text}\n")
    return extract_srs(srs, mid)


def _write_overrides(tmp_path, mid, body):
    p = tmp_path / f"{mid}.yaml"
    p.write_text(body, encoding="utf-8")
    return p


class TestReconcile:
    def test_unparsed_without_overrides_fails(self):
        r = _mk_report_with_unparsed()
        reconcile_overrides(r, None)
        assert any("without waiver" in f for f in r.failures)

    def test_valid_waiver_downgrades_to_warn(self, tmp_path):
        r = _mk_report_with_unparsed()
        u = r.unparsed[0]
        ov = _write_overrides(tmp_path, "m1", f"""\
schema_version: 1
milestone: m1
waivers:
  - fingerprint: {u.fingerprint}
    text: "{u.text}"
    reason: arrow-form
""")
        reconcile_overrides(r, ov)
        assert r.failures == []
        assert any("waived" in w for w in r.warnings)

    def test_parseable_text_in_waiver_fails(self, tmp_path):
        r = _mk_report_with_unparsed()
        u = r.unparsed[0]
        parseable = "html-react-parser@5.2.2 added"
        ov = _write_overrides(tmp_path, "m1", f"""\
schema_version: 1
milestone: m1
waivers:
  - fingerprint: {u.fingerprint}
    text: "{u.text}"
    reason: arrow-form
  - fingerprint: {_fingerprint(parseable)}
    text: "{parseable}"
    reason: prose-only
""")
        reconcile_overrides(r, ov)
        assert any("parses cleanly" in f for f in r.failures)
        assert any("stale waiver" in f for f in r.failures)

    def test_wrong_fingerprint_fails(self, tmp_path):
        r = _mk_report_with_unparsed()
        u = r.unparsed[0]
        ov = _write_overrides(tmp_path, "m1", f"""\
schema_version: 1
milestone: m1
waivers:
  - fingerprint: deadbeefdeadbeef
    text: "{u.text}"
    reason: arrow-form
""")
        reconcile_overrides(r, ov)
        assert any("fingerprint does not match" in f for f in r.failures)

    def test_bad_reason_enum_fails(self, tmp_path):
        r = _mk_report_with_unparsed()
        u = r.unparsed[0]
        ov = _write_overrides(tmp_path, "m1", f"""\
schema_version: 1
milestone: m1
waivers:
  - fingerprint: {u.fingerprint}
    text: "{u.text}"
    reason: because-i-said-so
""")
        reconcile_overrides(r, ov)
        assert any("not in enum" in f for f in r.failures)

    def test_duplicate_yaml_keys_fail(self, tmp_path):
        r = _mk_report_with_unparsed()
        ov = _write_overrides(tmp_path, "m1",
                              "schema_version: 1\nschema_version: 2\n"
                              "milestone: m1\n")
        reconcile_overrides(r, ov)
        assert any("unparseable" in f for f in r.failures)

    def test_unknown_top_key_fails(self, tmp_path):
        r = _mk_report_with_unparsed()
        ov = _write_overrides(tmp_path, "m1",
                              "schema_version: 1\nmilestone: m1\nbogus: 1\n")
        reconcile_overrides(r, ov)
        assert any("unknown top-level keys" in f for f in r.failures)

    def test_milestone_mismatch_fails(self, tmp_path):
        r = _mk_report_with_unparsed(mid="m1")
        ov = _write_overrides(tmp_path, "m1",
                              "schema_version: 1\nmilestone: OTHER\n")
        reconcile_overrides(r, ov)
        assert any("milestone mismatch" in f for f in r.failures)

    def test_test_requires_adds_entry(self, tmp_path):
        r = extract_srs(SRS_NONE, "m1")
        ov = _write_overrides(tmp_path, "m1", """\
schema_version: 1
milestone: m1
test_requires:
  - name: html-react-parser
    ecosystem: npm
    version: "5.2.2"
    evidence: "test/unit-tests/HtmlUtils-test.tsx:12"
""")
        reconcile_overrides(r, ov)
        assert r.failures == []
        assert any(e.change == "test-requires" and e.name == "html-react-parser"
                   for e in r.entries)


# ---------------------------------------------------------------------------
# Dockerfile lint
# ---------------------------------------------------------------------------

class TestLint:
    def test_undeclared_install_with_continuation(self, tmp_path):
        df = tmp_path / "Dockerfile"
        df.write_text(
            "RUN cd /testbed && \\\n"
            "    yarn add html-react-parser@5.2.2 linkify-element@4.2.0\n")
        r = extract_srs(SRS_CANONICAL, "m")  # declares h-r-p, linkify
        lint_dockerfile(r, df)
        assert r.lint_candidates == []  # both declared -> no candidates

    def test_undeclared_install_flagged(self, tmp_path):
        df = tmp_path / "Dockerfile"
        df.write_text("RUN yarn add left-pad@1.3.0\n")
        r = extract_srs(SRS_CANONICAL, "m")
        lint_dockerfile(r, df)
        assert any(c.startswith("left-pad") for c in r.lint_candidates)

    def test_self_install_skipped(self, tmp_path):
        df = tmp_path / "Dockerfile"
        df.write_text("RUN pip install -e . && pip install --editable .\n")
        r = extract_srs(SRS_NONE, "m")
        lint_dockerfile(r, df)
        assert r.lint_candidates == []


# ---------------------------------------------------------------------------
# Driver / CLI
# ---------------------------------------------------------------------------

def _mk_dataset(tmp_path: Path, srs_by_mid: dict) -> Path:
    root = tmp_path / "data"
    repo = root / "org_repo_v1_v2"
    for mid, srs in srs_by_mid.items():
        d = repo / "srs" / mid
        d.mkdir(parents=True)
        (d / "SRS.md").write_text(srs, encoding="utf-8")
        (repo / "dockerfiles" / mid).mkdir(parents=True, exist_ok=True)
    return root


class TestMain:
    def test_all_pass_exit_0(self, tmp_path, capsys):
        root = _mk_dataset(tmp_path, {"m1": SRS_CANONICAL, "m2": SRS_NONE})
        rc = main(["--repo", "org_repo", "--data-root", str(root)])
        out = capsys.readouterr().out
        assert rc == 0 and "ALL PASS" in out

    def test_unparsed_without_waiver_exit_1(self, tmp_path, capsys):
        root = _mk_dataset(tmp_path, {"M004": SRS_VARIANT_PROSE})
        rc = main(["--repo", "org_repo", "--data-root", str(root)])
        out = capsys.readouterr().out
        assert rc == 1 and "FAILURE(S)" in out

    def test_allow_missing_overrides_downgrades(self, tmp_path, capsys):
        root = _mk_dataset(tmp_path, {"M004": SRS_VARIANT_PROSE})
        rc = main(["--repo", "org_repo", "--data-root", str(root),
                   "--allow-missing-overrides"])
        out = capsys.readouterr().out
        assert rc == 0 and "uncovered" in out

    def test_overrides_root_staging_wins(self, tmp_path, capsys):
        root = _mk_dataset(tmp_path, {"m1": ""})
        # SRS with one unparsed line
        (root / "org_repo_v1_v2" / "srs" / "m1" / "SRS.md").write_text(
            "# Environment Dependency Changes (relative to Base Env)\n"
            "## Rust Packages\n- ureq: 2.12 → =3.0.12\n")
        staging = tmp_path / "staging"
        staging.mkdir()
        from scripts.verify_env_deps import _fingerprint as fp
        text = "ureq: 2.12 → =3.0.12"
        (staging / "m1.yaml").write_text(f"""\
schema_version: 1
milestone: m1
waivers:
  - fingerprint: {fp(text)}
    text: "{text}"
    reason: arrow-form
""")
        rc = main(["--repo", "org_repo", "--data-root", str(root),
                   "--overrides-root", str(staging)])
        assert rc == 0

    def test_json_report(self, tmp_path):
        root = _mk_dataset(tmp_path, {"m1": SRS_CANONICAL})
        out = tmp_path / "report.json"
        rc = main(["--repo", "org_repo", "--data-root", str(root),
                   "--json", str(out)])
        assert rc == 0
        data = json.loads(out.read_text())
        assert data["summary"]["milestones"] == 1
        assert data["milestones"][0]["entries"]

    def test_json_inside_data_root_refused(self, tmp_path):
        root = _mk_dataset(tmp_path, {"m1": SRS_NONE})
        rc = main(["--repo", "org_repo", "--data-root", str(root),
                   "--json", str(root / "report.json")])
        assert rc == 2

    def test_ambiguous_repo_refused(self, tmp_path):
        root = _mk_dataset(tmp_path, {"m1": SRS_NONE})
        (root / "org_repo_other" / "srs" / "m1").mkdir(parents=True)
        (root / "org_repo_other" / "srs" / "m1" / "SRS.md").write_text(SRS_NONE)
        with pytest.raises(SystemExit):
            main(["--repo", "org_repo", "--data-root", str(root)])
