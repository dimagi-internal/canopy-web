# Per-person drafts and authored messages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Everyone in a chat session types in their own box, sees everyone else typing live, and every sent message lands in send order, queued behind the running reply, labelled with its author — durably.

**Architecture:** `Draft` becomes one-per-(session, author); the socket routes a person's own draft frames only to their own tabs and fans a new `draft.typing` peer view to everyone else. Sends only enqueue (the mid-turn interjection is deleted). Authorship travels inside the transcript: `claim_turn` prefixes the delivered prompt with a one-line `[canopy from=… turn=…]` marker, and `persist_transcript_rows` / the live user frame parse it back into `Message.author` + `Message.source_turn_id`. A derived, never-stored `queued_messages(session)` list shows everyone the sends that have not reached the transcript yet.

**Tech Stack:** Django 5 + Channels (ASGI WS), Django Ninja + Pydantic v2, PostgreSQL, pytest; React 19 + TypeScript, vitest, the `canopy-ui` workspace package.

**Spec:** `docs/superpowers/specs/2026-09-26-per-person-drafts-and-authored-messages-design.md`

## Global Constraints

- Framework/product boundary: `apps/canopy_sessions`, `apps/harness`, `apps/realtime` are framework — import nothing from `projects`, `walkthroughs`, `reviews`, `shareouts`, `runs` (`tests/test_architecture_boundary.py`).
- A route docstring is published API documentation — rationale goes in `#` comments.
- Any change to `apps/**/schemas.py` or `api.py` ⇒ regenerate `frontend/src/api/generated.ts` (`cd frontend && npm run gen:api` with the backend on :8000, or `npm run gen:api:local`) and commit it. CI fails on a stale file.
- Touching `frontend/packages/canopy-ui/src` ⇒ bump `frontend/packages/canopy-ui/package.json` `version` (currently `0.12.2` → `0.13.0`). CI fails without it.
- Styling uses semantic tokens only (`bg-card`, `text-muted-foreground`, `text-primary`, …) — no raw palette literals.
- Migrations: write them the obvious way; destructive is fine (CLAUDE.md "MIGRATIONS").
- Marker exact shape: first line `[canopy from="<name>" user=<int>|contact=<int> turn=<32 lowercase hex>]`, then `\n`, then the text. `"` and `\` in the name are escaped with `\`.
- Backwards compatibility: an old `canopy-ui` (ace-web pins 0.12.2) against the new server must still type, send and receive — it just never sees peers typing.
- Commit trailer on every commit: `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## Review Focus

- **Two tabs, one person.** Alice types on desktop, then on phone: both tabs must converge on her one draft (version guard), and neither shows her as a peer "typing" row. → Task 5 test `test_own_tabs_share_one_draft_and_see_no_peer_row`.
- **An old client in a new room.** A 0.12.2 client must never receive a peer's `draft.updated`/`draft.committed` (it would paste the peer's text into its own box or fabricate a message from its own box). → Task 5 test `test_peer_never_receives_my_draft_frames`.
- **Someone quotes the marker.** A person or agent pasting `[canopy from="x" user=1 turn=…]` mid-message must not be misattributed; only a first line in exact shape counts. → Task 1 tests `test_marker_mid_text_is_not_parsed` / `test_parse_requires_exact_first_line`.
- **A name with quotes or a newline.** `O"Brien` or a name containing `\n` must round-trip and never break the one-line marker. → Task 1 test `test_name_with_quote_and_newline_round_trips`.
- **Send while the agent is replying, from two people.** Both sends must become QUEUED turns in send order, both visible to everyone as queued, neither pushed into the running turn. → Task 4 `test_send_during_running_turn_only_queues` + Task 6 `test_queued_lists_every_pending_send_in_order`.

---

## File map

| File | Responsibility | Tasks |
|---|---|---|
| `apps/canopy_sessions/authorship.py` (new) | mark/parse the author marker; the ONE place its syntax lives | 1 |
| `apps/canopy_sessions/models.py` | `Message.author`, `Message.source_turn_id`; `Draft.author` + constraint | 2, 5 |
| `apps/canopy_sessions/migrations/0032_message_author.py`, `0033_draft_per_author.py` (new) | schema | 2, 5 |
| `apps/canopy_sessions/services.py` | persist parse; ledger-path author; delete `_maybe_interject`; `queued_messages` | 2, 4, 6 |
| `apps/canopy_sessions/stream_map.py` | live `chat.user_message` parse | 2 |
| `apps/canopy_sessions/serializers.py` / `schemas.py` | `author` on message DTOs; draft DTOs; snapshot `peer_drafts`, `queued` | 2, 5, 6 |
| `apps/harness/api.py` | marker at claim | 3 |
| `apps/realtime/consumers.py`, `runner/ec2/cloud_runner.py` | delete interject plumbing | 4 |
| `apps/canopy_sessions/drafts.py` | per-author draft service | 5 |
| `apps/canopy_sessions/consumers.py` | frame routing, `draft.typing`, `session.queued` | 5, 6 |
| `apps/canopy_sessions/queued_feed.py` (new) | publish the whole queued list | 6 |
| `apps/canopy_sessions/status_feed.py`, `signals.py` | call the queued publisher at the same moments | 6 |
| `frontend/packages/canopy-ui/src/chat/protocol.ts`, `sessionReducer.ts` | types + state | 7 |
| `frontend/packages/canopy-ui/src/chat/TypingRows.tsx` (new), `QueuedRows.tsx` (new), `SendBox.tsx`, `ChatPanel.tsx`, `PresenceChips.tsx`, `MessageItem.tsx`, `useSessionSocket.ts`, `drafts.ts`, `index.ts` | UI | 8 |
| `frontend/src/pages/ChatPage.tsx`, `frontend/src/embed/EmbedApp.tsx` | drop `onTakeOver` | 8 |
| `CLAUDE.md` | one Design Decisions bullet | 9 |

---

### Task 1: The author marker (`authorship.py`)

**Files:**
- Create: `apps/canopy_sessions/authorship.py`
- Test: `tests/test_chat_authorship.py`

**Interfaces:**
- Produces:
  - `mark(text: str, *, name: str, turn_id: uuid.UUID | str, user_id: int | None = None, contact_id: int | None = None) -> str`
  - `parse(text: str) -> tuple[dict | None, str, str | None]` — `(author, bare_text, turn_id_hex)`; `author` is `{"name": str, "user_id": int}` or `{"name": str, "contact_id": int}`; all three `None`/original text/`None` when there is no valid marker.
  - `for_turn(turn) -> str` — the turn's prompt, marked iff it is a chat-session turn with an initiator user or contact; otherwise `turn.prompt` unchanged.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_chat_authorship.py
"""The author marker: the ONE syntax that carries who-said-it through Claude's transcript."""
from __future__ import annotations

import uuid

import pytest

from apps.canopy_sessions import authorship

TID = uuid.UUID("3f2a9c1e0b7d4c55a1e2f3a4b5c6d7e8")


def test_round_trip_user():
    marked = authorship.mark("hello there", name="Alice Smith", user_id=42, turn_id=TID)
    assert marked.splitlines()[0] == '[canopy from="Alice Smith" user=42 turn=3f2a9c1e0b7d4c55a1e2f3a4b5c6d7e8]'
    author, bare, tid = authorship.parse(marked)
    assert author == {"name": "Alice Smith", "user_id": 42}
    assert bare == "hello there"
    assert tid == TID.hex


def test_round_trip_contact():
    marked = authorship.mark("hi", name="Beth", contact_id=7, turn_id=TID)
    author, bare, _ = authorship.parse(marked)
    assert author == {"name": "Beth", "contact_id": 7}
    assert bare == "hi"


def test_multiline_body_survives():
    marked = authorship.mark("line one\nline two", name="A", user_id=1, turn_id=TID)
    assert authorship.parse(marked)[1] == "line one\nline two"


def test_unmarked_text_is_untouched():
    assert authorship.parse("just typed in emdash") == (None, "just typed in emdash", None)


def test_marker_mid_text_is_not_parsed():
    quoted = 'look at this:\n[canopy from="X" user=1 turn=' + TID.hex + ']\nweird'
    assert authorship.parse(quoted) == (None, quoted, None)


@pytest.mark.parametrize("line", [
    '[canopy from="A" user=x turn=' + TID.hex + ']',        # non-int id
    '[canopy from="A" user=1 turn=abc]',                    # short turn
    '[canopy from="A" turn=' + TID.hex + ']',               # no id
    ' [canopy from="A" user=1 turn=' + TID.hex + ']',       # leading space
])
def test_parse_requires_exact_first_line(line):
    text = line + "\nbody"
    assert authorship.parse(text) == (None, text, None)


def test_name_with_quote_and_newline_round_trips():
    marked = authorship.mark("x", name='Pat "PJ" O\\Brien\nJr', user_id=3, turn_id=TID)
    assert len(marked.splitlines()) == 2  # marker stays ONE line
    author, bare, _ = authorship.parse(marked)
    assert author == {"name": 'Pat "PJ" O\\Brien Jr', "user_id": 3}
    assert bare == "x"


