"""Real JSON log lines for production.

The old production formatter was a `%`-format string shaped like JSON:
`{"severity":"%(levelname)s","message":"%(message)s",...}`. Any message with a
quote, a backslash or a newline produced a line no JSON parser accepts, and it
carried no timestamp, no logger name and no request id — so CloudWatch could
neither parse it nor say when or where it came from.

`JsonFormatter` builds a dict and `json.dumps` it. Anything a caller passed as
`extra=` (e.g. the provenance fields on `TURN_CREATED`) becomes a top-level
field, so a log query can filter on `credential` or `parent_turn` directly.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

# Attributes every LogRecord has; anything else on a record came from `extra=`.
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message", "asctime", "request_id",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "timestamp": dt.datetime.fromtimestamp(record.created, tz=dt.timezone.utc)
            .isoformat(timespec="milliseconds"),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "request_id": getattr(record, "request_id", "") or "",
        }
        for key, value in vars(record).items():
            if key not in _STANDARD and not key.startswith("_") and key not in out:
                out[key] = value
        if record.exc_info:
            out["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            out["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(out, default=str, ensure_ascii=False)
