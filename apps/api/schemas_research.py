"""Request models for the converged ``/research`` task API.

The console is read-mostly: the only human write is the atomic task create (title +
description + optional cloud-drive materials) from the chat ``+ Research`` button.
Task *phase* is never writable from here — stage/gate/artifact-version mutations are
driven exclusively by the agent through the six research tools.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class TaskCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    # The cloud-drive working directory (My Drive) the task folder lands in; empty = root.
    parent_folder_path: str = Field(default="", max_length=500)
    material_asset_ids: list[str] = Field(default_factory=list, max_length=20)
