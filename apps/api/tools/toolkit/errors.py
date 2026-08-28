"""Toolkit pipeline exception hierarchy.

Each lifecycle stage raises its own subclass so a caller (the agent runtime or the worker
job) can report a precise, user-readable reason without leaking a traceback. All of them
derive from :class:`ToolKitError`; the pipeline re-raises them untouched and wraps anything
else into a generic :class:`ToolKitError`.
"""


class ToolKitError(Exception):
    """Base class for all toolkit generation failures."""


class SourceError(ToolKitError):
    """Input validation / extraction failure (bad path, unsupported format, empty text)."""


class GenerationError(ToolKitError):
    """The model failed to produce schema-valid JSON even after a retry."""


class RenderError(ToolKitError):
    """A rendered artifact failed its own output validation (e.g. Mermaid depth)."""


class PersistError(ToolKitError):
    """Writing a generated artifact to the output directory failed."""
