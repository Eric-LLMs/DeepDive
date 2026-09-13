"""Document AST contracts: the deterministic input of the Typst compiler.

Every block carries a stable ``block_id`` (the addressable unit of local repair,
invariant 6) and optional ``claim_ids`` / ``citations`` back-references resolved by
:mod:`artifact_compiler.validators` before anything renders.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class InlineText(BaseModel):
    text: str
    bold: bool | None = None
    code: bool | None = None
    citations: list[str] | None = None
    claim_ids: list[str] | None = None


class BaseBlock(BaseModel):
    block_id: str
    claim_ids: list[str] | None = None
    citations: list[str] | None = None

    model_config = {"extra": "forbid"}


class HeadingBlock(BaseBlock):
    type: Literal["heading"] = "heading"
    level: int = Field(ge=1, le=6)
    text: str


class ParagraphBlock(BaseBlock):
    type: Literal["paragraph"] = "paragraph"
    inlines: list[InlineText] = Field(min_length=1)


class ListItem(BaseModel):
    inlines: list[InlineText] = Field(min_length=1)


class ListBlock(BaseBlock):
    type: Literal["list"] = "list"
    ordered: bool = False
    items: list[ListItem] = Field(min_length=1)


class TableBlock(BaseBlock):
    type: Literal["table"] = "table"
    caption: str | None = None
    headers: list[str] = Field(min_length=1)
    rows: list[list[str]] = Field(default_factory=list)


class FigureBlock(BaseBlock):
    type: Literal["figure"] = "figure"
    asset_id: str
    svg_relative_path: str
    caption: str
    placement_policy: Literal["keep_together", "page_top"] = "keep_together"


CalloutBodyItem = Annotated[
    ParagraphBlock | ListBlock | TableBlock, Field(discriminator="type")
]


class CalloutBlock(BaseBlock):
    type: Literal["callout"] = "callout"
    variant: Literal["key_finding", "warning", "evidence"]
    title: str
    body: list[CalloutBodyItem] = Field(min_length=1)


ContentBlock = Annotated[
    HeadingBlock | ParagraphBlock | ListBlock | TableBlock | FigureBlock | CalloutBlock,
    Field(discriminator="type"),
]


class SectionAST(BaseModel):
    """One section's authored blocks (stored under ``ast/sections/``)."""

    section_id: str
    blocks: list[ContentBlock] = Field(default_factory=list)


class DocumentAST(BaseModel):
    """The stitched global document (``ast/document.ast.json``): the exact pure-function
    input of ``compile_ast``."""

    artifact_id: str
    sections: list[SectionAST] = Field(min_length=1)
