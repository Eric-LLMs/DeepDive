---
name: social_research
description: Research a topic across social sources (Reddit, web). Search, scan, cross-reference, and cite so the answer reflects real community opinion and current discussion. Use when the user wants opinions, experiences, or a broad external sweep of a topic.
keywords: research, reddit, social, opinions, community, discussion, x, twitter, zhihu, current
allowed_tools: search_social, web_search
---

# Social Research Procedure

Use this when the answer depends on what people actually think, experience, or are
currently discussing — product opinions, tool recommendations, community trends, or
"what are people saying about X".

## When to use

- The user asks for opinions, experiences, comparisons, or "what people say".
- The topic is time-sensitive (best practices, tooling, libraries) and community
  discussion is more informative than static docs.

## Steps

1. **Clarify the goal.** Decide what the user actually needs: a verdict on one question,
   a survey of opinions, or a comparison of options. Break a broad ask into sub-questions
   — one focused query per sub-question beats one vague query.

2. **Search Reddit.** Call `search_social` with `platform: "reddit"`:
   - Start with the broad question; if results are noisy, scope with `subreddit` (e.g.
     `"MachineLearning"` for ML tooling) or switch `platform` to `"x"`/`"auto"` when the
     user wants broader or real-time signal.
   - Prefer recent, high-comment threads: they carry real discussion, not just votes.
   - If the first query misses, retry with synonyms or narrower phrasing.

3. **Search the web for context.** Use `web_search` to corroborate, find official pages,
   and catch recent releases that community threads may not reflect yet.

4. **Scan and rank.** For each result look at title, score, comment count, and snippet.
   A high score with a low comment count can mean a popular-but-shallow post; a busy
   comment thread usually holds the reasoning and dissent.

5. **Cross-reference.** When two sources disagree, say so. Note both the majority view
   and the strong dissenting minority — "everyone loves X" is usually an over-simplification.

6. **Synthesize with attribution.** Answer in the user's terms, then cite sources inline
   (`r/subreddit` thread title or URL). Separate your synthesis from what the sources
   actually say. If the evidence is thin or one-sided, say that explicitly.

## Output style

- Lead with the direct answer, then the supporting voices.
- Cite each substantive claim to a source.
- Flag confidence: high (multiple independent sources agree), medium (one strong source
  or mixed signals), low (thin or conflicting evidence).
