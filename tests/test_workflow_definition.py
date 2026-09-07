"""Workflow Core definition spec + wf1- fingerprint determinism and boundary enforcement."""
from __future__ import annotations

import copy

import pytest

from workflow.definition import (
    FINGERPRINT_PREFIX,
    canonical_json,
    compute_fingerprint,
    validate_spec,
)


def _spec() -> dict:
    return {
        "name": "doc-chain",
        "transitions": {
            "idle": ["running"],
            "running": ["running", "waiting", "succeeded", "failed", "cancelled"],
            "waiting": [],
            "succeeded": [],
            "failed": [],
            "cancelled": [],
        },
        "activities": [
            {"task_name": "turn", "executor": "doc-synthesizer"},
            {"task_name": "settle", "executor": "deterministic-fallback"},
        ],
        "caps": ["max_turns", "max_no_progress", "max_spend"],
        "hooks": ["terminal"],
    }


class TestValidateSpec:
    def test_happy_spec_normalizes(self):
        norm = validate_spec(_spec())
        assert norm["caps"] == sorted(_spec()["caps"])
        assert [a["task_name"] for a in norm["activities"]] == ["settle", "turn"]  # sorted
        assert norm["transitions"]["waiting"] == []

    def test_unknown_key_is_structurally_rejected(self):
        # Runtime configuration has nowhere to live in a spec: "model" is not a spec key.
        bad = _spec()
        bad["model"] = "some-llm-v3"
        with pytest.raises(ValueError, match="unknown="):
            validate_spec(bad)

    @pytest.mark.parametrize("drop", ["name", "transitions", "activities", "caps", "hooks"])
    def test_missing_key_is_rejected(self, drop):
        bad = _spec()
        del bad[drop]
        with pytest.raises(ValueError, match="missing="):
            validate_spec(bad)

    def test_runtime_values_cannot_hide_in_activity_records(self):
        bad = _spec()
        bad["activities"] = [{"task_name": "turn", "executor": "x", "temperature": 0.2}]
        with pytest.raises(ValueError, match="exactly"):
            validate_spec(bad)

    def test_executor_must_be_a_stable_logical_name(self):
        for bad_executor in ("", None, 7):
            bad = _spec()
            bad["activities"] = [{"task_name": "turn", "executor": bad_executor}]
            with pytest.raises(ValueError):
                validate_spec(bad)

    def test_duplicate_activity_task_names_rejected(self):
        bad = _spec()
        bad["activities"] = [
            {"task_name": "turn", "executor": "a"},
            {"task_name": "turn", "executor": "b"},
        ]
        with pytest.raises(ValueError, match="duplicate"):
            validate_spec(bad)

    def test_empty_name_or_transitions_rejected(self):
        bad = _spec()
        bad["name"] = ""
        with pytest.raises(ValueError):
            validate_spec(bad)
        bad = _spec()
        bad["transitions"] = {}
        with pytest.raises(ValueError):
            validate_spec(bad)

    def test_non_json_leaf_values_rejected(self):
        # caps carrying a raw object (e.g. a UUID instance) fail the string-only rule —
        # no silent str() laundering is possible.
        bad = _spec()
        bad["caps"] = ["max_turns", object()]
        with pytest.raises(ValueError):
            validate_spec(bad)


class TestFingerprint:
    def test_prefix_and_shape(self):
        fp = compute_fingerprint(_spec())
        assert fp.startswith(FINGERPRINT_PREFIX)
        hex_part = fp.removeprefix(FINGERPRINT_PREFIX)
        assert len(hex_part) == 64
        assert all(c in "0123456789abcdef" for c in hex_part)

    def test_key_order_is_irrelevant(self):
        a = _spec()
        b = {k: a[k] for k in reversed(list(a))}  # different insertion order
        assert compute_fingerprint(a) == compute_fingerprint(b)

    def test_list_order_within_spec_is_normalized(self):
        a = _spec()
        b = copy.deepcopy(a)
        b["activities"] = list(reversed(b["activities"]))
        b["caps"] = list(reversed(b["caps"]))
        assert compute_fingerprint(a) == compute_fingerprint(b)

    def test_transition_target_sets_collapse_to_sorted_lists(self):
        a = _spec()
        b = copy.deepcopy(a)
        b["transitions"]["running"] = ["failed", "running", "waiting", "succeeded", "cancelled"]
        assert compute_fingerprint(a) == compute_fingerprint(b)

    @pytest.mark.parametrize("mutate", [
        lambda s: s.update(name="other"),
        lambda s: s["activities"][0].update(executor="renamed-executor"),
        lambda s: s["caps"].remove("max_spend"),
        lambda s: s["hooks"].append("progress"),
        lambda s: s["transitions"]["waiting"].append("running"),
    ])
    def test_structural_changes_change_the_fingerprint(self, mutate):
        base = compute_fingerprint(_spec())
        changed = _spec()
        mutate(changed)
        assert compute_fingerprint(changed) != base

    def test_logical_rename_is_a_drift_but_executor_rebinding_is_too_by_design(self):
        # task_name and executor are both structure: swapping the LOGICAL identity drifts.
        # Physical model routing never appears in the spec, so it cannot drift anything.
        a = _spec()
        b = copy.deepcopy(a)
        b["activities"][0]["executor"] = "doc-synthesizer-v2"
        assert compute_fingerprint(a) != compute_fingerprint(b)

    def test_canonical_json_is_ascii_strict_and_separated(self):
        text = canonical_json(_spec())
        assert ", " not in text and '": ' not in text
        assert text.isascii()

    def test_rejects_nan_payloads(self):
        bad = _spec()
        bad["caps"] = [float("nan")]
        with pytest.raises(ValueError):
            validate_spec(bad)
