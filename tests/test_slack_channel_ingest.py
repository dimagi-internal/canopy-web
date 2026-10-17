# ruff: noqa: F811 — the fixtures below are imported from test_slack and then requested by name.
"""A whole Slack channel, read for an agent — only with permission (canopy-web#1533).

Driven through the real REST routes and the real signed interactions webhook,
with Slack faked at the `requests` boundary as in test_slack.py. What is pinned
is the issue's list: nothing is read before approval; the agent's login cannot
approve; a non-member cannot approve; a decline reads nothing; revoke stops
follow; pagination resumes after a rate limit; only the requesting agent reads
the transcript.
"""
from __future__ import annotations

from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from apps.events.models import Event
from apps.slack import ingest
from apps.slack.models import SlackChannelIngest, SlackIngestMessage, SlackUserLink, SlackWorkspaceLink
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_user
from tests.test_slack import (  # noqa: F401
    ALICE,
    BOB,
    TEAM,
    _link_client,
    _owner,
    alice,
    click,
    configured,
    hal,
    installation,
    linked,
    slack,
    ws,
)

pytestmark = pytest.mark.django_db

CHAN, OWEN, UHAL, CREATOR = "C1", "UOWEN", "UHAL", "UCREATOR"
POST_TS = "1700000999.000100"   # what FakeSlack's chat.postMessage hands out


def _resp(body, status=200, headers=None):
    r = mock.Mock(status_code=status, headers=headers or {})
    r.raise_for_status = lambda: None
    r.json = lambda: body
    return r


class Channel:
    """C1's history, as Slack pages it: newest first, two to a page."""

    def __init__(self):
        self.history = [  # newest first
            {"ts": "1700000500.000000", "user": BOB, "text": "last word"},
            {"ts": "1700000400.000000", "user": ALICE, "text": "a thread about <@UBOB>", "reply_count": 2,
             "latest_reply": "1700000460.000000"},
            {"ts": "1700000300.000000", "user": BOB, "text": "middle"},
            {"ts": "1700000100.000000", "user": ALICE, "text": "first ever message"},
        ]
        self.replies = {"1700000400.000000": [
            {"ts": "1700000400.000000", "user": ALICE, "text": "a thread about <@UBOB>"},
            {"ts": "1700000450.000000", "user": BOB, "text": "reply one", "thread_ts": "1700000400.000000"},
            {"ts": "1700000460.000000", "user": ALICE, "text": "reply two", "thread_ts": "1700000400.000000"},
        ]}
        self.members = [ALICE, OWEN, UHAL, CREATOR]
        self.creator = CREATOR
        self.rate_limit_on: set[str] = set()   # cursors that answer 429 once
        self.reads: list[tuple[str, dict]] = []

    def answer(self, fake):
        def call(url, headers=None, json=None, data=None, timeout=None):
            method = url.rsplit("/", 1)[-1]
            p = dict(data or {})
            if method == "conversations.history":
                self.reads.append((method, p))
                cursor = p.get("cursor", "")
                if cursor in self.rate_limit_on:
                    self.rate_limit_on.discard(cursor)
                    return _resp({"ok": False, "error": "ratelimited"}, 429, {"Retry-After": "7"})
                msgs = [m for m in self.history if float(m["ts"]) >= float(p.get("oldest") or 0)]
                start = int(cursor or 0)
                page = msgs[start:start + 2]
                nxt = str(start + 2) if start + 2 < len(msgs) else ""
                return _resp({"ok": True, "messages": page, "has_more": bool(nxt),
                              "response_metadata": {"next_cursor": nxt}})
            if method == "conversations.replies":
                self.reads.append((method, p))
                msgs = [m for m in self.replies.get(p["ts"], [])
                        if m["ts"] == p["ts"] or float(m["ts"]) >= float(p.get("oldest") or 0)]
                return _resp({"ok": True, "messages": msgs, "has_more": False})
            if method == "conversations.list":
                self.reads.append((method, p))
                return _resp({"ok": True, "channels": [{"id": "C9", "name": "random"},
                                                       {"id": CHAN, "name": "canopy-support"}]})
            if method == "conversations.members":
                return _resp({"ok": True, "members": self.members})
            if method == "conversations.info":
                return _resp({"ok": True, "channel": {"id": CHAN, "name": "canopy-support",
                                                      "creator": self.creator}})
            return fake(url, headers=headers, json=json, data=data, timeout=timeout)
        return call


