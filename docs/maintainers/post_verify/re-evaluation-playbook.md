# Post-Verify — Re-evaluation Playbook

Run this before, during, and after any re-evaluation campaign (re-scoring
frozen agent artifacts after a data/image/eval-code repair, no agent re-run).
The protocol (rules, promotion) lives in `docs/maintainers/re-evaluation.md`; this file is
the **operational checklist and silent-failure catalog** — what actually goes
wrong in the field.

## The core hazard: silent degradation that survives direction-only comparison

Re-evaluation's declared method is "these pairs may change in this direction;
everything else must match." Its characteristic failure is a bug that
produces **plausible-looking wrong scores inside the declared scope**, so a
direction-only comparison blesses it. Every incident below passed a naive
"numbers moved the way we expected" check and was only caught by a preflight
probe or a full-batch sweep. Treat "the deltas look reasonable" as **not yet
verified**.

### Silent-failure catalog (each cost a full re-run)

| # | Signature | Root cause | How it hid | Catch |
|---|---|---|---|---|
| 1 | e2e report 0 suites, `Timed out … config.webServer` | 52 concurrent evals → host load storm → static webServer misses its 30 s window | failures landed on declared-poisoned tests → looked like legit flips | full-batch error-text cluster |
| 2 | `all predefined address pools have been fully subnetted` (×240/report) | docker bridge subnet pool exhausted by concurrency + **leaked networks** (RYUK disabled ⇒ killed evals never free their nets) | same — inside the poisoned pool; hit cells for *hours* outside the load spike | whole-time-range sweep + per-test final-outcome |
| 3 | N2P `required` explodes (go-zero M026: 17→222), score craters | `--workspace-root` = derived tree with no sibling `config/` → `test_framework=None` → `TestIdNormalizer` no-ops → Go random subtests stop collapsing to parent | numbers self-consistent across arms; "env drift" was a plausible story | preflight: check `required` vs primary on one random-subtest milestone |

**#3 now hardened** (`4e09cae`): `_resolve_test_framework` infers framework from the milestone test_config and fails loud when a baseline needs go_test normalization but it didn't resolve. Cross-repo normalizer drift (the deeper cause) tracked in DeepCommit-Env#30.

| # | Signature | Root cause | How it hid | Catch |
|---|---|---|---|---|
| 4 | A promotion "succeeds" but `collect_results` still reports the old score for some cells | `load_e2e_results(prefer_filtered=True)` reads **`evaluation_result_filtered.json` in preference to `evaluation_result.json`**. Promoting only the unfiltered file leaves a stale filtered file that shadows it — and the same asymmetry silently mis-states the *before* side of any comparison | every file you wrote is present and correct; the reader simply prefers a different one. Only 6 of 98 cells had a filtered file, so 92 cells corroborated the wrong conclusion | compare and promote through the **same** filtered-preference rule the reader uses; when the replay produced no filtered file but the destination has one, **delete the destination's stale filtered file** |
| 5 | An impact prediction says "nothing will change" against known-changed data | `calculate_score_v2` / `calculate_score_reliable` take the **full result dict**, not `result["test_summary"]`. Passing the inner dict makes every field default to 0 ⇒ every score computes as `1.0 × 1.0` ⇒ before == after everywhere | a uniform, self-consistent answer — every cell agrees, which reads as a clean result rather than a broken one | sanity-check the prediction against one cell you already know moved; a prediction that contradicts known data is the bug, not the finding |

**#4 / #5 (2026-08-19, element ENV-PATCH promotion).** Both were caught before
any wrong number was published — #5 because "0 cells will change" contradicted
measured data, #4 because the promotion procedure was re-read against
`collect_results`' actual preference before executing. The general lesson is one
rule: **read the record exactly the way the consumer reads it.** A comparison or
a promotion that uses a different file, a different field, or a different
preference than `collect_results` is not measuring the thing that gets published.

The pattern behind all three: **an environment/config gap makes the evaluator
emit a well-formed but wrong result, with no error raised.** Backlog for each:
the evaluator should fail-loud instead of silently degrading (webServer/pool →
`INFRA_FAILURE_PATTERNS`, done; `test_framework` unset → infer + fail-loud,
done `4e09cae`).

## Preflight (before launching the batch)

