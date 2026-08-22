# Maintainer docs

Documents for the people who maintain the benchmark itself (dataset, images, evaluator, published record).
Running the benchmark needs none of this; see [`docs/`](../README.md).

| document | what it is for |
|---|---|
| [re-evaluation.md](re-evaluation.md) | Re-scoring frozen trials after a data/image/evaluator repair without re-running agents; the explicit, human-approved promotion procedure |
| [post_verify/re-evaluation-playbook.md](post_verify/re-evaluation-playbook.md) | Operational checklist and silent-failure catalog for re-evaluation campaigns |
| [post_verify/infra-failure-audit.md](post_verify/infra-failure-audit.md) | Catching environment failures recorded as agent failures |
| [post_verify/prune-config-authoring.md](post_verify/prune-config-authoring.md) | Authoring and auditing residue-prune config for a dataset |
| [image-patching.md](image-patching.md) | Repairing a published evaluation image and releasing it as a patch version |
| [env-deps-verification.md](env-deps-verification.md) | Design draft: verifying environment dependencies declared by the dataset |
| [adding-a-repo.md](adding-a-repo.md) | The test-ID identity contract a new repo must satisfy, and the `scripts/check_test_id_identity.py` gate |
| [scoring-id-identity-fix.md](scoring-id-identity-fix.md) | Spec for the identity-preserving scoring key (issue #24) and the re-tally tool `harness/e2e/rescore.py` |

Local, git-ignored working notes (design specs and plans written by the planning workflow) live in
`docs/superpowers/`; they are not part of the repository.
