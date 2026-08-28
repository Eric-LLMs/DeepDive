---
name: fact_check
description: Cross-verify a claim or answer across independent sources before asserting it. Search for corroborating and contradicting evidence, then report a confidence level and the remaining uncertainty. Use when about to assert a contested, specific, or possibly outdated fact.
keywords: fact-check, verify, cross-check, source, confidence, accuracy, citation
allowed_tools: search_social, web_search
---

# Fact-Check Procedure

Use this when you are about to state something that is specific, contested, time-sensitive,
or that the user is relying on — numbers, release dates, "best" claims, comparisons, and
anything a user might act on.

## When to use

- The claim is specific (a number, a date, a name, a version).
- The user is making a decision based on your answer.
- The topic is fast-moving and your training data may be stale.

## Steps

1. **State the claim to verify.** Restate it precisely — vague claims are unfalsifiable.
   If the user's question embeds a false premise (e.g. "why is X broken" when X isn't),
   correct the premise before verifying.

2. **Search multiple independent sources.** Use `web_search` for official/recent
   documentation and `reddit_search` for lived experience and recent community reports.
   Independent sources sharing the same specific detail are far stronger than one source
   repeated by many mirrors.

3. **Look for disagreement deliberately.** Ask "who would contradict this?" and search for
   that too. Absence of a source that disagrees is weaker evidence than an active search
   that found none.

4. **Distinguish fact from opinion and anecdote.** A single first-person Reddit comment is
   an anecdote, not a fact — even if vivid. Report it as "one user reports...", not as truth.

5. **Rate confidence and say why:**
   - **High**: multiple independent, specific sources agree; no credible contradiction.
   - **Medium**: one strong source, or several that agree broadly but with gaps.
   - **Low**: thin, one-sided, anecdotal, or conflicting evidence.

## Output style

- State the claim, then the confidence, then the evidence trail with inline citations.
- Explicitly list what remains uncertain or unverified — a good fact-check names its gaps.
- If you cannot verify it, say "not verified" rather than hedging into vagueness.
