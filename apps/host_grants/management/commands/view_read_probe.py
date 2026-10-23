"""`manage.py view_read_probe --session <id> --email <person>`: prove, against the real
host, that a delegated read of a kept result is admitted only on canopy's `Canopy-View`.

For the first View-bearing tool call in the conversation that left a resource link (a
`labs-result://…`), it reads that link through the gateway twice, as the person's own
grant: once with no statement at all — which the host must REFUSE — and once as their
View, with `Canopy-View` — which must read. It prints outcomes and a row count, never a
row. Run it in the live container (ECS Exec) after a deploy of either side; the live half
of `scripts/e2e_sensitive_redteam.py`, which cannot hold canopy's DPoP key.
"""
from __future__ import annotations

import dataclasses
import json

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.canopy_sessions.models import Message, Session
from apps.harness import sensitivity
from apps.host_grants import host_gateway
from apps.mcp_apps_host import mcp_apps_views as views


def _rows(contents) -> int | None:
    try:
        body = json.loads(contents[0]["text"])
    except Exception:  # noqa: BLE001 - not a table: no count
        return None
    return len(body.get("rows") or []) if isinstance(body, dict) else None


class Command(BaseCommand):
    help = "A View's read of a kept result, with and without Canopy-View, against the live host."

    def add_arguments(self, parser):
        parser.add_argument("--session", required=True)
        parser.add_argument("--email", required=True, help="a person in the conversation, with a live grant")

    def handle(self, *args, session, email, **opts):
        convo = Session.objects.filter(pk=session).first()
        user = get_user_model().objects.filter(email__iexact=email, is_active=True).first()
        if convo is None or user is None:
            raise CommandError("no such conversation or person")
        target = None
        for content in (Message.objects.filter(session=convo, role=Message.TOOL_RESULT)
                        .order_by("-turn_index").values_list("content", flat=True)):
            links = (content or {}).get(sensitivity.RESOURCE_LINKS) or []
            if links and content.get("tool_use_id"):
                target = (content["tool_use_id"], links[0]["uri"])
                break
        if target is None:
            raise CommandError("the conversation holds no kept result link")
        tool_call_id, uri = target
        row = views.find(convo, tool_call_id)
        ctx = views.resolve_for_viewer(convo, views.Viewer(user=user, person=True, chat_surface=True), row)
        if ctx.view is None:
            raise CommandError(f"{email} is not a person in this conversation: no view statement")
        self.stdout.write(f"reading {uri} at {ctx.site} as {email}")
        for label, probe in (("no statement", dataclasses.replace(ctx, view=None)), ("Canopy-View", ctx)):
            try:
                contents = async_to_sync(host_gateway.view_read)(probe, uri)
                self.stdout.write(f"  {label}: READ ({_rows(contents)} rows on the page)")
            except host_gateway.GatewayRefusal as exc:
                self.stdout.write(f"  {label}: REFUSED ({exc.code}: {exc.message[:160]})")
