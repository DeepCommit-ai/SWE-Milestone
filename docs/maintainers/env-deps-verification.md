# Environment-Dependency Verification (design v3, DRAFT)

Status: **design draft — not implemented**. v2 was produced after a 3-lens
adversarial review; v3 after an independent Codex (gpt-5.6-sol, max effort)
review of v2 confirmed 3 blockers and a better factoring (§10 review log).
Motivated by the element-web `html-react-parser` incident: SRS documents
declared env dependency changes, most eval images didn't honor them, the
harness installs nothing at eval time, 22+ released cells silently zeroed.

**What this verifies (honest scope):** this is a **deficit verifier**. It
proves the eval environment is *not missing* anything the milestone's
environment contract requires (declared additions/upgrades, test-required
packages, hand-designated presences). It does **not** prove full
environment equality: upstream *removals* are not enforced as absence
(supersets are policy-acceptable), unpinned declarations check presence
only, and system packages check presence only. Those weakenings are stated
here, at the top, not discovered in a table.

## 1. Principle: producer / verifier separation

- **Producer** (data side): the milestone Dockerfile + offline closure must
  make the eval image satisfy the environment contract. Every fix is
  data-side (image/closure/policy) + a benchmark patch release (vX.Y.Z,
  digests-manifest diff proves what changed). 
- **Verifier** (harness side): mechanical, fail-closed checking only.
  **Never installs, never repairs.**

Trust model (v3, simplified): expectations are **derived at check time**
from SRS by a versioned, fixture-tested extractor, plus a small committed
file of *typed exceptions*. There is no bulk manifest to trust or to drift
— the former "manifest is a cache" awkwardness is gone because the cache is
gone. The actual side is always a runtime probe of image-owned state.

## 2. Expectations: derived core + committed exceptions

```
srs/<mid>/SRS.md                     # prose contract — unchanged, authoritative
dockerfiles/<mid>/env_deps_overrides.yaml   # NEW, small, only when needed
```

The expectation set for a milestone =

1. **Derived-from-SRS core (~95 % of lines, zero authoring):** the strict
   extractor parses the env section (case/level-insensitive heading match on
   "Environment Dependency Changes"; 6 base regexes + scoped-npm last-`@`
   split + `from X to Y` variant + subsection-heading→ecosystem map).
   Nothing about these lines is committed anywhere — the extractor is the
   single implementation, versioned in the harness, unit-tested against the
   census fixtures, with a per-repo derived-count canary test that trips on
   silent grammar drift.
2. **Committed typed exceptions** (`env_deps_overrides.yaml`, schema below):
   - `waivers`: one per SRS line the grammar genuinely rejects.
     `reason` is an enum of extractor failure classes (`prose-only`,
     `unpinned-version`, `wildcard-removal`, `arrow-form`, `table-row`,
     `quarantine-override`, `system-unversioned`). A waiver whose `text`
     parses cleanly = FAIL ("promote to derived"). Lines are identified by a
     **normalized-text fingerprint** (sha256 of canonicalized text), never
     line numbers (editorial SRS edits must not churn identities).
   - `test_requires`: packages required by the milestone's GT test tree but
     declared nowhere (the `e9a3625_sub-02` class — its SRS never mentions
     `html-react-parser`). **Not hand-asserted** (v3, Codex blocker):
     derived by a deterministic authoring scan of the END-tag test tree's
     import statements vs the base dependency set (import-name →
     distribution-name mapping table per ecosystem; dynamic imports land in
     a typed waiver), committed, and `--static` re-runs the scan and
     requires exact reconciliation. Where an ecosystem's scan can't be made
     deterministic, the doc narrows the guarantee for that ecosystem
     explicitly instead of pretending.
   - `probe_policy`: per-entry overrides — `probe: none` restricted to
     enumerated, statically verifiable categories each carrying evidence
     (`vendor-not-overlaid` ripgrep `/opt/vendor`; `anticheat-pruned` dubbo
     self-GAVs; `pinned-by-closure` toolchains; `graph-only-module`);
     `require-present` (CTE-critical package kept despite upstream removal —
     e.g. `linkify-element`; requires explicit policy sign-off because it
     interacts with the adjudicated element CTE case); `consumer_roots`,
     `store_root`, `build_required`, `tool_module` fields.

