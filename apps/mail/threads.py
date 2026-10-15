"""Which conversation a message belongs to: RFC 5322 reply headers, scoped to one route."""
from __future__ import annotations

import re

from django.db.models import Q

from .models import MailMessage, MailRoute, MailThread

_IDS = re.compile(r"<[^<>\s]+>")


def place(route: MailRoute, parsed) -> MailThread:
    ids = set(parsed.references) | set(_IDS.findall(parsed.in_reply_to or ""))
    if ids:
        match = Q()
        for i in ids:
            match |= Q(message_id__iexact=i)
        hit = (MailMessage.objects.filter(match, thread__route=route)
               .select_related("thread").order_by("-created_at").first())
        if hit is not None:
            return hit.thread
    return MailThread.objects.create(workspace=route.workspace, route=route,
                                     subject=parsed.subject[:998])