def test_marker_only_no_body():
    marked = authorship.mark("", name="A", user_id=1, turn_id=TID)
    assert authorship.parse(marked) == ({"name": "A", "user_id": 1}, "", TID.hex)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_chat_authorship.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apps.canopy_sessions.authorship'`

- [ ] **Step 3: Implement**

```python
# apps/canopy_sessions/authorship.py
"""Who said a line, carried THROUGH Claude's transcript.

A transcript-sourced session's durable user rows are re-read from Claude's own
transcript, which records the prompt the agent received and nothing else — so
an author known only to canopy's database is lost the moment the row comes back
(spec 2026-09-26). The fix is to put the author where the transcript will keep
it: one marker line at the top of the delivered prompt. `mark` writes it (at
claim, never into `Turn.prompt` — see the spec for the readers that must not see
it), `parse` reads it back on every durable and live path.

Strict on purpose: only an exact FIRST line counts, so a person or agent quoting
the syntax mid-message is never misattributed.
"""
from __future__ import annotations

import re
import uuid

_MARKER = re.compile(
    r'^\[canopy from="(?P<name>(?:[^"\\]|\\.)*)" '
    r'(?:user=(?P<user>\d+)|contact=(?P<contact>\d+)) '
    r'turn=(?P<turn>[0-9a-f]{32})\]$'
)


def _escape(name: str) -> str:
    one_line = " ".join(name.split())
    return one_line.replace("\\", "\\\\").replace('"', '\\"')


def _unescape(name: str) -> str:
    return re.sub(r"\\(.)", r"\1", name)


def mark(text: str, *, name: str, turn_id, user_id: int | None = None,
         contact_id: int | None = None) -> str:
    if (user_id is None) == (contact_id is None):
        raise ValueError("exactly one of user_id / contact_id")
    who = f"user={int(user_id)}" if user_id is not None else f"contact={int(contact_id)}"
    tid = uuid.UUID(str(turn_id)).hex
    return f'[canopy from="{_escape(name)}" {who} turn={tid}]\n{text}'


def parse(text: str) -> tuple[dict | None, str, str | None]:
    first, sep, rest = text.partition("\n")
    m = _MARKER.match(first)
    if m is None:
        return None, text, None
    author: dict = {"name": _unescape(m["name"])}
    if m["user"] is not None:
        author["user_id"] = int(m["user"])
    else:
        author["contact_id"] = int(m["contact"])
    return author, rest if sep else "", m["turn"]


def _display_name(user) -> str:
    return (user.get_full_name() or "").strip() or user.email


def for_turn(turn) -> str:
    """The prompt as the runner should deliver it. Only a chat-session turn with
    a known person is marked; everything else (agent, scheduled, email turns)
    is delivered exactly as before."""
    prompt = turn.prompt or ""
    if not turn.chat_session_id:
        return prompt
    if turn.initiator_user_id:
        return mark(prompt, name=_display_name(turn.initiator_user),
                    user_id=turn.initiator_user_id, turn_id=turn.pk)
    if turn.initiator_contact_id:
        contact = turn.initiator_contact
        name = (getattr(contact, "name", "") or getattr(contact, "email", "") or "contact").strip()
        return mark(prompt, name=name, contact_id=turn.initiator_contact_id, turn_id=turn.pk)
    return prompt
```

Check `apps/contacts/models.py` for the Contact's display field before relying on `name`/`email`; adjust the `getattr` names to the real fields.

- [ ] **Step 4: Run tests — PASS**

Run: `uv run pytest tests/test_chat_authorship.py -v`

- [ ] **Step 5: Add `for_turn` tests, then run**

Append to `tests/test_chat_authorship.py`:

```python
from types import SimpleNamespace


def _turn(**kw):
    base = dict(pk=TID, prompt="do it", chat_session_id=None,
                initiator_user_id=None, initiator_user=None,
                initiator_contact_id=None, initiator_contact=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_for_turn_leaves_non_chat_turns_alone():
    assert authorship.for_turn(_turn()) == "do it"


def test_for_turn_marks_a_chat_turn_with_its_initiator():
    user = SimpleNamespace(get_full_name=lambda: "Alice Smith", email="a@x")
    out = authorship.for_turn(_turn(chat_session_id=uuid.uuid4(), initiator_user_id=42, initiator_user=user))
    assert authorship.parse(out) == ({"name": "Alice Smith", "user_id": 42}, "do it", TID.hex)


def test_for_turn_without_initiator_is_unmarked():
    assert authorship.for_turn(_turn(chat_session_id=uuid.uuid4())) == "do it"
```

Run: `uv run pytest tests/test_chat_authorship.py -v` → PASS

- [ ] **Step 6: Commit**

```bash
git add apps/canopy_sessions/authorship.py tests/test_chat_authorship.py
git commit -m "feat(chat): author marker that survives the transcript round trip"
```

---

### Task 2: `Message.author` — persisted, live, and on the wire

**Files:**
- Modify: `apps/canopy_sessions/models.py` (class `Message`, ~line 159)
- Create: `apps/canopy_sessions/migrations/0032_message_author.py` (via makemigrations)
- Modify: `apps/canopy_sessions/services.py` — `persist_transcript_rows` (~470-570); ledger path in `send_message` (~1190)
- Modify: `apps/canopy_sessions/stream_map.py:46-53`
- Modify: `apps/canopy_sessions/serializers.py::message_dto`, `apps/canopy_sessions/schemas.py::MessageOut`
- Modify: `frontend/src/api/generated.ts` (regenerated)
- Test: `tests/test_chat_authorship_persist.py`

**Interfaces:**
- Consumes: `authorship.parse` (Task 1).
- Produces: `Message.author: dict | None` (`{"name", "user_id"?, "contact_id"?}`), `Message.source_turn_id: UUID | None`; `message_dto(...)["author"]`; `MessageOut.author: dict | None = None`; live frame `chat.user_message.data.author: dict | None`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_chat_authorship_persist.py
"""A marked transcript row becomes an authored message, on every path."""
from __future__ import annotations

import uuid

import pytest
from django.contrib.auth.models import User

from apps.canopy_sessions import authorship, serializers, stream_map
from apps.canopy_sessions import services as chat
from apps.canopy_sessions.models import Message
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
TID = uuid.uuid4()


def _session():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return chat.create_session(workspace=ws, created_by=owner)


def test_persist_strips_marker_and_records_author():
    session = _session()
    marked = authorship.mark("ship it", name="Alice", user_id=42, turn_id=TID)
    chat.persist_transcript_rows(session, [{"index": 10, "role": "user", "text": marked,
                                            "content": {"text": marked}}])
    msg = Message.objects.get(session=session)
    assert msg.plaintext == "ship it"
    assert msg.content["text"] == "ship it"
    assert msg.author == {"name": "Alice", "user_id": 42}
    assert msg.source_turn_id == TID


def test_unmarked_user_row_has_no_author():
    session = _session()
    chat.persist_transcript_rows(session, [{"index": 10, "role": "user", "text": "typed in emdash"}])
    msg = Message.objects.get(session=session)
    assert msg.author is None and msg.source_turn_id is None
    assert msg.plaintext == "typed in emdash"


def test_assistant_rows_are_never_parsed():
    session = _session()
    marked = authorship.mark("x", name="A", user_id=1, turn_id=TID)
    chat.persist_transcript_rows(session, [{"index": 11, "role": "assistant", "text": marked}])
    assert Message.objects.get(session=session).plaintext == marked


def test_backfill_path_parses_too():
    session = _session()
    marked = authorship.mark("old line", name="Bo", user_id=5, turn_id=TID)
    chat.write_backfill(session, [{"index": 3, "role": "user", "text": marked}])
    assert Message.objects.get(session=session).author == {"name": "Bo", "user_id": 5}


def test_message_dto_carries_author():
    session = _session()
    chat.persist_transcript_rows(session, [{"index": 1, "role": "user",
        "text": authorship.mark("hi", name="A", user_id=1, turn_id=TID)}])
    assert serializers.message_dto(Message.objects.get(session=session))["author"] == {"name": "A", "user_id": 1}


def test_live_user_frame_is_stripped_and_authored():
    marked = authorship.mark("hey", name="A", user_id=1, turn_id=TID)
    frames = stream_map.turn_event_to_frames(
        {"kind": "user", "seq": 20, "payload": {"text": marked}}, lambda _s: "m1")
    assert frames[0]["event"] == "chat.user_message"
    assert frames[0]["data"]["plaintext"] == "hey"
    assert frames[0]["data"]["author"] == {"name": "A", "user_id": 1}
```

- [ ] **Step 2: Run — FAIL** (`Message` has no `author`)

Run: `uv run pytest tests/test_chat_authorship_persist.py -v`

- [ ] **Step 3: Model fields + migration**

In `Message` (after `plaintext`):

```python
    # WHO wrote a user line: {"name", "user_id"} or {"name", "contact_id"}, parsed
    # from the marker canopy puts at the top of the delivered prompt
    # (`authorship.py`). Null for a line typed straight into emdash — attributing
    # that to the session owner would be a guess.
    author = models.JSONField(null=True, blank=True)
    # The Turn a user line was delivered by, from the same marker. What lets the
    # queued list (`services.queued_messages`) know a send has landed.
    source_turn_id = models.UUIDField(null=True, blank=True, db_index=True)
