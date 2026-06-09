---
name: ci-maintenance-check
description: Reviewer skill — audit whether the Dev genuinely maintains the project CI pipeline (ci.sh + .gitea/workflows/ci.yaml) and has not weakened it to make a PR pass.
---

# CI Maintenance Check (Reviewer)

The Dev owns and must continuously maintain the project's CI pipeline. When you review a PR, audit that
ownership. Run these checks in the checked-out branch and fold the result into your review verdict.

1. **The pipeline exists and is real.** `ci.sh` (repo root) and `.gitea/workflows/ci.yaml` are present;
   the workflow runs `ci.sh` on push/PR. `ci.sh` must actually **build the project AND run its test
   suite** for the affected area — not a stub, not `echo ok`.

2. **It covers this change.** If the PR adds or changes behavior, CI must build and test that code path
   (the right package/crate/module is in scope, not excluded).

3. **It was NOT weakened to pass (the key audit).** REQUEST_CHANGES if you find any of:
   - test or build steps removed / commented out / scoped away from the changed code;
   - failures masked: `|| true`, `continue-on-error`, `--no-fail-fast` used to hide red, `set -e` removed;
   - assertions deleted or weakened; tests skipped / `#[ignore]` / `t.Skip` / `xfail` added to dodge them;
   - the toolchain/PATH setup stripped so steps silently no-op.

4. **It is actually green on this head.** The `ci/build` commit status for the current head is `success`,
   and that green came from a real build+test run (cross-check against point 3).

Report concrete findings (cite the file + line). A weakened or unmaintained CI is itself grounds for
`REQUEST_CHANGES`, independent of the code change's correctness.
