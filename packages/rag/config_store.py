"""RAG config persistence: ``app_settings["rag"]`` JSON blob + in-process cache.

The admin console reads/writes the pipeline configuration here. The API tool path uses
the cached copy so a running pipeline does not re-read the DB per query; saving a new
config refreshes the cache and (in deps) clears the retriever lru_cache so the next
retrieval is built from the new topology.
"""
from __future__ import annotations

from rag.nodes.base import Node
from rag.pipeline.pipeline_config import RagPipelineConfig
from rag.pipeline.registry import registry

_loaded: RagPipelineConfig | None = None


async def load_config(session_factory) -> RagPipelineConfig:
    """Load the stored config (falling back to env-seeded defaults), validate, and cache."""
    from core.infrastructure.security import get_setting

    global _loaded
    async with session_factory() as session:
        raw = await get_setting(session, "rag")
    cfg = RagPipelineConfig.from_dict(raw) if raw else RagPipelineConfig.default()
    errors = validate(cfg)
    if errors:
        # Unknown node names / bad params would break the pipeline at run time; prefer a
        # stored config that still parses over one that cannot run. Fall back to defaults.
        _loaded = RagPipelineConfig.default()
    else:
        _loaded = cfg
    return _loaded


async def save_config(session_factory, cfg: RagPipelineConfig) -> list[str]:
    """Validate and persist ``cfg``; returns a list of validation errors (empty = saved)."""
    from core.infrastructure.security import set_setting

    errors = validate(cfg)
    if errors:
        return errors
    global _loaded
    async with session_factory() as session:
        await set_setting(session, "rag", cfg.to_dict())
    _loaded = cfg
    return []


def current_config() -> RagPipelineConfig:
    """The cached config, or env-seeded defaults if nothing has been loaded yet."""
    return _loaded if _loaded is not None else RagPipelineConfig.default()


def invalidate_config() -> None:
    """Drop the cached config (call after a direct DB write outside save_config)."""
    global _loaded
    _loaded = None


def validate(cfg: RagPipelineConfig) -> list[str]:
    """Return a list of configuration problems (empty = valid)."""
    errors: list[str] = []
    seen: set[str] = set()
    for nc in cfg.nodes:
        if nc.name in seen:
            errors.append(f"duplicate node '{nc.name}'")
        seen.add(nc.name)
        cls: type[Node] | None = registry.get(nc.name)
        if cls is None:
            errors.append(f"unknown node '{nc.name}' (not in registry)")
            continue
        schema = cls.params_schema.get("properties", {})
        for key, value in nc.params.items():
            if key not in schema:
                errors.append(f"node '{nc.name}' has unknown param '{key}'")
            elif not (value is None) and isinstance(schema[key], dict):
                ptype = schema[key].get("type")
                if ptype == "integer" and not isinstance(value, int):
                    errors.append(f"param '{nc.name}.{key}' must be an integer")
                elif ptype == "number" and isinstance(value, bool):
                    errors.append(f"param '{nc.name}.{key}' must be a number")
                elif ptype == "boolean" and not isinstance(value, bool):
                    errors.append(f"param '{nc.name}.{key}' must be a boolean")
                elif ptype == "string" and not isinstance(value, str):
                    errors.append(f"param '{nc.name}.{key}' must be a string")
    if cfg.chunking.strategy not in ("fixed", "paragraph", "sentence", "semantic"):
        errors.append(f"unknown chunking strategy '{cfg.chunking.strategy}'")
    if cfg.chunking.chunk_chars < 1:
        errors.append("chunk_chars must be >= 1")
    return errors
