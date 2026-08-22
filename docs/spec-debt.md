# Spec-debt ledger (SRS contract gaps found by evaluation evidence)

One row per **shipped or proposed SRS text change**. A row needs: the exact SRS file, the clause, the
evidence anchor (file + section/line in `SWE-Milestone-data/reeval/…`), the measured population the
clause addresses (with its denominator and what the population actually is — not what it would be nice
for it to be), and the status. SRS changes are forward-only: they never alter a published obligation;
they only change what future agents are told. Rows with "enumeration pending" are not shippable yet.

Status values: `shipped(<commit>)` · `proposed` · `enumeration pending` · `rejected`.

| # | SRS (data repo path) | Clause to add | Evidence anchor | Measured population (denominator) | Status |
|---|---|---|---|---|---|
| 1 | `BurntSushi_ripgrep_14.1.1_15.0.0/srs/maintenance_fixes_1_sub-01/SRS.md` | `SummaryKind::Quiet` retains its pre-existing semantics; the new behaviour is additive | `reeval/rgtraps_20260819/TRAPS.md` T4; `xmodel_calib_20260820/CALIBRATION.md` (Quiet) | 12/13 cells of glm-5.3 `_003` failed on the frozen sibling tests vs `_004` that deviated from the SRS; 3 ripgrep ids = 25.6% of all ripgrep P2P failure records in the 25-model calibration | shipped(`72bac10`, folded into v1.0.1) |
| 2 | `BurntSushi_ripgrep_14.1.1_15.0.0/srs/a6e0be3_1_sub-01/SRS.md` | `passthru` (the deciding symbol of `regression::r2094`) is named as an exported surface whose behaviour the descendant tests bind | `reeval/xuniv_20260820/pgb_B/FINDINGS.md` (ripgrep `r2094`) | 70/70 child cells fail `r2094` when the parent's shape is not reproduced; the parent's SRS mentions `passthru` 0 times | proposed (ship only if `passthru` is an exported symbol at the parent's END; else curation note) |
| 3 | `apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/srs/M003.2/SRS.md` | contract line for the Mutiny publisher/subscriber API shape that `M003.3`'s deferred tests bind | `reeval/xuniv_20260820/pgb_B/FINDINGS.md` L157-180 (dubbo `M003.2→M003.3`); `pgb_A/FINDINGS.md` natural experiment | 3 units; 12 cells lost solely to the binding; SRS-line cut: 18/42 → 0/12 (p = 0.0047) | proposed (promotion of the tests themselves is out of v1.0.2 scope — see spec §3.5) |
| 4 | `element-hq_element-web_v1.11.95_v1.11.97/srs/milestone_seed_be3778b_1/SRS.md` | FR3: state the retained rendering of the pre-existing surface that sibling snapshots freeze | `reeval/xuniv_20260820/element/FINDINGS.md` §(`be3778b_1`) | the `be3778b_1` owner-side conflicts in the census (element 91 ids total; this SRS's share to be stated from `census.json` before shipping) | enumeration pending |
| 5 | `zeromicro_go-zero_v1.6.0_v1.9.3/srs/M023/SRS.md` | name `(*Server).StartAsync` as the exported contract descendant tests bind | `reeval/xuniv_20260820/pgb_B/FINDINGS.md` L271-307 (go-zero `M023`) | `TestSqlxMetric`/`TestUnmarshalNullableSlice` family: 288/1,450 cells, 0 resolved flips (so the clause changes guidance, not published scores) | proposed |
| 6 | `zeromicro_go-zero_v1.6.0_v1.9.3/srs/M017/SRS.md` | none — `M017` already names the deciding symbol; the owner lacks feedback because its image does not compile | `reeval/xuniv_20260820/pgb_B/FINDINGS.md` L271-307 (go-zero `M017`) | n/a | rejected (not an SRS defect; image-side) |
| 7 | `nushell_nushell_0.106.0_0.108.0/srs/<each of the ten milestones other than core_development.2 among the 11 where `parse_block` arity deaths land>/SRS.md` | broadcast the `parse_block` arity retention clause that `core_development.2`'s SRS already carries (line 116) | `reeval/xuniv_20260820/nushell/FINDINGS.md` L290, L359-375; `pgb_A/FINDINGS.md` L140 | 56 cells across 11 milestones died on an **agent-invented** 6th parameter (C-class drift against a frozen call site readable in the agent's own tree; **not** an inherited hidden contract). The clause removes the temptation; its effect on future scores is not established | enumeration pending (list the ten SRS files from FINDINGS §6.2; one row each) |
| 8 | `nushell_nushell_0.106.0_0.108.0/srs/<each of G02_da9615f, G04_1ddae02, G04_ca0e961, G05_0b8531e, M02_parser, core_development.2>/SRS.md` | T6: `formats::to::md` `fragment`/`table` arity retention (mirror the forward clause `core_development.3`'s SRS carries, lines 107-112) | `reeval/xuniv_20260820/nushell/FINDINGS.md` L288, L336 | 46 cells died on the agent-invented variant; 0 latent cross-universe failures so far | enumeration pending (6 rows on shipping) |
| 9 | `nushell_nushell_0.106.0_0.108.0/srs/<descendants of core_development.2>/SRS.md` | broadcast the `is_windows_device_path` export clause `core_development.2`'s SRS carries (lines 252-256) | `pgb_A/FINDINGS.md` L142 | 48 cells; 8 of them are intra-trial regressions (the agent's later snapshot silently lost the `fn` and `pub use`) — the clause does not fix those | enumeration pending |

Notes

- The census's "27 avoidable ids" is an **id** count across repos; it is not the row count of this ledger.
  A row exists per SRS file actually edited, and its population column states what the evidence measured.
- Rows 7-9 correct v1's wording: the nushell report (`pgb_A` L133-151) attributes the principal losses to
  readable frozen-call-site drift and sibling traps, not to an inherited hidden contract.
- Evidence pointers are repeated in full on every row (no "same").
- The dubbo `M024` filter-list duplicate cleanup is not an SRS change and is tracked in the v1.0.2 spec §3.1, not here.
