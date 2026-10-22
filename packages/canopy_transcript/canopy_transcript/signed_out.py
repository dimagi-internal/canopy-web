"""A box whose Claude login is gone: recognise it in a transcript, and name it.

When Claude Code has no usable sign-in it does not fail the way a runner can see.
The prompt lands, and the "reply" is a synthetic assistant record flagged
`isApiErrorMessage` whose text is `Not logged in · Please run /login`. A laptop
turn finishes as soon as the prompt is delivered, so the turn read DONE, the
error rendered as the agent's answer, and the box kept reporting ready and kept
claiming — each new message got the same one-line "reply" (acedimagi-mbp-cdp,
2026-10-22).

Recognised here, once, for both the runner (which marks itself not ready) and
the transcript rows (which say the record is an error, not a reply). Requires
the API-error flag, never the text alone: an agent that WRITES "please run
/login" in a reply must not take its own box out of the fleet.

Stdlib only, like the rest of this package.
"""
from __future__ import annotations

#: The CLI's own sentences for a credential a person must sign in again. Mirrors
#: `runner/ec2/cloud_runner._AUTH_REQUIRED_MARKERS`, which keeps its own copy
#: because that runner must recognise a dead login even when this package failed
#: to import on the box.
AUTH_REQUIRED_MARKERS = (
    "not logged in",
    "please run /login",
    "auth token expired or invalid",
    "refresh token expired",
    "invalid api key",
    "oauth session expired",
)

#: The readiness note a box reports while it is signed out. The server keys on
#: this prefix (`Runner.signed_out`): a signed-out box claims nothing, and a turn
#: waiting on it says so instead of "Picking this up on …".
SIGNED_OUT_NOTE = "Claude is not signed in on this machine"

_NON_CONVERSATIONAL = {"system", "queue-operation", "summary"}


def is_auth_required(text: str) -> bool:
    """Does this text say the Claude credential needs a person to sign in?"""
    low = (text or "").lower()
    return any(m in low for m in AUTH_REQUIRED_MARKERS)


def signed_out_in_record(record) -> str | None:
    """The CLI's message, if this transcript record IS its "not signed in" notice."""
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return None
    if not record.get("isApiErrorMessage"):
        return None
    text = _record_text(record)
    if record.get("error") == "authentication_failed" or is_auth_required(text):
        return text or "not signed in"
    return None


def _last_conversational(path) -> dict | None:
    from .records import read_tail_records  # noqa: PLC0415

    for record in reversed(read_tail_records(path)):
        if record.get("type") in _NON_CONVERSATIONAL or record.get("isSidechain"):
            continue
        return record
    return None


def signed_out_at_transcript_end(path) -> tuple[str, str] | None:
    """(record uuid, message) when this conversation last ended on the CLI saying
    it is not signed in, else None. The uuid lets a caller act once per record."""
    record = _last_conversational(path)
    if record is None:
        return None
    text = signed_out_in_record(record)
    return (str(record.get("uuid") or record.get("timestamp") or ""), text) if text else None


def answered_at_transcript_end(path) -> bool:
    """Did this conversation last end on a real model reply? The proof a box's
    login works again — any session on the box shares the one login."""
    record = _last_conversational(path)
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return False
    if record.get("isApiErrorMessage"):
        return False
    return (record.get("message") or {}).get("model") not in (None, "", "<synthetic>")


def _record_text(record: dict) -> str:
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""