@pytest.fixture
def channel(slack):
    ch = Channel()
    with mock.patch("apps.slack.client.requests.post", side_effect=ch.answer(slack)):
        yield ch


@pytest.fixture
def enabled(installation, ws):
    SlackWorkspaceLink.objects.filter(workspace=ws).update(channel_ingest_enabled=True)


@pytest.fixture
def hal_login(hal, ws):
    user = a_user("hal@dimagi-ai.com")
    wsvc.ensure_member(ws, user, WorkspaceMembership.EDITOR)
    hal.user = user
    hal.save()
    return user


@pytest.fixture
def owen(installation, ws):
    """A workspace ADMIN, in the channel, with a linked Slack account."""
    user = a_user("owen@dimagi.com")
    wsvc.ensure_member(ws, user, WorkspaceMembership.ADMIN)
    SlackUserLink.objects.create(installation=installation, slack_user_id=OWEN, user=user)
    return user


def _request(client, *, follow=False, agent="hal"):
    return client.post("/api/slack-ingests/", {"agent": agent, "channel": CHAN, "purpose": "AI champions digest",
                                               "follow": follow}, content_type="application/json")


@pytest.fixture
def pending(channel, enabled, hal_login, slack):
    resp = _request(_link_client(hal_login))
    assert resp.status_code == 201, resp.content
    return SlackChannelIngest.objects.get(pk=resp.json()["id"])


def _approve(ingest_row, user=OWEN):
    return click(ingest.APPROVE, str(ingest_row.pk), user=user, message_ts=ingest_row.approval_ts)


def _reads(channel):
    return [m for m, _ in channel.reads]


def _run(row):
    """Tick until the ingest stops being due (bounded)."""
    for _ in range(20):
        SlackChannelIngest.objects.filter(pk=row.pk, next_attempt_at__isnull=False).update(
            next_attempt_at=timezone.now() - timedelta(seconds=1))
        ingest.tick()
        row.refresh_from_db()
        if row.status != SlackChannelIngest.READING:
            return row
    return row


# ---- the request ---------------------------------------------------------------

def test_a_request_posts_one_approval_message_and_reads_nothing(pending, channel, slack):
    assert pending.status == SlackChannelIngest.PENDING
    (post,) = slack.said("chat.postMessage")
    assert post["channel"] == CHAN
    buttons = {e["action_id"] for b in post["blocks"] if b["type"] == "actions" for e in b["elements"]}
    assert buttons == {ingest.APPROVE, ingest.DECLINE}
    assert "AI champions digest" in post["blocks"][0]["text"]["text"]
    assert pending.approval_ts == POST_TS
    assert Event.objects.filter(kind="slack.ingest_requested").count() == 1


def test_nothing_is_read_before_approval(pending, channel, hal_login):
    assert ingest.tick() == 0
    pending.next_attempt_at = timezone.now()   # even if something marked it due
    pending.save()
    ingest.advance(pending)
    assert not {"conversations.history", "conversations.replies"} & set(_reads(channel))
    resp = _link_client(hal_login).get(f"/api/slack-ingests/{pending.pk}/transcript")
    assert resp.status_code == 409


def test_the_workspace_switch_is_off_by_default(channel, installation, hal_login, slack):
    resp = _request(_link_client(hal_login))
    assert resp.status_code == 409
    assert "off" in resp.json()["detail"]
    assert not slack.said("chat.postMessage")
    assert not SlackChannelIngest.objects.exists()


def test_only_the_agent_or_its_admins_can_ask(channel, enabled, hal_login, alice):
    assert _request(_link_client(alice)).status_code == 403


