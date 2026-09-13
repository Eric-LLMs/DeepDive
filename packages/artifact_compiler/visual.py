"""Visual asset contract: the compiled form of a VisualSpec.

An Asset is the *derived*, provenance-bearing record of one rendered figure: it must
carry the same claim/evidence anchors as its spec so a reader of the PDF can trace a
picture back to sources (invariant 2). Primary format is SVG (vector, diffable,
sanitizable).
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from artifact_compiler.plan import Renderer


class AssetFormat(str, Enum):
    svg = "svg"
    png = "png"


class CompileStatus(str, Enum):
    success = "success"
    failed = "failed"


class Asset(BaseModel):
    asset_id: str
    spec_id: str
    renderer: Renderer
    format: AssetFormat = AssetFormat.svg
    source_path: str      # relative to the run dir: the raw .mmd / spec source
    output_path: str      # relative to the run dir: the sanitized rendered file
    compile_status: CompileStatus
    claim_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)

    model_config = {"extra": "forbid"}
