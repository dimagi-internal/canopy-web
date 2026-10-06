"""Real JSON log lines for production.

The production LOGGING used a *format string* shaped like JSON:
`{"severity":"%(levelname)s","message":"%(message)s",...}`. Any message with a
quote, a backslash or a newline produced a line no JSON parser accepts, and it
carried no timestamp and no logger name — so "when, and which component" had to
be guessed from neighbouring uvicorn lines. A Formatter class escapes properly
and can carry structured `extra=` fields (the provenance log's `turn_id`,
`credential`, `parent_turn`, …) as fields rather than prose.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

# Attributes every LogRecord has; anything else on a record came from `extra=`.
_STANDARD = frozenset(vars(logging.LogRecord("x", 0, "x", 0, "x", None, None))) | {
    "message", "asctime", "request_id", "taskName",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: timestamp, severity, logger, message,
    request_id, plus any `extra=` fields."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict = {
            "timestamp": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(
                timespec="milliseconds"),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "") or "",
            "module": record.module,
        }
        for key, value in vars(record).items():
            if key not in _STANDARD and not key.startswith("_") and key not in out:
                out[key] = value
        if record.exc_info:
            out["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            out["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(out, default=str, ensure_ascii=False)