def test_a_channel_may_be_named(channel, enabled, hal_login):
    resp = _link_client(hal_login).post("/api/slack-ingests/", {"agent": "hal", "channel": "#canopy-support",
                                                                  "purpose": "x"}, content_type="application/json")
    assert resp.status_code == 201, resp.content
    assert resp.json()["channel"] == CHAN and resp.json()["status"] == "pending"


def test_a_name_canopy_cannot_see_is_refused(channel, enabled, hal_login):
    client_ = _link_client(hal_login)
    resp = client_.post("/api/slack-ingests/", {"agent": "hal", "channel": "#nope", "purpose": "x"},
                        content_type="application/json")
    assert resp.status_code == 404
    resp = client_.post("/api/slack-ingests/", {"agent": "hal", "channel": "not a channel!", "purpose": "x"},
                        content_type="application/json")
    assert resp.status_code == 422


def test_an_owner_turns_the_switch_on(channel, installation, ws, alice):
    owner = _link_client(_owner(ws))
    resp = owner.put(f"/api/slack-config/{ws.slug}/channel-ingest", {"enabled": True},
                     content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["channel_ingest"] == {"enabled": True, "allowed": []}
    assert _link_client(alice).put(f"/api/slack-config/{ws.slug}/channel-ingest", {"enabled": False},
                                   content_type="application/json").status_code == 403


# ---- the decision --------------------------------------------------------------

def test_the_agent_login_cannot_approve(pending, channel, installation, hal_login, slack):
    SlackUserLink.objects.create(installation=installation, slack_user_id=UHAL, user=hal_login)
    # Even as a workspace admin: the agent can never answer its own request.
    wsvc.ensure_member(pending.workspace, hal_login, WorkspaceMembership.ADMIN)
    _approve(pending, user=UHAL)
    pending.refresh_from_db()
    assert pending.status == SlackChannelIngest.PENDING
    assert "can't answer its own request" in slack.said("chat.postEphemeral")[-1]["text"]
    assert Event.objects.filter(kind="slack.ingest_refused").exists()


def test_a_non_member_of_the_channel_cannot_approve(pending, channel, owen, slack):
    channel.members = [ALICE]
    _approve(pending)
    pending.refresh_from_db()
    assert pending.status == SlackChannelIngest.PENDING
    assert "member of this channel" in slack.said("chat.postEphemeral")[-1]["text"]


def test_a_member_who_is_not_an_admin_cannot_approve(pending, channel, linked, slack):
    _approve(pending, user=ALICE)       # alice is an editor, not an admin, and not the creator
    pending.refresh_from_db()
    assert pending.status == SlackChannelIngest.PENDING


def test_the_channels_creator_can_approve(pending, channel, linked):
    channel.creator = ALICE
    _approve(pending, user=ALICE)
    pending.refresh_from_db()
    assert pending.status == SlackChannelIngest.READING
    assert pending.decided_by_slack_id == ALICE


def test_a_click_on_another_message_is_not_a_decision(pending, channel, owen):
    click(ingest.APPROVE, str(pending.pk), user=OWEN, message_ts="1700000001.000000")
    pending.refresh_from_db()
    assert pending.status == SlackChannelIngest.PENDING


def test_a_decline_reads_nothing_and_is_final(pending, channel, linked, owen, slack):
    click(ingest.DECLINE, str(pending.pk), user=ALICE, message_ts=pending.approval_ts)
    pending.refresh_from_db()
    assert pending.status == SlackChannelIngest.DECLINED
    _approve(pending)                   # too late
    pending.refresh_from_db()
    assert pending.status == SlackChannelIngest.DECLINED
    _run(pending)
    assert not {"conversations.history", "conversations.replies"} & set(_reads(channel))
    (update,) = [u for u in slack.said("chat.update") if "Declined" in u["text"]]
    assert not [b for b in update["blocks"] if b["type"] == "actions"]
    assert Event.objects.filter(kind="slack.ingest_declined").count() == 1


# ---- the read ------------------------------------------------------------------

def test_an_approval_reads_the_whole_channel_threads_included(pending, channel, owen, hal_login, slack):
    _approve(pending)
    pending.refresh_from_db()
    assert pending.status == SlackChannelIngest.READING
    assert pending.decided_by == owen
    row = _run(pending)
    assert row.status == SlackChannelIngest.READY and row.backfilled_at
    assert row.messages == 6
    resp = _link_client(hal_login).get(f"/api/slack-ingests/{row.pk}/transcript")
    assert resp.status_code == 200, resp.content
    md = resp.json()["markdown"]
    assert "first ever message" in md and "last word" in md
    assert "  - **" in md and "reply one" in md            # replies nested under their parent
    assert "@Bob B" in md                                     # mentions named
    assert md.index("first ever message") < md.index("last word")
    assert "QUOTED MATERIAL" in md
    kinds = set(Event.objects.filter(source="slack").values_list("kind", flat=True))
    assert {"slack.ingest_requested", "slack.ingest_approved", "slack.ingest_batch"} <= kinds


def test_pagination_resumes_after_a_rate_limit(pending, channel, owen):
    channel.rate_limit_on = {"2"}       # the second page answers 429 once
    _approve(pending)
    row = ingest.advance(pending)
    assert row.status == SlackChannelIngest.READING
    assert row.history_cursor == "2"
    assert row.next_attempt_at > timezone.now() + timedelta(seconds=5)     # Retry-After: 7
    assert "rate limited" in row.error
    assert ingest.tick() == 0           # not due yet
    row = _run(row)
    assert row.status == SlackChannelIngest.READY and row.messages == 6
    first_pages = [p for m, p in channel.reads if m == "conversations.history" and not p.get("cursor")]
    assert len(first_pages) == 1        # resumed at the cursor, not from the top


def test_only_the_requesting_agent_reads_the_transcript(pending, channel, owen, hal_login, alice, ws):
    _approve(pending)
    _run(pending)
    url = f"/api/slack-ingests/{pending.pk}/transcript"
    assert _link_client(hal_login).get(url).status_code == 200
    assert _link_client(owen).get(url).status_code == 403          # a workspace admin
    assert _link_client(_owner(ws)).get(url).status_code == 403    # a workspace owner
    assert _link_client(alice).get(url).status_code == 404         # a member: not even its status
    assert _link_client(owen).get(f"/api/slack-ingests/{pending.pk}").json()["status"] == "ready"


# ---- follow, and revoke --------------------------------------------------------

@pytest.fixture
def following(channel, enabled, hal_login, owen):
    resp = _request(_link_client(hal_login), follow=True)
    row = SlackChannelIngest.objects.get(pk=resp.json()["id"])
    _approve(row)
    row = _run(row)
    assert row.status == SlackChannelIngest.READY and row.next_attempt_at is not None
    return row


def test_follow_adds_new_messages_and_replies(following, channel, hal_login):
    channel.history.insert(0, {"ts": "1700000600.000000", "user": BOB, "text": "something new"})
    channel.history[2]["latest_reply"] = "1700000650.000000"
    channel.replies["1700000400.000000"].append(
        {"ts": "1700000650.000000", "user": BOB, "text": "late reply", "thread_ts": "1700000400.000000"})
    row = _run(following)
    row.refresh_from_db()
    assert row.messages == 8 and row.followed_at
    md = _link_client(hal_login).get(f"/api/slack-ingests/{row.pk}/transcript").json()["markdown"]
    assert "something new" in md and "late reply" in md
    assert md.count("first ever message") == 1


def test_a_followed_read_catches_up_from_the_read_mark(following, channel, hal_login, owen):
    c = _link_client(hal_login)
    base = f"/api/slack-ingests/{following.pk}"
    first = c.get(f"{base}/transcript").json()
    assert first["after"] == 0 and first["messages"] == first["total"] == 6 and first["mark"] > 0
    assert c.get(base).json()["read_mark"] == 0                      # reading alone moves nothing
    resp = c.post(f"{base}/read-mark", {"mark": first["mark"]}, content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["read_mark"] == first["mark"] and resp.json()["read_at"]

    # Nothing new yet: an empty catch-up, and the mark stays put.
    none = c.get(f"{base}/transcript", {"after": first["mark"]}).json()
    assert none["messages"] == 0 and none["mark"] == first["mark"] and none["total"] == 6
    assert "Nothing new since the last read" in none["markdown"] and "last word" not in none["markdown"]

    # A new channel message, and a late reply to a thread that was already read.
    channel.history.insert(0, {"ts": "1700000600.000000", "user": BOB, "text": "something new"})
    channel.history[2]["latest_reply"] = "1700000650.000000"
    channel.replies["1700000400.000000"].append(
        {"ts": "1700000650.000000", "user": BOB, "text": "late reply", "thread_ts": "1700000400.000000"})
    _run(following)
    new = c.get(f"{base}/transcript", {"after": first["mark"]}).json()
    md = new["markdown"]
    assert new["messages"] == 2 and new["total"] == 8 and new["mark"] > first["mark"]
    assert (new["first_ts"], new["last_ts"]) == ("1700000600.000000", "1700000650.000000")
    assert "something new" in md and "late reply" in md
    assert "first ever message" not in md and "last word" not in md and "reply one" not in md
    assert "earlier thread, already read" in md and "a thread about" in md     # the reply keeps its context
    assert md.index("a thread about") < md.index("late reply")
    assert "New since the last read: 2 message(s)" in md


def test_the_read_mark_never_goes_back_or_past_the_transcript(following, hal_login, owen):
    c = _link_client(hal_login)
    base = f"/api/slack-ingests/{following.pk}"
    top = c.get(f"{base}/transcript").json()["mark"]
    assert c.post(f"{base}/read-mark", {"mark": top + 1000},
                  content_type="application/json").json()["read_mark"] == top
    assert c.post(f"{base}/read-mark", {"mark": 1}, content_type="application/json").json()["read_mark"] == top
    # Only the agent's own login records how far it has read.
    assert _link_client(owen).post(f"{base}/read-mark", {"mark": 0},
                                   content_type="application/json").status_code == 403


def test_revoke_stops_follow_and_deletes_the_transcript(following, channel, owen, hal_login):
    resp = _link_client(owen).post(f"/api/slack-ingests/{following.pk}/revoke")
    assert resp.status_code == 200, resp.content
    assert resp.json()["status"] == "revoked"
    assert not SlackIngestMessage.objects.filter(ingest=following).exists()
    channel.reads.clear()
    channel.history.insert(0, {"ts": "1700000700.000000", "user": BOB, "text": "after revoke"})
    SlackChannelIngest.objects.filter(pk=following.pk).update(next_attempt_at=timezone.now())
    ingest.tick()
    ingest.advance(following)
    assert not channel.reads
    assert _link_client(hal_login).get(f"/api/slack-ingests/{following.pk}/transcript").status_code == 409
    assert Event.objects.filter(kind="slack.ingest_revoked").count() == 1


def test_the_approver_can_revoke_from_slack(following, channel, slack):
    click(ingest.REVOKE, str(following.pk), user=OWEN, message_ts=following.approval_ts)
    following.refresh_from_db()
    assert following.status == SlackChannelIngest.REVOKED and following.revoked_by_slack_id == OWEN


def test_a_bystander_cannot_revoke(following, channel, linked, alice):
    click(ingest.REVOKE, str(following.pk), user=ALICE, message_ts=following.approval_ts)
    following.refresh_from_db()
    assert following.status == SlackChannelIngest.READY
    assert _link_client(alice).post(f"/api/slack-ingests/{following.pk}/revoke").status_code == 404


def test_turning_the_switch_off_stops_reading(following, channel, ws):
    SlackWorkspaceLink.objects.filter(workspace=ws).update(channel_ingest_enabled=False)
    channel.reads.clear()
    SlackChannelIngest.objects.filter(pk=following.pk).update(next_attempt_at=timezone.now())
    ingest.tick()
    assert not channel.reads


def test_moving_the_agent_revokes_its_reads_and_rehomes_them(following, channel, ws, hal):
    """The approval was given by the source workspace's admins; it does not travel."""
    from apps.agents import move
    from apps.workspaces.testing import a_workspace

    owner = _owner(ws)
    dest = a_workspace("elsewhere")
    wsvc.ensure_member(dest, owner, WorkspaceMembership.OWNER)
    plan = move.plan(hal, dest, by=owner)
    assert any(b["kind"] == "slack_channel_ingest" for b in plan.stops_working)
    move.move(hal, dest, by=owner)
    following.refresh_from_db()
    assert following.status == SlackChannelIngest.REVOKED and following.workspace_id == "elsewhere"
    assert not SlackIngestMessage.objects.filter(ingest=following).exists()


def test_a_pending_request_can_be_withdrawn(pending, channel, hal_login, owen):
    resp = _link_client(hal_login).post(f"/api/slack-ingests/{pending.pk}/revoke")
    assert resp.json()["status"] == "revoked"
    _approve(pending)
    pending.refresh_from_db()
    assert pending.status == SlackChannelIngest.REVOKED


# ---- the allow-list: an owner's explicit, standing permission --------------------

def _allow(ws, *, follow=False, channel="#canopy-support", user=None):
    return _link_client(user or _owner(ws)).post(
        f"/api/slack-config/{ws.slug}/channel-ingest/allowed",
        {"agent": "hal", "channel": channel, "follow": follow, "note": "AI champions"},
        content_type="application/json")


@pytest.fixture
def allowed(channel, installation, ws, hal_login):
    resp = _allow(ws)
    assert resp.status_code == 200, resp.content
    (entry,) = resp.json()["channel_ingest"]["allowed"]
    return entry


def test_an_owner_allow_lists_a_channel_by_name(allowed, ws):
    assert allowed["channel"] == CHAN and allowed["channel_name"] == "canopy-support"
    assert allowed["agent"] == "hal" and allowed["follow"] is False and allowed["note"] == "AI champions"
    assert allowed["added_by_email"] == _owner(ws).email
    assert Event.objects.filter(kind="slack.ingest_allowed").count() == 1


def test_an_admin_or_owner_can_allow_list_but_not_a_member(channel, installation, ws, hal, hal_login, owen, alice):
    for user in (alice, hal_login):           # a member; the agent itself
        assert _allow(ws, user=user).status_code == 403
    resp = _allow(ws, user=owen)              # an admin
    assert resp.status_code == 200, resp.content
    (entry,) = resp.json()["channel_ingest"]["allowed"]
    assert entry["added_by_email"] == owen.email
    resp = _link_client(owen).delete(f"/api/slack-config/{ws.slug}/channel-ingest/allowed/{entry['id']}")
    assert resp.status_code == 200 and resp.json()["channel_ingest"]["allowed"] == []
    with pytest.raises(ingest.IngestError):   # and the agent can never grant itself, even if made owner
        ingest.allow(workspace=ws, agent=hal, channel=CHAN, follow=False, note="", by=hal_login)


def test_an_allow_listed_channel_is_never_read_from_the_api(allowed, channel, ws, hal_login, slack):
    """Owner decision, 2026-10-16: a read starts only from a person in Slack."""
    for ref in (CHAN, "#canopy-support"):
        channel.reads.clear()
        resp = _link_client(hal_login).post("/api/slack-ingests/", {"agent": "hal", "channel": ref, "purpose": "x"},
                                            content_type="application/json")
        assert resp.status_code == 409 and "/hal read-channel" in resp.json()["detail"]
        assert "conversations.list" not in _reads(channel)        # an allow-listed name resolves locally
    assert not slack.said("chat.postMessage") and not SlackChannelIngest.objects.exists()


def test_another_channel_still_needs_the_switch(allowed, channel, hal_login):
    resp = _link_client(hal_login).post("/api/slack-ingests/", {"agent": "hal", "channel": "C9", "purpose": "x"},
                                        content_type="application/json")
    assert resp.status_code == 409 and "allow-listed" in resp.json()["detail"]


# ---- `/hal read-channel <ask>`: the one way an allow-listed channel is read --------

def _read_channel(text="what's new?", user=ALICE):
    from tests.test_slack import command
    resp = command(f"hal read-channel {text}".strip(), user=user)
    assert resp.status_code == 200, resp.content
    return resp.json()["text"]


def _turns():
    from apps.harness.models import Turn
    return list(Turn.objects.order_by("created_at"))


def test_read_channel_reads_everything_then_only_what_is_new(allowed, channel, linked, hal, ws, slack):
    slack.ts_seq = ["1700000801.000100", "1700000802.000100"]   # each command's own anchor message
    note = _read_channel("what's new?")
    assert "is reading this channel (everything in it)" in note
    row = SlackChannelIngest.objects.get()
    assert row.status == SlackChannelIngest.READING and row.allowance_id and row.pending_ask["ask"] == "what's new?"
    assert not _turns()                                           # nothing sent until the read is done
    row = _run(row)
    assert row.status == SlackChannelIngest.READY and row.pending_ask == {}
    (turn,) = _turns()
    assert turn.chat_session.agent == hal
    assert "first ever message" in turn.prompt and "reply two" in turn.prompt
    assert turn.prompt.rstrip().endswith("what's new?")
    assert f"Approved by: {_owner(ws).email} (allow-listed)" in turn.prompt
    assert row.read_mark and Event.objects.filter(kind="slack.ingest_read_channel").count() == 1

    channel.history.insert(0, {"ts": "1700000600.000000", "user": BOB, "text": "brand new"})
    note = _read_channel("and now?")
    assert "since its last read here" in note
    _run(SlackChannelIngest.objects.get())
    second = _turns()[-1]
    assert second.pk != turn.pk
    assert "brand new" in second.prompt and "first ever message" not in second.prompt
    assert second.prompt.rstrip().endswith("and now?")


def test_read_channel_needs_the_channel_on_the_list(channel, linked, hal, installation):
    note = _read_channel()
    assert "isn't on `hal`'s list of channels it may read" in note and "/settings/slack" in note
    assert not SlackChannelIngest.objects.exists() and not _turns()
    assert "conversations.history" not in _reads(channel)


def test_read_channel_is_refused_while_an_earlier_one_is_reading(allowed, channel, linked, hal):
    _read_channel()
    assert "still reading this channel" in _read_channel("again")
    assert SlackChannelIngest.objects.count() == 1


def test_removing_an_entry_revokes_its_reads(allowed, channel, ws, linked, hal):
    _read_channel()
    row = _run(SlackChannelIngest.objects.get())
    resp = _link_client(_owner(ws)).delete(f"/api/slack-config/{ws.slug}/channel-ingest/allowed/{allowed['id']}")
    assert resp.status_code == 200, resp.content
    assert resp.json()["channel_ingest"]["allowed"] == []
    row.refresh_from_db()
    assert row.status == SlackChannelIngest.REVOKED
    assert not SlackIngestMessage.objects.filter(ingest=row).exists()
    assert "isn't on `hal`'s list" in _read_channel()              # and it can't be asked for again
    assert Event.objects.filter(kind="slack.ingest_disallowed").count() == 1


def test_moving_the_agent_drops_its_allow_list(allowed, channel, ws, hal):
    from apps.agents import move
    from apps.slack.models import SlackChannelIngestAllow
    from apps.workspaces.testing import a_workspace

    owner = _owner(ws)
    dest = a_workspace("elsewhere")
    wsvc.ensure_member(dest, owner, WorkspaceMembership.OWNER)
    assert any(b["kind"] == "slack_channel_allow" for b in move.plan(hal, dest, by=owner).stops_working)
    move.move(hal, dest, by=owner)
    assert not SlackChannelIngestAllow.objects.exists()
