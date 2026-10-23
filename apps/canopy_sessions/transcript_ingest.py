"""Recording a session's transcript: the ordinal scheme and transcript
identity (a reused emdash name or task is a new session), persisting shipped
rows and backfills, the stream-desired flag, the next free index, and the
projection of the dev stub's ledger into Message rows.
"""
from __future__ import annotations

import datetime as _dt
import re
import uuid

from canopy_transcript import BLOCK_STRIDE
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.harness import provenance
from apps.harness.models import Turn

from . import activity as session_activity
from . import attach, authorship, services, transcript_read
from .models import Message, RunnerBinding, Session
from .transcript_noise import is_system_noise, scrub_nul


def storage_content(content: dict, text: str) -> dict:
    """The row's `content` as STORED — the wire payload minus its `text` key.

    The wire and the row deliberately share one shape (the live frame's `block`,
    the backfill payload, and this column are the same dict), and that shape has
    to carry `text` because the client builds a message from the frame alone.
    Storage does not: `plaintext` is its own column, and nothing on the render
    path reads `content["text"]` — MessageItem and ToolCallPair both use
    `plaintext`.

    Keeping the copy cost 36% of all stored transcript bytes (measured over
    20,585 rows of live transcripts, 2026-07-26: 9.4MB of 26.2MB), mostly tool
    result bodies duplicated verbatim. Only the redundant key is dropped: a
    `text` that somehow DIFFERS from plaintext is kept, and every other key
    (`id`, `name`, `input`, `tool_use_id`, `client_id`) is untouched.
    """
    if content.get("text") != text:
        return content
    return {k: v for k, v in content.items() if k != "text"}


# The transcript-ordinal scheme this build writes. Bumped whenever the mapping
# from a transcript record to a `turn_index` changes; see Session.ordinal_scheme.
ORDINAL_SCHEME = 1


def _ensure_current_ordinal_scheme(locked_session, offset: int = 0) -> int:
    """Drop rows written under a superseded ordinal scheme, so the incoming ones
    can't interleave with them.

    Two schemes in one session is not a cosmetic problem: `turn_index` is the
    sort order AND the paging cursor, so an old row at 500 and a new row for the
    same record at 32,000 would render the conversation shuffled, and
    `get_or_create` would never notice they are the same record.

    This is `reset` — the existing first-class action — fired automatically on
    the first write instead of waiting for someone to run it. Derived rows only;
    Turns and their ledger are never touched. Returns rows deleted.

    Scoped to the current epoch (`offset`), for the same reason
    `ensure_transcript_identity` is — and with a stronger justification, because
    the remedy this function relies on is not available across a transfer.
    "Drop and re-derive" is safe only while something can re-derive; the rows a
    transfer carried across were derived from a transcript on a box this server
    may never be able to reach again, so dropping them is final. The
    interleaving hazard does not reach across the boundary either: the offset
    guarantees every inherited row sorts below every new one whatever scheme
    composed their numbers, so epoch order dominates scheme and the render stays
    chronological. Two epochs in two schemes is therefore fine; two SCHEMES in
    ONE epoch is what this still prevents.
    """
    if locked_session.ordinal_scheme == ORDINAL_SCHEME:
        return 0
    deleted, _ = Message.objects.filter(
        session=locked_session, turn_index__gte=offset
    ).delete()
    locked_session.ordinal_scheme = ORDINAL_SCHEME
    locked_session.save(update_fields=["ordinal_scheme", "updated_at"])
    # The dropped rows are about to be re-shipped and folded again; recount from
    # what is left so they are not counted twice.
    session_activity.rebuild(locked_session)
    return deleted


