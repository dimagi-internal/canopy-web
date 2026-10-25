"""More ways to reach a person (`PersonAddress`): a workspace holds a member's
work email beside the address that identifies them, and its agents can read it.

The rules these pin:

* only the person themself or an admin of the workspace writes one — never an
  agent's login, never a plain member;
* every member of that workspace reads them, an agent's login included, and
  each read is logged where the person sees it;
* a workspace never serves another workspace's rows, and a person it does not
  deal with is "not found";
* the person sees every row in their own `GET /api/people/me/`;
* a row never becomes an identity: it joins no one.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.contacts import people
from apps.contacts import services as contacts
from apps.contacts.models import PersonAccess, PersonAddress
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.people_brain import _agent, _client, _member

pytestmark = pytest.mark.django_db


@pytest.fixture()
def home():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="family", display_name="Family", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    other = Workspace.objects.create(slug="work", display_name="Work", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=other, role=WorkspaceMembership.OWNER)
    gina = _member(ws, "gina")
    kid = _member(ws, "kid")
    bailey = _agent(ws, "bailey", owner)
    return {"owner": owner, "ws": ws, "other": other, "gina": gina, "kid": kid,
            "bailey": bailey, "person": contacts.person_for(user=gina)}


def _add(client, person, **body):
    body = {"workspace": "family", "kind": "email", "label": "work",
            "value": "gina@employer.org", **body}
    return client.post(f"/api/people/{person.pk}/addresses/", body, content_type="application/json")


def test_an_admin_adds_a_members_work_email_and_the_agent_reads_it(home):
    r = _add(_client(home["owner"]), home["person"], value="Gina@Employer.org", label=" Work ")
    assert r.status_code == 201, r.content
    assert r.json()["value"] == "gina@employer.org" and r.json()["label"] == "work"
    assert r.json()["added_by"] == "jj@dimagi.com"

    r = _client(home["bailey"].user).get(
        f"/api/people/{home['person'].pk}/addresses/?workspace=family")
    assert r.status_code == 200, r.content
    assert [(a["kind"], a["label"], a["value"]) for a in r.json()["addresses"]] == [
        ("email", "work", "gina@employer.org")]
    read = PersonAccess.objects.filter(person=home["person"]).latest("created_at")
    assert read.reader_agent == home["bailey"] and read.workspace_id == "family"


def test_the_person_adds_their_own_and_the_same_value_again_relabels(home):
    c = _client(home["gina"])
    assert _add(c, home["person"], label="work").status_code == 201
    assert _add(c, home["person"], label="office").status_code == 201
    assert list(PersonAddress.objects.values_list("label", flat=True)) == ["office"]
    assert _add(c, home["person"], kind="phone", label="mobile", value="+1 (617) 555-0100").status_code == 201
    assert PersonAddress.objects.count() == 2


@pytest.mark.parametrize("who", ["kid", "agent"])
def test_a_plain_member_or_an_agent_cannot_write_one(home, who):
    user = home["kid"] if who == "kid" else home["bailey"].user
    assert _add(_client(user), home["person"]).status_code == 403
    assert not PersonAddress.objects.exists()


@pytest.mark.parametrize("body", [{"kind": "fax"}, {"label": ""}, {"label": "x" * 41},
                                  {"value": "not-an-email"},
                                  {"kind": "phone", "value": "call me"}])
def test_a_bad_address_is_a_400(home, body):
    assert _add(_client(home["owner"]), home["person"], **body).status_code == 400


def test_one_workspace_never_serves_anothers_rows(home):
    _add(_client(home["owner"]), home["person"])
    # The owner is in both; Gina is known only in `family`.
    r = _client(home["owner"]).get(f"/api/people/{home['person'].pk}/addresses/?workspace=work")
    assert r.status_code == 404
    stranger = User.objects.create_user("x", "x@else.org", "pw")
    r = _client(stranger).get(f"/api/people/{home['person'].pk}/addresses/?workspace=family")
    assert r.status_code == 404


def test_delete_is_the_person_or_an_admin(home):
    row_id = _add(_client(home["owner"]), home["person"]).json()["id"]
    url = f"/api/people/{home['person'].pk}/addresses/{row_id}/"
    assert _client(home["kid"]).delete(url).status_code == 404
    assert _client(home["bailey"].user).delete(url).status_code == 404
    assert _client(home["gina"]).delete(url).status_code == 204
    assert not PersonAddress.objects.exists()


def test_the_person_sees_every_row_in_their_own_read(home):
    _add(_client(home["owner"]), home["person"])
    r = _client(home["gina"]).get("/api/people/me/")
    assert r.status_code == 200, r.content
    assert [(a["workspace"], a["value"]) for a in r.json()["addresses"]] == [
        ("family", "gina@employer.org")]


def test_an_address_is_never_an_identity(home):
    _add(_client(home["owner"]), home["person"])
    assert people.by_email("gina@employer.org") is None
