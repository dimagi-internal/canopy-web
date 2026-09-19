"""Slack as a front door to canopy agents.

Framework tier: a transport, agent-agnostic, like ``inbound``. A Slack thread
becomes a ``canopy_sessions.Session`` exactly as an email thread does; see
``docs/superpowers/specs/2026-09-18-slack-front-door-design.md``.

Two rows, and deliberately nothing that grants anything:

* ``SlackInstallation`` — one Slack team, bound to one canopy Workspace. The bot
  token lives here, encrypted. The agent never sees it.
* ``SlackUserLink`` — "this Slack user is this canopy user", made by that person
  signing in, and only when the two email addresses agree (see ``views_auth``).
  Being in the Slack workspace is not being in the canopy workspace: a link
  without a membership is refused at send time.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.common.encryption import decrypt_secret, encrypt_secret


class SlackInstallation(models.Model):
    team_id = models.CharField(max_length=32, unique=True)
    team_name = models.CharField(max_length=255, blank=True, default="")
    bot_user_id = models.CharField(max_length=32)
    bot_token_enc = models.TextField()
    # NOT NULL, like every other tenant FK here: a nullable one is how six
    # predicates grew a "no tenant => allow" leg (see agents/0013).
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="slack_installations",
    )
    installed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    installed_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.team_name or self.team_id} -> {self.workspace_id}"

    @property
    def bot_token(self) -> str:
        return decrypt_secret(self.bot_token_enc)

    @bot_token.setter
    def bot_token(self, plaintext: str) -> None:
        self.bot_token_enc = encrypt_secret(plaintext)


class SlackUserLink(models.Model):
    installation = models.ForeignKey(
        SlackInstallation, on_delete=models.CASCADE, related_name="user_links",
    )
    slack_user_id = models.CharField(max_length=32)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="slack_links",
    )
    linked_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["installation", "slack_user_id"], name="slack_user_link_unique",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.slack_user_id} -> {self.user_id}"


class SlackRelayPost(models.Model):
    """One agent reply (a ledger row) that was posted into a Slack thread.

    Exists to make relaying exactly-once: the row is INSERTED before the post,
    under a unique (turn, seq), so a re-delivered signal or two processes
    handling the same append cannot both post. A relay that failed keeps its
    row with the error, and is not retried — a reply that arrives twice in a
    thread is worse than one that arrives never plus a logged reason.
    """

    turn = models.ForeignKey("harness.Turn", on_delete=models.CASCADE, related_name="slack_posts")
    seq = models.PositiveIntegerField()
    channel_id = models.CharField(max_length=32)
    slack_ts = models.CharField(max_length=32, blank=True, default="")
    error = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["turn", "seq"], name="slack_relay_once_per_row"),
        ]


class SlackTurnPost(models.Model):
    """The one status line a turn gets in its Slack thread, edited as it moves.

    Posted the moment a Slack message is enqueued — "picked up on X", or
    "queued: X is offline" with a way out — so the sender learns at once whether
    anything is happening, instead of inferring it from silence. Edited in place
    on every status change after that, so a thread carries one line per ask
    rather than a line per transition. A turn that started somewhere else
    (canopy-web, the phone) on a Slack-born session gets one too, which is what
    tells the thread that the conversation moved on without it.

    Inserted before posting under a unique turn, like `SlackRelayPost`, so a
    re-delivered signal cannot post a second line.
    """

    turn = models.OneToOneField("harness.Turn", on_delete=models.CASCADE, related_name="slack_status")
    channel_id = models.CharField(max_length=32)
    slack_ts = models.CharField(max_length=32, blank=True, default="")
    #: The text last rendered, so an event that changes nothing is not an edit.
    rendered = models.TextField(blank=True, default="")
    #: The thread reply saying this turn's runner went offline mid-turn. An edit
    #: to the line notifies nobody, and "your runner died" is the one change
    #: somebody must hear about. Cleared when the episode ends, so a second
    #: outage pings again. Claimed with a conditional UPDATE ("pending") so two
    #: sweeps noticing the same dead runner post one reply between them.
    offline_notice_ts = models.CharField(max_length=32, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)


class SlackMenuPost(models.Model):
    """A blocked agent's question that was posted into its Slack thread.

    Keyed on the question's CONTENT, not the report: the runner re-reports an
    open dialog every ~10s, and some producers re-stamp `observed_at` on every
    sighting, so "the menu changed" fires far more often than a new question
    exists. Rows are deleted when the dialog clears, so the same question asked
    again later is posted again.
    """

    session = models.ForeignKey(
        "canopy_sessions.Session", on_delete=models.CASCADE, related_name="slack_menu_posts",
    )
    key = models.CharField(max_length=64)
    slack_ts = models.CharField(max_length=32, blank=True, default="")
    channel_id = models.CharField(max_length=32, blank=True, default="")
    #: The question as asked, so the post can be rewritten without its buttons
    #: once the dialog is over.
    question = models.CharField(max_length=300, blank=True, default="")
    #: Set when a click answered it, so the "answered elsewhere" rewrite on the
    #: dialog clearing does not overwrite "Answered by @x: …".
    resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["session", "key"], name="slack_menu_once_per_question"),
        ]
