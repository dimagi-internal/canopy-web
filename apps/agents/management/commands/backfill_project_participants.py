"""Link people to the projects they take part in (fleet brain v1.1, canopy#804).

    manage.py backfill_project_participants

Every fact filed against a project makes its subject a participant (source
fact), and every task in a project raised by a human's turn makes that human
one (source turn). Idempotent. Migration `agents/0041` did the fact half on
deploy; new facts and tasks link themselves (`apps/agents/participants.py`).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Backfill AgentProject participants from facts and task raisers (idempotent)."

    def handle(self, *args, **opts):
        from apps.agents import participants

        r = participants.backfill()
        self.stdout.write(f"facts seen: {r['facts_seen']}, tasks seen: {r['tasks_seen']}, "
                          f"participants added: {r['participants_added']}")
