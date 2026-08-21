# Adding a repo: the test-ID identity contract

The evaluator scores a cell by matching the baseline classification's test IDs
against the runtime report's test IDs. The matching key is the test's
**identity**: two IDs that map to the same key share one outcome. Since the
fix for issue #24 the key is the raw ID for every framework except two
documented exceptions (Maven/Gradle fold JVM hashcodes; Ginkgo bridges the Go
module prefix). That only works if the dataset keeps one promise:

> **One parser, one ID dialect, both sides.** The IDs in
> `test_results/<MID>/<MID>_classification.json` and the IDs the evaluator's
> runtime report emits must be produced by the same parser in the same,
> fully-qualified form. The scorer never repairs representation gaps.

The scorer needs no per-repo change. What a new repo must satisfy:

1. **Framework resolves for every milestone.** `dockerfiles/<MID>/test_config.json`
   is a mode list with a `framework` field (or a command the inference
   recognises). A milestone whose framework resolves to `None` is scored with
   the identity key and a warning, but it is a dataset defect — fix the config.
2. **IDs are fully qualified and stable.** The parser for the framework emits
   file/module/package + test name + parameters; the form is identical at
   classification time and at evaluation time; container roots such as
   `/testbed/` are stripped **in the parser** (jest/vitest/playwright already
   do this), never in the scorer; IDs do not embed run-varying text (object
   addresses, random suffixes) unless an explicit normalizer handles them
   (`harness/utils/test_id_normalizer.py` for go_test random subtests).
3. **No duplicate raw IDs in a universe.** A duplicate means the parser lost
   identity before scoring (the known case: Cargo emits only the test name, so
   two crates with the same full test name are indistinguishable). Fix the
   parser or split the universe; the scorer cannot recover it.
4. **Identity key injective.** Over every universe's IDs (all buckets), and
   over expected ∪ runtime IDs for at least one golden END run, the scoring key
   maps distinct IDs to distinct keys. Ginkgo merges are reported as the known
   bridge residual until the package-aware key lands.
5. **Bridges are explicit and two-sided.** If a framework genuinely needs
   representation bridging, add a validated mapping to the repo config and a
   fail-closed bridge (exactly one owner on both sides); prefer fixing the
   parser so no bridge is needed.
6. **Mixed test modes keep their framework tags.** Backend Ginkgo + frontend
   Vitest under one framework label is a known gap; do not introduce new mixed
   universes until per-mode tags survive the merged report.

Run the check when the repo is added and on every dataset rebuild:

```bash
python scripts/check_test_id_identity.py --data-root /data2/gangda/SWE-Milestone-data/<repo_key> --json report.json
# optional: include one runtime report in the injectivity check
python scripts/check_test_id_identity.py --data-root ... --runtime-payload <cell>/artifacts/<pid>/eval_summary.json
```

Exit status 1 means a hard failure (items 1, 3, 4 for non-Ginkgo frameworks).
The dataset-build side (DeepCommit-Env `test_runner/core/classifier.py`) should
fail or flag duplicate raw IDs instead of overwriting them, so both ends of the
contract are gated.

Background and the full fix design: `docs/scoring-id-identity-fix.md`.
