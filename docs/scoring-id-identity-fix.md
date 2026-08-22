# Spec: test-ID identity in scoring (fix for issue #24)

Status: **v2 — revised after adversarial review (codex, 2026-08-21)** · Owner: harness/e2e ·
Scope: `harness/e2e/evaluator.py` scoring path; re-tally of affected stored results.
Review record: `SWE-Milestone-data/reeval/issue24/codex_review_v1.md` (data tree, kept verbatim; not in this repository).

## 0. v3 addendum (2026-08-21) — scope narrowed after cross-agent evidence

An independent full-corpus re-score (2,827 published cells, today's scorer vs a path-preserving
key; read-only; source: parallel investigation, artifacts to be cross-checked) reports:
427 cells change (399 on the final attempt), 2,037 test-id flips; **767 false failures**
(direction a) and **1,259 false passes** (direction b — a never-run test credited via a
namesake), with **zero** cases of a genuinely relocated test being correctly bridged; 29
`resolved` flips, all scikit-learn (M04/M11/M17), of which **26 were scored in an earlier
"pass-wins" aggregation era** (their published values are correct by luck; re-scoring them with
today's fail-close scorer would wrongly change them — the non-idempotence mechanism) and **3 are
current-board misjudgments** (glm-5.2-1m, glm-5.2, gpt-5.6-sol at M11: published
`resolved=False`, should be `True`). It also establishes the sequencing constraint with #23:
v1.0.2's element-web re-evaluation runs through this scorer and 19/19 element universes carry
collision pairs (contamination already observed in 3/33 trials).

Consequences for this spec:

1. **Scope of PR-1 is narrowed to identity-preserving canonicalisation for every non-Ginkgo,
   non-Maven/Gradle framework** (pytest, jest, cargo, go_test, vitest, playwright, mocha,
   unittest, django_runtests, nushell_script, `None`/unknown → raw nodeid). Ginkgo's
   package-aware key (F1a), the two-sided fail-closed bridge for Ginkgo and Java (F2) and the
   absent-suite canonicalisation for Go (F4) move to a follow-up issue. This removes review
   blockers 1 and 2 from the critical path; blocker 3 (re-tally provenance) remains and is
   addressed by replay selection (7.1).
2. **Scorer revision is part of provenance**: "the old scorer" is not one scorer (pass-wins era
   vs fail-close era). The re-tally tool replays both: `legacy-prefix-drop` (the scorer on main
   before the fix) and `legacy-prefix-drop-passwins` (the scorer before 0a779f0: prefix-dropping
   key for every framework including Java, last-write aggregation, no module-less fallback). A
   cell reproducible only under pass-wins is labelled `pass-wins` and **frozen by default** (report
   only, no mirror output; `--include-pass-wins` overrides); reproducible under both is
   `era-agnostic` (no aggregation-sensitive collision, safe to re-tally); only under the current
   legacy key is `fail-close`. Era is thus derived from which scorer reproduces the stored
   projection, not guessed from outcomes.
3. **Absent-suite inference is unaffected** by restored prefixes: `_absent_suites_from_missing_ids()`
   is Ginkgo-only and the Ginkgo key is unchanged. (An earlier draft of this section claimed a
   possible side effect; the review disproved it.) The re-tally preserves the stored
   `absent_suites` / `partial_test_universe` and only compares `absent_suites` when the stored
   envelope recorded the field.
4. **Publication policy proposal**: correct the 3 current-board misjudgments through the
   documented promotion procedure; freeze pass-wins-era cells; record the full 427-cell delta and
   schedule the remaining corrections together with v1.0.2's grading-semantics change set, so
   #24 lands **before** any v1.0.2 re-evaluation (#23/element) — the fix is a prerequisite, not a
   follow-up.

5. **What the re-tally tool does and does not regenerate** (implementation review, 2026-08-21):
   `mirror` mode writes `evaluation_result.json`, `evaluation_result_filtered.json` (only when the
   selected artifact's `eval.json` is available; locks re-applied after filtering, N2P missing
   re-derived), a copy of the selected artifact directory, `rescore_manifest.json` (payload,
   classification, test-config, repo-config and filter-list hashes; replay policies; era; scorer
   revision) and `PROMOTION_NOTES.md`. It does **not** regenerate the trial `summary.json` /
   `summary_filtered.json`, `feedback_report.md` or `artifacts.tar.gz`; those are listed as stale
   and must be handled at promotion (docs/re-evaluation.md). Inputs drift (stored repo-config
   binding ≠ current config; classification ≠ the one at the trial's recorded data commit) makes
   a cell non-replayable. Re-running on an already corrected cell is a verified no-op.
6. **Known residuals not addressed on this branch**: Ginkgo package-aware key (F1a), two-sided
   fail-closed bridge for Ginkgo and Java (F2), Go absent-suite canonicalisation (F4), mixed-mode
   payloads scored under one framework (navidrome Ginkgo+Vitest: the Vitest ids still take the
   Ginkgo prefix-dropping key), Cargo cross-crate identical raw ids (parser-level), and the
   classifier's overwrite of duplicate raw observations (dataset-build side of the contract).

## 0.1 v2 text below (still authoritative for design details not superseded above)

## 1. Summary

The evaluator decides pass/fail/missing for every expected test by matching baseline
classification IDs against runtime-report IDs through a canonical key. For every framework
other than `maven`/`gradle` — including `None` and unrecognized values — that key is built by
`normalize_ginkgo_nodeid()`, which discards everything before the first `::`. **The key is
lossy**: distinct tests that share a name collapse into one outcome slot. Consequences:

- **(a) cross-crediting / fail-close**: an expected test receives a same-named test's outcome;
  when several observations share the key, `_aggregate_test_outcomes()` picks by precedence
  `error > failed > skipped > passed`;
- **(b) missing masking**: an expected test that never ran (compile / collection kill) is
  credited with a surviving namesake's outcome instead of being counted missing. This needs no
  aggregation at all — a single surviving namesake suffices.

Both directions affect F2P, N2P and P2P (a missing F2P/N2P can be turned into "achieved"; a
missing P2P into success or regression). Issue #24 reports (a) on element-web and leaves scope
"unknown, plausibly wide". This spec sizes the scope (wide), fixes the lossy key for all
frameworks, and defines a **re-tally without re-execution** for affected cells, with a
replay-selection procedure that makes historical re-tally trustworthy.

## 2. Code pointers (symbols; line numbers as of the review are not maintained)

- `normalize_ginkgo_nodeid()` — `:943` (prefix drop `:968–972`, `" > "`→`" / "` `:974–975`).
- `normalize_scoring_nodeid()` in `harness/e2e/evaluator.py`; before the fix only `maven`/`gradle` escaped to hashcode-only.
- `_aggregate_test_outcomes()` — `:1032`; precedence `error > failed > skipped > passed`.
- `_build_scoring_test_outcomes()` — `:1045`; runtime outcomes keyed by canonical; collision
  merge `:1065–1069`. **Production calls it without `go_module`** (`:6293`).
- `_lookup_scoring_outcome()` — `:1102`; does **not** accept `go_module`; returns a namesake's
  outcome at `:1112–1114` (this is (b)).
- Scoring block: payload/tally `:6285–6422` (strict resolution), extra gates `:6431–6447`,
  result assembly `:6449–6617`. Category semantics of a lookup result:

  | lookup | F2P | P2P | N2P |
  |---|---|---|---|
  | `passed` | success | success | success |
  | `failed`/`error` | failure | regression | failure |
  | `skipped` | failure | **counted in `pass_to_pass_missing`** | failure |
  | `unknown` | failure | `pass_to_pass_missing` + missing-ID metadata | failure + N2P-missing |

- `pass_to_pass_missing == 0` is a hard gate on the **strict persisted** resolution in
  `compare_results()` (`:6420`). The orchestrator later recomputes DAG resolution from
  configurable thresholds with no zero-missing condition (`orchestrator.py:178`), after the JSON
  is written (`orchestrator.py:117`); persisted JSON, live DAG decision and `summary.json` can
  therefore encode different policies. This spec changes only the persisted scoring.
- `convert_to_summary()` — `:6164` (multi-mode merge into `eval_summary.json`).
- Filtered-result generator — `:2124`; it reads the **current filter list** (`:2139`), **every**
  `artifacts/*/eval.json` raw ID (`:2158–2173`), and exact raw-ID membership (`:2044`); its
  resolver (`:2091`) ignores infrastructure/residue/Go resolution locks.
- Framework resolution `_resolve_test_framework()` (`:1232`, inference `:1252–1289`). Actual
  values on the checked-in universes: ripgrep `cargo`, dubbo `maven` (25) **and `None` for M020**
  (legacy object-form `test_config.json` rejected by list-only inference `:1264`), element `jest`,
  navidrome `ginkgo`, nushell `cargo`, scikit `pytest`, go-zero `go_test`. Parsers also support
  `vitest`, `playwright`, `mocha`, `unittest`, `django_runtests`, `nushell_script`. Mixed-mode
  payloads are merged **without framework tags** (`milestone_attempt.py:723`): navidrome
  ginkgo+vitest scored as `ginkgo`; element jest+playwright as `jest`; nushell `M04_std`
  cargo+nushell_script as `cargo`.
- Upstream parsers already path-normalize where needed: pytest keeps IDs (`report_parser.py:269`),
  jest strips `/testbed/` (`:625`), vitest/playwright strip container roots (`:715`, `:900`),
  ginkgo strips filesystem roots and optionally prepends a module (`go_report_utils.py:960`);
  current ginkgo hierarchy separator is `" > "` (`go_report_utils.py:104`), `" / "` is legacy.
- **Cargo identity is already lost upstream**: the parser emits only `test.name`
  (`report_parser.py:557`; binary known at `cargo_report_utils.py:152` but dropped), and the
  classifier overwrites duplicate raw IDs (`classifier.py:224`). Two crates with the same full
  Rust test name are indistinguishable before scoring. This is **out of scope** for #24 (tracked
  separately) — F1 restores the identity that the scorer drops, not identity never recorded.
- The normalizers have no callers outside `evaluator.py` (`harness/`, `scripts/`, `tests/`).

## 3. Evidence

### 3.1 Runtime and baseline IDs share one format per framework (sampled published cells)

| framework | runtime `results.passed[]` | baseline `pass_to_pass[]` | same format? |
|---|---|---|---|
| cargo | `glob::tests::any1` | `pathutil::tests::ext4` | yes (crate-relative module path) |
| pytest | `sklearn/_loss/tests/test_loss.py::test_loss_dtype[...]` | `sklearn/impute/tests/...[coo_matrix]` | yes |
| jest | `test/unit-tests/DeviceListener-test.ts::DeviceListener > ...` | `test/unit-tests/.../PollHistory-test.tsx::<PollHistory /> > ...` | yes |
| go_test | `github.com/zeromicro/go-zero/core/cmdline/TestEnterToContinue` | `github.com/.../core/mapping/TestUnmarshal.../invalid_option` | yes; no `::` |
| ginkgo | `adapters/taglib::Extractor > ReplayGain > ...` | `github.com/navidrome/navidrome/persistence::PlaylistRepository > ...` | **no — runtime is module-relative, baseline carries the Go module** (parser discovers `go.mod` near the report, `report_parser.py:354`) |

### 3.2 Collision census under the current key (every `stable_classification` universe)

| repo | framework | P2P × non-P2P (direction a) | P2P × P2P IDs (a and b) | universes affected |
|---|---|---|---|---|
| nushell | cargo | 0 | **1883** | 17/17 |
| scikit-learn | pytest | **21** | **1708** | 12/12 |
| ripgrep | cargo | 1 | **1056** | 18/18 |
| navidrome | ginkgo | 0 | 72 | 9/9 |
| element-web | jest | 0 | 38 | 19/19 |
| go-zero | go_test | 0 | 0 | 0 |
| dubbo | maven | 48 | 44 | hashcode-instance dedup — by design |

This census is a **lower bound**: it covers expected IDs only; runtime-only namesakes (tests the
agent added) also collide and must be counted in the impact run (Section 7).

### 3.3 Observed damage

- scikit-learn: `test_pipeline.py::test_routing_passed_metadata_not_supported[decision_function]`
  (P2P, M11) inherits the failure of the same-named F2F test in `test_self_training.py`: 9 M11
  cells (8 trials, 3 published) recorded unresolved with 0 real P2P failures / 0 missing / 20/20 F2P.
- element-web: the #24 jest pair; 2 phantom P2P failures on
  `_openhands_minimax-m2.5_run_002 × e9a3625_1_sub-02`; replay/primary disagreement 5,019 / 5,017
  around a true 5,018.
- Direction (b) prevalence by category (F2P/P2P/N2P × namesake outcome) is **unknown** and is a
  required output of the impact run.

## 4. Root cause

A lossy canonical key (prefix drop) designed to bridge one framework's representation gap
(Ginkgo: module-qualified baseline vs module-relative runtime) is applied to every framework
except Maven/Gradle. The Java branch states the governing principle — prefixes are identity —
but (i) the same principle was never applied to file/module paths, and (ii) its own module-less
fallback is only *runtime-side* unique and can cross-credit several expected modules (existing
tests cover one expected module, not two: `tests/e2e/test_module_aware_scoring.py:70`).

## 5. Fix design (v2)

### F1 — identity-preserving canonicalisation, per framework

| framework | canonical |
|---|---|
| `maven`, `gradle` | unchanged (Java hashcode normalization) |
| `ginkgo` | **structured** `(package, hierarchy)`: split at the first `::`; package canonicalised through a **pinned module mapping** (see F1a); hierarchy with `" > "`/`" / "` unified; rejoined as `pkg::hierarchy` |
| every other value (`pytest`, `jest`, `cargo`, `go_test`, `vitest`, `playwright`, `mocha`, `unittest`, `django_runtests`, `nushell_script`, `None`, unknown strings) | **raw nodeid, unchanged** — no hashcode rewriting (a pytest/jest title containing `@abcdef` is meaningful text) |

`None`/unknown frameworks are treated as identity and **logged as a configuration defect**;
dubbo M020's legacy test config must be fixed so it resolves to `maven` (separate small PR).

**F1a — module identity for Ginkgo is not available today and must be pinned first.**
`go_module` is accepted by the helper but never supplied by production (`:6293`), the lookup does
not accept it, and navidrome's repo config has neither `test_framework` nor `go_module`.
Required: an explicit, validated module-root mapping in the repo config (supporting multiple
module roots), threaded through runtime indexing, expected lookup, absent-suite computation and
the re-tally manifest. Rules to specify exactly: root package equals the module (not only
`module + "/"`), prefix boundaries (`example/proj` vs `example/proj2`), `/testbed` vs
`/testbed/pkg` (only the root maps to the empty package), IDs without `::` or with extra literal
`::`, literal `" > "` in titles.

### F2 — fail-closed bridging on both sides (Ginkgo and Java)

Build ownership indexes for **both** the expected universe and the runtime observations. A
package-/module-less bridge is allowed only when there is exactly one logical owner on **both**
sides, and **one runtime observation may satisfy at most one expected logical identity**.
Otherwise `"unknown"`. Apply the same correction to the existing Java fallback. Persist sorted,
structured bridge/collision records (canonical ID, raw IDs, outcomes, mode provenance) in the
result metadata — not only logs.

### F3 — unexpected identity collisions make the scoring untrusted

If, after F1, two distinct raw nodeids still map to one canonical and are not an approved
equivalence (Maven hash instances; Go random-subtest normalization; the same test repeated across
merged test **modes** — attempts are *not* merged, retries overwrite the artifact directory), the
cell is marked scoring-untrusted (fail-closed), not merely warned about.

### F4 — absent-suite inference uses canonical packages on both sides

`_absent_suites_from_missing_ids()` (`:1189`, caller `:6393`) currently compares canonical
runtime keys (package already lost) against raw missing IDs; under F1 it must canonicalise both
expected and observed packages through the same module mapping, and record **why**
`partial_test_universe` was set (today it has four unrelated causes: `:4928`, `:5905`, `:6153`,
`:6399`).

### F5 — non-goals (tracked separately)

Cargo cross-crate identical raw IDs (parser-level); mixed-mode framework tagging in merged
reports; the orchestrator's threshold-based DAG resolution policy; Ginkgo's legacy package-less
runtime form beyond the fail-closed bridge.

## 6. Tests (new `tests/e2e/test_scoring_identity.py`; extend `test_module_aware_scoring.py`)

Identity: #24 jest pair; pytest pair (P2P no longer inherits a same-named F2F failure — scikit
M11 model); cargo `glob::tests::any1` vs `pathutil::tests::any1`; go_test unchanged; names
repeated across F2P/P2P/N2P/F2F/P2F buckets, not only within P2P; runtime-only namesakes.
Ginkgo: module-qualified baseline ↔ module-relative runtime matches; two packages with identical
hierarchies do **not** collide; package-less runtime + two expected packages → unknown;
package-less expected + two runtime packages → unknown; root-module equality, trailing slash,
false prefix boundary, multiple modules, `/testbed`, `/testbed/pkg`, empty package, no `::`,
extra `::`, both `>` and `/`. Java: one module-less runtime ID + two module-qualified expected
IDs → unknown; uppercase hash; non-Java `@abcdef` preserved.
Masking: missing expected ID paired with passed / failed / skipped / error / xfailed / xpassed
namesakes for each of F2P/P2P/N2P; duplicate observations with every precedence and permuted
input order (score and diagnostics invariant). `TestIdNormalizer` random-subtest interaction with
`dedupe_by_normalization`. Absent-suite with module-qualified expected vs relative runtime.
Frameworks: every supported value incl. `vitest`, `playwright`, `mocha`, `unittest`,
`django_runtests`, `nushell_script`, `None`, invalid string; dubbo M020 object-form config.
Mixed payloads: ginkgo+vitest, jest+playwright, cargo+nushell_script.
Re-tally: multiple PID artifacts, no unique old replay, missing `eval.json`, retry-directory
selection, stale candidate; preservation of unknown JSON keys, `error_message`, build/infra
metadata and resolution locks; filtered regeneration with canonical mismatch and multiple PIDs;
feedback/summary/filtered-summary coherence; rescore idempotence (twice → byte-identical).
Property on real fixtures: over `expected ∪ runtime` IDs across all buckets, the new key is
injective except for explicitly allow-listed equivalences.

