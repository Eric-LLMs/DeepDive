"""Backend: deterministic margin rules over Recall candidates.

The transition stub of the cost ladder (§3 Node 3, "重构过渡期先让漏斗转起来").
It inherits the leader-vs-runner-up margin discipline the cosine corpus proved,
but with the CORRECTED exit: a too-close race is UNCERTAIN (escalate upward to
the Decision LLM), never a silent abstain-to-Agent (design §5: this is the
core semantic fix of the refactor).

Single-candidate races confirm when the candidate carries a trustworthy
provenance: a calibrated cosine (origin="recall") or a deterministic table
hit (origin="matcher_hit", score 1.0 by construction). Matcher-AMBIGUOUS
escalations (origin="matcher_ambiguous", score=0) are inherently "two table
patterns claiming the turn" — the stub never resolves those, it escalates.

Extraction power is zero by design: the stub answers WHICH, never WITH WHAT.
Its verdicts carry ``arguments=None``, so a stub-routed turn with a schema'd
capability exits BIND_MISSING to the Agent — the honest consequence of
pinning the ladder at the deterministic end.
"""
from __future__ import annotations

from ..contract import (
    TOOL_INTENT_CONFIDENT,
    TOOL_INTENT_REJECT,
    TOOL_INTENT_UNCERTAIN,
    ToolIntentVerdict,
)

_TRUSTED = ("recall", "matcher_hit")


def evaluate(candidates, *, margin: float) -> ToolIntentVerdict:
    if not candidates:
        return ToolIntentVerdict(TOOL_INTENT_REJECT, None, "no candidates")
    ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
    head = ranked[0]
    if len(ranked) == 1:
        if head.origin not in _TRUSTED:
            # only a table-ambiguous candidate: no score to trust — escalate
            return ToolIntentVerdict(TOOL_INTENT_UNCERTAIN, None, "matcher_ambiguous without scores")
        return ToolIntentVerdict(TOOL_INTENT_CONFIDENT, head.capability_id,
                           "single trusted candidate")
    second = ranked[1]
    if head.origin in _TRUSTED and second.origin in _TRUSTED \
            and head.score - second.score >= margin:
        return ToolIntentVerdict(
            TOOL_INTENT_CONFIDENT, head.capability_id,
            f"margin {head.score - second.score:.3f} >= {margin}",
        )
    return ToolIntentVerdict(TOOL_INTENT_UNCERTAIN, None, "race too close / mixed provenance")