Runtime-relevant waivers are **not silent**: they surface in `--static`
output as per-line WARNs, in the release gate as counts, and in the per-cell
preflight persistence as `waived: [...]` so a cell's coverage is inspectable.

Schema discipline (v3): a strict published JSON Schema before any authoring;
`sources` as an array (a package can be SRS-declared *and* test-required);
unknown keys and duplicate entries rejected; YAML duplicate-key rejection.

`--audit` (image-vs-base store diff) is **demoted to an optional forensic
command**: its output is *candidates for human policy decisions*, never
auto-promoted expectations (an expectation generated from whatever bytes an
image happens to contain would be self-certification). The Dockerfile lint
likewise stays a best-effort authoring WARN with no claim about image bytes
(shipped `8bb4d44` v1.0 contains a hand `docker commit` layer).

## 3. `scripts/verify_env_deps.py`

```
verify_env_deps.py --repo <substring> [--milestone <mid>] [--data-root <path>]
                   [--static] [--probe] [--audit]      # repeatable, combinable
                   [--mode authoring|release]          # named bundles
                   [--allow-missing-overrides <repo,…>]  # loud, recorded escape
                   [--keep] [--jobs N]
```

Exit codes: `0` = all checks consistent; `1` = contract mismatch (a real
deficit/drift); `2` = verifier/infrastructure error (could not observe —
never conflated with a pass *or* a contract failure).

### 3.1 `--static` (no Docker)

1. Extract per milestone; reconcile against overrides: every SRS env line ⇒
   exactly one derived entry or one typed waiver (fingerprint match);
   parseable-line-in-waiver = FAIL; waiver-without-line = FAIL (stale).
2. Re-run the `test_requires` derivation scan; committed entries must equal
   scan output exactly.
3. Coverage is a **derived fact**: a milestone with ≥1 parseable declaration
   or non-empty test-requires scan and a missing/required-but-absent
   overrides state = FAIL (no self-declared "complete" markers anywhere).
   Rollout escape: `--allow-missing-overrides`, loud per invocation.
4. Variant/absent SRS sections (go-zero M004 prose H2; M006/M022/M023 none)
   are *seen* and dispositioned (waiver or FAIL), never silently empty.

### 3.2 `--probe` (eval machine)

1. Compose the exact eval image via `ensure_offline_evaluation_image()`;
   launch the ephemeral container through the same start/env path
   `start_container` uses.