## 7. Re-tally without re-execution (v2)

Test execution is unaffected by the defect: the raw report records every executed test by full
nodeid with its real outcome. Affected cells are corrected by re-running the **scoring** step on
the stored raw report — no container, no agent, no test execution. But the historical re-tally is
only trustworthy with the following procedure.

### 7.1 Replay selection (mandatory)

`evaluation_result.json` records neither the artifact PID nor a payload digest, and several
cells hold more than one `eval_summary.json` (local tree 65/3,526; log+retired 93/4,185; up to
five candidates). Therefore, per cell: replay the **old** scorer against every candidate payload
with pinned historical inputs; accept a candidate only if it **exactly reproduces** the stored
scoring projection; if zero or several candidates reproduce it, list the cell as
**non-replayable** (policy decision, Section 9) — never choose by timestamp or directory order.
Artifact/result disagreements (e.g. the 19 element-web `webset_replay` cells) are resolved by
this rule, not by declaring artifacts "the correct side".

### 7.2 Narrow pure function + envelope patching

Extract only the core tally (canonicalisation, indexing, lookup, category partition, counts,
missing/absent metadata) into a pure function used by production. For re-tally, **deep-copy the
stored JSON envelope and replace an explicit allow-list of score-owned fields**; never round-trip
through `EvaluationResult.from_result_dict()` (documented partial inverse, drops diagnostics).
Preserve patch state, Go/residue gates and locks, build/infra metadata, `partial_test_universe`
(cannot be safely recomputed), and unknown keys (`error_message`).

