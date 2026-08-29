from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_STANDARD_LOGRECORD_KEYS = frozenset(
    vars(logging.LogRecord("x", logging.INFO, "x", 1, "x", (), None)).keys()
)


class JSONFormatter(logging.Formatter):
    """Formats each LogRecord as one JSON line. Only fields explicitly set on the record via
    `extra={...}` at the call site are included beyond timestamp/level/logger/message -- nothing
    is captured implicitly, so a field nobody intended to log (raw audio, lyrics, a signed URL)
    can never leak in just because it happened to be a local variable somewhere in the call
    stack. CLAUDE.md's "never log raw audio, lyrics, or signed URLs" rule is enforced by this
    formatter never reading anything beyond what a caller explicitly passed via `extra`.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in vars(record).items():
            if key not in _STANDARD_LOGRECORD_KEYS:
                payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    """Replaces the root logger's handlers with a single stdout handler emitting one JSON line
    per record. Idempotent -- clears existing handlers before adding its own rather than
    appending, so calling this more than once (app startup, then again in a test) never produces
    duplicate log lines per record.
    """
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)
