"""Tests for the Context/Fiber dependency-injection state machine.

Covers the properties that replaced the old ``_drain_pending`` fixpoint: order-independent
activation, cycles that never activate, unresolved injections staying PENDING, and duplicate
capability registration raising.
"""
import pytest

from agent.di import Context, FiberState


class _Plugin:
    def __init__(self, name, inject=(), provides=()):
        self.name = name
        self.inject = list(inject)
        self.provides = {p: f"{name}.{p}" for p in provides}


def test_order_independent_activation():
    ctx = Context()
    mounted = []

    a = _Plugin("a", provides=["cap_a"])
    b = _Plugin("b", inject=["cap_a"], provides=["cap_b"])
    c = _Plugin("c", inject=["cap_b"])

    # Register in reverse dependency order; the state machine must still settle topologically.
    ctx.plugin(c, lambda: mounted.append("c"), lambda: None)
    ctx.plugin(b, lambda: mounted.append("b"), lambda: None)
    ctx.plugin(a, lambda: mounted.append("a"), lambda: None)

    assert mounted == ["a", "b", "c"]
    assert ctx.state_of("a") is FiberState.ACTIVE
    assert ctx.resolve("cap_b") == "b.cap_b"


def test_cycle_never_activates():
    ctx = Context()

    a = _Plugin("a", inject=["cap_b"], provides=["cap_a"])
    b = _Plugin("b", inject=["cap_a"], provides=["cap_b"])
    ctx.plugin(a, lambda: None, lambda: None)
    ctx.plugin(b, lambda: None, lambda: None)

    assert ctx.state_of("a") is FiberState.PENDING
    assert ctx.state_of("b") is FiberState.PENDING


def test_unresolved_inject_stays_pending():
    ctx = Context()
    a = _Plugin("a", inject=["missing"])
    ctx.plugin(a, lambda: None, lambda: None)

    assert ctx.state_of("a") is FiberState.PENDING


def test_duplicate_capability_raises():
    ctx = Context()
    ctx.plugin(_Plugin("a", provides=["cap"]), lambda: None, lambda: None)

    with pytest.raises(ValueError):
        ctx.plugin(_Plugin("b", provides=["cap"]), lambda: None, lambda: None)


def test_external_provide_resolves_immediately():
    ctx = Context()
    ctx.provide("retrieval", object())

    assert ctx.has("retrieval")
    assert ctx.resolve("retrieval") is not None