### 7.3 Derived outputs

`evaluation_result_filtered.json` is regenerated with a **frozen, hashed filter list**, the
**selected** artifact only, and `ran_test_ids` matched through the same identity layer; all
resolution locks preserved. `summary.json`, `summary_filtered.json` (retry keys) and
`feedback_report.md` (`orchestrator.py:1927`) are regenerated or explicitly marked stale
(policy, Section 9).

### 7.4 Invariants the impact run must assert

1. Selected artifact + pinned inputs reproduce the stored projection under the old scorer.
2. Raw totals and outcome-bucket contents unchanged.
3. Every expected ID has a trace: exact match / approved bridge / ambiguous-or-absent → unknown.
4. No runtime observation satisfies more than one expected identity.
5. Collision analysis over `expected ∪ runtime` IDs across all buckets.
6. Conservation: `F2P required = success + failure`; `N2P required = success + failure`,
   `missing ≤ failure`; `P2P required = success + failure + missing`.
7. Unexpected distinct-identity collisions → cell untrusted.
8. Permuting runtime outcome lists changes nothing.
9. Non-score JSON fields semantically identical except the allow-list.
10. Re-scoring twice → byte-identical normalized output.
11. Filtered JSON, aggregates, authoritative retry selection, feedback agree.
12. Baseline, config, filter, payload and scorer revisions hash-bound in a manifest.