def _open_epoch(binding, session) -> None:
    """Lift `binding.index_offset` past every row `session` already holds, so the
    next transcript's ordinals land ABOVE that history instead of on top of it.

    Rounded UP to a stride boundary past the high-water mark, so a stored index
    minus the offset still decomposes into (record, block). `+ BLOCK_STRIDE`
    rather than a bare ceiling leaves one clear stride of gap between the epochs —
    without it an offset landing exactly on the mark lets the new transcript's
    record 0 sit in the same stride as the old tail. A session with no rows keeps
    its offset. Caller holds the binding lock and saves the binding."""
    high = Message.objects.filter(session=session).aggregate(m=Max("turn_index"))["m"]
    if high is not None:
        binding.index_offset = ((int(high) // BLOCK_STRIDE) + 2) * BLOCK_STRIDE


def ensure_transcript_identity(session, transcript_id: str) -> int:
    """Keep two transcripts from sharing one session's ordinals when the transcript
    being shipped is not the one the held rows came from.

    The sibling of `_ensure_current_ordinal_scheme`, for the other way a session's
    `turn_index` space can be invalidated. `turn_index` is a PER-FILE ordinal, so
    rows from two files in one index space are not merely untidy: the
    first_index/last_index markers derived from the old file are nonsense against
    the new one, and a shorter successor sits entirely below the old high-water
    mark and is suppressed forever (issue #615).

    A changed id opens a NEW EPOCH — exactly what `transfer_session` does — so the
    new transcript's rows land above the old ones and the old ones stay. It used
    to drop them and re-derive, on the claim that the runner re-ships the whole
    history; that holds only when the new file CONTAINS the old conversation, and
    nothing guarantees it. Measured 2026-10-20 (issue #1731) on session f6e30263:
    a credential failover on cloud-ec2-2 started a fresh claude conversation for
    the same session, the next ship carried its id, and every earlier row was
    deleted with nothing left that could re-derive them. A changed id inside one
    session is the same conversation continuing in a new file (a failover,
    `/clear`, a box that could not resume), and its history is the session's.

    The case dropping was built for — a reused emdash task NAME pointing this
    binding at a genuinely different conversation — no longer reaches here: the
    forks catch it first and move the binding to a new Session, leaving the old
    one whole (`fork_if_name_reused` on the ship path, `fork_if_task_changed` on
    the report path).

    Dropping remains for the one case it is provably safe: a BLANK recorded id
    (a session that predates this field, or a binding a transfer or fork just
    reset). Those rows are of unknown provenance, the shipper sends the full
    history for exactly that reason, and the drop is scoped to the current epoch —
    at or above `binding.index_offset` — so history carried across a transfer is
    never touched (session 169212e2, 2026-09-12, lost 60+ rows before that scope
    existed).

    Blank incoming `transcript_id` (an old runner) is a no-op: it carries no claim
    about provenance. Returns rows deleted (0 when an epoch was opened instead).
    """
    if not transcript_id:
        return 0
    with transaction.atomic():
        # The BINDING is the only row that needs locking — it holds the flag that
        # makes this idempotent, so serializing on it is what stops two concurrent
        # ships both dropping. Deliberately NOT also locking the Session:
        # `harness.replace_reported_sessions` takes its locks binding-first and
        # then writes the session row, so grabbing them in the other order here
        # would make the two a deadlock pair — every ~10s report against every
        # ship. `persist_transcript_rows` still takes its own Session lock
        # afterwards, in its own transaction, exactly as before.
        binding = (
            RunnerBinding.objects.select_for_update().filter(session=session).first()
        )
        if binding is None or binding.transcript_id == transcript_id:
            return 0
        if binding.transcript_id:
            # Same session, new file: keep what is held and shift the new
            # transcript above it. The markers reported back to the runner are
            # scoped to the new epoch (apps/harness/api.py::get_session_streams),
            # so they read empty and the runner ships the new file from its start.
            _open_epoch(binding, session)
            binding.transcript_id = transcript_id
            binding.save(update_fields=["transcript_id", "index_offset", "updated_at"])
            return 0
        # First sighting (blank) still drops: a session that predates this field
        # is exactly the state issue #615 describes — rows of unknown provenance,
        # possibly a previous task's — and the shipper is sending the full history
        # for precisely that reason. Rebuilding once is cheap and self-healing.
        deleted, _ = Message.objects.filter(
            session=session, turn_index__gte=binding.index_offset
        ).delete()
        binding.transcript_id = transcript_id
        binding.save(update_fields=["transcript_id", "updated_at"])
        # Recount activity from what survived (inherited history below the
        # offset), so the re-shipped rows are not counted twice. Locking the
        # session AFTER the binding is the order replace_reported_sessions and
        # fork_if_name_reused already use — the deadlock warned of above is the
        # reverse order.
        session_activity.rebuild(Session.objects.select_for_update().get(pk=session.pk))
        return deleted


#: Set on a session the runner report UN-ARCHIVED (harness.replace_reported_sessions)
#: and cleared by the first transcript ship that follows. Until then nothing says
#: whether the task was REOPENED or is a NEW task that reused a closed one's name.
REOPENED_KEY = "reopened_unconfirmed"


def fork_if_name_reused(session, transcript_id: str):
    """The successor Session when a closed session's task NAME was reused by a
    different conversation; None when this ship belongs to `session` after all.

    The binding is keyed on the emdash task name, so a new task named like a
    closed one is matched to the closed one's binding, and the report revives it.
    `ensure_transcript_identity` would then drop the old conversation's rows and
    fill the session with the new one — inheriting everything ELSE the session
    carries too: its Slack thread, participants, turns. Observed 2026-10-05: a new
    `slack` task in canopy-web revived the Sep 21 `slack` session, wiped its
    history, and posted "carrying on in the agent's session" into the Slack thread
    that had been told on Sep 22 the session was closed.

    So a REVIVED session whose transcript then turns out to differ forks instead:
    the binding moves to a new Session and the old one goes back to archived with
    its rows and metadata intact. A revived session whose transcript matches was
    genuinely reopened, and carries on. Only revived sessions fork — a live
    session's transcript can change for ordinary reasons (`/clear`), and its Slack
    thread or chat should follow it. A blank recorded id (legacy, or a transfer
    that cleared it) is no evidence of a different conversation, so it does not
    fork either.
    """
    if not transcript_id or not (session.metadata or {}).get(REOPENED_KEY):
        return None
    with transaction.atomic():
        # Binding first, then session: the lock order replace_reported_sessions
        # and ensure_transcript_identity already use.
        binding = RunnerBinding.objects.select_for_update().filter(session=session).first()
        if binding is None:
            return None
        old = Session.objects.select_for_update().get(pk=session.pk)
        meta = dict(old.metadata or {})
        if not meta.pop(REOPENED_KEY, None):
            return None     # a concurrent ship already decided
        old.metadata = meta
        if not binding.transcript_id or binding.transcript_id == transcript_id:
            old.save(update_fields=["metadata", "updated_at"])
            return None
        return _move_binding_to_successor(old, binding, transcript_id=transcript_id)


def _move_binding_to_successor(old, binding, *, transcript_id: str = "", successor=None):
    """Re-point `binding` from `old` to a successor Session and archive `old` with
    its rows and metadata intact. The successor is `successor` when given (a
    session this binding was moved off earlier — see `fork_if_task_changed`),
    else a new Session. Caller holds the binding lock and `old`'s lock, in that
    order (the order every writer here uses), inside a transaction.

    The binding's derived state belongs to the conversation it was describing,
    so it is reset: the transcript identity (blank makes the runner ship the
    successor's whole history — see `ensure_transcript_identity`), the epoch
    offset, any dialog, and the per-viewer flags (a viewer is attached to the OLD
    session's id, not the successor's)."""
    if successor is None:
        from .autotitle import names_nothing

        meta = old.metadata or {}
        new_meta = {services.TRANSCRIPT_SOURCED: True} if meta.get(services.TRANSCRIPT_SOURCED) else {}
        successor = Session.objects.create(
            agent=old.agent, project=old.project, workspace=old.workspace,
            origin=old.origin, ordinal_scheme=old.ordinal_scheme,
            title=(old.title if names_nothing(binding.session_key) else binding.session_key or old.title)[:200],
            metadata=new_meta,
            # The successor of the session whose task name was reused.
            **provenance.session_fields(parent={"session": old}, forked_from=str(old.pk)),
        )
    elif successor.status != Session.ACTIVE:
        successor.status = Session.ACTIVE
        successor.save(update_fields=["status", "updated_at"])
    old.status = Session.ARCHIVED
    old.save(update_fields=["metadata", "status", "updated_at"])
    binding.session = successor
    binding.transcript_id = transcript_id
    binding.index_offset = 0
    binding.transferred_at = binding.transferred_from = None
    binding.pending_question = None
    binding.pending_answer = None
    binding.stream_desired = False
    binding.backfill_requested = False
    binding.close_requested = False
    binding.save()
    return successor


#: Stamped on a session whose binding moved to another emdash task of the same
#: name: the task id it WAS for, so the record can be found again if that task
#: comes back to the front (two live namesakes in one project alternate in the
#: report, which keeps only the newest — see harness.replace_reported_sessions).
TASK_UID_KEY = "emdash_task_uid"


def fork_if_task_changed(binding, task_uid: str):
    """The Session `binding` now belongs to, when the emdash TASK behind its name
    changed; None when it is the same task (or either side makes no claim).

    The report-side sibling of `fork_if_name_reused`, keyed on the evidence that
    actually decides the question: emdash's task id (`RunnerBinding.task_uid`).
    A new task that reused a name gets its own session, the old record is
    archived with its rows intact, and `claude --resume` / `--continue` /
    `/clear` inside one task — same id — never fork. Called by the report loop
    while it holds the binding lock; takes the session lock itself.

    A blank on either side is no evidence and only FILLS the column (the first
    report after this deploys adopts whatever task is live, as the binding
    already assumed). If the reported task was this binding's before — it has a
    session archived under its id with no binding of its own — that session is
    re-bound instead of creating a third record.
    """
    if not task_uid:
        return None
    if not binding.task_uid:
        binding.task_uid = task_uid
        return None
    if binding.task_uid == task_uid:
        return None
    old = Session.objects.select_for_update().get(pk=binding.session_id)
    old.metadata = {**(old.metadata or {}), TASK_UID_KEY: binding.task_uid}
    old.metadata.pop(REOPENED_KEY, None)
    # `runner_binding__isnull` is a LEFT OUTER JOIN, and Postgres refuses to lock
    # the nullable side of one ("FOR UPDATE cannot be applied to the nullable side
    # of an outer join") — lock only the Session rows. SQLite ignores FOR UPDATE,
    # so the suite never saw this; every forking report 500'd in production.
    previous = (
        Session.objects.select_for_update(of=("self",))
        .filter(runner_binding__isnull=True, workspace=old.workspace,
                origin=old.origin, **{f"metadata__{TASK_UID_KEY}": task_uid})
        .order_by("-created_at").first()
    )
    binding.task_uid = task_uid
    return _move_binding_to_successor(old, binding, successor=previous)


def persist_transcript_rows(session, rows, *, attribute: bool = True) -> int:
    """THE durable write path for a runner session's transcript. rows:
    [{"index","role","text"[,"content"]}] chronological.

    `attribute=False` (used only by `write_backfill`) skips the text-match
    half of server-side attribution below — see that half's docstring for
    why a full-history ship cannot use it safely. The vouched-marker path is
    unaffected either way: it names an exact turn id, so it carries no
    ordering risk.

    `index` is the transcript ordinal (`record * BLOCK_STRIDE + block` — see
    `canopy_transcript.compose_index`, imported above so this scheme has exactly
    one definition) — because the stream (forward) and
    backfill (older) both key on it, they produce the SAME rows by identity, so
    every re-ship (retry, overlap, catch-up) is a no-op.
    index < 0 (an old runner) falls back to sequential server-side assignment.
    Returns rows actually created.

    BULK, deliberately. This was one `get_or_create` per row — four round trips
    each (SELECT, SAVEPOINT, INSERT, RELEASE), measured at 805 queries for 200
    rows. On labs a 846-row backfill took ~14.6s end to end while the runner's
    whole share (reading and parsing a 6.5 MB transcript) was 29 ms; the rest was
    sequential round trips to RDS. Every durable path funnels through here — live
    stream, backfill, reset — so the cost was paid on all of them, and it scaled
    with session length, i.e. it was worst exactly where history matters most.
    Now: one existence probe plus batched inserts, regardless of row count.

    Ordinals are shifted by the binding's `index_offset` on the way in, so a
    transferred session's new box writes ABOVE the history it inherited instead of
    colliding with it. THE single funnel for that shift, deliberately: live
    stream, backfill and reset all come through here, so applying it anywhere
    upstream would mean applying it three times and forgetting it once. The
    identity property the docstring above rests on survives — a given
    (transcript ordinal, epoch) still maps to exactly one `turn_index`, so
    re-ships stay no-ops. Offset 0 (never transferred) is a no-op addition.

    **Server-side attribution (2026-09-27), the primary path now that canopy no
    longer marks a chat send's prompt at claim** (authorship.py). A USER row
    with no vouched marker is matched to the EARLIEST of this session's
    claimed, unlinked chat-send turns whose `prompt` equals the row's text
    with ALL whitespace ignored (`_squash` — the laptop runner delivers a
    prompt via CDP `keyboard.insertText`, which drops newlines, so a
    multi-line send comes back with none; `.strip()` alone still needed the
    newline to survive) — lazily loaded and consumed within the batch via
    `_MatchPool` — one query, only when a row actually needs it, so a batch
    of entirely vouched or entirely unmatched rows costs nothing extra). No
    match leaves `author=None`, exactly the right answer for a line typed
    straight into emdash. The vouched-marker path (m3) is checked first and
    wins when present — it is a stronger claim (the transcript named a turn,
    and the turn's own initiator agrees) than a text match ever is.

    **Only the LIVE stream path attributes; a backfill never does**
    (`attribute=False`, above). `_MatchPool` orders candidates by
    `Turn.created_at`, which has no relation to when a transcript ROW
    happened — a backfill ships a session's full history in one shot, and
    neither `canopy_transcript.conversational_messages`/`row_payload` nor
    `write_backfill`'s own `messages` shape carries a per-row timestamp on
    the wire today. Without one, an old backfilled "yes" from last week could
    link to a turn created TODAY, and today's real "yes" would then miss it —
    a live-shipped row has no such gap (it ships as it happens, so ordering
    by claim time is sound). Revisit if the wire ever carries a row
    timestamp: the tighter fix is bounding candidates to
    `claimed_at <= row_ts + 2min` and `created_at >= row_ts - 7d`, not
    disabling the match outright.

    **`held` (which rows are already persisted) is computed BEFORE any
    matching, never after.** It used to run once, on `prepared`'s indices,
    right before `bulk_create` — by which point every row, including one
    whose index already exists (a re-ship: reconnect, catch-up, backfill
    overlap — routine, not an error), had already called `pool.claim`. A
    re-shipped row is dropped at `bulk_create` and so was never going to
    consume a candidate — but it did, stealing the match a genuinely NEW row
    in the same batch with the same (squashed) text needed, which then got
    `author=None` or the wrong turn. Only an explicit ordinal (`index >= 0`)
    can already be held: a row with none always gets a FRESH one from
    `_next_index`, one past the session's high-water mark, so it can never
    collide — the precomputed set below only needs to check those."""
    offset = _index_offset(session)
    with transaction.atomic():
        locked = Session.objects.select_for_update().get(pk=session.pk)
        # `is not None`, never a truthiness test: index 0 is a real ordinal (the
        # transcript's first record) and `x or -1` would read it as "no ordinal".
        if any(r.get("index") is not None and int(r["index"]) >= 0 for r in rows):
            _ensure_current_ordinal_scheme(locked, offset)
        held = set(
            Message.objects.filter(
                session=locked,
                turn_index__in=[
                    int(r["index"]) + offset for r in rows
                    if r.get("index") is not None and int(r["index"]) >= 0
                ],
            ).values_list("turn_index", flat=True)
        )
        next_index = None
        prepared: list[tuple[int, str, str, dict, dict | None, str | None, dict | None]] = []
        claimed: set[int] = set()
        from apps.harness import sensitivity

        # A sensitive conversation keeps which call and whether it failed, never
        # the result (spec 2026-10-22-phi-agents-on-zdr-runners §4).
        withhold = sensitivity.is_sensitive(locked)
        vouched = _vouched_markers(locked, rows)
        pool = _MatchPool(locked) if attribute else None
        for row in rows:
            role = row.get("role")
            if role not in transcript_read._BACKFILL_ROLES:
                continue
            text = str(row.get("text", ""))
            # A Claude transcript records harness output (task notifications,
            # system reminders, local command stdout) as `type: "user"`, so
            # without this the machine's event stream renders on the HUMAN's side
            # of the chat. Scoped to USER rows: the rule is about records
            # masquerading as human input, and assistant text that happens to
            # quote a marker is still the agent talking.
            #
            # The row is DROPPED, never renumbered — turn_index is the transcript
            # ordinal that the live stream, catch-up and backfill all key on, so
            # closing the gap would make one record arrive under two indices.
            if role == Message.USER and is_system_noise(text):
                continue
            index = row.get("index")
            index = -1 if index is None else int(index)
            if index < 0:
                if next_index is None:
                    next_index = _next_index(locked)
                index, next_index = next_index, next_index + 1
            else:
                # Only ordinal-keyed rows shift. The `index < 0` leg above already
                # assigns an ABSOLUTE index off the session's own high-water mark,
                # so offsetting it too would double-count.
                index += offset
            # Retention purged everything below the floor. A re-ship of that
            # history (backfill, reset) must not write it back.
            if index < locked.retention_floor_index:
                continue
            # First occurrence wins, matching what `get_or_create` did implicitly:
            # a repeat within ONE payload used to find the row its predecessor had
            # just written. A bulk insert has no such ordering, and the pair would
            # violate the unique constraint, so the dedupe has to be explicit.
            if index in claimed:
                continue
            claimed.add(index)
            content = row.get("content")
            if not isinstance(content, dict):
                content = {}
            author = turn_hex = None
            if role == Message.USER:
                # The marker canopy prepended at claim (authorship.py). Stripped
                # here, the single funnel for live stream, backfill and reset, so
                # every path gets the same text and the same author.
                author, bare, turn_hex = authorship.parse(text)
                if author is not None and vouched.get(turn_hex) == _marker_identity(author):
                    text = bare
                    if isinstance(content.get("text"), str):
                        content = {**content, "text": authorship.parse(content["text"])[1]}
                else:
                    # Unmarked, or a marker nobody can vouch for: anyone who can
                    # type into the transcript can write the syntax, so a line
                    # naming a turn that is not this session's, or a person who
                    # did not send that turn, stays exactly what it was typed as.
                    author = turn_hex = None
                    # A row whose index is already `held` will be dropped at
                    # bulk_create below (it's a re-ship) — never spend a
                    # match candidate on it.
                    if pool is not None and index not in held:
                        matched = pool.claim(text)
                        if matched is not None:
                            matched_author = authorship.author_of(matched)
                            if matched_author is not None:
                                author, turn_hex = matched_author, matched.pk.hex
            # Postgres rejects NUL in text/jsonb, and the batch is ONE
            # transaction — an unscrubbed byte from a binary tool result 500s
            # every other row with it. See transcript_noise.scrub_nul.
            sealed = None
            if withhold:
                # The columns record that something was said (a tool call's name,
                # an id); the words are sealed for the conversation's own people,
                # and a tool's result is not kept at all (`sensitivity.seal`).
                sealed_text = scrub_nul(text)
                text, content, sealed = sensitivity.seal(
                    role, sealed_text, storage_content(scrub_nul(content), sealed_text))
            text = scrub_nul(text)
            content = storage_content(scrub_nul(content), text)
            prepared.append((index, role, text, content, author, turn_hex, sealed))
        # `held` was computed above, before matching — reused here, not
        # re-queried.
        fresh = [
            Message(session=locked, turn_index=i, role=r, plaintext=t, content=c,
                    author=a, source_turn_id=uuid.UUID(h) if h else None, sealed=sealed)
            for (i, r, t, c, a, h, sealed) in prepared
            if i not in held
        ]
        # What the session DID (activity.py). Context (cwd / gitBranch) is a set
        # union, so every shipped row may contribute it, re-ships included; the
        # counted signals come from `fresh` only, which is what keeps a re-ship
        # from double-counting. Saved even when nothing is fresh: a re-ship from a
        # newer runner is how a session ingested before this learns its context.
        act = session_activity.Activity(locked.activity)
        for row in rows:
            act.context(row.get("cwd"), row.get("git_branch"))
        session_activity.fold_rows(act, ((m.role, m.plaintext, m.content) for m in fresh))
        session_activity.store(locked, act)
        if not fresh:
            return 0
        # `ignore_conflicts` is belt-and-braces on top of the row lock above (which
        # already serializes writers for THIS session), so a racing writer costs a
        # skipped row rather than a failed batch.
        Message.objects.bulk_create(fresh, batch_size=500, ignore_conflicts=True)
        return len(fresh)


def _marker_identity(author: dict) -> tuple[int | None, int | None]:
    return author.get("user_id"), author.get("contact_id")


def _vouched_markers(session, rows) -> dict[str, tuple[int | None, int | None]]:
    """turn hex -> (initiator_user_id, initiator_contact_id), for every turn a
    user row's marker names that really is one of THIS session's turns. One
    query for the whole batch, whatever its size.

    The live frame (`stream_map`) is deliberately not checked the same way — it
    has no cheap query, and the durable row written here is what every reload
    shows."""
    hexes = set()
    for row in rows:
        if row.get("role") != Message.USER:
            continue
        _author, _bare, turn_hex = authorship.parse(str(row.get("text", "")))
        if turn_hex:
            hexes.add(turn_hex)
    if not hexes:
        return {}
    found = Turn.objects.filter(
        chat_session=session, pk__in=[uuid.UUID(h) for h in hexes],
    ).values_list("pk", "initiator_user_id", "initiator_contact_id")
    return {pk.hex: (uid, cid) for pk, uid, cid in found}


def _squash(text: str) -> str:
    """Whitespace-blind comparison key. The laptop runner delivers a prompt via
    CDP `keyboard.insertText` into emdash's Claude Code TUI, which DROPS
    newlines — a two-line send ("line one\nline two") comes back in the
    transcript as "line oneline two", so a plain `.strip()` equality (which
    still requires the newline to survive) missed every multi-line message.
    Squashing ALL whitespace out of both sides makes the match blind to
    exactly the characters the delivery path is known to mangle, at the cost
    of conflating "a b" and "ab" — accepted, because `_MatchPool.claim` still
    resolves that ambiguity the same way as an exact duplicate: earliest
    unlinked candidate, consumed in send order."""
    return re.sub(r"\s+", "", text or "")


class _MatchPool:
    """Server-side attribution's candidate pool for ONE `persist_transcript_rows`
    call — the earliest of this session's claimed, unlinked chat-send turns,
    each matched against a row's text at most once.

    Lazily loaded on the FIRST `claim()` call, never in `__init__`: a batch
    that is entirely vouched-marker or entirely genuinely-unattributable rows
    (the common cases) must cost nothing beyond that check, and a batch with
    no user rows at all must issue no query — matching a `_vouched_markers`'s
    own "only when the batch has user rows" rule. One query however many rows
    need it, because every `claim()` after the first is served from the same
    in-memory list.

    `claim()` removes a match from the pool — "consumed within the batch" —
    so two identical-text rows in one batch map to two different turns, in
    the order they appear, exactly like two identical replies from two
    different people ("yes", "yes") should. The same consumption order is
    what resolves the ambiguity `_squash` introduces (a turn "a b" and a turn
    "ab" now compare equal): whichever of the two is earliest and still
    unlinked wins, which is the best any text-only match can do — the marker
    path (m3, checked first and always preferred) is the exact answer for
    when that is not good enough."""

    def __init__(self, session):
        self._session = session
        self._loaded = False
        self._candidates: list[Turn] = []

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        cutoff = timezone.now() - _dt.timedelta(days=7)
        already_linked = Message.objects.filter(
            session=self._session, source_turn_id__isnull=False
        ).values("source_turn_id")
        turns = (
            Turn.objects.filter(
                chat_session=self._session, claimed_at__isnull=False, created_at__gte=cutoff,
            )
            .exclude(pk__in=already_linked)
            .select_related("initiator_user", "initiator_contact")
            .order_by("created_at")
        )
        self._candidates = [t for t in turns if authorship.is_chat_send(t)]

    def claim(self, text: str) -> Turn | None:
        """The earliest remaining candidate whose prompt matches `text` with
        ALL whitespace ignored (see `_squash`) — or None. Removes it from the
        pool."""
        self._load()
        target = _squash(authorship.without_attachment_note(text))
        for i, turn in enumerate(self._candidates):
            if _squash(turn.prompt or "") == target:
                return self._candidates.pop(i)
        return None


def write_backfill(session, messages) -> int:
    """Write a runner's shipped full transcript as Message rows. Ordinal-keyed
    payloads (a current runner) upsert-fill: they add the older rows the live
    stream never saw and skip anything already persisted. A legacy payload (no
    ordinals) keeps the old write-once contract — sequential, and only into an
    empty session. messages: [{"role","text"[,"index"]}] chronological.

    `attribute=False`: a backfill ships a session's full history in one shot
    with no per-row timestamp, so server-side attribution's "earliest claimed
    turn" match (`persist_transcript_rows`) has no way to tell an old row from
    a new one — see that function's docstring. The vouched-marker path still
    runs (it names an exact turn id, not a guess), so a legacy marked row
    backfilled from before this change is still attributed correctly."""
    ordinal = any(int(m.get("index", -1)) >= 0 for m in messages)
    if not ordinal and Message.objects.filter(session=session).exists():
        return 0
    # An ordinal-less payload is numbered from the floor up, so it would write
    # purged history back above the floor. Refuse it for a purged session.
    if not ordinal and session.content_purged_at is not None:
        return 0
    return persist_transcript_rows(session, messages, attribute=False)


def _set_stream_desired(session, desired: bool) -> bool:
    """Flip the bound binding's stream_desired and, on a real change, signal the
    bound runner over its control channel. Returns the resulting desired state
    (False when the session has no binding to stream)."""
    from apps.canopy_sessions.models import RunnerBinding

    binding = RunnerBinding.objects.filter(session=session).first()
    if binding is None:
        return False
    if binding.stream_desired != desired:
        binding.stream_desired = desired
        binding.save(update_fields=["stream_desired", "updated_at"])
    if binding.runner_id:
        from apps.realtime import groups
        groups.publish(groups.runner_group(binding.runner_id), {
            "type": "runner.stream",
            "session_id": str(session.id),
            "session_key": binding.session_key,
            "desired": desired,
        })
    return desired


def seed_stream_desired(binding) -> None:
    """A viewer who arrived BEFORE the session had a runner binding still gets streamed to.

    `attach_session` marks a binding `stream_desired` only on the 0->1 viewer
    edge, and only if a binding exists at that moment. A brand-new chat has
    none yet: the embedded widget sends the first message, opens its socket and
    attaches within a second, while the runner binds the session a moment
    later when it claims the turn. The edge had already fired into nothing, the
    new binding started at False, and `post_session_stream` then persisted every
    event and pushed none — the viewer saw "Thinking…" and never their own
    words or the reply (connect-labs, 2026-09-25).

    So whoever creates or refreshes a binding calls this after saving it: if
    someone is watching and the binding does not know, it is told — and the
    runner with it, on commit, since the caller is usually inside an atomic
    block. A no-op otherwise, which makes it safe on every save.
    """
    from django.db import transaction

    from . import attach

    if binding.stream_desired or attach.count(binding.session_id) <= 0:
        return
    session = binding.session
    transaction.on_commit(lambda: _set_stream_desired(session, True))


def attach_session(session) -> bool:
    """A viewer attached. On the 0->1 edge, mark streaming desired + signal the runner."""
    n = attach.attach(session.id)
    if n == 1:
        return _set_stream_desired(session, True)
    from apps.canopy_sessions.models import RunnerBinding
    b = RunnerBinding.objects.filter(session=session).first()
    return bool(b and b.stream_desired)


def detach_session(session) -> bool:
    """A viewer detached. On the 1->0 edge, stop streaming + signal the runner."""
    n = attach.detach(session.id)
    if n == 0:
        return _set_stream_desired(session, False)
    from apps.canopy_sessions.models import RunnerBinding
    b = RunnerBinding.objects.filter(session=session).first()
    return bool(b and b.stream_desired)


def _next_index(session: Session) -> int:
    current = Message.objects.filter(session=session).aggregate(m=Max("turn_index"))["m"]
    # Never below the retention floor: a purged session has no rows left, and a
    # new row at 0 would sit under the floor that hides the purged history.
    # Read fresh, because callers pass whatever instance they were handed.
    floor = Session.objects.filter(pk=session.pk).values_list(
        "retention_floor_index", flat=True
    ).first() or 0
    return max(0 if current is None else current + 1, floor)


def _index_offset(session) -> int:
    """This session's current transcript epoch base — see
    `RunnerBinding.index_offset`. No binding means nothing has ever been
    transferred, so 0."""
    binding = RunnerBinding.objects.filter(session=session).values_list(
        "index_offset", flat=True
    ).first()
    return int(binding or 0)


def project_events(turn: Turn, rows) -> int:
    """Materialize a turn's newly-appended assistant/tool events into Message rows.
    Idempotent per source ledger seq, so a re-delivered signal never doubles a row.

    Runner sessions are excluded: their durable rows come from the transcript
    (ordinal-keyed, via persist_transcript_rows) — the bridged reply lands in the
    ledger too, and projecting it as well would persist it twice in a second
    index space. The ledger frames still stream to the live client unchanged."""
    if not turn.chat_session_id:
        return 0
    if services.transcript_sourced(turn.chat_session):
        return 0
    from apps.harness import sensitivity

    sensitive = sensitivity.is_sensitive(turn)
    created = 0
    with transaction.atomic():
        session = Session.objects.select_for_update().get(pk=turn.chat_session_id)
        act = session_activity.Activity(session.activity)
        index = _next_index(session)
        for row in rows:
            role = transcript_read._ROLE_FOR_KIND.get(row.kind)
            if role is None:
                continue  # status/heartbeat/error etc. are not transcript rows
            if Message.objects.filter(turn=turn, content__source_seq=row.seq).exists():
                continue
            payload = row.payload or {}
            sealed = None
            if sensitive:
                # The signal's rows carry the live words; the stored row keeps
                # metadata, the words sealed (`sensitivity.seal`).
                _, payload, sealed = sensitivity.seal(role, str(payload.get("text", "")), payload)
            Message.objects.create(
                session=session, turn=turn, turn_index=index, role=role,
                content={**payload, "source_seq": row.seq},
                plaintext=str(payload.get("text", "")), sealed=sealed,
            )
            act.row(role, str(payload.get("text", "")), payload)
            index += 1
            created += 1
        session_activity.store(session, act)
    return created
