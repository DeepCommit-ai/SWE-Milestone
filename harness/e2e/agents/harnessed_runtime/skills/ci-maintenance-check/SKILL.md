---
name: ci-maintenance-check
description: Use when reviewing pull requests or code changes that touch build, tests, CI workflows, test runners, dependencies, or release safety; audits whether CI remains real, change-covering, unweakened, reproducible, secure, and locally green.
---

# CI Maintenance Check

The change author owns the project's CI signal. During review, audit whether the repository still has a
real, trustworthy build-and-test path for the changed code. Run these checks in the checked-out branch
and fold the result into your review verdict.

1. **Discover the CI entrypoint.** Identify how this repo expects CI to run: workflow files
   (`.github/workflows`, `.gitea/workflows`, `.gitlab-ci.yml`, etc.), CI wrapper scripts
   (`ci.sh`, `scripts/ci*`), `Makefile` targets, package scripts, or language-native build files
   (`package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, and similar). Prefer the documented
   one-command CI path when it exists.

2. **The pipeline exists and is real.** CI must actually build the project and run the relevant test
   suite for the affected area. It is not acceptable for CI to be a stub, an `echo ok`, a metadata-only
   check, or a command that silently no-ops when tools are missing.

3. **It covers this change.** If the PR adds or changes behavior, CI must build and test that code path:
   the affected package, crate, module, service, or integration path must be in scope. Check path
   filters, matrices, package selection, test patterns, and workspace exclusions for accidental or
   intentional gaps.

4. **It was NOT weakened to pass (the key audit).** REQUEST_CHANGES if you find any of:
   - build, lint, or test steps removed, commented out, narrowed, or scoped away from the changed code;
   - failures masked with `|| true`, `continue-on-error`, swallowed exit codes, `set +e`, or wrappers that
     always return success;
   - assertions deleted or weakened; tests skipped with `skip`, `only`, `#[ignore]`, `t.Skip`, `xfail`, or
     equivalent mechanisms to dodge failures;
   - the toolchain/PATH setup stripped so steps silently no-op.

5. **Important CI health dimensions are intact.**
   - Reproducibility: dependencies, lockfiles, tool versions, and generated artifacts are consistent
     enough that CI can run from a clean checkout.
   - Security: workflows do not expose secrets, print tokens, install untrusted code with elevated
     permissions, or grant broader permissions than the job needs.
   - Diagnostics: failures should be visible and actionable; timeouts, flaky-test quarantines, and
     retries need clear justification rather than hiding red builds.

6. **It is locally green on this head.** Run the repo's local CI equivalent when possible, or the closest
   build-and-test commands for the affected area. A green result only counts if it came from a real
   build+test run and the anti-weakening audit above still holds. If local execution is impossible,
   report the concrete blocker and review the CI definition more strictly.

Report concrete findings (cite the file + line). A weakened or unmaintained CI is itself grounds for
`REQUEST_CHANGES`, independent of the code change's correctness.
