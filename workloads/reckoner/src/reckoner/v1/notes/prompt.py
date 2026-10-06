"""Pinned instructions and allowlisted data stay separate."""

import json

from reckoner.resources import PROMPTS
from reckoner.v1.notes.validate import note_data

MODEL = "anthropic/claude-haiku-4-5-20251001"


def build_note_context(case, evidence, score, config):
    """Frozen routing and original scorer confidence are separate evidence values."""
    if (
        case["tenant_id"] != evidence["tenant_id"]
        or case["evidence_id"] != evidence["evidence_id"]
        or case["config_id"] != config["config_id"]
    ):
        raise ValueError("note context does not match frozen decision inputs")
    if case["scorer_status"] == "succeeded":
        if score.get("attempt_status") != "responded" or any(
            score.get(key) != case[key]
            for key in ("tenant_id", "run_id", "task_id", "call_id", "raw_probability")
        ):
            raise ValueError("note context does not match original scorer response")
    return {
        **note_data(evidence, score),
        "routing": {
            "raw_probability": case["raw_probability"],
            "effective_probability": case["effective_probability"],
            "score_mode": config["score_mode"],
            "calibration_id": config["calibration_id"],
        },
    }


def build_note_request(case, evidence, score, config):
    model = config["note_model"]
    if (
        model["model"] != MODEL
        or model["provider"] != "anthropic"
        or model["prompt_version"] != "note-v1"
    ):
        raise ValueError("unsupported note model/prompt")
    return {
        "model": MODEL,
        "system": (PROMPTS / "case-note-v1.md").read_text(),
        "messages": [
            {
                "role": "user",
                "content": json.dumps(
                    build_note_context(case, evidence, score, config), sort_keys=True
                ),
            }
        ],
        "max_tokens": config["limits"]["max_output_tokens"],
        "temperature": 0,
    }
