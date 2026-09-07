"""Generic runtime wiring: Workflow Definition -> RunnerDeps.

This module is the thin, *structural* seam between a :class:`~workflow.definition.
WorkflowDefinition` and the hand-built ``RunnerDeps`` the runner consumes. It owns
exactly two decisions and nothing else:

- **executor resolution**: the definition's activity list binds ``task_name`` to a
  logical ``executor`` id; an :class:`ExecutorRegistry` (a port — the core defines
  the contract, the adapter owns the bindings) turns that id into an
  :class:`~workflow.ports.Executor`. Resolution is a lookup, not a construction:
  the core never instantiates, imports or inspects what it resolves.
- **cap assembly**: a definition declares *which* cap dimensions the workflow is
  bound by (structural — part of the fingerprint); the caller supplies the *values*
  (runtime config — never part of the spec). :func:`build_loop_policy` joins them.

Everything else that ``RunnerDeps`` needs (store, probe, facts, prompt, retry,
lease, publisher, hook) is an adapter-built port instance that rides through
untouched. This factory must never grow domain parameters: if a caller needs a
domain-shaped argument, that argument belongs in the adapter's closure, not here.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from workflow.definition import WorkflowDefinition, validate_spec
from workflow.policy import LoopCaps, LoopPolicy
from workflow.ports import Executor, LeaseStore
from workflow.retry import RetryPolicy
from workflow.runner import IterationRequest, RunnerDeps
from workflow.states import WorkflowState

# The cap dimensions a LoopPolicy can actually enforce — derived from the core
# dataclass itself so the check tracks core evolution instead of a hand copy.
_CAP_FIELDS = frozenset(f.name for f in dataclasses.fields(LoopCaps))


@runtime_checkable
class ExecutorRegistry(Protocol):
    """Turn a logical executor id (from a definition) into an executable port.

    Implementations live with the adapter/app layer that knows what the ids mean;
    resolution may be a dict lookup, a factory, or a remote handle — the core only
    ever sees :class:`~workflow.ports.Executor`.
    """

    def resolve(self, executor_id: str) -> Executor: ...


class MappingRegistry:
    """The canonical registry: a frozen id -> Executor binding table."""

    def __init__(self, bindings: Mapping[str, Executor]) -> None:
        self._bindings: dict[str, Executor] = dict(bindings)

    def resolve(self, executor_id: str) -> Executor:
        try:
            return self._bindings[executor_id]
        except KeyError:
            raise LookupError(
                f"executor not registered: {executor_id!r}"
            ) from None


def _validated_activities(definition: WorkflowDefinition) -> list[dict[str, str]]:
    """Normalize the definition once per call — a malformed spec fails loudly here."""
    return validate_spec(definition.spec())["activities"]


def resolve_executor(
    definition: WorkflowDefinition,
    registry: ExecutorRegistry,
    *,
    task_name: str | None = None,
) -> Executor:
    """Bind one iteration to an executor via ``task_name -> executor id -> resolve``.

    A single-activity definition needs no ``task_name`` (there is nothing to choose);
    a multi-activity definition must be told which activity this iteration runs —
    choosing *is* workflow-state knowledge and stays with the caller (the adapter's
    state machine), never with the core.
    """
    activities = _validated_activities(definition)
    if task_name is None:
        if len(activities) != 1:
            raise ValueError(
                "multi-activity definition requires an explicit task_name"
            )
        activity = activities[0]
    else:
        matches = [a for a in activities if a["task_name"] == task_name]
        if len(matches) != 1:
            raise ValueError(f"task not declared by definition: {task_name!r}")
        activity = matches[0]
    return registry.resolve(activity["executor"])


def build_loop_policy(
    definition: WorkflowDefinition,
    caps_values: Mapping[str, Any],
    *,
    cap_outcome: WorkflowState | None = None,
    signal_outcome: WorkflowState | None = None,
) -> LoopPolicy:
    """Join the definition's declared cap *dimensions* with runtime *values*.

    Strict both ways: every declared dimension needs a value (a missing value would
    silently disable a cap the flow says exists), and no undeclared value may be
    passed (that would smuggle behavior the fingerprint never saw). ``None`` is a
    legal value — it means "dimension active in shape, ceiling lifted this run".
    """
    names = validate_spec(definition.spec())["caps"]
    unknown = sorted(set(names) - _CAP_FIELDS)
    if unknown:
        raise ValueError(f"definition declares unenforceable caps: {unknown}")
    missing = sorted(set(names) - set(caps_values))
    if missing:
        raise ValueError(f"runtime caps missing declared dimensions: {missing}")
    extra = sorted(set(caps_values) - set(names))
    if extra:
        raise ValueError(f"runtime caps not declared by definition: {extra}")
    kwargs = {name: caps_values[name] for name in names}
    policy_kwargs: dict[str, Any] = {"caps": LoopCaps(**kwargs)}
    if cap_outcome is not None:
        policy_kwargs["cap_outcome"] = cap_outcome
    if signal_outcome is not None:
        policy_kwargs["signal_outcome"] = signal_outcome
    return LoopPolicy(**policy_kwargs)


def build_deps(
    *,
    definition: WorkflowDefinition,
    registry: ExecutorRegistry,
    store: LeaseStore,
    probe: Any,                      # ProgressProbe (snapshot may be sync or async)
    business_facts: Callable[[], Mapping[str, Any]]
    | Callable[[], Awaitable[Mapping[str, Any]]],
    compose_prompt: Callable[[IterationRequest, int], str],
    retry: RetryPolicy,
    task_name: str | None = None,
    executor: Executor | None = None,
    caps_values: Mapping[str, Any] | None = None,
    policy: LoopPolicy | None = None,
    cap_outcome: WorkflowState | None = None,
    signal_outcome: WorkflowState | None = None,
    **passthrough: Any,
) -> RunnerDeps:
    """Assemble RunnerDeps from a definition + registry + adapter-built ports.

    Only the *structural* parts (executor binding, cap policy) are derived here;
    ports pass through verbatim and remaining keyword args (lease, publisher, hook)
    forward to :class:`RunnerDeps`, whose own constructor validates them.
    """
    if executor is None:
        executor = resolve_executor(definition, registry, task_name=task_name)
    if policy is None:
        policy = build_loop_policy(
            definition, caps_values or {},
            cap_outcome=cap_outcome, signal_outcome=signal_outcome,
        )
    return RunnerDeps(
        store=store,
        executor=executor,
        policy=policy,
        probe=probe,
        retry=retry,
        business_facts=business_facts,
        compose_prompt=compose_prompt,
        **passthrough,
    )
