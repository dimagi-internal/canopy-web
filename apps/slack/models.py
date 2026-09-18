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
