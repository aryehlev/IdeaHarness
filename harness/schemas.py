"""Output schemas, a small JSON Schema validator, and JSON extraction from free text.

Harnesses with native structured output (Claude Code, Codex) get the schema directly.
Every other harness is asked to end its reply with a JSON block, which is extracted and
validated here.
"""

from __future__ import annotations

import copy
import json
import re

SCORE_KEYS = ["novelty", "demand", "goal_fit", "feasibility"]

COMPETITOR = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "url", "stars", "difference"],
    "properties": {
        "name": {"type": "string"},
        "url": {"type": "string"},
        "stars": {"type": ["integer", "null"]},
        "difference": {"type": "string", "description": "Why the pitched idea is not this."},
    },
}

IDEA_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "name", "one_liner", "problem", "solution", "target_users", "why_now",
        "why_this_person", "hook", "mvp", "known_competitors", "pivot_of",
    ],
    "properties": {
        "name": {"type": "string"},
        "one_liner": {"type": "string", "description": "The README tagline, under 15 words."},
        "problem": {"type": "string"},
        "solution": {"type": "string"},
        "target_users": {"type": "string"},
        "why_now": {"type": "string", "description": "What changed recently that makes this possible or needed."},
        "why_this_person": {"type": "string", "description": "Why this idea fits the person in the profile specifically."},
        "hook": {"type": "string", "description": "What grabs the target audience within ten seconds (for a stars goal: of opening the README)."},
        "mvp": {"type": "string", "description": "What this person ships as a first version, and roughly how long it takes."},
        "known_competitors": {"type": "array", "items": COMPETITOR},
        "pivot_of": {"type": ["string", "null"], "description": "Name of the graveyard idea this pivots, or null."},
    },
}

VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "cause_of_death", "summary", "attacks", "prior_art", "scores", "what_would_save_it"],
    "properties": {
        "verdict": {"type": "string", "enum": ["kill", "survive"]},
        "cause_of_death": {"type": "string", "description": "One sentence. Empty string if it survives."},
        "summary": {"type": "string", "description": "Two or three sentences on the overall judgement."},
        "attacks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["angle", "severity", "finding", "evidence"],
                "properties": {
                    "angle": {
                        "type": "string",
                        "enum": ["prior_art", "demand", "goal_fit", "feasibility", "timing", "platform_risk", "other"],
                    },
                    "severity": {"type": "string", "enum": ["fatal", "serious", "minor"]},
                    "finding": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}, "description": "URLs or exact tool output you retrieved."},
                },
            },
        },
        "prior_art": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "url", "stars", "last_active", "overlap"],
                "properties": {
                    "name": {"type": "string"},
                    "url": {"type": "string"},
                    "stars": {"type": ["integer", "null"]},
                    "last_active": {"type": "string", "description": "Date of last push or release, or 'unknown'."},
                    "overlap": {"type": "string", "enum": ["same", "partial", "adjacent"]},
                },
            },
        },
        "scores": {
            "type": "object",
            "additionalProperties": False,
            "required": SCORE_KEYS,
            "properties": {k: {"type": "integer", "minimum": 1, "maximum": 10} for k in SCORE_KEYS},
        },
        "what_would_save_it": {"type": "string", "description": "The most promising concrete pivot."},
    },
}

PROFILE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["profile_markdown", "questions"],
    "properties": {
        "profile_markdown": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}, "description": "Up to five questions."},
    },
}


def schema_instructions(schema: dict) -> str:
    return (
        "## Output format\n\n"
        "When you are done, give your final answer as one JSON object that matches the JSON Schema "
        "below. Put it in a ```json fenced block at the very end of your reply, with nothing after it.\n\n"
        "```json\n" + json.dumps(schema, indent=1) + "\n```"
    )


def portable_schema(schema: dict) -> dict:
    """Strip keywords some structured-output implementations reject; validation still enforces them."""
    schema = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            for key in ("minimum", "maximum"):
                node.pop(key, None)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return schema


_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def validate(data, schema: dict, path: str = "$") -> list[str]:
    """Validate (and lightly normalise in place) data against the subset of JSON Schema used here.

    Normalisation: integer-valued floats and numeric strings become ints where an integer is
    expected, and keys not in the schema are dropped. Returns a list of error strings.
    """
    errors: list[str] = []
    types = schema.get("type")
    types = [types] if isinstance(types, str) else (types or [])
    if types and not any(_TYPES[t](data) for t in types):
        errors.append(f"{path}: expected {' or '.join(types)}, got {type(data).__name__}")
        return errors
    if "enum" in schema and data not in schema["enum"]:
        errors.append(f"{path}: {data!r} is not one of {schema['enum']}")
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        if "minimum" in schema and data < schema["minimum"]:
            errors.append(f"{path}: {data} is below {schema['minimum']}")
        if "maximum" in schema and data > schema["maximum"]:
            errors.append(f"{path}: {data} is above {schema['maximum']}")
    if isinstance(data, dict) and "properties" in schema:
        props = schema["properties"]
        for key in [k for k in data if k not in props]:
            del data[key]
        for key in schema.get("required", []):
            if key not in data:
                errors.append(f"{path}: missing required key '{key}'")
        for key, sub in props.items():
            if key in data:
                data[key] = _coerce(data[key], sub)
                errors += validate(data[key], sub, f"{path}.{key}")
    if isinstance(data, list) and "items" in schema:
        for i, item in enumerate(data):
            data[i] = _coerce(item, schema["items"])
            errors += validate(data[i], schema["items"], f"{path}[{i}]")
    return errors


def _coerce(value, schema: dict):
    types = schema.get("type")
    types = [types] if isinstance(types, str) else (types or [])
    if "integer" in types:
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip().replace(",", "")):
            return int(value.strip().replace(",", ""))
    return value


def extract_json(text: str) -> dict | None:
    """Return the last JSON object in a reply: fenced ```json blocks first, then any bare object."""
    if not text:
        return None
    for block in reversed(re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.S)):
        try:
            value = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    decoder = json.JSONDecoder()
    found = None
    i = text.find("{")
    while i != -1:
        try:
            value, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(value, dict):
            found = value
        i = text.find("{", end)
    return found