2. **One `docker exec` per store kind**, running a generated single-pass
   inventory script (element milestones can carry 100+ entries; per-entry
   execs and per-entry tree walks don't scale). Probe results are cacheable
   keyed by `offline_cache_effective_image_id` + extractor version +
   overrides sha (immutable stores ⇒ reusable verdicts).
3. Tri-state per entry: `present` / `MISSING` / `PROBE-ERROR`; all-entries
   PROBE-ERROR aborts the run (exit 2) — a broken probe environment must
   never read as verified.
4. Cost honesty: derived images are GB-scale on cold machines (dubbo ≈4 GB);
   `--probe` belongs on the eval machine; `--jobs` throttles cold builds;
   old derived images must not be pruned while resumable trials exist.
   Before per-cell enablement, benchmark P50/P95 on element + dubbo and add
   real-container smoke tests (mock-only tests cannot validate resolver
   layouts or timing).

### 3.3 Wiring (CI reality)

The data repo is a HuggingFace dataset — no CI. Real wiring: authoring gate
(`--mode authoring` = static+probe, FAIL blocks image release); release
runbook step 5 (standing of `verify_image_digests --local`); harness CI
scheduled + dispatch job fetching only `**/srs/**` + `**/dockerfiles/**`
from HF (`allow_patterns`) pinned to the benchmark tag, running `--static`.

## 4. Evaluator preflight (per-cell gate)

### 4.1 Placement: **pre-snapshot** (v3, Codex finding)

Store probes run **after `start_container()` (milestone-end checkout,
image-owned `/testbed`) and *before* `apply_patch()`** — not post-apply as
in v2. Two reasons, both verified:

- **Spoofing:** the snapshot filter has no inherent `node_modules`
  exclusion (`src_filter.py`), and Node resolution walks up from the
  importing file — an agent-shipped `src/node_modules/<dep>/` would satisfy
  (and thus mask) a post-apply probe. Pre-snapshot, `/testbed` is
  image-owned; attribution is clean: a deficit is an *environment* defect
  by construction, agent-independent.
- **Parity is unobtainable post-apply anyway:** tests inject per-exec env
  (`_go_exec_env`: private modfile, GOFLAGS) that no `docker exec printenv`
  can observe. Instead of chasing it, v3 factors **one evaluation-exec
  adapter** (env, cwd, user) shared by the test runner and any exec-class
  probe (env-var, toolchain), and documents residual parity limits.

What post-snapshot hooks affect is handled by **reuse, not duplication**
(Codex finding): the dubbo/go-zero hooks edit manifests, not stores; the Go
*graph* question ("is module X selected?") is answered by the existing Go
module closure, which already runs `go list` against the real evaluation
graph and fails closed via `go_module_test_graph_contract_error` →
`resolution_locked_false`. Raw `@v/*.mod` cache presence is reported only as
`available_offline`, never claimed as graph membership.

### 4.2 Trust chain

At trial launch, run_e2e **freezes bytes, not digests** (v3; exactly the
`repo_config_binding` model): the raw SRS env-section bytes per milestone +
the overrides file bytes + the extractor version are hard-linked/copied
atomically under the trial root, sha256-recorded in `trial_metadata.json`,
symlinks rejected. The preflight parses **only those frozen buffers**
(TOCTOU-free; resume-stable across legitimate data edits; re-eval replays
read the same frozen bytes). Missing-coverage and drift checks re-derive
in-process from the frozen SRS bytes (<10 ms).

### 4.3 Verdict plumbing (v3, Codex blocker)

There is no `EvaluationResult` before `compare_results()` — v2's "set a
field and return the result" was unimplementable. v3 specifies a
**baseline-aware infra-invalid result factory**:
`make_env_invalid_result(baseline, reason)` builds the result skeleton from
the already-loaded classification (required counts populated, zero achieved,
`infrastructure_failure = "env-deps preflight: <name>@<ver> missing
(<store>)"` — a string, the F-2a shape; `infra_invalid_reason` stays owned
by `classify_zero_test_result`). Used by: the in-`evaluate()` preflight path
(container still alive, before the `finally` cleanup), the exception
finalizer, and `run_milestone`'s fallback (whose current handler builds a
required-counts-0 result with no infra fields — that gap gets the same
factory). One factory, three call sites, no duplicated construction.

Preflight verdict is first-class, machine-consumed (v3):

```json
"env_deps_preflight": {"status": "ok|deficit|probe-error|drift|unchecked",
                        "srs_sha256": "…", "overrides_sha256": "…",
                        "extractor_version": "…", "entries": N,
                        "missing": [], "probe_errors": [], "waived": [],
                        "scope": null}
```

`deficit`/`probe-error`/`drift` all set `infrastructure_failure` (the
existing `scoring_untrusted` channel — verified to lock `resolved=False`
through orchestrator, run_milestone, re-eval CLI, and resume). `unchecked`
(escape hatch `SWE_MILESTONE_ENV_DEPS_CHECK=off | off:<mids> | off:repo:<r>`,
scope recorded) is enforced non-promotable **in code**: collect_results and
the promotion tooling read the persisted block — not a documentation rule
(doc-only rules are not machine-enforced; and the orchestrator's live
summary must carry infra-invalid through instead of degrading it to
`eval_status: "error"`). Deterministic preflight reasons are added to a
non-transient list so the orchestrator doesn't burn snapshot-chain retries.

## 5. Probe catalog (image-owned state, single-pass, read-only)

