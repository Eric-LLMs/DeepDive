"""Request models for the converged ``/research`` task API.

The console is read-mostly: the only human write is the atomic task create (title +
description + optional cloud-drive materials) from the chat ``+ Research`` button.
Task *phase* is never writable from here — stage/gate/artifact-version mutations are
driven exclusively by the agent through the six research tools.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TaskCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    # The cloud-drive working directory (My Drive) the task folder lands in; empty = root.
    parent_folder_path: str = Field(default="", max_length=500)
    material_asset_ids: list[str] = Field(default_factory=list, max_length=20)
    # Execution control-flow mode: 'progressive' (default) or 'strict'. The agent reads it at
    # resume; progressive records gate-FAIL diagnostics into project['diagnostics'] and lets
    # the stage advance (nothing parks on a human override), strict blocks on a failed gate.
    # The auto-run is now the code-driven pipeline, whose doctrine is degrade-honestly-and-
    # advance (failure ledger + force advance; structural inability terminalizes via
    # StructuralStop), so the entry default is 'progressive'; strict remains selectable.
    execution_mode: Literal["strict", "progressive"] = "progressive"


class TaskBriefUpdate(BaseModel):
    """The one human-writable field after creation: the task's description (research brief).

    The pipeline re-reads ``task_spec.json`` at every node entry, so a saved edit is the
    input to the *next* run — the loop "edit description → re-run → report improves".
    Empty/blank resets to the server's DEFAULT_RESEARCH_DESCRIPTION (same rule as create).
    """

    description: str = Field(default="", max_length=4000)
