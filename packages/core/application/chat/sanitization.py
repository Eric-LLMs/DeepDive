"""Input hygiene for the fast paths (narrowed scope, per the pre-implementation survey).

The survey established two facts that bound what sanitization has to do:

  * History rows are ALREADY tool-free — ``load_session_messages`` filters to
    user/assistant and the v2 client tail carries only those roles — so there is no
    embedded-tool-output scrubbing to do here.
  * Base-context resolution performs NO new I/O: by the time a fast path is
    considered, attach/handoff notes have already been prefixed into ``ctx.user_text``
    and the viewer/research gates have already been evaluated.

So the only jobs left are (1) a PURITY check — reject a turn whose raw text is not
plain user content (a capability note prefix, a control/NUL byte, or a serialized
structure has leaked in) so it can never be answered by a tool-less direct call — and
(2) a LENGTH bound, so a direct single-shot request stays inside its latency budget.
Neither is a security boundary (the model still runs under the agent's sandbox); it is
correctness guarding for the fast path: when in doubt, return ``None`` and let the turn
fall through to the full Agent path.
"""
from __future__ import annotations

import re

# The bracketed note prefixes :func:`core.application.chat.context` attaches for
# capabilities a direct call cannot honor. If one is present on the RAW message, the
# turn is not standalone.
_NOTE_PREFIXES = ("[Attached:", "[Research handoff:")

# Control chars that must never ride a user message (NUL, and the C0 range minus the
# tabs/newlines that legitimately appear in multi-line text).
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def is_pure_user_text(text: str) -> bool:
    """True only for plain, structured-free user text."""
    if not text or not text.strip():
        return False
    stripped = text.strip()
    if any(stripped.startswith(p) for p in _NOTE_PREFIXES):
        return False
    if _CONTROL.search(text):
        return False
    # A turn that embeds a serialized tool/JSON blob is not a plain question.
    return not (stripped.startswith(("{", "[")) and stripped.endswith(("}", "]")))


def truncate(text: str, max_chars: int) -> str:
    """Bound ``text`` to ``max_chars`` with an ellipsis marker (no-op when it fits)."""
    if max_chars > 0 and len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def sanitize_for_direct(text: str, *, max_chars: int = 2000) -> str | None:
    """Return sanitized direct-path text, or ``None`` when the turn is ineligible.

    Combines the purity check and the length bound: an impure message, empty message,
    or one longer than ``max_chars`` returns ``None`` so the caller abstains and the
    turn routes to the full Agent path (the direct path is for short plain asks only).
    """
    if not is_pure_user_text(text):
        return None
    cleaned = text.strip()
    if max_chars > 0 and len(cleaned) > max_chars:
        return None
    return cleaned