- [ ] **Image tag pinned & correct** — `SWE_MILESTONE_IMAGE_TAG` set to the
  repaired tag; `docker images` confirms the digest matches
  `manifests/digests-<ver>.tsv`.
- [ ] **`config/<repo>.yaml` reachable from workspace-root** — evaluator reads
  `test_framework` from `<workspace_root>/../config/`. If using a derived /
  de-pinned workspace, symlink the canonical `EvoClaw-data/config`. Missing ⇒
  Go subtest normalization silently off (catalog #3).
- [ ] **Anchor probe** — re-eval ONE milestone that has random-subtest or
  parameterized IDs; assert its `none_to_pass_required` equals the primary
  record. Mismatch ⇒ stop, a normalizer/config path is broken.
- [ ] **Concurrency budget** — each container is `--cpus N`; keep
  Σ(containers × N) ≲ 1.5 × cores, **and** active docker networks < ~31
  (bridge pool limit). e2e/testcontainers batches are network-bound, not just
  CPU-bound (catalog #1, #2).
- [ ] **Don't co-schedule CPU-dense and latency-sensitive batches** — a
  `go test`/`cargo` fan-out (CPU-dense, bursty) running alongside a playwright
  e2e batch (latency-sensitive: needs CPU promptly to start its webServer and
  drive browsers) starves the latter to a crawl — observed a `yarn build`
  taking 1.5 h at 6% CPU. Either run them in sequence, or `docker pause` the
  e2e containers (SIGSTOP freezes progress AND the test timers — monotonic
  clock, so no false timeout — and the in-container webServer freezes with
  them) while the CPU-dense batch finishes, then `docker unpause`.
- [ ] **EXPECTATION.md written** — declared (arm × milestone) scope + direction
  + data commit + image tag, per protocol.

## During the run

- Rolling pool, not all-at-once; stagger starts a few seconds to de-peak
  service startup (webServer, testcontainers).
- If RYUK is disabled (`TESTCONTAINERS_RYUK_DISABLED=true`, needed for the
  socket-mount path), **prune leaked networks periodically** — killed evals
  don't release theirs, and exhaustion snowballs into later runs.
- Monitor for terminal states, not just success: watch for the known infra
  signatures live; a silent stall with 0 containers ≠ done.

## Reconciliation (before believing any score)

1. **Full-batch, full-time-range signature sweep** over raw reports
   (`artifacts/*/eval_*.json`, `eval.log`) for every catalog signature — do
   NOT restrict to an observed incident window (catalog #2 bled for hours).
2. **Judge health per-test by final outcome**, not report size — retries can
   absorb transient infra errors (text present, outcome passed = harmless);
   only a *final-outcome* failure carrying a signature is a poisoned cell.
3. **Compare against the primary via `artifacts/*/eval_summary.json`**, not
   `tests_status` (its P2P successes aren't listed). Prefer
   `evaluation_result_filtered.json` both sides; the CLI eval path does NOT
   auto-generate filtered — run `generate_filtered_evaluation` over the batch
   first or the comparison silently drops the filter.
4. **Attribute every out-of-scope delta** to a named cause (prune semantics /
   env drift / flaky / incident-cell). "Direction looks right" is not
   attribution. Any unexplained delta ⇒ stop, do not promote.
5. **Exclude known incident cells** (e.g. capture-invalid `.git`-loss,
   empty-tar) by machine field (`snapshot_missing_count`), not by eye.

## Score preview & promotion

`monitor.sh --data-root <log>/reeval --detail <repo>` reads the mirror tree
directly; diff against the primary root for per-milestone deltas (aggregates
are meaningless pre-promotion — only re-evaluated cells have data). Promotion
is the explicit, human-approved 4-step in `docs/maintainers/re-evaluation.md` (append-only
backup → copy outputs → **sync summary.json** → SCORE_DELTA). Primary record
is never written before that.

## Cross-references

- `docs/maintainers/re-evaluation.md` — protocol, promotion procedure, monitor preview.
- `infra-failure-audit.md` — the signature-cluster method these sweeps use;
  its `INFRA_FAILURE_PATTERNS` is where confirmed signatures get promoted.
- `prune-config-authoring.md` — prune semantics behind fe-style out-of-scope
  deltas.
