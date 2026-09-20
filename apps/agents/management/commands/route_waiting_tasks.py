"""Point each task's `assigned` text at the PERSON it means, where that is certain.

The fleet's boards say who a card waits on in prose: "Jonathan", "Jonathan
Jackson", "jjackson@dimagi.com", "Beth + Neal", "operator (restart, then the
build runs)". canopy cannot notify a string, so those waits reach nobody — the
whole reason `waiting_on_user` exists.

This routes only what is UNAMBIGUOUS, and prints everything it declines:

  * an exact email of a member of that agent's workspace, or
  * an exact full name / username of exactly one such member (with `--names`).

Anything else — two people, a sentence, a name matching nobody or several — is
left alone and reported. Guessing would put a wait in somebody's inbox that is
not theirs, which is worse than the silence it replaces.

Dry run by default:

    uv run python manage.py route_waiting_tasks              # report only
    uv run python manage.py route_waiting_tasks --apply      # emails only
    uv run python manage.py route_waiting_tasks --apply --names
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from apps.agents.models import AgentTask
from apps.agents.services import LIVE_STATUSES
from apps.workspaces.models import WorkspaceMembership


class Command(BaseCommand):
    help = "Resolve tasks' free-text `assigned` to a real person where it is unambiguous."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write the matches (default: report only).")
        parser.add_argument("--names", action="store_true",
                            help="Also match a full name or username, when exactly one member fits.")
        parser.add_argument("--agent", default="", help="Limit to one agent slug.")

    def handle(self, *args, **opts):
        User = get_user_model()
        tasks = (
            AgentTask.objects.filter(status__in=LIVE_STATUSES, waiting_on_user__isnull=True)
            .exclude(assigned="")
            .select_related("agent")
        )
        if opts["agent"]:
            tasks = tasks.filter(agent__slug=opts["agent"])

        members: dict[str, list] = {}
        matched = skipped = 0
        for task in tasks:
            ws = task.agent.workspace_id
            if ws not in members:
                members[ws] = list(
                    User.objects.filter(
                        pk__in=WorkspaceMembership.objects.filter(workspace_id=ws)
                        .values_list("user_id", flat=True)
                    )
                )
            who = (task.assigned or "").strip()
            user, why = self._resolve(who, members[ws], names=opts["names"])
            if user is None:
                skipped += 1
                self.stdout.write(f"  skip  {task.agent.slug} {task.ext_id}: {who!r} — {why}")
                continue
            matched += 1
            self.stdout.write(f"  route {task.agent.slug} {task.ext_id}: {who!r} -> {user.email}")
            if opts["apply"]:
                task.waiting_on_user = user
                task.save(update_fields=["waiting_on_user", "updated_at"])

        verb = "routed" if opts["apply"] else "would route"
        self.stdout.write(self.style.SUCCESS(f"{verb} {matched}; left alone {skipped}"))
        if not opts["apply"] and matched:
            self.stdout.write("re-run with --apply to write them")

    @staticmethod
    def _resolve(who: str, members: list, *, names: bool):
        """(user, why-not). An agent's own name is not a person waiting."""
        lowered = who.lower()
        # A card parked on the AGENT is not waiting on a human at all.
        if not who:
            return None, "empty"
        for user in members:
            if user.email and user.email.lower() == lowered:
                return user, ""
        if "@" in who:
            return None, "an address canopy does not know, or not a member here"
        if not names:
            return None, "not an email (re-run with --names to match names)"
        # Anything that names more than one person, or explains itself, is not a
        # routing target: "Beth + Neal", "operator (restart, then the build runs)".
        if any(ch in who for ch in "+,()/&") or len(who.split()) > 3:
            return None, "names more than one person, or carries an explanation"
        hits = [
            u for u in members
            if lowered in {(u.get_full_name() or "").lower(), u.get_username().lower(),
                           (u.first_name or "").lower()}
            and lowered
        ]
        if len(hits) == 1:
            return hits[0], ""
        return None, ("matches nobody in this workspace" if not hits
                      else f"matches {len(hits)} people")
