"""More ways to reach a person (`PersonAddress`): a member keeps their work email
beside the login that identifies them, and agents they work with can read it.

The rules these pin:

* the details are the ACCOUNT's own — one set per person, not per workspace —
  and only the person changes them: not a workspace owner, never an agent's login;
* anyone who shares a workspace with the person reads them, an agent's login
  included, and each read is logged where the person sees it;
* someone who shares no workspace with them learns nothing;
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
    gina = _member(ws, "gina")
    kid = _member(ws, "kid", role=WorkspaceMembership.VIEWER)
    bailey = _agent(ws, "bailey", owner)
    return {"owner": owner, "ws": ws, "gina": gina, "kid": kid,
            "bailey": bailey, "person": contacts.person_for(user=gina)}


def _add(user, **body):
    body = {"kind": "email", "label": "work", "value": "gina@employer.org", **body}
    return _client(user).post("/api/people/me/addresses/", body, content_type="application/json")


def test_a_person_adds_their_own_work_email_and_the_agent_reads_it(home):
    r = _add(home["gina"], value="Gina@Employer.org", label=" Work ")
    assert r.status_code == 201, r.content
    assert r.json()["value"] == "gina@employer.org" and r.json()["label"] == "work"

    r = _client(home["bailey"].user).get(
        f"/api/people/{home['person'].pk}/addresses/?workspace=family")
    assert r.status_code == 200, r.content
    assert [(a["kind"], a["label"], a["value"]) for a in r.json()["addresses"]] == [
        ("email", "work", "gina@employer.org")]
    read = PersonAccess.objects.filter(person=home["person"]).latest("created_at")
    assert read.reader_agent == home["bailey"] and read.workspace_id == "family"


def test_any_member_even_a_viewer_edits_their_own(home):
    assert _add(home["kid"], value="kid@school.org", label="school").status_code == 201
    kid = contacts.person_for(user=home["kid"])
    assert list(PersonAddress.objects.filter(person=kid).values_list("value", flat=True)) == ["kid@school.org"]


def test_the_details_belong_to_the_account_not_a_workspace(home):
    _add(home["gina"])
    work = Workspace.objects.create(slug="employer", display_name="Employer", created_by=home["owner"])
    colleague = _member(work, "colleague")
    WorkspaceMembership.objects.create(user=home["gina"], workspace=work, role=WorkspaceMembership.EDITOR)
    r = _client(colleague).get(f"/api/people/{home['person'].pk}/addresses/?workspace=employer")
    assert [a["value"] for a in r.json()["addresses"]] == ["gina@employer.org"]


def test_the_same_value_again_relabels_and_a_phone_is_its_own_row(home):
    _add(home["gina"], label="work")
    _add(home["gina"], label="office")
    assert list(PersonAddress.objects.values_list("label", flat=True)) == ["office"]
    assert _add(home["gina"], kind="phone", label="mobile", value="+1 (617) 555-0100").status_code == 201
    assert PersonAddress.objects.count() == 2


def test_nobody_writes_someone_elses_not_even_the_owner_or_an_agent(home):
    row_id = _add(home["gina"]).json()["id"]
    # There is no route to write another person's; the owner's /me/ is their own.
    assert _add(home["owner"]).status_code == 201
    assert PersonAddress.objects.filter(person=home["person"]).count() == 1
    assert _add(home["bailey"].user).status_code == 403
    for user in (home["owner"], home["kid"], home["bailey"].user):
        assert _client(user).delete(f"/api/people/me/addresses/{row_id}/").status_code in (403, 404)
    assert PersonAddress.objects.filter(pk=row_id).exists()
    assert _client(home["gina"]).delete(f"/api/people/me/addresses/{row_id}/").status_code == 204
    assert not PersonAddress.objects.filter(pk=row_id).exists()


@pytest.mark.parametrize("body", [{"kind": "fax"}, {"label": ""}, {"label": "x" * 41},
                                  {"value": "not-an-email"},
                                  {"kind": "phone", "value": "call me"}])
def test_a_bad_address_is_a_400(home, body):
    assert _add(home["gina"], **body).status_code == 400


def test_someone_sharing_no_workspace_learns_nothing(home):
    _add(home["gina"])
    other = Workspace.objects.create(slug="else", display_name="Else", created_by=home["owner"])
    stranger = _member(other, "stranger")
    assert _client(stranger).get(
        f"/api/people/{home['person'].pk}/addresses/?workspace=else").status_code == 404
    assert _client(stranger).get(
        f"/api/people/{home['person'].pk}/addresses/?workspace=family").status_code == 404
    assert _client(stranger).get("/api/people/addresses/?workspace=family").status_code == 404


def test_the_person_sees_every_row_in_their_own_read(home):
    _add(home["gina"])
    r = _client(home["gina"]).get("/api/people/me/")
    assert r.status_code == 200, r.content
    assert [a["value"] for a in r.json()["addresses"]] == ["gina@employer.org"]


def test_an_address_is_never_an_identity(home):
    _add(home["gina"])
    assert people.by_email("gina@employer.org") is None


def test_the_members_page_read_lists_each_person_and_only_you_edit_yours(home):
    _add(home["gina"])
    r = _client(home["kid"]).get("/api/people/addresses/?workspace=family")
    assert r.status_code == 200, r.content
    rows = {p["email"]: p for p in r.json()["people"]}
    # Humans only: the agent's login is not a person on this page.
    assert set(rows) == {"jj@dimagi.com", "gina@dimagi.com", "kid@dimagi.com"}
    assert [a["value"] for a in rows["gina@dimagi.com"]["addresses"]] == ["gina@employer.org"]
    assert {e for e, p in rows.items() if p["may_edit"]} == {"kid@dimagi.com"}
    r = _client(home["owner"]).get("/api/people/addresses/?workspace=family")
    assert {p["email"] for p in r.json()["people"] if p["may_edit"]} == {"jj@dimagi.com"}
    assert PersonAccess.objects.filter(person=home["person"], reader_user=home["kid"]).exists()