```

Run: `uv run python manage.py makemigrations canopy_sessions -n message_author` → creates `0032_message_author.py`.

- [ ] **Step 4: Parse in `persist_transcript_rows`**

Change `prepared` to carry author and turn id. Replace the block from `content = row.get("content")` through `prepared.append(...)`:

```python
            content = row.get("content")
            if not isinstance(content, dict):
                content = {}
            author = turn_hex = None
            if role == Message.USER:
                # The marker canopy prepended at claim (authorship.py). Stripped
                # here, the single funnel for live stream, backfill and reset, so
                # every path gets the same text and the same author.
                author, text, turn_hex = authorship.parse(text)
                if author is not None and isinstance(content.get("text"), str):
                    content = {**content, "text": authorship.parse(content["text"])[1]}
            text = scrub_nul(text)
            content = storage_content(scrub_nul(content), text)
            prepared.append((index, role, text, content, author, turn_hex))
```

and update the consumers of `prepared`:

```python
        prepared: list[tuple[int, str, str, dict, dict | None, str | None]] = []
        ...
        fresh = [
            Message(session=locked, turn_index=i, role=r, plaintext=t, content=c,
                    author=a, source_turn_id=uuid.UUID(h) if h else None)
            for (i, r, t, c, a, h) in prepared
            if i not in held
        ]
```

Add `from . import authorship` to the module imports (and `import uuid` if missing — it is already used by `_send_transcript_sourced_message`).

- [ ] **Step 5: Ledger path (dev stub) writes the author directly**

In `send_message`'s non-transcript branch, the `Message.objects.create(...)` happens BEFORE the turn exists. After `turn, _created = harness_services.enqueue_turn(...)` add:

```python
        # The ledger path writes its own row, so it records the author directly
        # rather than through the transcript marker.
        if user is not None and getattr(user, "is_authenticated", False):
            message.author = {"name": (user.get_full_name() or "").strip() or user.email,
                              "user_id": user.pk}
        if turn is not None:
            message.source_turn_id = turn.pk
        message.save(update_fields=["author", "source_turn_id"])
```

- [ ] **Step 6: Live frame + DTOs**

`stream_map.py`, the `kind == "user"` branch:

```python
    if kind == "user":
        mid = resolve_message_id(seq)
        # Same parse as the durable path, so a watcher sees the stripped text and
        # the author before any reload — and the sender's echo matches on text.
        author, text, _turn = authorship.parse(str(payload.get("text", "")))
        return [{"event": "chat.user_message",
                 "data": {"message_id": mid, "turn_index": seq,
                          "plaintext": text, "author": author}}]
```

(import `from . import authorship` at the top of `stream_map.py`).

`serializers.message_dto`: add `"author": msg.author,` after `"plaintext"`. Transient messages (`_send_transcript_sourced_message`) have no `author` attribute set explicitly — Django model defaults make it `None`, which is correct.

`schemas.MessageOut`: add `author: dict | None = None`.

- [ ] **Step 7: Run tests — PASS**, plus the neighbours

Run: `uv run pytest tests/test_chat_authorship_persist.py tests/test_chat_serializers.py tests/test_agui_projection.py -q`
If `test_agui_projection` pins the exact `chat.user_message` data dict, add `"author": None` to its expectation.

- [ ] **Step 8: Regenerate types, commit**

```bash
DJANGO_SETTINGS_MODULE=config.settings.test uv run python -c "
import django, json
django.setup()
from apps.api.api import api
json.dump(api.get_openapi_schema(), open('frontend/openapi.json', 'w'), indent=2)
"
(cd frontend && npm run gen:api:local)
git add apps/canopy_sessions frontend/src/api/generated.ts tests/test_chat_authorship_persist.py tests/test_agui_projection.py
git commit -m "feat(chat): messages carry their author, parsed from the transcript marker"
```

(This is the exact dump CI runs in `.github/workflows/ci.yml`; do not commit `frontend/openapi.json` unless it is already tracked — check `git ls-files frontend/openapi.json`.)

---

### Task 3: Mark the prompt at claim

**Files:**
- Modify: `apps/harness/api.py::claim_turn` (~line 946)
- Test: `tests/test_claim_marks_chat_prompt.py`

**Interfaces:**
- Consumes: `authorship.for_turn(turn) -> str` (Task 1).
- Produces: `POST /api/harness/runners/{rid}/claim` returns `prompt` marked for chat-session turns with an initiator; `Turn.prompt` in the DB stays bare.

- [ ] **Step 1: Failing test**

Find the existing claim test helpers first: `grep -rn "def _pair\|/claim" tests/test_harness*.py | head`. Reuse its runner-pairing fixture and bearer header. The test:

```python
# tests/test_claim_marks_chat_prompt.py
"""The runner gets the author line; the database keeps the bare words."""
import pytest

from apps.canopy_sessions import authorship
from apps.canopy_sessions import services as chat
from apps.harness.models import Turn

pytestmark = pytest.mark.django_db


def test_claimed_chat_prompt_is_marked_but_stored_bare(paired_runner_client, chat_session_with_agent, owner):
    # fixtures: reuse/adapt from the existing harness claim tests — a runner that
    # may claim this agent's turns, and an agent chat session owned by `owner`.
    _msg, turn = chat.send_message(session=chat_session_with_agent, text="ship it", user=owner, client_id="c1")
    resp = paired_runner_client.post(f"/api/harness/runners/{paired_runner_client.runner_id}/claim")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(turn.id)
    author, bare, tid = authorship.parse(body["prompt"])
    assert author["user_id"] == owner.id and bare == "ship it" and tid == turn.id.hex
    assert Turn.objects.get(pk=turn.id).prompt == "ship it"
```

If the harness tests use plain helpers rather than fixtures, write the test with those helpers — the three assertions are what matter.

- [ ] **Step 2: Run — FAIL** (prompt unmarked)

Run: `uv run pytest tests/test_claim_marks_chat_prompt.py -v`

- [ ] **Step 3: Implement** — in `claim_turn`, just before `return Status(200, turn)`:

```python
    # Who wrote a chat line rides INSIDE the delivered prompt, because the
    # transcript that comes back records only what the agent read (spec
    # 2026-09-26). Set on this in-memory instance only — never saved: Slack's
    # status line and the lost-turn re-ask read Turn.prompt and must see the
    # bare words.
    from apps.canopy_sessions.authorship import for_turn

    turn.prompt = for_turn(turn)
```

`claim_next_turn` may not `select_related` the initiator; `for_turn` reads `turn.initiator_user` (one extra query) — acceptable on a claim.

- [ ] **Step 4: Run — PASS**; also `uv run pytest tests/test_architecture_boundary.py -q` (harness importing canopy_sessions is framework→framework, allowed).

- [ ] **Step 5: Commit**

```bash
git add apps/harness/api.py tests/test_claim_marks_chat_prompt.py
git commit -m "feat(harness): deliver chat prompts with an author line"
```

---

### Task 4: Delete the mid-turn interjection

**Files:**
- Modify: `apps/canopy_sessions/services.py` — delete `_maybe_interject` (~1406-1425) and its two call sites (end of `send_message` ~1240-1245, and in `_send_transcript_sourced_message`).
- Modify: `apps/realtime/consumers.py:245-252` — delete `runner_interject`.
- Modify: `runner/ec2/cloud_runner.py` — delete the `elif mtype == "interject":` branch (~3945-3957) and the steering helper it calls (~1849-1898) if nothing else uses it (`grep -n "def _steer\|_interject" runner/ec2/cloud_runner.py`).
- Modify: `tests/test_realtime_runner_consumer.py` (delete `test_interject_frame_reaches_the_runner`, replace `test_send_message_interjects_the_running_runner`), `runner/ec2/tests/test_cloud_runner.py` (delete the interject tests near line 1789).

**Interfaces:** Produces: `send_message` publishes nothing to a runner group.

- [ ] **Step 1: Replace the interject test with its inverse** — in `tests/test_realtime_runner_consumer.py`, rename `test_send_message_interjects_the_running_runner` to `test_send_during_running_turn_only_queues` and change its tail. Keep the setup (a running turn claimed by the connected runner, then `send_message(..., text="actually, stop and do X")`), then assert:

```python
    # A second send while a turn runs is QUEUED behind it and never pushed into
    # the running turn (spec 2026-09-26: messages land in send order).
    frames = []
    for _ in range(4):
        try:
            frames.append(await comm.receive_json_from(timeout=0.5))
        except Exception:
            break
    assert not any(f.get("type") == "interject" for f in frames)
    queued = await database_sync_to_async(
        lambda: list(Turn.objects.filter(chat_session=session, status=Turn.QUEUED)
                     .order_by("created_at").values_list("prompt", flat=True)))()
    assert queued[-1] == "actually, stop and do X"
