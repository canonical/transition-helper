"""JSON schema for the model's final_report."""

from __future__ import annotations

import jsonschema

CATEGORIES = [
    "FTBFS", "DEPWAIT", "BUILD_INFRA", "BUILD_PENDING", "SUPERSEDED",
    "AUTOPKGTEST_REGRESSION", "AUTOPKGTEST_PENDING", "DEPENDS_ON_UNMIGRATED", "UNINSTALLABLE",
    "MISSING_BUILD", "BLOCKED", "OTHER", "NONE",
]

FINAL_REPORT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["category", "root_cause", "is_transition_related", "confidence", "evidence",
                 "fixes", "kb_matches", "suggested_actions"],
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "root_cause": {"type": "string", "minLength": 10},
        "is_transition_related": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["log_id", "lines"],
                "properties": {
                    "log_id": {"type": "string"},
                    "lines": {"type": "string", "description": "line or range, e.g. '1234' or '1200-1240'"},
                    "note": {"type": "string"},
                },
            },
        },
        "fixes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["target_pkg", "target_version", "rationale"],
                "properties": {
                    "target_pkg": {"type": "string"},
                    "target_version": {"type": "string"},
                    "rationale": {"type": "string",
                                  "description": "why THIS package is the right place to fix it"},
                    "patch_ref": {"type": "string",
                                  "description": "patch_name of a successful propose_patch call"},
                },
            },
        },
        "kb_matches": {"type": "array", "items": {"type": "string"}},
        "suggested_actions": {"type": "array", "items": {"type": "string"}},
        "bug_draft": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "description"],
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
            },
        },
    },
}


def validate_final_report(report: dict) -> list[str]:
    v = jsonschema.Draft202012Validator(FINAL_REPORT_SCHEMA)
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}"
            for e in sorted(v.iter_errors(report), key=lambda e: list(e.absolute_path))]
