"""Backfill: the members on each email-born session's thread become its participants.

The live rule (`apps/harness/channels/email.py::admit_thread_people`) admits, as
each message arrives, the members on the thread: From, To and Cc of an inbound
message whose From is aligned, and the To and Cc of the agent's own replies. This
applies the same rule to the sessions that already exist, from what canopy kept:

* a thread on a workspace mail domain (`apps/mail`, SES): every stored message —
  inbound From/To/Cc graded from its stored Authentication-Results, outbound To/Cc;
* a Gmail thread (the runner's poller): canopy never stored the messages, only each
  email turn's `origin_ref` — its From and that message's Authentication-Results.
  So the senders are reconstructed and the To/Cc of past Gmail mail are not.

Same bar as live: one verified holder of the address, a member of the session's
workspace, a person (not an agent login, not a system account); never a sensitive
session. Idempotent (get_or_create); the reverse removes nothing (a participant
row may since have been confirmed or re-made by hand).
"""
from __future__ import annotations

from email.utils import getaddresses

from django.conf import settings
from django.db import migrations

EMAIL_THREAD_KEY = "email_thread_key"     # apps.harness.session_sync
EMAIL_THREAD_PREFIX = "email:"
SENSITIVE_KEY = "runner_requirements"     # apps.harness.runner_requirements.METADATA_KEY
ALIGNED = {"dmarc", "dkim_aligned"}       # apps.contacts.models.Contact.EMAIL_ALIGNED


def _aligned(headers, authserv_id: str, from_addr: str) -> bool:
    from apps.contacts.email_auth import grade_from_headers  # pure: no models

    grade, _ = grade_from_headers(headers, authserv_id=authserv_id, from_address=from_addr)
    return grade in ALIGNED


def _addrs(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [a.strip().lower() for _, a in getaddresses([str(v) for v in value if v]) if a][:50]


def thread_addresses(apps, session) -> set[str]:
    """Every address the live rule would have admitted on this session's thread."""
    MailThread = apps.get_model("mail", "MailThread")
    Turn = apps.get_model("harness", "Turn")
    thread_id = session.metadata[EMAIL_THREAD_KEY][len(EMAIL_THREAD_PREFIX):]
    out: set[str] = set()
    thread = None
    try:
        thread = MailThread.objects.filter(uuid=thread_id, workspace_id=session.workspace_id).first()
    except Exception:  # noqa: BLE001 — a Gmail thread id is not a UUID
        thread = None
    if thread is not None:
        for m in thread.messages.all():
            if m.direction == "out":
                out.update(_addrs(m.to) + _addrs(m.cc))
                continue
            headers = [("Authentication-Results", v) for v in (m.auth_results or "").split("\n") if v]
            if _aligned(headers, settings.MAIL_SES_AUTHSERV_ID, m.from_addr):
                out.update(_addrs(m.from_addr) + _addrs(m.to) + _addrs(m.cc))
    for ref in Turn.objects.filter(chat_session=session, origin="email").values_list("origin_ref", flat=True):
        ref = ref if isinstance(ref, dict) else {}
        frm = _addrs(ref.get("from"))
        headers = ref.get("headers") if isinstance(ref.get("headers"), list) else []
        authserv = (settings.MAIL_SES_AUTHSERV_ID if ref.get("backend") == "ses"
                    else settings.INBOUND_EMAIL_AUTHSERV_ID)
        if frm and _aligned(headers, authserv, frm[0]):
            out.update(frm + _addrs(ref.get("to")) + _addrs(ref.get("cc")))
    return out


def backfill(apps, schema_editor=None) -> int:
    Session = apps.get_model("canopy_sessions", "Session")
    SessionParticipant = apps.get_model("canopy_sessions", "SessionParticipant")
    SessionKeyWrap = apps.get_model("canopy_sessions", "SessionKeyWrap")
    EmailAddress = apps.get_model("account", "EmailAddress")
    Membership = apps.get_model("workspaces", "WorkspaceMembership")
    Agent = apps.get_model("agents", "Agent")
    SystemAccount = apps.get_model("workspaces", "SystemAccount")
    not_people = (set(Agent.objects.filter(user__isnull=False).values_list("user_id", flat=True))
                  | set(SystemAccount.objects.values_list("user_id", flat=True)))
    added = 0
    for session in Session.objects.filter(metadata__has_key=EMAIL_THREAD_KEY).iterator():
        key = (session.metadata or {}).get(EMAIL_THREAD_KEY)
        if not isinstance(key, str) or not key.startswith(EMAIL_THREAD_PREFIX):
            continue
        if SENSITIVE_KEY in (session.metadata or {}) or SessionKeyWrap.objects.filter(session=session).exists():
            continue
        for addr in sorted(thread_addresses(apps, session)):
            holders = list(EmailAddress.objects.filter(email__iexact=addr, verified=True)
                           .values_list("user_id", flat=True).distinct()[:2])
            if len(holders) != 1 or holders[0] in not_people:
                continue
            uid = holders[0]
            if not Membership.objects.filter(workspace_id=session.workspace_id, user_id=uid,
                                             user__is_active=True).exists():
                continue
            _, created = SessionParticipant.objects.get_or_create(
                session=session, user_id=uid, defaults={"role": "editor"})
            added += int(created)
    return added


class Migration(migrations.Migration):

    dependencies = [
        ("mail", "0007_message_size"),
        ("canopy_sessions", "0057_session_review_grant"),
        ("harness", "0117_retire_missing_sessions"),
        ("workspaces", "0013_system_accounts"),
        ("agents", "0093_retire_runtime_yaml_help_text"),
        ("account", "0009_emailaddress_unique_primary_email"),
    ]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
