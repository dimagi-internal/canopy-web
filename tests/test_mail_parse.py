from __future__ import annotations

from pathlib import Path

import pytest
from django.contrib.auth.models import User

from apps.agents.models import Agent
from apps.mail import parse, threads
from apps.mail.models import MailDomain, MailMessage, MailRoute, MailThread
from apps.workspaces.models import Workspace

FIX = Path(__file__).parent / "fixtures" / "mail"


def raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


def test_decodes_rfc2047_headers_and_latin1_body():
    p = parse.parse(raw("encoded_headers.eml"))
    assert p.from_name == "José Pérez" and p.from_addr == "jose@example.org"
    assert p.subject == "¡Hola!"
    assert p.text.strip() == "Mañana"


def test_missing_message_id_gets_a_stable_synthetic_one():
    a = parse.parse(raw("no_message_id.eml"))
    b = parse.parse(raw("no_message_id.eml"))
    assert a.message_id == b.message_id and a.message_id.endswith("@canopy>")


def test_html_only_mail_gets_a_text_rendering():
    p = parse.parse(raw("html_only.eml"))
    assert "Hello there" in p.text and "<b>" in p.html


def test_attachment_is_extracted():
    p = parse.parse(raw("with_attachment.eml"))
    assert [(a.filename, a.content_type) for a in p.attachments] == [("a.pdf", "application/pdf")]


def test_automated_markers():
    assert parse.parse(raw("auto.eml")).automated is True
    assert parse.parse(raw("plain.eml")).automated is False


@pytest.mark.django_db
def test_reply_joins_the_thread_it_references():
    u = User.objects.create_user("jj", "jj@x.com", "pw")
    ws = Workspace.objects.create(slug="m", display_name="M", created_by=u)
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    a = Agent.objects.create(slug="mada", name="Ada", workspace=ws)
    r = MailRoute.objects.create(domain=d, local_part="ada", kind="agent", agent=a)
    t = MailThread.objects.create(workspace=ws, route=r, subject="x")
    MailMessage.objects.create(thread=t, direction="out", message_id="<orig-1@marshellis.com>")
    assert threads.place(r, parse.parse(raw("reply.eml"))) == t
    assert threads.place(r, parse.parse(raw("plain.eml"))) != t


def test_bogus_charset_single_part_parses():
    p = parse.parse(raw("bogus_charset.eml"))
    assert "Plain body here." in p.text


def test_bogus_charset_multipart_parses():
    p = parse.parse(raw("bogus_charset_multipart.eml"))
    assert "Text part body." in p.text and "Html part body." in p.html


@pytest.mark.django_db
def test_multi_id_in_reply_to_and_case_insensitive_match():
    u = User.objects.create_user("jj2", "jj2@x.com", "pw")
    ws = Workspace.objects.create(slug="m2", display_name="M", created_by=u)
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    a = Agent.objects.create(slug="mada2", name="Ada", workspace=ws)
    r = MailRoute.objects.create(domain=d, local_part="ada", kind="agent", agent=a)
    t = MailThread.objects.create(workspace=ws, route=r, subject="x")
    MailMessage.objects.create(thread=t, direction="out", message_id="<Orig-1@Marshellis.com>")
    p = parse.parse(raw("reply.eml"))
    p.references = []
    p.in_reply_to = "<x@y> <orig-1@marshellis.com>"
    assert threads.place(r, p) == t
