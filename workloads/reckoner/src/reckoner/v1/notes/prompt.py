"""Pinned instructions and allowlisted data stay separate."""

import json

from reckoner.resources import PROMPTS
from reckoner.v1.notes.validate import note_data

MODEL = "anthropic/claude-haiku-4-5-20251001"


def build_note_request(case, evidence, score, config):
    model = config["note_model"]
    if (
        model["model"] != MODEL
        or model["provider"] != "anthropic"
        or model["prompt_version"] != "note-v1"
    ):
        raise ValueError("unsupported note model/prompt")
    if case["tenant_id"] != evidence["tenant_id"] or case["evidence_id"] != evidence["evidence_id"]:
        raise ValueError("case evidence mismatch")
    return {
        "model": MODEL,
        "system": (PROMPTS / "case-note-v1.md").read_text(),
        "messages": [
            {"role": "user", "content": json.dumps(note_data(evidence, score), sort_keys=True)}
        ],
        "max_tokens": config["limits"]["max_output_tokens"],
        "temperature": 0,
    }