### 7.5 Impact report

`SCORE_DELTA_issue24.md`: per repo — replayable / non-replayable cells; deltas for F2P success,
P2P failures, P2P missing, N2P achieved, N2P missing; resolved flips in **both** directions,
stated separately for strict persisted resolution vs filtered resolution; direction-(b)
prevalence by category and namesake outcome; per-cell list.

## 8. Rollout

0. **Implemented on branch `fix/issue24-scoring-id-identity`** (see commits there):
   - PR-0: `_infer_framework_from_test_config` accepts the legacy object-form config
     (dubbo M020 now resolves to `maven`).
   - PR-1: identity-preserving `normalize_scoring_nodeid` with a policy switch
     (`identity-v2` default; `legacy-prefix-drop` / `legacy-prefix-drop-passwins` for replay
     only); `_build_scoring_indexes` records identity collisions and marks an unexpected lossy
     key as scoring-untrusted; the core tally is the pure `tally_scoring()` used by production
     and by `harness/e2e/rescore.py`; `evaluation_result.json` carries a `scoring_identity`
     provenance block (policy, payload path + sha256, classification sha256, collisions).
     Tests: `tests/e2e/test_scoring_identity.py`, `tests/e2e/test_rescore.py`.
   - PR-2 (new-repo contract): `scripts/check_test_id_identity.py` + `docs/adding-a-repo.md` —
     framework must resolve; no duplicate raw IDs; identity key injective over every universe
     (Ginkgo merges reported as the bridge residual); optional expected ∪ runtime check; meant
     to run on repo onboarding and every dataset rebuild, with the dataset-build side
     (classifier) gating duplicate raw IDs too.
