"""Which conversation a message belongs to: RFC 5322 reply headers, scoped to one route."""
from __future__ import annotations

from .models import MailMessage, MailRoute, MailThread


def place(route: MailRoute, parsed) -> MailThread:
    ids = set(parsed.references) | ({parsed.in_reply_to} if parsed.in_reply_to else set())
    if ids:
        hit = (MailMessage.objects.filter(thread__route=route, message_id__in=ids)
               .select_related("thread").order_by("-created_at").first())
        if hit is not None:
            return hit.thread
    return MailThread.objects.create(workspace=route.domain.workspace, route=route,
                                     subject=parsed.subject[:998])
