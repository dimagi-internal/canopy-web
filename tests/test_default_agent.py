"""Which agent an embedded panel opens on.

The ranking (`apps/embedding/defaults.py`): a page that chose its agent (a bound
token) > the person's own preference > the site's default. Each counts only when
the agent is one this panel may offer this visitor — a preference grants
nothing. `GET /api/embed/agents` marks the winner `default: true`; the panel's
own fallbacks (the host's named agent, the only one, the picker) sit below it.
"""

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent, PreferredAgent
from apps.embedding import embed_apps
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import AppCredential, AppCredentialAgent, DelegatedToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

LABS = "https://labs.connect.dimagi.com"


@pytest.fixture
def site():
    owner = User.objects.create_user("owner", "owner@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    visitor = User.objects.create_user("v", "v@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=visitor, workspace=ws, role=WorkspaceMembership.EDITOR)
    agents = {s: Agent.objects.create(slug=s, name=s.title(), workspace=ws)
              for s in ("ada", "trellis", "labsdata")}
    app = AppCredential.create_credential(name="connect-labs", created_by=owner, workspace=ws)
    embed_apps.set_agents(app, ["ada", "trellis", "labsdata"])
    return {"ws": ws, "owner": owner, "visitor": visitor, "app": app, **agents}


def _bearer(app, user, bound=None):
    raw, _ = DelegatedToken.issue(app=app, user=user, ttl_seconds=3600, bound_agent=bound)
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


def _default(site, user=None, bound=None):
    body = Client().get("/api/embed/agents", **_bearer(site["app"], user or site["visitor"], bound)).json()
    defaults = [a["slug"] for a in body if a["default"]]
    assert len(defaults) <= 1
    return defaults[0] if defaults else None


# --- the ranking ------------------------------------------------------------------


def test_no_default_anywhere_leaves_it_to_the_panel(site):
    assert _default(site) is None


def test_the_site_default(site):
    embed_apps.set_default_agent(site["app"], "trellis")
    assert _default(site) == "trellis"


def test_the_persons_preference_beats_the_site_default(site):
    embed_apps.set_default_agent(site["app"], "trellis")
    PreferredAgent.objects.create(user=site["visitor"], agent=site["ada"])
    assert _default(site) == "ada"
    # ...for that person only.
    assert _default(site, user=site["owner"]) == "trellis"


def test_a_page_that_chose_its_agent_beats_the_preference(site):
    embed_apps.set_default_agent(site["app"], "trellis")
    PreferredAgent.objects.create(user=site["visitor"], agent=site["ada"])
    assert _default(site, bound=site["labsdata"]) == "labsdata"


def test_a_preference_the_site_does_not_offer_is_passed_over(site):
    embed_apps.set_default_agent(site["app"], "trellis")
    other = Agent.objects.create(slug="elsewhere", name="Elsewhere", workspace=site["ws"])
    PreferredAgent.objects.create(user=site["visitor"], agent=other)
    assert _default(site) == "trellis"


def test_a_preference_the_person_cannot_reach_is_passed_over(site):
    """Allowlisted by the site, preferred by the person, but they are no longer a
    member of its workspace: it is not on their list, so it cannot be default."""
    embed_apps.set_default_agent(site["app"], "trellis")
    theirs = Workspace.objects.create(slug="theirs", display_name="T", created_by=site["owner"])
    stranger = Agent.objects.create(slug="stranger", name="Stranger", workspace=theirs)
    AppCredentialAgent.objects.create(app=site["app"], agent=stranger)
    PreferredAgent.objects.create(user=site["visitor"], agent=stranger)
    assert _default(site) == "trellis"


def test_the_preferred_flag_marks_the_persons_choice(site):
    PreferredAgent.objects.create(user=site["visitor"], agent=site["ada"])
    body = Client().get("/api/embed/agents", **_bearer(site["app"], site["visitor"])).json()
    assert [a["slug"] for a in body if a["preferred"]] == ["ada"]


# --- the site's default -----------------------------------------------------------


def _owner_client(site):
    c = Client()
    c.force_login(site["owner"])
    grant_step_up(c)
    return c


def test_an_owner_sets_and_clears_a_sites_default(site):
    c = _owner_client(site)
    url = f"/api/workspaces/w1/connected-apps/{site['app'].pk}"
    r = c.patch(url, data={"default_agent": "trellis"}, content_type="application/json")
    assert r.status_code == 200 and r.json()["default_agent"] == "trellis"
    r = c.patch(url, data={"default_agent": "ada"}, content_type="application/json")
    assert r.json()["default_agent"] == "ada"
    assert AppCredentialAgent.objects.filter(app=site["app"], is_default=True).count() == 1
    r = c.patch(url, data={"default_agent": ""}, content_type="application/json")
    assert r.json()["default_agent"] == ""