| Kind | Probe |
|---|---|
| npm — consumption claims (`added`, `test_requires`, `require-present`) | **Node resolver semantics, not filesystem inventory** (v3, Codex blocker): `createRequire(<consumer_root>/x.js).resolve("<name>/package.json")`, version of the *resolved* copy must match. Default consumer root `/testbed` (navidrome ui: `/testbed/ui` via `consumer_roots`). A nested inaccessible copy must not pass; a hoisted resolvable copy must not fail |
| npm — store-composition claims (transitive `upgraded`/`downgraded`) | single-pass tree inventory: declared version ∈ collected `*/node_modules/<name>` set (these packages aren't imported directly; the claim is about what the lockfile-built store contains) |
| npm-global | `/usr/local/lib/node_modules/<name>/package.json` |
| pip | `importlib.metadata.version` |
| go | selection: reuse closure graph verdict (§4.1); offline availability: `@v/<escaped-ver>.mod` with `module.EscapePath` semantics (uppercase → `!`+lowercase) — reported as `available_offline` |
| go-tool | `command -v <bin> && <bin> version` under the evaluation-exec adapter; optional `tool_module` fallback |
| maven | GAV path `test -d` (explicit GAV encoding rule in schema) |
| cargo raw-cache (nushell) | `ls …/cache/index.crates.io-*/<name>-<ver>.crate` |
| cargo vendor (ripgrep) | `probe: none (vendor-not-overlaid)` — producer fix (extend `cache_paths`) if ever needed at eval |
| toolchain | closure-pinned repos: check against closure policy value or `probe: none (pinned-by-closure)`; never against SRS prose the harness deliberately overrides |
| env-var | evaluation-exec adapter env; quarantine-managed vars (GOPROXY, GOTOOLCHAIN, GOMODCACHE, GOCACHE, GOFLAGS, GOSUMDB, PATH) are excluded by list — their SRS lines waive as `quarantine-override` (anti-cheat wins by design) |
| system | `dpkg -s` presence-only (stated weakening) |
| browser / workspace | presence probe or `probe: none` + typed reason; static-only for workspace-manifest classes |

Standing image caveats (dubbo anticheat-pruned self-GAVs, etc.) are typed
`probe: none` categories with evidence — never passing lies, never free text.

## 6. Policy summary

0. **CTE conditional rule (USER-DECIDED 2026-08-17):** an *environment error*
   (module/import resolution failure at test collection — the suite never
   runs) in ANY milestone's eval is an environment defect, even when the
   package belongs only to an *upstream* milestone's contract: agents that
   legitimately carried forward upstream work must never hit
   `Cannot find module` for packages inside the benchmark's own dependency
   closure. Remedy: each milestone's eval env must satisfy the **CTE
   dependency envelope**. AMENDED 2026-08-17 (user-approved, element Phase-4
   audit): the envelope is **closure-wide presence** for signature packages
   — agents' real execution order is not confined to DAG-ancestor paths
   (6 observed non-ancestor execution-order cells), so ancestor-union
   under-covers; packages that have produced environment-error signatures
   are guaranteed present in EVERY milestone image of the repo.
   **Presence semantics (CORRECTED 2026-08-18, build-time incident):** the
   envelope guarantees *a resolvable compatible version*, never an exact
   pin — a milestone whose own contract pins a different version keeps its
   own pin (own contract wins; exact-pin-everywhere is pure golden risk
   with zero benefit when the failure class is absence). Element's actual
   signature list after correction: `html-react-parser@5.2.2` (real
   deficit) and `linkify-element` (presence — already satisfied in every
   v1.0 image; the deficits were v0.9x-era). `compound-design-tokens` is
   **retracted as a signature package**: the 22 swept "compound signatures"
   were agent-authored imports of icons that exist in NO version
   (`eye-off`/`trash` vs real `visibility-off`/`delete`) — behavioral
   failures, out of scope; compound 4.0.0→4.0.1 remains only an
   own-contract drift item (e662c19/7ff1fd2), decoupled from any re-eval.
   Golden-baseline invariance stays the hard gate on every addition. Affected cells are re-evaluated,
   selected by failure *signature uniformly across all arms* (kimi/opus
   included), gated on golden-baseline invariance. *Behavioral* CTE
   contamination (tests run, assertions differ — the adjudicated flex-wrap
   class) stays under the existing adjudication: scores kept, no re-eval.
1. Deficit verification only (top-of-doc scope statement governs).
2. Version matching: exact after per-ecosystem normalization; unpinned ⇒
   presence-only, recorded.
3. Fail-closed: unparseable overrides → FAIL; parseable-in-waiver → FAIL;
   uncovered declaration → FAIL; probe-error ≠ pass; missing coverage is a
   derived fact, all escapes are per-invocation and recorded.
4. The verifier never installs; remedies are data-side + patch release.

## 7. Incident replay (v3)

| Defect | Caught by |
|---|---|
| sub-03 / 05df321 / maintenance_bug_fixes: SRS-declared dep missing from image | derived-core probe (authoring + per-cell) |
| sub-02: tree imports it, SRS silent | `test_requires` — **mechanically derived + reconciled**, no longer a manual assertion |
| 8bb4d44: image adds undeclared linkify-element | Dockerfile lint / `--audit` → **identify + report only** (USER-DECIDED: SRS-incompleteness hygiene defect; SRS/overrides amendment, never a score action) |
| downstream CTE cells (f59af37, fba5938, sub-01, 599112e, aa99601, e662c19) | **CTE envelope rule (§6.0, USER-DECIDED)**: environment-error signature ⇒ fix images to the envelope + re-eval, uniformly across all arms |
| 26+ released cells silently zeroed | preflight → `infrastructure_failure`, non-promotable, non-transient |
| 3f47487 START-vs-END wrong-vintage tree | **not covered in v1 scope** (honest); authoring-time option: lockfile-vs-store diff during `--mode authoring` |
| "No changes detected." + drifted env | out of scope (full lockfile-completeness, future) |

## 8. Testing

Real-container smoke tests are **required** before per-cell enablement
(resolver-layout and timing cannot be mocked); mock suites cover: extractor
fixtures (census tail incl. M004 prose, scoped-npm, from-to, `!`-escaping),
waiver-abuse FAIL, coverage-derived-fact FAIL, fingerprint stability under
SRS editorial edits, result-factory field contract on all three call sites,
frozen-bytes verify + symlink rejection, scoped escape recording,
non-transient classification, collector/promotion consumption of
`env_deps_preflight.status`. Shipped-artifact canary pins per-repo derived
counts.

## 9. Rollout

Element-web first (holds every incident class), one benchmark patch release
per repo batch. Authoring burden after v3's derive-don't-duplicate: ~25
waivers + per-repo test-requires scans + probe-policy entries — not 1,303
rows.

## 10. Review log

**v1→v2** (3-lens adversarial review, 30 findings, all CONFIRMED): see git
history of this file.

**v2→v3** (independent Codex gpt-5.6-sol max-effort review; blockers
verified against code by hand): (1) `test_requires` was a trusted manual
assertion → now derived + reconciled; (2) npm anywhere-in-tree probe could
pass an unresolvable copy → resolver semantics from consumer roots, with
inventory semantics only for transitive store-composition claims; (3) no
`EvaluationResult` exists at the preflight point and `finally` kills the
container before outer handlers → baseline-aware infra-invalid result
factory used by all three call sites. Majors: digest-freeze → bytes-freeze
(repo_config_binding model); pre-snapshot probing (agent-spoofable
post-apply store + unobservable per-exec env); Go graph verdict reused from
closure instead of `.mod`-cache claims; typed waiver enums with runtime
visibility (fail-open free-text `probe:none` closed); `image-audit` demoted
to forensic; first-class machine-consumed validity status (doc-rules alone
don't enforce; orchestrator summary degradation fixed); honest
"deficit verifier" scope statement; strict JSON Schema + fingerprint
identities (no line-number churn); CLI flags made combinable + exit 0/1/2;
derive-at-check-time replaces the 1,303-row committed manifest; probe-cost
caching + real-container smoke tests.

**Adjacent findings to file separately (both hand-verified):**
1. `evaluator.py` `from_result_dict` reads `go_module_closure` at top level
   (:1517) but `to_dict` nests it under `evaluation_environment` (:1462) —
   resolution lock silently lost on resume ingestion.
2. The snapshot filter admits `src/node_modules/**` (no inherent exclusion
   in `src_filter.py`), and Node resolution walks up from the importing
   file — an agent can vendor packages inside `src/` and have eval resolve
   them. Independent of this design: a potential masking/cheat vector worth
   an explicit snapshot-filter exclusion + residue-prune rule.
