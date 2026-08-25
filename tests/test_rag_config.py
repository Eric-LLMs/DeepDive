"""Tests for pipeline config serialization + validation (app_settings["rag"] blob)."""
from rag.config_store import validate
from rag.pipeline_config import ChunkingConfig, NodeConfig, RagPipelineConfig


def test_roundtrip_to_dict_from_dict():
    cfg = RagPipelineConfig(
        nodes=[
            NodeConfig("query_rewrite", enabled=False, params={"n_variants": 3, "hyde": True}),
            NodeConfig("cross_encoder", params={"model_name": "bge-reranker-v2"}),
        ],
        chunking=ChunkingConfig(strategy="paragraph", chunk_chars=800, overlap=100),
        contextual=True,
        parent_child=True,
        cjk=True,
    )
    restored = RagPipelineConfig.from_dict(cfg.to_dict())
    assert restored == cfg


def test_validation_accepts_defaults():
    assert validate(RagPipelineConfig.default()) == []


def test_validation_rejects_unknown_node():
    cfg = RagPipelineConfig.default()
    cfg.nodes.append(NodeConfig("does_not_exist"))
    errors = validate(cfg)
    assert any("does_not_exist" in e for e in errors)


def test_validation_rejects_unknown_param():
    cfg = RagPipelineConfig.default()
    cfg.nodes[0].params["bogus"] = 1
    errors = validate(cfg)
    assert any("bogus" in e for e in errors)


def test_validation_rejects_bad_strategy():
    cfg = RagPipelineConfig.default()
    cfg.chunking.strategy = "magic"
    errors = validate(cfg)
    assert any("strategy" in e for e in errors)


def test_enabled_nodes_filters_disabled():
    cfg = RagPipelineConfig.default()
    cfg.nodes[1].enabled = False
    assert "vector_recall" not in [n.name for n in cfg.enabled_nodes]
    assert len(cfg.enabled_nodes) == len(cfg.nodes) - 1
