"""A session that ended on a Claude Code API error that is neither a usage cap nor a
signed-out login: "Your computer went to sleep mid-response", a 5xx, a dropped connection.

Claude Code writes it as a synthetic assistant record flagged `isApiErrorMessage` and then
returns to the prompt with no `Stop` hook. Requires that flag on the LAST conversational
record, never the text: an agent that WRITES "API Error" in a reply is not a failed turn.

Stdlib only, like the rest of this package.
"""
from __future__ import annotations

from .signed_out import _last_conversational, _record_text, signed_out_in_record
from .usage_limit import limit_in_record

TEXT_MAX = 600


def api_error_in_record(record) -> str | None:
    """The error text, if this record IS a generic API-error notice."""
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return None
    if not record.get("isApiErrorMessage"):
        return None
    if limit_in_record(record) is not None or signed_out_in_record(record) is not None:
        return None  # those have their own handling: pause the box / sign in again
    return " ".join(_record_text(record).split())[:TEXT_MAX] or "API error (no text)"


def api_error_at_transcript_end(path) -> tuple[str, str] | None:
    """(record uuid, error text) when the conversation last ended on a generic API
    error, else None. The uuid lets a caller act once per record."""
    record = _last_conversational(path)
    if record is None:
        return None
    text = api_error_in_record(record)
    return (str(record.get("uuid") or record.get("timestamp") or ""), text) if text else None
