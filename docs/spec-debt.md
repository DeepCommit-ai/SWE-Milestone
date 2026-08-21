# Spec debt ledger

One row per SRS (or other specification) change that shipped in a patch/minor release **without
re-running the agents whose published results it would have changed**. `docs/versioning.md`
("Hotfix" and "Impact analysis") requires the entry; this file is the record. Rows are append-only.

A spec change cannot be back-applied: the agent's decision is frozen in its submission. The ledger
exists so that anyone comparing a pre-change trial with a post-change trial on the same version
line knows the spec moved between them, and by how much the change was measured to matter.

| Version | Repo / milestone | Change | Measured effect | Evidence | Status |
|---|---|---|---|---|---|
| v1.0.1 | nushell `milestone_core_development.2` | FR3 API contract: `parse_block` 5-parameter freeze; NFR1 source-level compatibility | counterfactual re-eval: affected trials 13.4–15.4% → 55.3–56.7% reliable (4 trials converge within 1.41 pts); prospective glm-5.3: 14% → 53.72% | `reeval/contractfix_20260818/RESULTS.md` | shipped, agents not re-run |
| v1.0.1 | nushell `milestone_core_development.3` | FR4 exact `fragment`/`table` signatures | part of the above | same | shipped |
| v1.0.1 | nushell `milestone_G04_1ddae02` | `execute_xpath_query` four-parameter freeze | part of the above | same | shipped |
| v1.0.1 | nushell `milestone_G05_0b8531e` | IC1/IC2 folded into FR1/FR2 | part of the above | same | shipped |
| v1.0.1 (fold) | ripgrep `maintenance_fixes_1_sub-01` | FR1: keep `SummaryKind::Quiet` name and `quit_early()` membership | 3 new trials 3/3 comply; `summary::tests::quiet` 12/13 → 0 failures; no resolved flip (sibling traps remain) | data commit `72bac10`; `reeval/rgtraps_20260819/TRAPS.md` | shipped |
| v1.0.2 (planned) | nushell: 10 SRSs lacking the `cd.2` retention clause | replicate the frozen-call-site retention clause | ~150 dead cells attributed to agent-invented signature drift against uncontracted call sites | `reeval/xuniv_20260820/pgb_A/FINDINGS.md` | planned |
| v1.0.2 (planned) | dubbo `M003.2` | remaining bound shape (2-arg constructor, `protected subscription`, `onSubscribe(CallStreamObserver<?>)`, `startRequest()` flush, `subscribe()` → `request(1)`) | natural experiment on the first line: 18/42 → 0/12 (p=0.0047) | `reeval/xuniv_20260820/pgb_B/dubbo_FINDINGS.md` §6 | planned |
| v1.0.2 (planned) | ripgrep `milestone_seed_a6e0be3_1_sub-01` | `--passthru` interaction with the match limit | `regression::r2094` fails 70/70 child cells | `reeval/xuniv_20260820/pgb_A/FINDINGS.md` §1 | planned |
| v1.0.2 (planned) | element `milestone_seed_be3778b_1` | FR3: destructive list items, `%(brand)s` substitution | `DeleteKeyStoragePanel` snapshot 161/167 failing in descendants | `reeval/xuniv_20260820/pgb_B/element_FINDINGS.md` | planned |
| v1.0.2 (planned) | go-zero `M023` | `(*Server).StartAsync` carries the value into `CreateHttpHandler` | 99.2% of descendant cells die on it; 33/33 agents chose the other design | `reeval/xuniv_20260820/pgb_B/gozero_FINDINGS.md` | planned |
| v1.0.2 (planned) | owners of the 27 avoidable cross-universe ids | retention/contract lines per `census/CENSUS.md` appendix | see census | `reeval/xuniv_20260820/census/CENSUS.md` | planned |
