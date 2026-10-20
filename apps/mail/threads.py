"""Which conversation a message belongs to: RFC 5322 reply headers, scoped to one route."""
from __future__ import annotations

import re

from django.db.models import Q

from . import ses
from .models import MailMessage, MailRoute, MailThread

_IDS = re.compile(r"<[^<>\s]+>")


def place(route: MailRoute, parsed) -> MailThread:
    ids = set(parsed.references) | set(_IDS.findall(parsed.in_reply_to or ""))
    if ids:
        match = Q()
        for i in ids:
            match |= Q(message_id__iexact=i)
            # SES replaces the Message-ID canopy wrote on an outbound message, so a reply
            # to one cites SES's id: match it to the message's recorded `ses_message_id`.
            if sid := ses.ses_id_of(i):
                match |= Q(ses_message_id=sid)
        hit = (MailMessage.objects.filter(match, thread__route=route)
               .select_related("thread").order_by("-created_at").first())
        if hit is not None:
            return hit.thread
    return MailThread.objects.create(workspace=route.workspace, route=route,
                                     subject=parsed.subject[:998])
