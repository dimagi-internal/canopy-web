"""Mail models: the invariants the database itself must hold."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction

from apps.agents.models import Agent
from apps.mail.models import MailDomain, MailMessage, MailRoute, MailThread
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture()
def ws():
    u = User.objects.create_user("jj", "jjackson@dimagi.com", "pw")
    return Workspace.objects.create(slug="marshellis", display_name="M", created_by=u)


def test_domain_name_is_globally_unique(ws):
    other = Workspace.objects.create(slug="other", display_name="O", created_by=ws.created_by)
    MailDomain.objects.create(workspace=ws, name="marshellis.com")
    with pytest.raises(IntegrityError), transaction.atomic():
        MailDomain.objects.create(workspace=other, name="marshellis.com")


def test_one_route_per_local_part(ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com")
    MailRoute.objects.create(domain=d, local_part="*", kind=MailRoute.DROP)
    with pytest.raises(IntegrityError), transaction.atomic():
        MailRoute.objects.create(domain=d, local_part="*", kind=MailRoute.DROP)


def test_message_id_unique_per_mailbox(ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com")
    a = Agent.objects.create(slug="mada", name="Ada", workspace=ws)
    r = MailRoute.objects.create(domain=d, local_part="ada", kind=MailRoute.AGENT, agent=a)
    t = MailThread.objects.create(workspace=ws, route=r, subject="hi")
    MailMessage.objects.create(thread=t, direction=MailMessage.IN, message_id="<x@y>")
    with pytest.raises(IntegrityError), transaction.atomic():
        MailMessage.objects.create(thread=t, direction=MailMessage.IN, message_id="<x@y>")
    b = Agent.objects.create(slug="mbob", name="Bob", workspace=ws)
    rb = MailRoute.objects.create(domain=d, local_part="bob", kind=MailRoute.AGENT, agent=b)
    tb = MailThread.objects.create(workspace=ws, route=rb, subject="hi")
    MailMessage.objects.create(thread=tb, direction=MailMessage.IN, message_id="<x@y>")  # same mail, other agent: fine


def test_mail_is_off_by_default(settings):
    assert settings.MAIL_ENABLED is False
