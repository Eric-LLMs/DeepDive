"""Typed tool definition: ``define_tool()`` separates canonical output from model-visible content.

``define_tool()`` is a plain function returning a ``ToolDefinition`` whose ``output`` has
two parts: ``schema`` (the canonical value's JSON Schema) and ``render`` (``(args, value)`` →
content blocks the model sees). This keeps that split and wraps the user ``execute`` so
that args are validated before the body runs and the output is validated/rendered after it.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft7Validator

from agent.engine.decisions import ContentBlock, ToolExecution
from agent.tools.tool_permissions import ToolPermission, permission_names


class ToolArgsError(ValueError):
    """Raised when tool arguments fail their JSON Schema validation."""


class ToolOutputError(ValueError):
    """Raised when the tool's canonical output fails its JSON Schema validation."""


def _validate(schema: dict, instance: Any) -> list[str]:
    """Return human-readable validation errors (empty list = valid)."""
    if not schema:
        return []
    errors = Draft7Validator(schema).iter_errors(instance)
    return [
        f"{'->'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in errors
    ]


def _decode_stringified(schema: dict, value: Any) -> Any:
    """Best-effort decode of JSON-string-encoded object/array tool parameters.

    Some model providers / tool-call harnesses double-encode a nested ``object`` (or
    ``array``) parameter into a JSON *string* — ``"node": "{\"id\": ...}"``. A single
    top-level ``json.loads`` cannot recover that, so an object-typed parameter reaches the
    schema validator as a string and is wrongly rejected (``... is not of type 'object'``) —
    the failure that froze research's ``record_node`` on an empty graph. Walk the tool's
    parameter schema and decode any string the schema declares as object/array back into a
    structure *before* validation. A string that does not parse is left untouched so the
    validator still reports a precise error instead of masking it.
    """
    if not schema:
        return value
    raw_type = schema.get("type")
    types = raw_type if isinstance(raw_type, list) else [raw_type]
    if isinstance(value, str) and ("object" in types or "array" in types):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return value
        return _decode_stringified(schema, parsed)
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        return {
            key: _decode_stringified(props.get(key, {}) or {}, item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        item_schema = schema.get("items") or {}
        return [_decode_stringified(item_schema, item) for item in value]
    return value


def _empty_render(args: dict, value: Any) -> list[ContentBlock]:
    return []


@dataclass
class ToolOutput:
    """The output contract: canonical value schema + model-visible renderer."""

    schema: dict = field(default_factory=dict)
    render: Callable[[dict, Any], list[ContentBlock]] = _empty_render


_WRITE_HINTS = (
    "file",
    "path",
    "write",
    "save",
    "append",
    "overwrite",
    "delete",
    "remove",
    "mkdir",
    "create",
)
_NETWORK_HINTS = ("url", "http", "web", "network", "host", "curl", "socket")


def classify_permissions(defn: ToolDefinition) -> frozenset[ToolPermission]:
    """Auto-classify a tool's permission class from its definition.

    An explicit ``defn.permission`` wins. Otherwise: destructive → WRITE; parameter
    names/descriptions hinting at file mutation → WRITE; URL/HTTP/network hints → NETWORK;
    anything else defaults to READ-only.
    """
    if defn.permission is not None:
        return frozenset(defn.permission)

    perms: set[ToolPermission] = set()
    if defn.destructive:
        perms.add(ToolPermission.WRITE)

    for pname, pspec in defn.parameters.get("properties", {}).items():
        blob = f"{pname} {pspec}".lower()
        if any(w in blob for w in _WRITE_HINTS):
            perms.add(ToolPermission.WRITE)
        if any(w in blob for w in _NETWORK_HINTS):
            perms.add(ToolPermission.NETWORK)

    if not perms:
        perms.add(ToolPermission.READ)
    return frozenset(perms)


@dataclass
class ToolDefinition:
    """A tool: schema + render + body (the user ``execute``)."""

    name: str
    description: str
    parameters: dict
    output: ToolOutput
    execute: Callable[[dict, ToolExecution], Awaitable[Any]]
    destructive: bool = False
    is_concurrency_safe: bool | None = None
    permission: set[ToolPermission] | None = None  # explicit override; None → classify

    def schema(self) -> dict:
        """Model-visible projection (name/description/parameters only; no execute/render)."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    @property
    def permissions(self) -> frozenset[ToolPermission]:
        """The effective permission class (explicit or auto-classified)."""
        return classify_permissions(self)

    @property
    def permission_tag(self) -> str:
        """Comma-joined permission names, e.g. ``"read,network"`` (catalog hint)."""
        return ",".join(permission_names(self.permissions))


def define_tool(
    *,
    name: str,
    description: str,
    parameters: dict,
    output: ToolOutput,
    execute: Callable[[dict, ToolExecution], Awaitable[Any]],
    destructive: bool = False,
    is_concurrency_safe: bool | None = None,
    permission: set[ToolPermission] | None = None,
) -> ToolDefinition:
    """Define a tool.

    - ``parameters``: JSON Schema for the tool arguments (OpenAI function-calling format).
    - ``output``: a :class:`ToolOutput` carrying the canonical value schema + a renderer.
    - ``execute``: ``async (args, exec) -> value`` — the actual body.

    The returned ``ToolDefinition.execute`` wraps the user body: validate args → run body →
    validate output. Validation failures are raised as :class:`ToolArgsError` /
    :class:`ToolOutputError` (converted into a ``ToolFailure`` by the runtime).
    """

    async def _execute(args: dict, exec: ToolExecution) -> Any:
        args = _decode_stringified(parameters, args)
        arg_errors = _validate(parameters, args)
        if arg_errors:
            raise ToolArgsError("; ".join(arg_errors))
        value = await execute(args, exec)
        out_errors = _validate(output.schema, value)
        if out_errors:
            raise ToolOutputError("; ".join(out_errors))
        return value

    return ToolDefinition(
        name=name,
        description=description,
        parameters=parameters,
        output=output,
        execute=_execute,
        destructive=destructive,
        is_concurrency_safe=is_concurrency_safe,
        permission=permission,
    )
