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


def test_crlf_in_encoded_display_name_does_not_raise():
    p = parse.parse(raw("crlf_display_name.eml"))
    assert p.from_addr == "a@b.org"
    assert "\r" not in p.from_name and "\n" not in p.from_name


def test_crlf_injection_in_subject_is_neutralised():
    p = parse.parse(raw("crlf_display_name.eml"))
    assert "\r" not in p.subject and "\n" not in p.subject
    assert "Line1" in p.subject and "Line2" in p.subject


def _mail(headers: bytes) -> bytes:
    return headers + b"To: ada@marshellis.com\r\nMessage-ID: <h@x>\r\n\r\nbody\r\n"


@pytest.mark.parametrize("n", [5, 0])
def test_literal_parking_token_in_name_is_preserved(n):
    p = parse.parse(_mail(
        f"From: ENCWORD{n}X <a@b.org>, =?utf-8?q?Hi?= <c@d.org>\r\n".encode()))
    assert p.from_addr == "a@b.org"
    assert p.from_name == f"ENCWORD{n}X"


def test_8bit_raw_headers_are_encodable():
    p = parse.parse(_mail(b"From: Jos\xe9\xff <a@b.org>\r\nSubject: caf\xe9 \xff\r\n"))
    assert p.from_addr == "a@b.org"
    p.subject.encode("utf-8")
    p.from_name.encode("utf-8")


def test_malformed_encoded_word_does_not_raise():
    p = parse.parse(_mail(b"From: =?utf-8?b?!!!?= <a@b.org>\r\nSubject: =?utf-8?b?!!!?=\r\n"))
    assert p.from_addr == "a@b.org"


def test_unicode_line_separators_scrubbed():
    p = parse.parse(_mail(b"Subject: =?utf-8?q?a=C2=85b=E2=80=A8c?=\r\n"))
    assert p.subject == "a b c"


def test_headers_and_attachment_filename_are_json_and_utf8_safe():
    import json

    raw_mail = (
        b"From: a@b.org\r\nAuthentication-Results: mx; dkim=pass d=caf\xe9\xff\r\n"
        b"Message-ID: <h@x>\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nhi\r\n"
        b"--B\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="caf\xe9\xff.pdf"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERg==\r\n--B--\r\n"
    )
    p = parse.parse(raw_mail)
    json.dumps(p.headers, ensure_ascii=False).encode("utf-8")
    assert p.attachments
    p.attachments[0].filename.encode("utf-8")
