"""Targeted repair = patch application (Task 5.4 core half; docs/research/19 §9).

Core never regenerates content (invariant 10): the Skill authors a replacement
block/asset and submits a :class:`Patch`. Applying it here is deterministic and
CAS-guarded by the caller's RunStore transaction; afterwards the pipeline re-runs
Typst compilation and all QA layers **globally** (invariant 6) — only the content
swap is local.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from artifact_compiler.doc_ast import ContentBlock, DocumentAST, SectionAST


class ReplaceBlockPatch(BaseModel):
    """Swap one block for a structurally-compatible replacement (same id, same type).

    Type-compatibility is enforced so a repair cannot change a section's block-type
    multiset behind the contract gate's back (a failed figure must be replaced by a
    figure — an asset-level repair then re-renders it)."""

    op: Literal["replace_block"] = "replace_block"
    block_id: str
    block: ContentBlock
    reason: str = ""

    model_config = {"extra": "forbid"}


class ReplaceAssetPatch(BaseModel):
    """Point a figure block at a newly rendered asset (same asset slot, new bytes)."""

    op: Literal["replace_asset"] = "replace_asset"
    block_id: str          # the FigureBlock to re-point
    asset_id: str          # the replacement asset
    svg_relative_path: str
    reason: str = ""

    model_config = {"extra": "forbid"}


Patch = Annotated[
    ReplaceBlockPatch | ReplaceAssetPatch, Field(discriminator="op")
]


class PatchError(ValueError):
    """A patch that does not address exactly one existing block, or violates the
    replacement type contract."""


def _find_block(doc: DocumentAST, block_id: str) -> tuple[SectionAST, int]:
    for sec in doc.sections:
        for idx, b in enumerate(sec.blocks):
            if b.block_id == block_id:
                return sec, idx
    raise PatchError(f"no block with id {block_id!r} in document AST")


def apply_patch(doc: DocumentAST, patch: Patch) -> DocumentAST:
    """Return a NEW DocumentAST with the patch applied; the input is never mutated."""
    sec, idx = _find_block(doc, patch.block_id)
    target = sec.blocks[idx]

    if patch.op == "replace_block":
        if patch.block.block_id != patch.block_id:
            raise PatchError(
                f"replacement block_id {patch.block.block_id!r} != target "
                f"{patch.block_id!r} (block ids are the repair address, immutable)"
            )
        if patch.block.type != target.type:
            raise PatchError(
                f"replacement changes block type {target.type!r} -> "
                f"{patch.block.type!r}; that is a plan change, not a repair"
            )
        new_blocks = list(sec.blocks)
        new_blocks[idx] = patch.block
    else:  # replace_asset — only FigureBlock slots accept asset swaps
        from artifact_compiler.doc_ast import FigureBlock

        if not isinstance(target, FigureBlock):
            raise PatchError(
                f"replace_asset targets {target.type!r} block, not a figure"
            )
        new_blocks = list(sec.blocks)
        new_blocks[idx] = target.model_copy(
            update={"asset_id": patch.asset_id,
                    "svg_relative_path": patch.svg_relative_path}
        )

    new_sections = [
        s.model_copy(update={"blocks": new_blocks}) if s is sec else s
        for s in doc.sections
    ]
    return doc.model_copy(update={"sections": new_sections})