```

Add a second send in the same test (`"and also Y"`) and assert `queued[-2:] == ["actually, stop and do X", "and also Y"]`.

- [ ] **Step 2: Run — FAIL** (an interject frame is received)

Run: `uv run pytest tests/test_realtime_runner_consumer.py -k only_queues -v`

- [ ] **Step 3: Delete** the four pieces listed under Files; delete `test_interject_frame_reaches_the_runner` and the cloud-runner interject tests.

- [ ] **Step 4: Run — PASS**

Run: `uv run pytest tests/test_realtime_runner_consumer.py -q && (cd runner/ec2 && uv run pytest tests/test_cloud_runner.py -q)` — check `runner/ec2` for its own test invocation (`ls runner/ec2; cat runner/ec2/pyproject.toml | head`) and use that.

- [ ] **Step 5: Commit**

```bash
git add -A apps/canopy_sessions/services.py apps/realtime/consumers.py runner/ec2 tests/test_realtime_runner_consumer.py
git commit -m "fix(chat): a send during a reply queues once instead of arriving twice"
```

---

### Task 5: Per-author drafts (model, service, socket routing)

**Files:**
- Modify: `apps/canopy_sessions/models.py` (class `Draft`, ~285)
- Create: `apps/canopy_sessions/migrations/0033_draft_per_author.py`
- Rewrite: `apps/canopy_sessions/drafts.py`
- Modify: `apps/canopy_sessions/serializers.py` (`draft_dto`, new `peer_draft_dto`, `session_state_dto`)
- Modify: `apps/canopy_sessions/consumers.py` (actions, `_chat_send`, `_commit_and_send`, `_discard_active`, group handlers, `_broadcast_draft`, `_snapshot`)
- Test: `tests/test_chat_multiplayer.py` (replace draft tests), `tests/test_chat_session_consumer.py` (update draft tests, add new)

**Interfaces:**
- Produces (Python):
  - `drafts.draft_for(session, user) -> Draft`
  - `drafts.update_draft(session, *, user, expected_version: int, body: str) -> Draft` (raises `DraftVersionMismatch`)
  - `drafts.commit_draft(session, user) -> str`
  - `drafts.discard_draft(session, user) -> Draft`
  - `drafts.peer_drafts(session, user) -> list[Draft]` — other authors' non-empty open drafts
  - `serializers.draft_dto(draft)` → `{id, slot, status, body, version, last_editor, last_edit_at, author_id}` (`last_editor` kept = `author_id` for old clients)
  - `serializers.peer_draft_dto(draft)` → `{author: {id, name}, body, at}`
- Produces (wire):
  - group message `{"type": "draft.updated", "draft": <dto>, "author_id": int}` → consumer sends `draft.updated` ONLY if `self.user.id == author_id`; to every OTHER socket it sends `{"event": "draft.typing", "data": <peer_draft_dto>}`.
  - group message `{"type": "draft.committed", ..., "author_id": int}` / `{"type": "draft.discarded", ..., "author_id": int}` → sent only to the author's sockets.
  - snapshot: `active_draft` = the connecting user's own draft; `peer_drafts: list[peer_draft_dto]`.
  - `draft.take_over` action: accepted, no-op.

- [ ] **Step 1: Replace the service tests** — in `tests/test_chat_multiplayer.py` delete `test_update_bumps_version_and_records_editor`, `test_version_mismatch_raises_with_authoritative_state`, `test_live_lock_blocks_others_then_take_over_after_release`, `test_lock_frees_when_holder_not_present`, `test_commit_returns_text_and_resets_draft`, and add:

```python
def _two():
    owner, ws, session = _ctx()
    other = User.objects.create_user("o", "o@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=other, workspace=ws, role=WorkspaceMembership.EDITOR)
    return owner, other, session


def test_two_people_draft_at_once_without_blocking():
    owner, other, session = _two()
    a = drafts.update_draft(session, user=owner, expected_version=0, body="mine")
    b = drafts.update_draft(session, user=other, expected_version=0, body="theirs")
    assert (a.body, b.body) == ("mine", "theirs")
    assert a.pk != b.pk


def test_version_guard_is_per_author():
    owner, _other, session = _two()
    drafts.update_draft(session, user=owner, expected_version=0, body="a")
    with pytest.raises(drafts.DraftVersionMismatch) as exc:
        drafts.update_draft(session, user=owner, expected_version=0, body="stale")
    assert exc.value.current_body == "a"


def test_commit_takes_only_my_text():
    owner, other, session = _two()
    drafts.update_draft(session, user=owner, expected_version=0, body="send me")
    drafts.update_draft(session, user=other, expected_version=0, body="not yet")
    assert drafts.commit_draft(session, owner) == "send me"
    assert drafts.draft_for(session, owner).body == ""
    assert drafts.draft_for(session, other).body == "not yet"


def test_peer_drafts_excludes_me_and_empty():
    owner, other, session = _two()
    drafts.update_draft(session, user=other, expected_version=0, body="typing")
    drafts.draft_for(session, owner)  # exists, empty
    assert [d.author_id for d in drafts.peer_drafts(session, owner)] == [other.id]
    assert drafts.peer_drafts(session, other) == []
```

- [ ] **Step 2: Run — FAIL**

Run: `uv run pytest tests/test_chat_multiplayer.py -v`

- [ ] **Step 3: Model + migration**

`Draft`:

```python
class Draft(models.Model):
    """One person's outgoing message in a session. One OPEN draft (slot='next')
    per (session, author) — everyone types in their own box, and the others see
    it live as a `draft.typing` row (spec 2026-09-26). It replaced a single shared
    draft with a 2s soft lock, which made two people who wanted to speak at once
    queue for the keyboard. `version` still guards one person's own tabs."""

    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="drafts")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    slot = models.CharField(max_length=16, default="next")
    body = models.TextField(blank=True, default="")
    version = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["session", "author"],
                condition=models.Q(slot="next"),
                name="one_open_draft_per_session_author",
            )
        ]
```

(`last_editor` is removed — it was the lock's input.) Generate: `uv run python manage.py makemigrations canopy_sessions -n draft_per_author`. Then edit the generated migration so the FIRST operation deletes existing rows (they have no author to backfill; text-in-progress is acceptable to lose):

```python
def _drop_shared_drafts(apps, schema_editor):
    apps.get_model("canopy_sessions", "Draft").objects.all().delete()

operations = [
    migrations.RunPython(_drop_shared_drafts, migrations.RunPython.noop),
    # ...generated RemoveConstraint / RemoveField(last_editor) / AddField(author) / AddConstraint
]
```

When makemigrations asks for a default for the non-null `author`, answer with a one-off default of `1` — the RunPython delete runs first so no row takes it. Verify with `uv run python manage.py migrate canopy_sessions` on a DB holding a draft.

- [ ] **Step 4: Rewrite `drafts.py`**

```python
"""Each person's outgoing draft in a session (spec 2026-09-26).

One open draft per (session, author). There is no lock: nobody waits for
anybody. The optimistic `version` survives only to reconcile one person's own
tabs (desktop + phone on the same account)."""
from __future__ import annotations

from django.db import transaction

from .models import Draft, Session


class DraftVersionMismatch(Exception):
    def __init__(self, current_version: int, current_body: str):
        self.current_version = current_version
        self.current_body = current_body
        super().__init__("draft version mismatch")


def draft_for(session: Session, user) -> Draft:
    draft, _ = Draft.objects.get_or_create(session=session, author=user, slot="next")
    return draft


def update_draft(session: Session, *, user, expected_version: int, body: str) -> Draft:
    with transaction.atomic():
        draft = Draft.objects.select_for_update().get(pk=draft_for(session, user).pk)
        if expected_version != draft.version:
            raise DraftVersionMismatch(draft.version, draft.body)
        draft.body = body
        draft.version += 1
        draft.save(update_fields=["body", "version", "updated_at"])
    return draft


def commit_draft(session: Session, user) -> str:
    """Take MY draft's text and reset it. Returns the committed text."""
    with transaction.atomic():
        draft = Draft.objects.select_for_update().get(pk=draft_for(session, user).pk)
        text = draft.body
        draft.body = ""
        draft.version += 1
        draft.save(update_fields=["body", "version", "updated_at"])
    return text


def discard_draft(session: Session, user) -> Draft:
    return _clear(session, user)


def _clear(session, user) -> Draft:
    with transaction.atomic():
        draft = Draft.objects.select_for_update().get(pk=draft_for(session, user).pk)
        if draft.body:
            draft.body = ""
            draft.version += 1
            draft.save(update_fields=["body", "version", "updated_at"])
    return draft


def peer_drafts(session: Session, user) -> list[Draft]:
    return list(
        Draft.objects.select_related("author")
        .filter(session=session, slot="next").exclude(author=user).exclude(body="")
        .order_by("updated_at")
    )
