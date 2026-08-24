# Maintainer docs

Documents for the people who maintain the benchmark itself (dataset, images, evaluator, published record).
Running the benchmark needs none of this; see [`docs/`](../README.md).

| document | what it is for |
|---|---|
| [re-evaluation.md](re-evaluation.md) | Re-scoring frozen trials after a data/image/evaluator repair without re-running agents; the explicit, human-approved promotion procedure |
| [post_verify/re-evaluation-playbook.md](post_verify/re-evaluation-playbook.md) | Operational checklist and silent-failure catalog for re-evaluation campaigns |
| [post_verify/infra-failure-audit.md](post_verify/infra-failure-audit.md) | Catching environment failures recorded as agent failures |
| [post_verify/prune-config-authoring.md](post_verify/prune-config-authoring.md) | Authoring and auditing residue-prune config for a dataset |
| [residue-prune-spec.md](residue-prune-spec.md) | Design record of the residue prune (eval-tree reassembly semantics) that `harness/e2e/residue_prune.py` implements |
| [image-patching.md](image-patching.md) | Repairing a published evaluation image and releasing it as a patch version |
| [env-deps-verification.md](env-deps-verification.md) | Design draft: verifying environment dependencies declared by the dataset |
| [adding-a-repo.md](adding-a-repo.md) | The test-ID identity contract a new repo must satisfy, and the `scripts/check_test_id_identity.py` gate |
| [scoring-id-identity-fix.md](scoring-id-identity-fix.md) | Spec for the identity-preserving scoring key (issue #24) and the re-tally tool `harness/e2e/rescore.py` |
| [v1.0.2-release-plan.md](v1.0.2-release-plan.md) | v1.0.2 plan: cross-universe conflict filters (#25), reference-unachievable and undisclosed-prerequisite filters (#23, #26), forward-only SRS fixes, the conditional crash-hidden re-evaluation; harness changes R1–R3, commands, expected deltas, maintainer decisions |

Local, git-ignored working notes (design specs and plans written by the planning workflow) live in
`docs/superpowers/`; they are not part of the repository.
