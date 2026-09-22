"""Understanding Engine contracts: the typed answer to "what does this turn need?".

``TurnRequirements`` is the OUTPUT contract of understanding (and, later,
``understanding.resolve_requirements``); its owner is this module. It describes
CAPABILITY DEMAND ONLY — it has no knowledge of executors, policies or plans.

Phase 1 ships the contract without a resolver: the orchestrator constructs the
neutral default (``confidence=ABSTAIN``), which the policy maps to the AGENT plan,
keeping every turn on the legacy path while the control plane ships dark.
Phase 2 adds L0 rules + cheap signals + the optional L1 fast-LLM arbiter here.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Signal(str, Enum):
    """Certainty of one capability demand. AMBIGUOUS forces L1/agent arbitration."""

    HIGH = "high"
    LOW = "low"
    AMBIGUOUS = "ambiguous"


class Confidence(str, Enum):
    """Overall routing confidence of the requirement set."""

    HIGH = "high"
    LOW = "low"
    AMBIGUOUS = "ambiguous"
    ABSTAIN = "abstain"


class Complexity(str, Enum):
    LOW = "low"
    MODERATE = "moderate"
    COMPLEX = "complex"


@dataclass(frozen=True)
class TurnRequirements:
    """What capabilities the user's turn demands (never how to execute them).

    ``requested_action`` carries the *semantic intent* of an action request (a name +
    raw slot hints), NOT an instantiated ActionSpec — validation and authorization
    happen in Pre-flight / the ACTION executor, never here.
    """

    needs_private: Signal = Signal.LOW
    needs_web: Signal = Signal.LOW
    needs_viewer: Signal = Signal.LOW
    needs_action: Signal = Signal.LOW
    needs_memory: bool = False
    requested_action: dict | None = None
    complexity: Complexity = Complexity.LOW
    confidence: Confidence = Confidence.ABSTAIN
