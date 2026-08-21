#!/usr/bin/env python3
"""Re-tally stored evaluation cells under the identity-preserving scoring key
(issue #24) without re-running any test.

Examples:
  # Report only (no writes outside --out): every cell of one trial
  python scripts/rescore_cells.py \
      --data-root /data2/gangda/SWE-Milestone-data/scikit-learn_scikit-learn_1.5.2_1.6.0 \
      --trial-root /data2/gangda/SWE-Milestone-log/scikit-learn_scikit-learn_1.5.2_1.6.0/e2e_trial/_codex_gpt-5.6-sol_run_002 \
      --out /data2/gangda/SWE-Milestone-data/reeval/issue24/scikit_gpt56 --mode report

  # Also write corrected outputs under --out/mirror/<repo>/<trial>/<MID>/ for a
  # later human-approved promotion (docs/re-evaluation.md):
  ... --mode mirror

See harness/e2e/rescore.py for the replay-selection procedure and invariants.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.e2e.rescore import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
