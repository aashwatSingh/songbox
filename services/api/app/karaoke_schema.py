from __future__ import annotations

# JSON Schema (Draft 2020-12) for karaoke.json v1 -- the versioned document GET
# /tracks/{id}/package assembles from the flat karaoke_packages DB row (M5) and validates before
# returning. CLAUDE.md: "karaoke.json is a versioned schema. Any shape change needs a migration
# path, not a silent bump" -- bumping the app.routes.tracks.KARAOKE_SCHEMA_VERSION constant in
# lockstep with a NEW, separately-named schema dict here (never mutating this one in place) is
# that migration path.
KARAOKE_SCHEMA_V1: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "karaoke.json v1",
    "type": "object",
    "required": [
        "schema_version",
        "track_id",
        "words",
        "pitch",
        "tempo_bpm",
        "beats_ms",
        "sections_ms",
    ],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"const": 1},
        "track_id": {"type": "string"},
        "words": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["idx", "start_ms", "end_ms", "confidence", "text"],
                "additionalProperties": False,
                "properties": {
                    "idx": {"type": "integer", "minimum": 0},
                    "start_ms": {"type": "integer", "minimum": 0},
                    "end_ms": {"type": "integer", "minimum": 0},
                    "confidence": {"type": "number"},
                    "text": {"type": ["string", "null"]},
                },
            },
        },
        "pitch": {
            "type": "object",
            "required": ["model", "hop_ms", "frames"],
            "additionalProperties": False,
            "properties": {
                "model": {"type": "string"},
                "hop_ms": {"type": "integer", "minimum": 1},
                "frames": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["time_ms", "hz", "confidence"],
                        "additionalProperties": False,
                        "properties": {
                            "time_ms": {"type": "integer", "minimum": 0},
                            "hz": {"type": ["number", "null"]},
                            "confidence": {"type": "number"},
                        },
                    },
                },
            },
        },
        "tempo_bpm": {"type": "number", "minimum": 0},
        "beats_ms": {"type": "array", "items": {"type": "integer", "minimum": 0}},
        "sections_ms": {"type": "array", "items": {"type": "integer", "minimum": 0}},
    },
}
