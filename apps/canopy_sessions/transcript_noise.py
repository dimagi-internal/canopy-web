"""What is NOT a chat message, defined once.

A Claude Code transcript encodes far more than the conversation: the harness
injects task notifications, system reminders and local command output as records
of `type: "user"`, indistinguishable by shape from something a person typed. Ship
those verbatim and the machine's own event stream renders on the HUMAN's side of
the chat — observed on labs 2026-07-26, where a session's transcript read:

    [1146] role=user  <task-notification> … Monitor event: "Labs deploy run …"
    [1160] role=user  <task-notification> … Deploy to Fargate: success
    [1168] role=user  Can you add the ability for me to attach something…

Only the last of those was the user.

The RULE itself (`SYSTEM_NOISE_PREFIXES` / `is_system_noise`) now lives in
`canopy_transcript.noise`, the transcript core the server and both runners
already share — it used to be defined here AND privately in the runner, and the
two drifted: two prefixes were added here on 2026-07-26 and the runner's copy
never grew them (see that module's docstring). This module keeps the
server-specific half (`scrub_nul`) and re-exports the rule so existing importers
are unaffected.

Server-side ENFORCEMENT is still the point. A producer-side filter only ever
covers producers that have been updated, and the runner is a laptop daemon on a
checkout that lags, so every path that admits runner-supplied rows applies the
rule again on arrival: `persist_transcript_rows` (the durable write),
`post_session_stream` (the live fanout), and `tail_as_messages` (the binding
tail a local session renders before its backfill lands).
"""
from __future__ import annotations

from canopy_transcript import SYSTEM_NOISE_PREFIXES, is_system_noise

__all__ = ["SYSTEM_NOISE_PREFIXES", "is_system_noise", "scrub_nul"]


def scrub_nul(value):
    """Strip NUL bytes from every string in `value` (str / dict / list).

    Postgres rejects 0x00 outright in text and jsonb. A tool result is raw bytes
    from whatever the tool touched, so a `Read` of a compressed or binary file
    carries one straight into the write path — observed on labs 2026-07-26: one
    row in 683 took down the whole batch with a 500, and because the write is a
    single transaction and the runner retries forever, that session's history
    could never rebuild.

    Server-side for the same reason the noise filter is enforced here (see the
    module docstring): `canopy_transcript.scrub` does this producer-side too, but
    a producer-side scrub only covers producers that have been updated, and the
    runner is a laptop daemon on a checkout that lags. This is the single durable
    write path, so enforcing it here is what makes the guarantee hold.
    """
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, dict):
        return {k: scrub_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_nul(v) for v in value]
    return value
