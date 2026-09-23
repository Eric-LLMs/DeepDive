"""Judge backend: deterministic margin rules over Recall candidates.

The transition stub of the cost ladder (§3 Node 3, "重构过渡期先让漏斗转起来").
It inherits the leader-vs-runner-up margin discipline the cosine corpus proved,
but with the CORRECTED exit: a too-close race is UNCERTAIN (escalate upward to
the Decision LLM), never a silent abstain-to-Agent (design §5: this is the
core semantic fix of the refactor).

Single-candidate races only confirm when the candidate carries a calibrated
cosine score; Matcher-AMBIGUOUS escalations (origin="matcher_ambiguous",
score=0 by construction) are inherently "two table patterns claiming the turn"
— the stub never resolves those, it escalates.
"""
from __future__ import annotations

from ..contract import JUDGE_CONFIDENT, JUDGE_REJECT, JUDGE_UNCERTAIN, JudgeVerdict


def judge(candidates, *, margin: float) -> JudgeVerdict:
    if not candidates:
        return JudgeVerdict(JUDGE_REJECT, None, "no candidates")
    ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
    head = ranked[0]
    if len(ranked) == 1:
        if head.origin != "recall":
            # only a table-ambiguous candidate: no score to trust — escalate
            return JudgeVerdict(JUDGE_UNCERTAIN, None, "matcher_ambiguous without scores")
        return JudgeVerdict(JUDGE_CONFIDENT, head.capability_id, "single recall candidate")
    second = ranked[1]
    if head.origin == "recall" and second.origin == "recall" and head.score - second.score >= margin:
        return JudgeVerdict(
            JUDGE_CONFIDENT, head.capability_id,
            f"margin {head.score - second.score:.3f} >= {margin}",
        )
    return JudgeVerdict(JUDGE_UNCERTAIN, None, "race too close / mixed provenance")
