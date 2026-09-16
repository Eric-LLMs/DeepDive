"""Deterministic Visual Compiler (M1): PresentationBrief → layout → Typst → PDF.

Given the same brief + the same sliced assets, every artifact is re-derivable
with zero LLM calls (§1.2.6): :mod:`.theme` tokens, :mod:`.layout_engine` slot
geometry, :mod:`.materializer` PNG drawings. The brief-native Typst emit lives
in :mod:`..typst_deck` / :mod:`..render`; the native PPTX compiler joins in M2.
"""