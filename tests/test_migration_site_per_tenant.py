"""`tokens.0021` + `contacts.0007` against prod's actual shape, and the one it refuses.

Prod on 2026-09-24: three sites (ace-web, canopy-web, connect-labs), each with
exactly one grant, from its own registering workspace. The grant's
`resolvable_domains` is the only thing that has to move, and it must not be
lost — ace-web's three domains were typed by hand and exist in no file.
"""

import pytest

BEFORE = [("tokens", "0020_site_outlives_its_registrant"),
          ("contacts", "0006_one_person_across_tenants")]
AFTER = [("tokens", "0021_site_belongs_to_one_tenant"),
         ("contacts", "0007_person_keyed_on_signer")]


def _executor():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    return executor


def _to_leaf():
    executor = _executor()
    executor.migrate(executor.loader.graph.leaf_nodes())


def _seed_prod_shape(apps):
    User = apps.get_model("auth", "User")
    Workspace = apps.get_model("workspaces", "Workspace")
    Site = apps.get_model("tokens", "AppCredential")
    Grant = apps.get_model("tokens", "AppCredentialTenant")
    Contact = apps.get_model("contacts", "Contact")
    Person = apps.get_model("contacts", "Person")

    creator = User.objects.create(username="c")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=creator)
    ace = Site.objects.create(name="ace-web", token_hash="h1", workspace=ws,
                              jwks_url="https://labs.connect.dimagi.com/ace/api/canopy/jwks")
    Grant.objects.create(app=ace, workspace=ws,
                         resolvable_domains=["dimagi.com", "dimagi-ai.com",
                                             "dimagi-associate.com"])
    for i, name in enumerate(("canopy-web", "connect-labs"), start=2):
        site = Site.objects.create(name=name, token_hash=f"h{i}", workspace=ws)
        Grant.objects.create(app=site, workspace=ws)
    person = Person.objects.create(app=ace, external_id="u-1")
    Contact.objects.create(workspace=ws, app=ace, external_id="u-1", person=person)
    return ws


@pytest.mark.django_db(transaction=True)
def test_prods_shape_migrates_and_keeps_the_hand_typed_domains():
    executor = _executor()
    executor.migrate(BEFORE)
    _seed_prod_shape(executor.loader.project_state(BEFORE).apps)

    executor = _executor()
    executor.migrate(AFTER)
    apps = executor.loader.project_state(AFTER).apps
    Site = apps.get_model("tokens", "AppCredential")
    Person = apps.get_model("contacts", "Person")
    Contact = apps.get_model("contacts", "Contact")

    ace = Site.objects.get(name="ace-web")
    assert ace.workspace_id == "connect"
    assert ace.resolvable_domains == ["dimagi.com", "dimagi-ai.com", "dimagi-associate.com"]
    assert {s.name for s in Site.objects.filter(workspace_id="connect")} \
        == {"ace-web", "canopy-web", "connect-labs"}
    person = Person.objects.get()
    assert (person.issuer, len(person.signer), person.external_id) == ("ace-web", 64, "u-1")
    assert Contact.objects.get().person_id == person.pk, "the contact keeps its person"

    _to_leaf()


@pytest.mark.django_db(transaction=True)
def test_a_site_shared_by_two_tenants_is_refused_not_guessed():
    """Splitting it would mean deciding which tenant keeps its contacts — not a
    migration's call. Prod had none; this pins that the step says so loudly."""
    executor = _executor()
    executor.migrate(BEFORE)
    apps = executor.loader.project_state(BEFORE).apps
    ws = _seed_prod_shape(apps)
    other = apps.get_model("workspaces", "Workspace").objects.create(
        slug="other", display_name="Other", created_by=ws.created_by)
    apps.get_model("tokens", "AppCredentialTenant").objects.create(
        app=apps.get_model("tokens", "AppCredential").objects.get(name="connect-labs"),
        workspace=other)

    executor = _executor()
    with pytest.raises(RuntimeError, match="granted by 2 tenants"):
        executor.migrate(AFTER)

    # Leave the database at the leaf for the tests that follow.
    apps.get_model("tokens", "AppCredentialTenant").objects.filter(workspace=other).delete()
    _to_leaf()
