#!/usr/bin/env python3
"""Static verification of SRS environment-dependency declarations.

Implements the ``--static`` half of docs/env-deps-verification.md (design v3):
for every milestone of a repo dataset, re-derive the environment expectation
set from the SRS "Environment Dependency Changes" section with a strict
grammar, reconcile it against the milestone's typed-exception file
(``env_deps_overrides.yaml``), and lint the milestone Dockerfile for
install commands not visible in the derived contract.

Read-only by construction: the ONLY writes this tool ever performs are the
optional ``--json`` report (which must lie outside the data root) and stdout.

Exit codes: 0 = all checks consistent (warnings allowed);
            1 = contract mismatch (any FAIL);
            2 = verifier/internal error (could not observe).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    print("verify_env_deps: PyYAML is required", file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# Section / heading detection
# ---------------------------------------------------------------------------

# Canonical H1 heading (116/120 SRS files). Matching is case- and
# level-insensitive on the phrase so variant headings (go-zero M004 uses H2)
# are SEEN and dispositioned, never silently treated as "no section".
_SECTION_PHRASE = "environment dependency changes"

# Subsection heading (normalized: lowercase, collapsed spaces) -> ecosystem.
# Unknown headings do NOT fail: their lines still parse, entries carry
# ecosystem "unknown" and a WARN is emitted (map gaps must be visible, not
# fatal — the grammar itself is ecosystem-independent).
ECOSYSTEM_MAP: Dict[str, str] = {
    # npm
    "node.js packages": "npm",
    "node packages": "npm",
    "npm packages": "npm",
    "node.js global packages": "npm-global",
    "node.js packages (global)": "npm-global",
    # python
    "python packages": "pip",
    "python dependencies": "pip",
    "pip packages": "pip",
    # go
    "go packages": "go",
    "go packages (added)": "go",
    "go packages (new)": "go",
    "go packages (removed)": "go",
    "go packages (upgraded)": "go",
    "go packages (version upgrades)": "go",
    "go module dependencies": "go",
    "go tools": "go-tool",
    # toolchains
    "go runtime": "toolchain",
    "go version": "toolchain",
    "rust toolchain": "toolchain",
    "node.js runtime": "toolchain",
    "python runtime": "toolchain",
    "java runtime": "toolchain",
    # maven
    "java/maven dependencies": "maven",
    "maven dependencies": "maven",
    "java dependencies": "maven",
    # misc
    "system packages": "system",
    "system packages (apt)": "system",
    "playwright browsers": "browser",
    "environment variables": "env-var",
    "base image": "structural",
    "rust packages": "cargo",
    "cargo packages": "cargo",
    "rust dependencies": "cargo",
    "crates": "cargo",
    "workspace dependency upgrades": "cargo",
}

# Contains-rules applied when no exact map hit (still deterministic).
_HEADING_CONTAINS_RULES: List[Tuple[str, str]] = [
    ("cargo.toml", "cargo"),
]

_NONE_MARKER = "no changes detected."


def _fingerprint(text: str) -> str:
    """Stable identity for a declaration line: sha256 of whitespace-normalized
    text (bullet stripped), first 16 hex chars. Never line numbers — SRS
    editorial edits must not churn identities."""
    norm = " ".join(text.split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Line grammar
# ---------------------------------------------------------------------------

# A version token is "exact" when it pins a single version (digits, optional
# leading v, e.g. 1.2.3 / v2.2.1 / 5.2.2-rc1). Constraint-ish tokens
# (^10.0.0, @latest, >=1.0, latest, workspace) still PARSE but yield a
# presence-only expectation (version=None, constraint recorded).
_EXACT_VERSION_RE = re.compile(r"^v?\d[\w.+-]*$")

_CHANGE_WORDS = r"(?P<change>added|removed)"

# npm @-form:  name@ver added   (scoped: @scope/pkg@ver — split at LAST @)
_RE_AT_FORM = re.compile(
    rf"^(?P<name>\S+?)@(?P<ver>[^@\s]+)\s+{_CHANGE_WORDS}$"
)
# space-form:  name ver added   (pip / go / maven norm)
_RE_SPACE_FORM = re.compile(
    rf"^(?P<name>\S+)\s+(?P<ver>\S+)\s+{_CHANGE_WORDS}$"
)
# upgraded / downgraded to
_RE_UP_TO = re.compile(
    r"^(?P<name>\S+)\s+(?P<change>upgraded|downgraded|updated)\s+to\s+(?P<ver>\S+)$"
)
# upgraded from X to Y
_RE_FROM_TO = re.compile(
    r"^(?P<name>\S+)\s+(?P<change>upgraded|downgraded|updated)\s+from\s+\S+\s+to\s+(?P<ver>\S+)$"
)
# versionless:  name added
_RE_VERSIONLESS = re.compile(rf"^(?P<name>\S+)\s+{_CHANGE_WORDS}$")
# env-var forms:  NAME set to VALUE / NAME=VALUE set / NAME set (...)
_RE_ENV_SET_TO = re.compile(
    r"^(?P<name>[A-Z_][A-Z0-9_]*)\s+set\s+to\s+(?P<val>.+)$"
)
_RE_ENV_ASSIGN = re.compile(
    r"^(?P<name>[A-Z_][A-Z0-9_]*)=(?P<val>\S+)(\s+(set|added|configured).*)?$"
)

WAIVER_REASONS = frozenset(
    {
        "prose-only",
        "unpinned-version",
        "wildcard-removal",
        "arrow-form",
        "table-row",
        "quarantine-override",
        "system-unversioned",
        "imperative",
        "structural",
        "other-unparseable",
    }
)


@dataclass
class Entry:
    name: str
    ecosystem: str
    version: Optional[str]
    change: str
    constraint: Optional[str]
    fingerprint: str
    line_no: int
    text: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ecosystem": self.ecosystem,
            "version": self.version,
            "change": self.change,
            "constraint": self.constraint,
            "fingerprint": self.fingerprint,
            "line_no": self.line_no,
            "text": self.text,
        }


@dataclass
class Unparsed:
    fingerprint: str
    line_no: int
    text: str
    ecosystem: str

    def to_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "line_no": self.line_no,
            "text": self.text,
            "ecosystem": self.ecosystem,
        }


@dataclass
class MilestoneReport:
    milestone: str
    section_found: bool = False
    section_variant: bool = False
    none_marker: bool = False
    entries: List[Entry] = field(default_factory=list)
    unparsed: List[Unparsed] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    lint_candidates: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "milestone": self.milestone,
            "section_found": self.section_found,
            "section_variant": self.section_variant,
            "none_marker": self.none_marker,
            "entries": [e.to_dict() for e in self.entries],
            "unparsed": [u.to_dict() for u in self.unparsed],
            "failures": self.failures,
            "warnings": self.warnings,
            "lint_candidates": self.lint_candidates,
        }


def _normalize_line(raw: str) -> str:
    """Strip bullet marker and inline backticks, collapse whitespace."""
    text = raw.strip()
    if text.startswith(("- ", "* ")):
        text = text[2:]
    elif text in ("-", "*"):
        text = ""
    text = text.replace("`", "")
    return " ".join(text.split())


def _classify_version(token: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (exact_version, constraint). Exactly one is non-None."""
    if _EXACT_VERSION_RE.match(token):
        return token, None
    return None, token