```

Grep for other users of the removed names and fix them: `grep -rn "active_draft\|commit_active_draft\|take_over\|lock_holder\|DraftLockHeld" apps tests | grep -v migrations` (the embed/contact REST send paths may call `commit_active_draft` — switch them to `commit_draft(session, user)` or drop the call if they have no user).

- [ ] **Step 5: Serializers**

```python
def draft_dto(draft: Draft | None) -> dict | None:
    if draft is None:
        return None
    return {
        "id": str(draft.pk),
        "slot": draft.slot,
        "status": "open",
        "body": draft.body,
        "version": draft.version,
        # `last_editor` kept for canopy-ui <= 0.12, which treats a draft whose
        # last_editor is itself as its own. This DTO only ever reaches its author.
        "last_editor": draft.author_id,
        "author_id": draft.author_id,
        "last_edit_at": _iso(draft.updated_at),
    }


def peer_draft_dto(draft: Draft) -> dict:
    user = draft.author
    return {
        "author": {"id": draft.author_id,
                   "name": (user.get_full_name() or "").strip() or user.email},
        "body": draft.body,
        "at": _iso(draft.updated_at),
    }
```

`session_state_dto(..., draft, peer_drafts=(), messages)`: keep `"active_draft": draft_dto(draft)` and add `"peer_drafts": [peer_draft_dto(d) for d in peer_drafts],`.

- [ ] **Step 6: Consumer tests (write first, then Step 7)** — in `tests/test_chat_session_consumer.py`, update `test_draft_update_broadcasts_to_other_socket` → rename `test_my_typing_reaches_others_as_draft_typing` and add two tests:

```python
async def test_my_typing_reaches_others_as_draft_typing():
    owner, teammate, session = await database_sync_to_async(_seed)()
    a, b = await _connect(session, owner), await _connect(session, teammate)
    await a.connect(); await b.connect()
    await _recv_match(a, lambda f: f["event"] == "session.state")
    await _recv_match(b, lambda f: f["event"] == "session.state")
    await a.send_json_to({"action": "draft.update", "data": {"version": 0, "body": "hi there"}})
    typing = await _recv_match(b, lambda f: f["event"] == "draft.typing")
    assert typing["data"]["author"]["id"] == owner.id
    assert typing["data"]["body"] == "hi there"
    await a.disconnect(); await b.disconnect()


async def test_peer_never_receives_my_draft_frames():
    owner, teammate, session = await database_sync_to_async(_seed)()
    a, b = await _connect(session, owner), await _connect(session, teammate)
    await a.connect(); await b.connect()
    await _recv_match(a, lambda f: f["event"] == "session.state")
    await _recv_match(b, lambda f: f["event"] == "session.state")
    await a.send_json_to({"action": "draft.update", "data": {"version": 0, "body": "x"}})
    await a.send_json_to({"action": "chat.send", "data": {"text": "x", "client_id": "c1"}})
    seen = []
    for _ in range(12):
        try:
            seen.append((await b.receive_json_from(timeout=1))["event"])
        except Exception:
            break
    assert "draft.updated" not in seen and "draft.committed" not in seen
    await a.disconnect(); await b.disconnect()


async def test_own_tabs_share_one_draft_and_see_no_peer_row():
    owner, _t, session = await database_sync_to_async(_seed)()
    t1, t2 = await _connect(session, owner), await _connect(session, owner)
    await t1.connect(); await t2.connect()
    await _recv_match(t1, lambda f: f["event"] == "session.state")
    await _recv_match(t2, lambda f: f["event"] == "session.state")
    await t1.send_json_to({"action": "draft.update", "data": {"version": 0, "body": "from desk"}})
    upd = await _recv_match(t2, lambda f: f["event"] in ("draft.updated", "draft.typing"))
    assert upd["event"] == "draft.updated" and upd["data"]["body"] == "from desk"
    await t1.disconnect(); await t2.disconnect()


async def test_snapshot_has_my_draft_and_peer_drafts():
    owner, teammate, session = await database_sync_to_async(_seed)()
    from apps.canopy_sessions import drafts
    await database_sync_to_async(drafts.update_draft)(session, user=teammate, expected_version=0, body="wip")
    comm = await _connect(session, owner)
    await comm.connect()
    snap = await _recv_match(comm, lambda f: f["event"] == "session.state")
    assert snap["data"]["active_draft"]["author_id"] == owner.id
    assert [p["body"] for p in snap["data"]["peer_drafts"]] == ["wip"]
    await comm.disconnect()


async def test_take_over_is_accepted_and_ignored():
    owner, _t, session = await database_sync_to_async(_seed)()
    comm = await _connect(session, owner)
    await comm.connect()
    await _recv_match(comm, lambda f: f["event"] == "session.state")
    await comm.send_json_to({"action": "draft.take_over", "data": {}})
    assert await comm.receive_nothing(timeout=0.5)
    await comm.disconnect()
```

Also update `test_version_conflict_uses_session_error` and `test_send_broadcasts_draft_committed` to the new service signatures (the committed frame now reaches only the sender). Run: `uv run pytest tests/test_chat_session_consumer.py -v` → the new tests FAIL.

- [ ] **Step 7: Consumer changes**

```python
_EDIT_ACTIONS = ("draft.update", "draft.take_over", "draft.discard", "chat.send")
```

(`draft.take_over` stays listed so a viewer's old client still gets `forbidden`.) In `receive_json`:

```python
        elif action == "draft.take_over":
            # Nothing to take: everyone has their own draft (spec 2026-09-26).
            # Still accepted because canopy-ui <= 0.12 sends it.
            return
```

Delete `_draft_take_over` and `draft_lock_changed`. `_draft_update`:

```python
    async def _draft_update(self, data):
        try:
            draft = await database_sync_to_async(drafts.update_draft)(
                self.session, user=self.user,
                expected_version=int(data.get("version", 0)),
                body=str(data.get("body", "")),
            )
        except drafts.DraftVersionMismatch as exc:
            await self._error(
                "draft_version_mismatch", "Draft changed since your last edit.",
                {"current_version": exc.current_version, "current_body": exc.current_body},
            )
            return
        await self._broadcast_draft(draft)
```

`_draft_discard`:

```python
    async def _draft_discard(self):
        draft = await database_sync_to_async(drafts.discard_draft)(self.session, self.user)
        await self._broadcast({"type": "draft.discarded", "draft_id": str(draft.pk),
                               "author_id": self.user.id})
        await self._broadcast_draft(draft)
```

`_chat_send`: replace `draft = await database_sync_to_async(drafts.active_draft)(self.session)` with `draft = await database_sync_to_async(drafts.draft_for)(self.session, self.user)` and add `"author_id": self.user.id` to the `draft.committed` broadcast. `_commit_and_send`: `committed = drafts.commit_draft(self.session, self.user)`. Delete `_discard_active`.

`_broadcast_draft` (loads the author for the peer DTO in the sync context):

```python
    async def _broadcast_draft(self, draft):
        payload = await database_sync_to_async(
            lambda: {"draft": serializers.draft_dto(draft),
                     "peer": serializers.peer_draft_dto(
                         type(draft).objects.select_related("author").get(pk=draft.pk))})()
        await self.channel_layer.group_send(
            self.group, {"type": "draft.updated", "author_id": draft.author_id, **payload})
```

Group handlers — a person's own frames to their own sockets, the peer view to everyone else:

```python
    def _is_author(self, message) -> bool:
        user = getattr(self, "user", None)
        return user is not None and message.get("author_id") == user.id

    async def draft_updated(self, message):
        if self._is_author(message):
            await self.send_json({"event": "draft.updated", "data": message["draft"]})
        else:
            # canopy-ui <= 0.12 adopts ANY draft.updated into its own composer, so
            # a peer's draft must never arrive under that name.
            await self.send_json({"event": "draft.typing", "data": message["peer"]})

    async def draft_committed(self, message):
        if not self._is_author(message):
            return  # an old client would build a message out of its OWN box
        await self.send_json({
            "event": "draft.committed",
            "data": {"draft_id": message["draft_id"], "user_message_id": message["user_message_id"],
                     "client_id": message.get("client_id", "")},
        })

    async def draft_discarded(self, message):
        if self._is_author(message):
            await self.send_json({"event": "draft.discarded", "data": {"draft_id": message["draft_id"]}})
```

A contact's read-only socket has `self.user = None`, so it receives only `draft.typing` — correct.

`_snapshot`: where it currently reads the shared draft (search `active_draft` in `_snapshot`), use:

```python
        own = drafts.draft_for(self.session, self.user) if self.user else None
        peers = drafts.peer_drafts(self.session, self.user) if self.user else \
            list(Draft.objects.select_related("author").filter(session=self.session, slot="next").exclude(body=""))