def test_a_default_must_be_one_of_the_sites_agents(site):
    Agent.objects.create(slug="elsewhere", name="Elsewhere", workspace=site["ws"])
    r = _owner_client(site).patch(f"/api/workspaces/w1/connected-apps/{site['app'].pk}",
                                  data={"default_agent": "elsewhere"}, content_type="application/json")
    assert r.status_code == 422
    assert not AppCredentialAgent.objects.filter(app=site["app"], is_default=True).exists()


def test_agents_and_default_change_together(site):
    """Validated against the list as it is AFTER this request's `agents`."""
    Agent.objects.create(slug="newbie", name="Newbie", workspace=site["ws"])
    r = _owner_client(site).patch(f"/api/workspaces/w1/connected-apps/{site['app'].pk}",
                                  data={"agents": ["ada", "newbie"], "default_agent": "newbie"},
                                  content_type="application/json")
    assert r.status_code == 200 and r.json()["default_agent"] == "newbie"


def test_taking_the_default_off_the_list_takes_the_default_with_it(site):
    embed_apps.set_default_agent(site["app"], "trellis")
    embed_apps.set_agents(site["app"], ["ada"])
    assert embed_apps.default_agent_slug(site["app"]) == ""


def test_a_default_can_be_set_when_connecting(site):
    c = _owner_client(site)
    r = c.post("/api/workspaces/w1/connected-apps", content_type="application/json",
               data={"name": "other", "origins": [LABS], "agents": ["ada", "trellis"],
                     "default_agent": "trellis"})
    assert r.status_code == 201 and r.json()["default_agent"] == "trellis"


def test_an_editor_cannot_set_a_sites_default(site):
    c = Client()
    c.force_login(site["visitor"])
    r = c.patch(f"/api/workspaces/w1/connected-apps/{site['app'].pk}",
                data={"default_agent": "ada"}, content_type="application/json")
    assert r.status_code in (403, 404)


# --- the person's own default -------------------------------------------------------


def test_a_person_sets_and_clears_their_own_default(site):
    c = Client()
    c.force_login(site["visitor"])
    r = c.put("/api/people/me/default-agent/", data={"agent": "ada"}, content_type="application/json")
    assert r.status_code == 200 and r.json()["agent"] == "ada"
    assert c.get("/api/people/me/default-agent/").json()["agent"] == "ada"
    assert _default(site) == "ada"
    r = c.put("/api/people/me/default-agent/", data={"agent": ""}, content_type="application/json")
    assert r.json()["agent"] == "" and not PreferredAgent.objects.exists()


def test_a_person_cannot_prefer_an_agent_outside_their_workspaces(site):
    theirs = Workspace.objects.create(slug="theirs", display_name="T", created_by=site["owner"])
    Agent.objects.create(slug="stranger", name="Stranger", workspace=theirs)
    c = Client()
    c.force_login(site["visitor"])
    r = c.put("/api/people/me/default-agent/", data={"agent": "stranger"}, content_type="application/json")
    assert r.status_code == 404 and not PreferredAgent.objects.exists()


def test_the_panel_sets_the_visitors_default_among_the_sites_agents(site):
    headers = _bearer(site["app"], site["visitor"])
    r = Client().put("/api/embed/default-agent", data={"agent": "trellis"},
                     content_type="application/json", **headers)
    assert r.status_code == 200 and r.json()["agent"] == "trellis"
    assert PreferredAgent.objects.get(user=site["visitor"]).agent == site["trellis"]
    assert _default(site) == "trellis"


def test_the_panel_cannot_set_an_agent_the_site_does_not_offer(site):
    Agent.objects.create(slug="elsewhere", name="Elsewhere", workspace=site["ws"])
    r = Client().put("/api/embed/default-agent", data={"agent": "elsewhere"},
                     content_type="application/json", **_bearer(site["app"], site["visitor"]))
    assert r.status_code == 404 and not PreferredAgent.objects.exists()


def test_a_bound_panel_can_name_only_its_own_agent(site):
    headers = _bearer(site["app"], site["visitor"], bound=site["labsdata"])
    r = Client().put("/api/embed/default-agent", data={"agent": "ada"},
                     content_type="application/json", **headers)
    assert r.status_code == 404


# --- contacts -------------------------------------------------------------------------


def test_a_contact_sees_the_sites_default(site):
    from apps.contacts.models import Contact
    from apps.tokens.models import ContactToken

    embed_apps.set_default_agent(site["app"], "trellis")
    contact = Contact.objects.create(app=site["app"], workspace=site["ws"], external_id="c-1",
                                     display_name="C")
    raw, _ = ContactToken.issue(app=site["app"], contact=contact, ttl_seconds=3600)
    body = Client().get("/api/contact/me", HTTP_AUTHORIZATION=f"Bearer {raw}").json()
    assert [a["slug"] for a in body["agents"] if a["default"]] == ["trellis"]