def parse_declaration(text: str, ecosystem: str) -> Optional[Entry]:
    """Parse one normalized declaration line. Returns None when the strict
    grammar rejects it (caller records it as unparsed).

    Trailing parenthetical annotations are human commentary, not part of the
    declaration: "xvfb added (virtual framebuffer...)", "Go upgraded to
    1.21.13 (from 1.19.13)". When the line does not parse as-is, exactly one
    trailing group is stripped and parsing retried; the entry's identity
    (fingerprint, text) always stays on the ORIGINAL line text.
    """
    entry = _parse_once(text, ecosystem)
    if entry is None:
        stripped = re.sub(r"\s*\([^()]*\)$", "", text)
        if stripped != text:
            entry = _parse_once(stripped, ecosystem)
            if entry is not None:
                entry.text = text
                entry.fingerprint = _fingerprint(text)
    return entry


def _parse_once(text: str, ecosystem: str) -> Optional[Entry]:
    if not text:
        return None

    if ecosystem == "env-var":
        m = _RE_ENV_SET_TO.match(text) or _RE_ENV_ASSIGN.match(text)
        if m:
            return Entry(
                name=m.group("name"),
                ecosystem="env-var",
                version=None,
                change="set",
                constraint=(m.groupdict().get("val") or None),
                fingerprint=_fingerprint(text),
                line_no=-1,
                text=text,
            )
        # fall through: env-var sections may also contain package-like lines
    for regex in (_RE_AT_FORM, _RE_FROM_TO, _RE_UP_TO, _RE_SPACE_FORM,
                  _RE_VERSIONLESS):
        m = regex.match(text)
        if not m:
            continue
        name = m.group("name")
        # @-form guard: a bare leading-@ scope with no package part, or a
        # name that ends up empty, is not a valid parse.
        if not name or name == "@" or name.endswith("/"):
            return None
        groups = m.groupdict()
        ver_token = groups.get("ver")
        change = groups.get("change") or "added"
        if change == "updated":
            change = "upgraded"
        if ver_token is None:
            version, constraint = None, None
        else:
            version, constraint = _classify_version(ver_token)
            if regex is _RE_SPACE_FORM and version is None:
                # "name word added" where word is not versionish is far more
                # likely prose than a constraint — reject to unparsed rather
                # than invent an expectation.
                return None
        return Entry(
            name=name,
            ecosystem=ecosystem,
            version=version,
            change=change,
            constraint=constraint,
            fingerprint=_fingerprint(text),
            line_no=-1,
            text=text,
        )
    return None