```

and pass `draft=own, peer_drafts=peers` to `session_state_dto`.

- [ ] **Step 8: Run — PASS**

Run: `uv run pytest tests/test_chat_multiplayer.py tests/test_chat_session_consumer.py tests/test_agui_socket.py tests/test_agui_projection.py tests/test_chat_serializers.py -q`
`agui.py:497` passes `draft.*` frames verbatim, so `draft.typing` needs no projection change; if `test_agui_*` pins `draft.lock_changed`, remove that case.

- [ ] **Step 9: Commit**

```bash
git add -A apps/canopy_sessions tests
git commit -m "feat(chat): everyone drafts in their own box; peers see draft.typing"
```

---

### Task 6: The queued list, visible to everyone

**Files:**
- Modify: `apps/canopy_sessions/services.py` — add `queued_messages`
- Create: `apps/canopy_sessions/queued_feed.py`
- Modify: `apps/canopy_sessions/status_feed.py::publish_for_turn` (tail call), `apps/canopy_sessions/signals.py` (receiver on `transcript_rows_streamed`), `apps/canopy_sessions/serializers.py::session_state_dto`, `apps/canopy_sessions/consumers.py` (handler `session_queued`, snapshot)
- Test: `tests/test_chat_queued.py`

**Interfaces:**
- Consumes: `Message.source_turn_id` (Task 2).
- Produces:
  - `services.queued_messages(session) -> list[dict]` — each `{"turn_id": str, "client_id": str, "author": {"name", "user_id"?, "contact_id"?} | None, "text": str, "sent_at": iso, "state": "queued" | "delivering"}`, ordered by `created_at`.
  - `queued_feed.publish_queued(session_id) -> None` (never raises) → group message `{"type": "session.queued", "queued": [...]}` → WS `{"event": "session.queued", "data": {"queued": [...]}}`.
  - snapshot key `"queued": [...]`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_chat_queued.py
"""Sends not yet in the transcript, shown to everyone, in send order."""
import uuid

import pytest
from django.contrib.auth.models import User

from apps.canopy_sessions import authorship
from apps.canopy_sessions import services as chat
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _runner_session():
    """A transcript-sourced session: the path where a send writes no durable row."""
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jon")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    session = chat.create_session(workspace=ws, created_by=owner)
    # Make it transcript-sourced — see services.transcript_sourced for the flag
    # this needs (e.g. session.transcript_sourced = True; session.save()).
    return owner, session


def test_queued_lists_every_pending_send_in_order():
    owner, session = _runner_session()
    chat.send_message(session=session, text="first", user=owner, client_id="c1")
    chat.send_message(session=session, text="second", user=owner, client_id="c2")
    q = chat.queued_messages(session)
    assert [e["text"] for e in q] == ["first", "second"]
    assert [e["client_id"] for e in q] == ["c1", "c2"]
    assert q[0]["author"] == {"name": "Jon", "user_id": owner.id}
    assert q[0]["state"] == "queued"


def test_a_send_leaves_the_list_when_its_transcript_row_lands():
    owner, session = _runner_session()
    _m, turn = chat.send_message(session=session, text="first", user=owner, client_id="c1")
    chat.persist_transcript_rows(session, [{"index": 10, "role": "user",
        "text": authorship.mark("first", name="Jon", user_id=owner.id, turn_id=turn.pk)}])
    assert chat.queued_messages(session) == []


def test_terminal_turns_are_not_queued():
    owner, session = _runner_session()
    _m, turn = chat.send_message(session=session, text="first", user=owner, client_id="c1")
    Turn.objects.filter(pk=turn.pk).update(status=Turn.CANCELLED)
    assert chat.queued_messages(session) == []


def test_claimed_turn_is_delivering():
    owner, session = _runner_session()
    _m, turn = chat.send_message(session=session, text="first", user=owner, client_id="c1")
    Turn.objects.filter(pk=turn.pk).update(status=Turn.CLAIMED)
    assert chat.queued_messages(session)[0]["state"] == "delivering"
```

Check the exact terminal-status constant names in `apps/harness/models.py` (`Turn.NON_TERMINAL`, `Turn.CANCELLED` / `Turn.CANCELED`) and fix the test.

- [ ] **Step 2: Run — FAIL** (`queued_messages` missing)

- [ ] **Step 3: Implement `queued_messages`** (in `services.py`, near `send_message`):

```python
def queued_messages(session: Session) -> list[dict]:
    """Human sends in this session that have not reached the transcript yet.

    Derived from Turn rows, never stored — the same reasoning as
    harness.turn_status: it is a function of rows that change on their own clock.
    A send leaves the list when the transcript row carrying its turn id lands
    (Message.source_turn_id, parsed from the author marker), or when its turn
    ends without one (cancelled, failed)."""
    landed = set(Message.objects.filter(session=session, source_turn_id__isnull=False)
                 .values_list("source_turn_id", flat=True))
    turns = (Turn.objects.select_related("initiator_user", "initiator_contact")
             .filter(chat_session=session, status__in=list(Turn.NON_TERMINAL))
             .exclude(initiator_user__isnull=True, initiator_contact__isnull=True)
             .order_by("created_at"))
    prefix = f"chat:{session.id.hex}:"
    out = []
    for t in turns:
        if t.pk in landed:
            continue
        author, _bare, _tid = authorship.parse(authorship.for_turn(t))
        key = t.idempotency_key or ""
        out.append({
            "turn_id": str(t.pk),
            "client_id": key[len(prefix):] if key.startswith(prefix) else "",
            "author": author,
            "text": t.prompt or "",
            "sent_at": t.created_at.isoformat(),
            "state": "queued" if t.status == Turn.QUEUED else "delivering",
        })
    return out
```

- [ ] **Step 4: Run — PASS**

- [ ] **Step 5: The feed + wiring, test first** — add to `tests/test_chat_session_consumer.py`:

```python
async def test_everyone_sees_a_teammates_queued_send():
    owner, teammate, session = await database_sync_to_async(_seed)()
    a, b = await _connect(session, owner), await _connect(session, teammate)
    await a.connect(); await b.connect()
    snap = await _recv_match(b, lambda f: f["event"] == "session.state")
    assert snap["data"]["queued"] == []
    await _recv_match(a, lambda f: f["event"] == "session.state")
    await a.send_json_to({"action": "chat.send", "data": {"text": "from jj", "client_id": "c9"}})
    q = await _recv_match(b, lambda f: f["event"] == "session.queued"
                          and any(e["text"] == "from jj" for e in f["data"]["queued"]), tries=20)
    assert q["data"]["queued"][-1]["author"]["user_id"] == owner.id
    await a.disconnect(); await b.disconnect()
```

