# Post-Verify — Infrastructure Failure Audit

Catch evaluation failures caused by the **environment** (container runtime
down, dependency resolver tears, toolchain/network incidents, env drift)
that were recorded as agent failures. Two layers of defense:

| Layer | Where | Catches |
|---|---|---|
| deterministic | harness detector (`INFRA_FAILURE_PATTERNS`, `harness/test_runner/core/test_executor.py`) + orchestrator retry + `scoring_untrusted` fail-closed | **known** signatures, at evaluation time |
| this skill | agent-run sweep over archived results | the **unknown tail**, after the fact; its confirmed findings get promoted into the deterministic layer |

If something can be decided purely by code, put it in the code layer.
This skill exists for what code cannot yet name.

## Core fingerprint (generalizes across languages)

**Agent failures are diverse; infrastructure failures are identical.**
The strongest single signal is the same error text — byte-identical or
near-identical after normalization — repeated across arms, milestones, or a
time window. Real incidents that all carried this fingerprint:

- element-web: ~122 failures × 5 arms, one string
  ("Could not find a working container runtime strategy");
- nushell G01: 23/23 archived eval logs byte-identical (cargo resolver);
- nushell core_development.4: 17/17 byte-identical (version pin);
- element-web re-eval 2026-07-11: 31 cells, one string ("Timed out waiting
  30000ms from config.webServer") — host-load storm at batch start;
- element-web re-eval 2026-07-12: 51/53 cells, one string ("all predefined
  address pools have been fully subnetted") — docker network pool exhausted
  by concurrency **plus** leaked networks (TESTCONTAINERS_RYUK_DISABLED means
  killed evaluations never release their networks; resource exhaustion
  snowballs across later runs until leaks are pruned).

Heuristic: cluster failures by normalized error text (strip paths, PIDs,
timestamps, hashes). Any cluster spanning ≥3 arms, or covering ≥80% of one
arm's failures for a milestone, is an infrastructure suspect.

## Inputs

- `EvoClaw-log/<range>/e2e_trial/<arm>/evaluation/<mid>/` —
  `evaluation_result.json`, `evaluation_error.log`, `feedback_report.md`,
  `artifacts/*/eval*.{json,log}` (first fatal error is surfaced in the
  top-level RuntimeError since the F-2b fix);
- `scripts/monitor.sh <trial> --detail` (💥/error status clusters);
- control probe: GT empty-overlay self-grade on the same image
  (see docs/re-evaluation.md) — if GT fails the same way, the agent is
  innocent by construction.

## Re-evaluation trap (learned 2026-07-12)

When a re-eval campaign *declares* a pool of tests that "may flip to real
outcomes", an infrastructure failure inside that pool masquerades as a
legitimate flip — direction-only comparison will bless it. Two rules:

1. Sweep the **whole batch, whole time range** for signature clusters before
   accepting any comparison; do not restrict the sweep to a known incident
   window (the 2026-07-12 pool-exhaustion hit cells for hours outside the
   observed load spike).
2. "Report is large / suites > 0" is not health. playwright's retries can
   absorb transient infra errors (text present, outcome passed — harmless),
   so judge by **final-outcome failures carrying the signature**, per test.

## Procedure

1. **Sweep**: per (range, milestone), collect failing test ids + first fatal
   error per arm.
2. **Cluster** by normalized error text; rank by arm-spread × size.
3. **Classify** each cross-arm cluster:
   - runtime/service unavailable → B3 infra incident (re-run lane, retry
     mechanism should have caught it — if it didn't, that is also a finding);
   - dependency-resolver / manifest tear → transfer lane (ENV-PATCH +
     conciliation hook; see repair_scope_spec D-1);
   - deterministic-but-wrong-vs-classification → env-drift lane
     (classification adjudication);
   - same wrong *implementation choice* across arms (not same error text) →
     name-lottery, srs lane — walk the taxonomy gates, do NOT auto-file.
4. **Verify one sample in-image** (worlds.md discipline: knowability only in
   the base image; binding/gold only in the milestone image).
5. **Route** per taxonomy and record in repair_scope_spec; **promote every
   confirmed mechanical signature into `INFRA_FAILURE_PATTERNS`** so the
   deterministic layer catches it live next time.

## Output

Candidate table — (range, milestone, arms, cluster signature, classification,
route, evidence paths). Read-only over all data: candidates go to
repair_scope_spec / issues, never directly into dataset files.
