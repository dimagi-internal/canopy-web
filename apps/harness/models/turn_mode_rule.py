"""`TurnModeRule`: whether an agent's work from one channel, one person, or both runs
`manual` or `auto`.

Separate from routing on purpose. A routing rule (`RunnerAssignment`) decides
WHICH BOX runs a piece of work, and writing one needs rights over the box. A
turn-mode rule decides whether the agent may act on that work WITHOUT ASKING
FIRST, and writing one needs rights over the agent (its admins, for anything
that can raise autonomy). Neither names the other: a mode rule names no runner,
and a routing rule carries no mode.

How the rules are read at claim (the ladder, the verified-sender check on a
person's `auto`) is `apps/harness/turn_mode.py`; who may write them is
`apps/agents/api_turn_mode.py`.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class TurnModeRule(models.Model):
    MANUAL, AUTO = "manual", "auto"
    MODE_CHOICES = [(MANUAL, "Manual: outbound actions wait for approval"),
                    (AUTO, "Auto: the agent acts and reports")]

    agent = models.ForeignKey(
        "agents.Agent", on_delete=models.CASCADE, related_name="turn_mode_rules",
    )
    #: A `Turn.origin` (`RoutableSource`), or "" for every channel.
    source = models.CharField(max_length=32, blank=True, default="")
    #: A normalized bare email address (`actors.normalize_actor`), matched against
    #: `actors.actor_of(turn)`, or "" for anyone. A rule must name a source, a
    #: person, or both: one naming neither is the agent's own switch
    #: (`Agent.turn_mode`), which already exists.
    actor = models.CharField(max_length=254, blank=True, default="")
    turn_mode = models.CharField(max_length=8, choices=MODE_CHOICES)
    set_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                               null=True, blank=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["agent_id", "actor", "source"]
        constraints = [
            models.UniqueConstraint(fields=["agent", "source", "actor"],
                                    name="one_turn_mode_rule_per_agent_source_actor"),
            models.CheckConstraint(condition=~models.Q(source="", actor=""),
                                   name="turn_mode_rule_names_a_source_or_a_person"),
            models.CheckConstraint(condition=models.Q(turn_mode__in=["manual", "auto"]),
                                   name="turn_mode_rule_mode_is_manual_or_auto"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.agent_id}:{self.source or 'any channel'}/{self.actor or 'anyone'}={self.turn_mode}"