(In the stub-executor test setting the ledger path writes a row with `source_turn_id` immediately, so the entry may already be gone by the time the frame is built. If so, mark this test's session transcript-sourced the same way as in `tests/test_chat_queued.py`, or assert on the frame emitted at enqueue before the stub runs.)

`apps/canopy_sessions/queued_feed.py`:

```python
"""Push the WHOLE queued list to a session's watchers (spec 2026-09-26).

Whole, never a delta: a just-connected client has no correct prior to apply
a delta to (the status_feed rule). Never raises — the list is an enhancement
to a conversation that works without it."""
from __future__ import annotations

import logging

from apps.realtime.groups import publish, session_group

logger = logging.getLogger(__name__)


def publish_queued(session_id) -> None:
    from .models import Session
    from .services import queued_messages

    try:
        session = Session.objects.get(pk=session_id)
        payload = queued_messages(session)
    except Exception:  # noqa: BLE001
        logger.exception("could not derive queued list for %s", session_id)
        return
    publish(session_group(session_id), {"type": "session.queued", "queued": payload})
```


Wire it:
- `status_feed.publish_for_turn`, at the end (after the `publish(...)` call): `from .queued_feed import publish_queued; publish_queued(turn.chat_session_id)` — this covers enqueue (`turn_status_changed`) and every status transition (claim, done, cancel).
- `signals.py`: a receiver on `transcript_rows_streamed` (`from apps.harness.signals import transcript_rows_streamed`), `dispatch_uid="chat_queued_on_transcript"`, calling `publish_queued(session.pk)` when any streamed row has role `"user"`.
- `consumers.py`: handler

```python
    async def session_queued(self, message):
        await self.send_json({"event": "session.queued", "data": {"queued": message.get("queued") or []}})
```

  and in `_snapshot` pass `queued=chat_services.queued_messages(self.session)`; `session_state_dto` gains `queued=()` → `"queued": list(queued)`.
- `agui.py`: confirm `session.queued` passes through (line ~497 checks `draft.`/`presence.` prefixes — add `event == "session.queued"` to that verbatim-passthrough condition; an unrecognised frame is dropped silently otherwise, per CLAUDE.md).

- [ ] **Step 6: Run — PASS**

Run: `uv run pytest tests/test_chat_queued.py tests/test_chat_session_consumer.py tests/test_agui_socket.py -q`

- [ ] **Step 7: Commit**

```bash
git add -A apps/canopy_sessions tests
git commit -m "feat(chat): everyone sees queued sends, with their author, in send order"
```

---

### Task 7: Frontend protocol + reducer

**Files:**
- Modify: `frontend/packages/canopy-ui/src/chat/protocol.ts`
- Modify: `frontend/packages/canopy-ui/src/chat/sessionReducer.ts`
- Test: `frontend/packages/canopy-ui/src/chat/sessionReducer.test.ts`, `sessionReducer.events.test.ts`

**Interfaces:**
- Produces (TS):

```ts
export interface MessageAuthor { name: string; user_id?: number; contact_id?: number }
// Message gains:  author?: MessageAuthor | null;
export interface PeerDraft { author: { id: number; name: string }; body: string; at: string | null }
export interface QueuedMessage {
  turn_id: string; client_id: string; author: MessageAuthor | null;
  text: string; sent_at: string; state: "queued" | "delivering";
}
// Draft gains:  author_id?: number;
// SessionState gains:  peer_drafts?: PeerDraft[];  queued?: QueuedMessage[];
// WsEvent gains:
//   | { event: "draft.typing"; data: PeerDraft }
//   | { event: "session.queued"; data: { queued: QueuedMessage[] } }
// chat.user_message data gains:  author?: MessageAuthor | null
// WsEvent LOSES: draft.lock_changed.  WsAction keeps draft.take_over (deprecated).
```

- [ ] **Step 1: Failing reducer tests** (append to `sessionReducer.test.ts`, using its existing `baseState()`-style helper — check the top of the file for its name):

```ts
describe("per-person drafts", () => {
  it("draft.typing upserts a peer by author and an empty body removes it", () => {
    let s = sessionReducer(base(), { event: "draft.typing", data: { author: { id: 2, name: "Bo" }, body: "hi", at: null } });
    s = sessionReducer(s, { event: "draft.typing", data: { author: { id: 2, name: "Bo" }, body: "hi there", at: null } });
    expect(s.peer_drafts).toEqual([{ author: { id: 2, name: "Bo" }, body: "hi there", at: null }]);
    s = sessionReducer(s, { event: "draft.typing", data: { author: { id: 2, name: "Bo" }, body: "", at: null } });
    expect(s.peer_drafts).toEqual([]);
  });

  it("presence.left drops that person's typing row", () => {
    let s = sessionReducer(base(), { event: "draft.typing", data: { author: { id: 2, name: "Bo" }, body: "x", at: null } });
    s = sessionReducer(s, { event: "presence.left", data: { user_id: 2 } });
    expect(s.peer_drafts).toEqual([]);
  });

  it("session.queued replaces the whole list", () => {
    const q = { turn_id: "t1", client_id: "c1", author: { name: "Bo", user_id: 2 }, text: "yo", sent_at: "2026-09-26T00:00:00Z", state: "queued" as const };
    let s = sessionReducer(base(), { event: "session.queued", data: { queued: [q] } });
    expect(s.queued).toEqual([q]);
    s = sessionReducer(s, { event: "session.queued", data: { queued: [] } });
    expect(s.queued).toEqual([]);
  });

  it("chat.user_message keeps the author", () => {
    const s = sessionReducer(base(), { event: "chat.user_message", data: { message_id: "m1", turn_index: 5, plaintext: "hi", author: { name: "Bo", user_id: 2 } } });
    expect(s.messages.at(-1)?.author).toEqual({ name: "Bo", user_id: 2 });
  });
});
```

Remove any existing test for `draft.lock_changed`.

- [ ] **Step 2: Run — FAIL**

Run: `cd frontend && npx vitest run packages/canopy-ui/src/chat/sessionReducer.test.ts`

- [ ] **Step 3: Implement** — types per Interfaces above in `protocol.ts` (delete the `draft.lock_changed` variant; mark `draft.take_over` with a `/** @deprecated accepted and ignored by the server since 0.13 */` comment). In `sessionReducer.ts`:
  - `REDUCER_EVENTS`: remove `"draft.lock_changed"`, add `"draft.typing"`, `"session.queued"`.
  - Delete the `case "draft.lock_changed"` branch.
  - `case "draft.updated"`: this frame now only ever carries MY draft, so keep the existing "keep local body" behaviour but test `incoming.author_id ?? incoming.last_editor` against `prev.current_user_id`.
  - Add:

```ts
    case "draft.typing": {
      // Someone else's box, live. Keyed by author; an empty body means they
      // sent, discarded or cleared it.
      const rest = (prev.peer_drafts ?? []).filter((d) => d.author.id !== frame.data.author.id);
      return { ...prev, peer_drafts: frame.data.body ? [...rest, frame.data] : rest };
    }

    case "session.queued":
      // Wholesale, like session.turn_status: a client that just connected has
      // no correct prior to merge a delta into.
      return { ...prev, queued: frame.data.queued };
```

  - `case "presence.left"`: also `peer_drafts: (prev.peer_drafts ?? []).filter((d) => d.author.id !== frame.data.user_id)`.
  - `case "chat.user_message"`: in both the update and the insert branch set `author: frame.data.author ?? m.author ?? null` / `author: frame.data.author ?? null`.
  - `case "draft.committed"`: unchanged (it now only reaches the sender).

- [ ] **Step 4: Run — PASS**, including `sessionReducer.events.test.ts` (asserts `REDUCER_EVENTS` matches the switch).

- [ ] **Step 5: Commit**

```bash
git add frontend/packages/canopy-ui/src/chat
git commit -m "feat(canopy-ui): reducer tracks peer drafts, the queued list, and authors"
```

---

### Task 8: Frontend UI

**Files:**
- Create: `frontend/packages/canopy-ui/src/chat/TypingRows.tsx`, `QueuedRows.tsx`
- Modify: `SendBox.tsx`, `ChatPanel.tsx`, `PresenceChips.tsx`, `MessageItem.tsx`, `MessageList.tsx` (pass `currentUserId`), `useSessionSocket.ts`, `drafts.ts`, `index.ts`, `package.json` (version `0.13.0`)
- Modify: `frontend/src/pages/ChatPage.tsx:788`, `frontend/src/embed/EmbedApp.tsx:482,833` (drop `onTakeOver`)
- Test: `frontend/packages/canopy-ui/src/chat/ChatPanel.multiplayer.test.tsx` (new); update `ChatPanel.pending.test.tsx`, `useSessionSocket.*.test.tsx` where they reference the lock

**Interfaces:**
- Consumes: `SessionState.peer_drafts`, `SessionState.queued`, `Message.author` (Task 7).
- Produces: `<TypingRows peers={PeerDraft[]} />`, `<QueuedRows queued={QueuedMessage[]} hideClientIds={Set<string>} currentUserId={number} />`; `ChatPanel` prop `onTakeOver?: () => void` (optional, ignored, `@deprecated`); `useSessionSocket().takeOverDraft` kept as a deprecated no-op.

- [ ] **Step 1: Failing component tests**

```tsx
// frontend/packages/canopy-ui/src/chat/ChatPanel.multiplayer.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ChatPanel } from "./ChatPanel";
import type { SessionState } from "./protocol";

function state(over: Partial<SessionState> = {}): SessionState {
  return {
    messages: [], active_draft: null, participants: [
      { user_id: 1, email: "a@x", display_name: "Alice A", role: "owner", joined_at: null, last_seen_at: null },
      { user_id: 2, email: "b@x", display_name: "Bo B", role: "editor", joined_at: null, last_seen_at: null },
    ], presence_user_ids: [1, 2], current_user_id: 1, ...over,
  } as SessionState;
}

const noop = () => undefined;
const props = { connected: true, currentUserId: 1, onSend: noop, onStop: noop, onUpdateDraft: noop } as const;
// Check ChatPanel's Props for the exact required prop names and fill any others with noops.

describe("multiplayer composer", () => {
  it("shows a teammate's live text and never locks my box", () => {
    render(<ChatPanel {...props} state={state({ peer_drafts: [{ author: { id: 2, name: "Bo B" }, body: "thinking out loud", at: null }] })} />);
    expect(screen.getByTestId("typing-row")).toHaveTextContent("Bo B");
    expect(screen.getByTestId("typing-row")).toHaveTextContent("thinking out loud");
    expect(screen.getByTestId("composer")).not.toBeDisabled();
    expect(screen.queryByTestId("coedit-banner")).toBeNull();
  });

  it("send stays available while the agent is replying", () => {
    const streaming = { id: "a1", turn_index: 2, role: "assistant" as const, content: {}, plaintext: "working", status: "streaming" as const, error_detail: null, started_at: null, completed_at: null, created_at: "" };
    render(<ChatPanel {...props} state={state({ messages: [streaming] })} />);
    expect(screen.getByRole("button", { name: /stop/i })).toBeInTheDocument();
    expect(screen.getByTestId("send")).toBeInTheDocument();
  });

  it("renders a teammate's queued send with their name", () => {
    render(<ChatPanel {...props} state={state({ queued: [{ turn_id: "t", client_id: "c", author: { name: "Bo B", user_id: 2 }, text: "next up", sent_at: "", state: "queued" }] })} />);
    expect(screen.getByTestId("queued-row")).toHaveTextContent("Bo B");
    expect(screen.getByTestId("queued-row")).toHaveTextContent("queued");
  });

  it("labels someone else's message with their name, not mine", () => {
    const mine = { id: "m1", turn_index: 1, role: "user" as const, content: {}, plaintext: "me", status: "complete" as const, error_detail: null, started_at: null, completed_at: null, created_at: "", author: { name: "Alice A", user_id: 1 } };
    const theirs = { ...mine, id: "m2", turn_index: 2, plaintext: "them", author: { name: "Bo B", user_id: 2 } };
    render(<ChatPanel {...props} state={state({ messages: [mine, theirs] })} />);
    expect(screen.getAllByTestId("message-author").map((n) => n.textContent)).toEqual(["Bo B"]);
  });
});
```

- [ ] **Step 2: Run — FAIL**

Run: `cd frontend && npx vitest run packages/canopy-ui/src/chat/ChatPanel.multiplayer.test.tsx`

- [ ] **Step 3: New components**

```tsx
// TypingRows.tsx
import type { PeerDraft } from "./protocol";

/** Everyone else's box, live — the multiplayer half of the composer. One row
 *  per person, newest edit last, right above your own box. */
export function TypingRows({ peers }: { peers: PeerDraft[] }) {
  if (peers.length === 0) return null;
  return (
    <ul className="space-y-1 border-t border-border bg-background px-3 py-2" aria-live="polite">
      {peers.map((p) => (
        <li key={p.author.id} data-testid="typing-row" className="flex min-w-0 gap-2 text-xs">
          <span className="shrink-0 font-medium text-foreground">{p.author.name}</span>
          <span className="shrink-0 text-muted-foreground">is typing:</span>
          <span className="min-w-0 truncate italic text-foreground-secondary">{p.body}</span>
        </li>
      ))}
    </ul>
  );
}
```

```tsx
// QueuedRows.tsx
import type { QueuedMessage } from "./protocol";

/** Sends that have not reached the agent yet, in send order, for everyone.
 *  Your own are skipped when your optimistic echo already shows them
 *  (`hideClientIds`), so a line never appears twice. */
export function QueuedRows({ queued, hideClientIds, currentUserId }: {
  queued: QueuedMessage[]; hideClientIds: Set<string>; currentUserId: number;
}) {
  const rows = queued.filter((q) => !q.client_id || !hideClientIds.has(q.client_id));
  if (rows.length === 0) return null;
  return (
    <div className="px-3">
      {rows.map((q) => {
        const mine = q.author?.user_id === currentUserId;
        return (
          <div key={q.turn_id} data-testid="queued-row"
               className={`my-2 max-w-[80%] rounded-2xl border border-dashed border-border px-4 py-2 text-sm ${mine ? "ml-auto" : "mr-auto"}`}>
            <div className="mb-0.5 flex gap-2 text-[11px] text-muted-foreground">
              <span className="font-medium text-foreground">{mine ? "You" : q.author?.name ?? "Someone"}</span>
              <span>{q.state === "queued" ? "queued" : "sending to agent…"}</span>
            </div>
            <div className="whitespace-pre-wrap text-foreground-secondary [overflow-wrap:anywhere]">{q.text}</div>
          </div>
        );
      })}
    </div>
  );
}
```

- [ ] **Step 4: Wire into ChatPanel / SendBox / MessageItem / PresenceChips**
  - `ChatPanel.tsx`: delete the lock tick effect, `holderId`, `holderIsPresent`, `holderName`; make `onTakeOver?: () => void` optional with `/** @deprecated ignored since 0.13 — everyone has their own draft */`; stop passing it to `SendBox`. Pass `currentUserId` to `MessageList`. Render `<QueuedRows queued={state.queued ?? []} hideClientIds={new Set(state.messages.map((m) => m.content?.client_id).filter((c): c is string => typeof c === "string"))} currentUserId={currentUserId} />` inside the scroll container after `<MessageList>`, and `<TypingRows peers={state.peer_drafts ?? []} />` between the scroll container and `<SendBox>`. Add `(state.queued?.length ?? 0)` to `scrollDep`. Change `PresenceChips` call: drop `draftHolderId` / `draftHolderIdle`.
  - `SendBox.tsx`: delete props `currentUserId`, `holderIsPresent`, `holderName`, `onTakeOver`; delete the tick effect, `holderId`/`isHolder`/`holderIsIdle`, the `theirEdit` adopt effect (a draft that reaches this box is always mine), `canEdit`, `lockedByTeammate` and the coedit banner. `canSend = connected && draft != null && body.trim().length > 0 && !blocked` — no `!isStreaming`. `disabled={blocked}`. Placeholder: `blocked ? disabledReason : !draft ? "Type a message… (connecting…)" : isStreaming ? "Type a message — it will be sent after the current reply" : "Type a message… (Enter to send, Shift+Enter for newline)"`. Keep the Stop button while `isStreaming`; Send renders beside it.
  - `MessageItem.tsx`: accept `currentUserId?: number`; for a `user` message with `message.author` whose `user_id !== currentUserId`, render left-aligned (`mr-auto bg-muted text-foreground`) with `<div data-testid="message-author" className="mb-0.5 text-[11px] font-medium text-muted-foreground">{message.author.name}</div>` above the text. A user message with no author keeps the current right-aligned primary bubble **unless** `content.client_id` is absent AND another participant exists — do NOT add that heuristic; unauthored stays as today. A `user` message with `author === null` from the transcript and no `client_id` gets a small `typed in emdash` caption (`data-testid="message-author"` text `typed in emdash`) — only when `message.author === null` explicitly (not `undefined`, which is an older server).
  - `MessageList.tsx`: thread `currentUserId` to `MessageItem`.
  - `PresenceChips.tsx`: delete `draftHolderId`, `draftHolderIdle`, the `editor` logic and label; `describe(present)` drops its editor clause.
  - `useSessionSocket.ts`: `takeOverDraft = useCallback(() => undefined, [])` with a `@deprecated` JSDoc on the result type.
  - `drafts.ts`: delete `IDLE_THRESHOLD_MS`, `isDraftIdle`, `msUntilDraftIdle`; `index.ts`: remove their export and export `TypingRows`, `QueuedRows`, and the new types. (Before deleting exports, `grep -rn "isDraftIdle\|IDLE_THRESHOLD_MS\|msUntilDraftIdle" <ace-web checkout>/frontend/src` — if ace-web imports them, keep them as `@deprecated` exports instead.)
  - Hosts: remove `onTakeOver=` from `frontend/src/pages/ChatPage.tsx:788` and `frontend/src/embed/EmbedApp.tsx:482,833`.
  - `frontend/packages/canopy-ui/package.json`: `"version": "0.13.0"`.

- [ ] **Step 5: Run the whole chat suite + build**

Run: `cd frontend && npx vitest run packages/canopy-ui src && npm run build`
Fix any test still asserting the coedit banner, `take-over`, `presence-editing-label` or a disabled send while streaming — those behaviours are intentionally gone; rewrite each assertion to the new behaviour rather than deleting coverage.

- [ ] **Step 6: Commit**

```bash
git add -A frontend
git commit -m "feat(canopy-ui): own composer per person, live typing rows, queued sends, authored bubbles"
```

---

### Task 9: Docs, full suite, live check

**Files:**
- Modify: `CLAUDE.md` (Design Decisions — replace nothing; add one bullet after the "A chat is recorded the same way whatever surface it started on" bullet)
- Modify: `docs/superpowers/specs/2026-09-26-per-person-drafts-and-authored-messages-design.md` status line → "SHIPPED (#<pr>)" after merge

- [ ] **Step 1: CLAUDE.md bullet**

```markdown
- **Everyone in a chat has their own draft, and every line says who wrote it** (spec 2026-09-26). `Draft` is one per (session, author) — the shared draft with a 2s soft lock made two people who wanted to speak queue for the keyboard. A person's own `draft.updated`/`draft.committed` reach only their own tabs; everyone else gets `draft.typing`, because canopy-ui ≤0.12 adopts ANY `draft.updated` into its composer. A send only enqueues (the mid-turn interjection delivered it twice), and `queued_messages` (derived, never stored) shows every watcher the sends not yet in the transcript. **Authorship rides INSIDE the transcript**: `claim_turn` prefixes the delivered prompt with `[canopy from="…" user=N turn=<hex>]` (`apps/canopy_sessions/authorship.py`), and `persist_transcript_rows` + the live user frame parse it into `Message.author`/`source_turn_id`. The marker is added at claim and never stored on `Turn.prompt`, which Slack's status line and the lost-turn re-ask read verbatim.
```

- [ ] **Step 2: Full backend + frontend suites**

Run: `uv run pytest -q` and `cd frontend && npm test && npm run build`
Expected: all green. Paste failures, fix, re-run.

- [ ] **Step 3: Live two-browser check (manual, before calling it done)** — `uv run honcho start -f Procfile.dev`, open one agent chat as two users (normal + private window, two accounts). Verify: (1) both type at once, each sees the other's `is typing:` row, neither box locks; (2) while a reply streams, both send — both appear as queued, in order, for both people; (3) after the replies, each user bubble shows its author on the other person's screen and survives a reload. Record what you saw in the PR description.

- [ ] **Step 4: Commit, PR with auto-merge**

```bash
git add CLAUDE.md docs/superpowers
git commit -m "docs: per-person drafts and authored messages"
git push -u origin HEAD
gh pr create --fill
gh pr merge <n> --auto
gh pr view <n> --json autoMergeRequest
```

(No `--squash` — the merge queue owns strategy. PR body ends with the Claude Code attribution line.)