1. Ginkgo module mapping (F1a) and the two-sided bridge (F2) → follow-up issue, not this branch.
2. (superseded by 0)
3. Impact run → `SCORE_DELTA_issue24.md`; review.
4. Human-approved promotion per `docs/re-evaluation.md` (append-only backup; promote result
   **and** artifacts; sync summaries; record the flip).
5. Versioning: harness-only, zero image/data bytes → fold under the current version
   (`docs/versioning.md` "Hotfix"); release-notes line. Publish corrected cells (log corpus);
   refresh website data.

## 9. Decisions required from the owner

1. Non-replayable cells: rerun, exclude, or publish unchanged (and how denominators treat them)?
2. `feedback_report.md`: regenerate, preserve-and-mark-superseded, or keep as history?
3. Which resolution is authoritative for the corrected publication: strict persisted
   (`missing == 0`), threshold-based DAG, or filtered? (They differ today.)
4. Source of truth for the Ginkgo module mapping (navidrome has none in config today).
5. Whether to open the Cargo cross-crate identity issue now (parser change) or after #24.

## 10. Acceptance criteria

Section 6 tests for the frameworks in scope (the Ginkgo/Java bridge cases are deferred, see §0)
pass; existing suite green; invariants 7.4 hold on every replayable cell; `SCORE_DELTA_issue24.md`
produced from the impact run (data tree); scikit M11 nine cells and the #24 element pair behave as the
known positive controls; no cell outside the **old/new identity-difference set over expected ∪
runtime IDs** changes; every changed cell carries a manifest.