# ---------------------------------------------------------------------------
# SRS extraction
# ---------------------------------------------------------------------------

def extract_srs(srs_text: str, milestone: str) -> MilestoneReport:
    report = MilestoneReport(milestone=milestone)
    lines = srs_text.splitlines()

    # Locate the env section: any heading line whose text contains the phrase.
    start_idx = None
    heading_level = None
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped.startswith("#"):
            continue
        m = re.match(r"^(#+)\s*(.*)$", stripped)
        if not m:
            continue
        if _SECTION_PHRASE in m.group(2).strip().lower():
            start_idx = i
            heading_level = len(m.group(1))
            report.section_found = True
            report.section_variant = not (
                heading_level == 1
                and m.group(2).strip().lower().startswith(_SECTION_PHRASE)
            )
            break
    if start_idx is None:
        report.warnings.append("no env-deps section in SRS (hygiene)")
        return report

    # Section body: until the next heading of the same-or-higher level.
    body: List[Tuple[int, str]] = []
    for j in range(start_idx + 1, len(lines)):
        stripped = lines[j].strip()
        m = re.match(r"^(#+)\s", stripped)
        if m and len(m.group(1)) <= heading_level:
            break
        body.append((j + 1, lines[j]))  # 1-indexed line numbers

    ecosystem = "unknown"
    unknown_headings: List[str] = []
    bullet_re = re.compile(r"^(\s*)[-*]\s+(.*)$")

    for idx, (line_no, raw) in enumerate(body):
        stripped = raw.strip()
        if not stripped:
            continue
        hm = re.match(r"^(#+)\s*(.*)$", stripped)
        if hm:
            head_norm = " ".join(hm.group(2).split()).lower().rstrip(":")
            ecosystem = ECOSYSTEM_MAP.get(head_norm, "unknown")
            if ecosystem == "unknown":
                for needle, eco in _HEADING_CONTAINS_RULES:
                    if needle in head_norm:
                        ecosystem = eco
                        break
            if ecosystem == "unknown":
                unknown_headings.append(hm.group(2).strip())
            continue
        if stripped.lower() == _NONE_MARKER:
            report.none_marker = True
            continue
        bm = bullet_re.match(raw)
        if bm is None:
            # Prose inside the section (e.g. go-zero M004 numbered items).
            text = _normalize_line(stripped)
            if text:
                report.unparsed.append(
                    Unparsed(_fingerprint(text), line_no, text, ecosystem)
                )
            continue
        # Group-header rule: bullet ending ':' immediately followed by a
        # deeper-indented bullet is structural, not a declaration.
        if bm.group(2).rstrip().endswith(":"):
            this_indent = len(bm.group(1))
            nxt = body[idx + 1][1] if idx + 1 < len(body) else ""
            nm = bullet_re.match(nxt)
            if nm and len(nm.group(1)) > this_indent:
                continue
        text = _normalize_line(raw)
        if not text:
            continue
        entry = parse_declaration(text, ecosystem)
        if entry is None:
            report.unparsed.append(
                Unparsed(_fingerprint(text), line_no, text, ecosystem)
            )
        else:
            entry.line_no = line_no
            report.entries.append(entry)

    for h in unknown_headings:
        report.warnings.append(f"unmapped subsection heading: '## {h}'")
    return report


