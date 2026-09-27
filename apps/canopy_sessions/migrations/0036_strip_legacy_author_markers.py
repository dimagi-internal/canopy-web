"""Strip the legacy author marker from already-recorded rows, recovering
attribution where the marker's claim checks out.

canopy used to prepend `[canopy from="…" user=N turn=…]` to a chat send's
prompt at claim (`apps.canopy_sessions.authorship`, the marking half removed
2026-09-27 — see that module's docstring). Attribution moved server-side
(`services.persist_transcript_rows`'s new-row matching), but rows recorded
before the change — and anything backfilled from the days around it — still
carry the marker in `plaintext` (and often in `content["text"]` too).

For each such row: parse it with the SAME tolerant rule `authorship.parse`
now uses (frozen here rather than imported, so this migration keeps meaning
the same thing regardless of any future change to that module). If the
marker's turn exists on THIS session and that turn's initiator matches who
the marker claims, it is exactly the vouching rule the live path already
applies — set `author` (from the marker's own name, the same source the live
path uses — no display-name recompute) and `source_turn_id`. Either way,
strip the marker: it is never the person's own words, vouched or not, and
leaving it behind is the bug this whole change fixes.

Idempotent — a row already stripped no longer starts with the marker, so a
re-run touches nothing. Not reversible: recovering the marker text serves
nobody.
"""
from __future__ import annotations

import re
import uuid

from django.db import migrations

_MARKER = re.compile(
    r'^\[canopy from="(?P<name>(?:[^"\\]|\\.)*)" '
    r'(?:user=(?P<user>\d+)|contact=(?P<contact>\d+)) '
    r'turn=(?P<turn>[0-9a-f]{32})\]\n?'
)


def _unescape(name: str) -> str:
    return re.sub(r"\\(.)", r"\1", name)


def _parse(text: str):
    """authorship.parse's tolerant rule, copied rather than imported. Returns
    (author dict or None, bare text, turn hex or None)."""
    m = _MARKER.match(text)
    if m is None:
        return None, text, None
    author = {"name": _unescape(m["name"])}
    if m["user"] is not None:
        author["user_id"] = int(m["user"])
    else:
        author["contact_id"] = int(m["contact"])
    return author, text[m.end():], m["turn"]


def strip_legacy_author_markers(apps, schema_editor):
    Message = apps.get_model("canopy_sessions", "Message")
    Turn = apps.get_model("harness", "Turn")

    rows = Message.objects.filter(role="user", plaintext__startswith='[canopy from=')
    stripped = vouched = 0
    for msg in rows.iterator():
        author, bare, turn_hex = _parse(msg.plaintext)
        if author is None:
            # Starts with the literal text but does not actually match the
            # marker grammar (malformed, or someone typed the prefix by hand).
            continue
        msg.plaintext = bare
        update_fields = ["plaintext"]
        content = msg.content or {}
        if isinstance(content.get("text"), str):
            _author, bare_content, _turn = _parse(content["text"])
            msg.content = {**content, "text": bare_content}
            update_fields.append("content")
        identity = (author.get("user_id"), author.get("contact_id"))
        turn = Turn.objects.filter(
            pk=uuid.UUID(turn_hex), chat_session_id=msg.session_id,
        ).values("initiator_user_id", "initiator_contact_id").first()
        if turn is not None and (turn["initiator_user_id"], turn["initiator_contact_id"]) == identity:
            msg.author = author
            msg.source_turn_id = uuid.UUID(turn_hex)
            update_fields += ["author", "source_turn_id"]
            vouched += 1
        msg.save(update_fields=update_fields)
        stripped += 1
    if stripped:
        print(f"\n  stripped {stripped} legacy marker row(s), {vouched} vouched")


class Migration(migrations.Migration):
    dependencies = [("canopy_sessions", "0035_draft_visibility")]
    operations = [migrations.RunPython(strip_legacy_author_markers, migrations.RunPython.noop)]
