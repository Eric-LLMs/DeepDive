"""Deck engine errors — subclasses of the toolkit family so the pipeline's single
readable-error contract keeps holding across the new stages."""
from __future__ import annotations

from ..errors import ToolKitError


class DeckError(ToolKitError):
    """Base for deck-engine failures."""


class DeckLayoutError(DeckError):
    """A slide cannot be laid out without dropping content — loud failure by contract:
    budgets are enforced upstream (Pass C validators), never trimmed here."""
