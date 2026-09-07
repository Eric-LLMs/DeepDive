"""Workflow Core: the generic control-plane abstractions shared by workflow adapters.

This package is intentionally free of any business-domain concepts. Concrete workflow
adapters import from here; nothing here ever imports an adapter.
"""

