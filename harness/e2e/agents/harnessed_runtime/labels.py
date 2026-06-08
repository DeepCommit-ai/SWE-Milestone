"""PR-label state machine (design §3.2) — pure, the coordination protocol.

`needs-code-changes` splits into `:R` (Reviewer-rejected → must re-review) and `:Q`
(QA-rejected → Dev's fix returns straight to QA, skipping Reviewer). `ready-to-merge` is
computed by the merge-gate, not hand-set; here it's just the terminal pre-merge state QA's
pass transitions toward.
"""

LABELS = [
    "evoclaw-task",
    "needs-review",
    "needs-qa",
    "needs-code-changes:R",
    "needs-code-changes:Q",
    "ready-to-merge",
]

# (current_label, actor, verdict) -> next_label
_TRANSITIONS = {
    ("needs-review", "reviewer", "approve"): "needs-qa",
    ("needs-review", "reviewer", "request-changes"): "needs-code-changes:R",
    ("needs-qa", "qa", "pass"): "ready-to-merge",
    ("needs-qa", "qa", "bug"): "needs-code-changes:Q",
    ("needs-code-changes:R", "dev", "fixed"): "needs-review",   # :R must re-review
    ("needs-code-changes:Q", "dev", "fixed"): "needs-qa",       # :Q skips Reviewer
}


def next_state(current: str, *, actor: str, verdict: str) -> str:
    """Return the next PR label, or raise ValueError if the transition is illegal."""
    try:
        return _TRANSITIONS[(current, actor, verdict)]
    except KeyError:
        raise ValueError(f"no transition from {current!r} by {actor} verdict={verdict!r}") from None