# ---------------------------------------------------------------------------
# Overrides (typed exceptions) reconciliation
# ---------------------------------------------------------------------------

class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys."""


def _strict_map(loader: _StrictLoader, node: yaml.nodes.MappingNode):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key: {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _strict_map
)

_ALLOWED_TOP_KEYS = {"schema_version", "milestone", "waivers",
                     "test_requires", "probe_policy"}
_ALLOWED_WAIVER_KEYS = {"fingerprint", "text", "reason"}
_ALLOWED_TESTREQ_KEYS = {"name", "ecosystem", "version", "evidence"}
_ALLOWED_PROBE_KEYS = {"name", "ecosystem", "probe", "reason", "version",
                       "store_root", "consumer_roots"}


def reconcile_overrides(
    report: MilestoneReport, overrides_path: Optional[Path]
) -> None:
    """Apply the typed-exception file to the report (mutates failures/warnings).

    Fail-closed rules (design §3.1):
      - every unparsed SRS line needs exactly one waiver (fingerprint+text);
      - a waiver whose text parses cleanly = FAIL (promote to derived);
      - a waiver matching no line = FAIL (stale);
      - unparseable/invalid overrides file = FAIL;
      - unparsed lines with no overrides file = FAIL per line.
    """
    waivers: List[dict] = []
    if overrides_path is not None and overrides_path.exists():
        try:
            with open(overrides_path, "r", encoding="utf-8") as f:
                data = yaml.load(f, Loader=_StrictLoader)
        except Exception as exc:
            report.failures.append(f"overrides unparseable: {exc}")
            return
        if not isinstance(data, dict):
            report.failures.append("overrides root must be a mapping")
            return
        unknown = set(data) - _ALLOWED_TOP_KEYS
        if unknown:
            report.failures.append(
                f"overrides has unknown top-level keys: {sorted(unknown)}"
            )
            return
        if data.get("milestone") != report.milestone:
            report.failures.append(
                "overrides milestone mismatch: "
                f"{data.get('milestone')!r} != {report.milestone!r}"
            )
            return
        for i, w in enumerate(data.get("waivers") or []):
            if not isinstance(w, dict) or set(w) - _ALLOWED_WAIVER_KEYS:
                report.failures.append(f"waiver[{i}] malformed")
                continue
            missing = {"fingerprint", "text", "reason"} - set(w)
            if missing:
                report.failures.append(
                    f"waiver[{i}] missing keys: {sorted(missing)}"
                )
                continue
            if w["reason"] not in WAIVER_REASONS:
                report.failures.append(
                    f"waiver[{i}] reason {w['reason']!r} not in enum"
                )
                continue
            if _fingerprint(w["text"]) != w["fingerprint"]:
                report.failures.append(
                    f"waiver[{i}] fingerprint does not match its text"
                )
                continue
            waivers.append(w)
        for i, t in enumerate(data.get("test_requires") or []):
            if not isinstance(t, dict) or set(t) - _ALLOWED_TESTREQ_KEYS:
                report.failures.append(f"test_requires[{i}] malformed")
                continue
            if not {"name", "ecosystem", "evidence"} <= set(t):
                report.failures.append(
                    f"test_requires[{i}] needs name/ecosystem/evidence"
                )
                continue
            report.entries.append(
                Entry(
                    name=t["name"],
                    ecosystem=t["ecosystem"],
                    version=t.get("version"),
                    change="test-requires",
                    constraint=None,
                    fingerprint=_fingerprint(f"test-requires {t['name']}"),
                    line_no=-1,
                    text=f"test-requires: {t['evidence']}",
                )
            )
        for i, p in enumerate(data.get("probe_policy") or []):
            if not isinstance(p, dict) or set(p) - _ALLOWED_PROBE_KEYS:
                report.failures.append(f"probe_policy[{i}] malformed")

    waived_fps = {w["fingerprint"] for w in waivers}
    line_fps = {u.fingerprint for u in report.unparsed}

    for w in waivers:
        norm_text = " ".join(w["text"].split())
        if parse_declaration(norm_text, "unknown") is not None:
            report.failures.append(
                f"waiver text parses cleanly — promote to derived: "
                f"'{w['text']}'"
            )
        if w["fingerprint"] not in line_fps:
            report.failures.append(
                f"stale waiver (matches no SRS line): '{w['text']}'"
            )

    for u in report.unparsed:
        if u.fingerprint in waived_fps:
            report.warnings.append(
                f"waived (unparsed) line {u.line_no}: '{u.text}'"
            )
        else:
            report.failures.append(
                f"unparsed declaration without waiver "
                f"(line {u.line_no}): '{u.text}'"
            )


# ---------------------------------------------------------------------------
# Dockerfile lint (best-effort WARN; identify-and-report only, never a gate)
# ---------------------------------------------------------------------------

_INSTALL_RES = [
    re.compile(r"\byarn\s+(?:global\s+)?add\s+(?P<args>[^&;|]+)"),
    re.compile(r"\bnpm\s+install\s+(?P<args>[^&;|]+)"),
    re.compile(r"\bpip3?\s+install\s+(?P<args>[^&;|]+)"),
    re.compile(r"\bgo\s+(?:get|install)\s+(?P<args>[^&;|]+)"),
    re.compile(r"\bcargo\s+add\s+(?P<args>[^&;|]+)"),
    re.compile(r"\bapt-get\s+install\s+(?P<args>[^&;|]+)"),
]
_SELF_INSTALL_TOKENS = {".", "-e", "--editable", "-r", "--requirement",
                        "-g", "--global", "-y", "--no-install-recommends",
                        "--frozen-lockfile", "--offline"}


def lint_dockerfile(report: MilestoneReport, dockerfile: Path) -> None:
    if not dockerfile.exists():
        return
    text = dockerfile.read_text(encoding="utf-8", errors="replace")
    # Join backslash continuations, then examine RUN lines split on && / ;
    joined = re.sub(r"\\\s*\n", " ", text)
    declared = {e.name.lower() for e in report.entries}
    for line in joined.splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("RUN "):
            continue
        for cmd in re.split(r"&&|;", stripped[4:]):
            for regex in _INSTALL_RES:
                m = regex.search(cmd)
                if not m:
                    continue
                for tok in m.group("args").split():
                    if tok.startswith("-") or tok in _SELF_INSTALL_TOKENS:
                        continue
                    name = tok
                    if "@" in tok[1:]:
                        name = tok[: tok.rindex("@")]
                    name = re.split(r"==|>=|<=", name)[0]
                    if name and name.lower() not in declared:
                        report.lint_candidates.append(
                            f"{name} (from: {cmd.strip()[:80]})"
                        )


# ---------------------------------------------------------------------------
# Repo / milestone discovery + driver
# ---------------------------------------------------------------------------

def discover_repo(data_root: Path, repo_sub: str) -> Path:
    candidates = sorted(
        d for d in data_root.iterdir()
        if d.is_dir() and (d / "srs").is_dir() and repo_sub in d.name
    )
    if len(candidates) != 1:
        names = [c.name for c in candidates]
        raise SystemExit(
            f"verify_env_deps: --repo {repo_sub!r} matched {len(candidates)} "
            f"dataset dirs {names}; need exactly 1"
        )
    return candidates[0]


def run_static(
    repo_dir: Path,
    milestone_filter: Optional[str],
    overrides_root: Optional[Path],
    allow_missing_overrides: bool,
) -> Tuple[List[MilestoneReport], int]:
    reports: List[MilestoneReport] = []
    srs_root = repo_dir / "srs"
    mids = sorted(p.name for p in srs_root.iterdir() if (p / "SRS.md").exists())
    if milestone_filter:
        mids = [m for m in mids if milestone_filter in m]
        if not mids:
            raise SystemExit(
                f"verify_env_deps: --milestone {milestone_filter!r} matched "
                "no milestone"
            )
    for mid in mids:
        srs_text = (srs_root / mid / "SRS.md").read_text(
            encoding="utf-8", errors="replace"
        )
        report = extract_srs(srs_text, mid)

        # Overrides precedence: staging root (if given) wins over data root.
        ov_path: Optional[Path] = None
        if overrides_root is not None:
            cand = overrides_root / f"{mid}.yaml"
            if cand.exists():
                ov_path = cand
        if ov_path is None:
            cand = repo_dir / "dockerfiles" / mid / "env_deps_overrides.yaml"
            if cand.exists():
                ov_path = cand

        needs_overrides = bool(report.unparsed)
        if needs_overrides and ov_path is None and allow_missing_overrides:
            report.warnings.append(
                "unparsed lines present but overrides missing "
                "(allowed by --allow-missing-overrides)"
            )
            for u in report.unparsed:
                report.warnings.append(
                    f"  uncovered (line {u.line_no}): '{u.text}'"
                )
        else:
            reconcile_overrides(report, ov_path)

        lint_dockerfile(report, repo_dir / "dockerfiles" / mid / "Dockerfile")
        reports.append(report)

    failures = sum(len(r.failures) for r in reports)
    return reports, failures


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--repo", required=True,
                        help="substring matching exactly one dataset dir")
    parser.add_argument("--milestone", default=None,
                        help="substring filter on milestone ids")
    parser.add_argument("--data-root", default=None,
                        help="dataset root (default: $SWE_MILESTONE_DATA_ROOT)")
    parser.add_argument("--overrides-root", default=None,
                        help="staging dir with <milestone>.yaml override files "
                             "(read in preference to the data root; lets "
                             "overrides be authored without touching the "
                             "data repo)")
    parser.add_argument("--allow-missing-overrides", action="store_true",
                        help="downgrade 'unparsed line without overrides file' "
                             "from FAIL to WARN (loud rollout escape)")
    parser.add_argument("--json", default=None,
                        help="write machine-readable report to this path "
                             "(must be OUTSIDE the data root)")
    args = parser.parse_args(argv)

    data_root_str = args.data_root or os.environ.get("SWE_MILESTONE_DATA_ROOT")
    if not data_root_str:
        print("verify_env_deps: set --data-root or SWE_MILESTONE_DATA_ROOT",
              file=sys.stderr)
        return 2
    data_root = Path(os.path.expandvars(data_root_str)).expanduser().resolve()
    if not data_root.is_dir():
        print(f"verify_env_deps: data root not a directory: {data_root}",
              file=sys.stderr)
        return 2

    if args.json:
        json_path = Path(args.json).resolve()
        if str(json_path).startswith(str(data_root) + os.sep):
            print("verify_env_deps: --json must lie outside the data root",
                  file=sys.stderr)
            return 2

    try:
        repo_dir = discover_repo(data_root, args.repo)
        overrides_root = (
            Path(args.overrides_root).resolve() if args.overrides_root else None
        )
        reports, failures = run_static(
            repo_dir, args.milestone, overrides_root,
            args.allow_missing_overrides,
        )
    except SystemExit:
        raise
    except Exception as exc:  # verifier error — never a silent pass
        print(f"verify_env_deps: internal error: {exc}", file=sys.stderr)
        return 2

    n_entries = 0
    for r in reports:
        n_entries += len(r.entries)
        status = "FAIL" if r.failures else ("WARN" if r.warnings else "PASS")
        print(f"  {status:4s}  {r.milestone}  "
              f"entries={len(r.entries)} unparsed={len(r.unparsed)}")
        for msg in r.failures:
            print(f"        FAIL  {msg}")
        for msg in r.warnings:
            print(f"        WARN  {msg}")
        for cand in r.lint_candidates:
            print(f"        WARN  dockerfile-install not in derived contract: "
                  f"{cand}")

    if args.json:
        payload = {
            "repo": repo_dir.name,
            "milestones": [r.to_dict() for r in reports],
            "summary": {
                "milestones": len(reports),
                "entries": n_entries,
                "failures": failures,
            },
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        print(f"  report: {args.json}")

    if failures:
        print(f"{failures} FAILURE(S)")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
