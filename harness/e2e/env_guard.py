"""Reject legacy EVOCLAW_* environment variables (renamed 2026-07-08).

The rename to SWE_MILESTONE_* was a clean break — no silent aliasing. A
legacy var that silently stopped influencing behavior would be the worst
failure mode: an ignored EVOCLAW_IMAGE_TAG lets a run grade against the
wrong data version without a word. So any EVOCLAW_* found in the
environment refuses the launch with an exact migration hint instead.

Called from every entry point that reads configuration from the
environment: scripts/run_all.py (after .env/.env_private loading),
harness/e2e/run_e2e.py, and the image_version plan CLI.
"""

import os
import sys

LEGACY_PREFIX = "EVOCLAW_"
NEW_PREFIX = "SWE_MILESTONE_"


def reject_legacy_env() -> None:
    """sys.exit with a rename map if any legacy EVOCLAW_* var is set."""
    legacy = sorted(k for k in os.environ if k.startswith(LEGACY_PREFIX))
    if not legacy:
        return
    mapping = "\n".join(
        f"  {k}  ->  {NEW_PREFIX}{k[len(LEGACY_PREFIX):]}" for k in legacy
    )
    sys.exit(
        f"ERROR: legacy {LEGACY_PREFIX}* environment variables detected "
        f"(renamed to {NEW_PREFIX}* on 2026-07-08):\n{mapping}\n"
        f"Update your .env_private / shell profile, e.g.:\n"
        f"  sed -i 's/^{LEGACY_PREFIX}/{NEW_PREFIX}/' .env_private"
    )
