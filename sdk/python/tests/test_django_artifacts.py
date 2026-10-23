"""Host-held results for a Django host: the model and migration, owner from the
delegated request, the resource handler, the sweep command, ``site.json``'s
declared scheme and the notification on ``update``."""
from __future__ import annotations

import json
from datetime import timedelta
from io import StringIO
from unittest import mock

import pytest
from canopy_sdk import resources
from canopy_sdk import site as site_mod
from canopy_sdk.django import artifacts
from canopy_sdk.django.models import Artifact as Row
from canopy_sdk.host import DelegatedPrincipal, delegated_principal
from canopy_sdk.host.artifacts import ArtifactRefused
from canopy_sdk.host.config import HostNotConfigured
from canopy_sdk.keys import private_pem
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone

from .helpers import CANOPY, CLIENT_ID, World

pytestmark = pytest.mark.django_db


def acl(reader, artifact):
    return reader == "colleague"


@pytest.fixture
def world():
    return World()


def _settings(world, **artifacts_cfg):
    return {"SIGNING_KEY": private_pem(world.host_key), "CANOPY_BASE_URL": CANOPY,
            "APP_NAME": "connect-labs", "WORKSPACE": "connect", "CLIENT_ID": CLIENT_ID,
            "ARTIFACTS": {"SCHEME": "labs-result", "TTL": 3600, **artifacts_cfg}}


@pytest.fixture
def host(world):
    with override_settings(CANOPY_HOST=_settings(world, MAY_READ="tests.test_django_artifacts.acl")):
        yield world


def as_visitor(subject):
    principal = DelegatedPrincipal(subject=subject, client_id=CLIENT_ID, actor="ace", scopes=("data:read",),
                                   cnf_jkt="jkt", expires_at=9e9, allowed_tools=frozenset())
    return delegated_principal.set(principal)


def test_save_inside_a_delegated_request_belongs_to_the_visitor(host):
    marker = as_visitor("u-42")
    try:
        link = artifacts.save("query", {"columns": ["a"], "rows": [[1], [2]]}, tool_call_id="call-1",
                              name="Query result")
    finally:
        delegated_principal.reset(marker)
    row = Row.objects.get()
    assert link == {"type": "resource_link", "uri": f"labs-result://{row.artifact_id}",
                    "name": "Query result", "mimeType": "application/json"}
    assert row.owner == "u-42" and row.kind == "query" and row.tool_call_id == "call-1"
    assert row.expires_at - row.created_at == timedelta(seconds=3600)


def test_without_a_delegated_request_the_owner_is_the_users_subject_or_named(host, django_user_model):
    user = django_user_model.objects.create_user("jo", password="pw")
    link = artifacts.save("q", {"n": 1}, user=user)
    assert Row.objects.get(artifact_id=link["uri"].split("//")[1]).owner == str(user.pk)
    assert Row.objects.filter(owner="explicit").count() == 0
    artifacts.save("q", {}, owner="explicit")
    assert Row.objects.filter(owner="explicit").count() == 1
    with pytest.raises(ValueError, match="no owner"):
        artifacts.save("q", {})


def test_the_resource_handler_reads_as_the_reader_with_the_hosts_acl(host):
    link = artifacts.save("query", {"columns": ["a"], "rows": [[i] for i in range(150)]}, owner="u-42")
    marker = as_visitor("u-42")
    try:
        [c] = artifacts.resource_handler(link["uri"] + "?page=2")
    finally:
        delegated_principal.reset(marker)
    assert json.loads(c["text"])["rows"][0] == [100]
    assert c["_meta"]["canopy/artifact"]["pages"] == 2
    assert artifacts.resource_handler(link["uri"], reader="colleague")[0]["uri"] == link["uri"]
    for reader in ("stranger", None):
        with pytest.raises(ArtifactRefused) as exc:
            artifacts.resource_handler(link["uri"], reader=reader)
        assert exc.value.code == "forbidden"


def test_a_reader_hook_names_who_is_reading(world):
    cfg = _settings(world, READER=lambda: "u-42")
    with override_settings(CANOPY_HOST=cfg):
        uri = artifacts.save("q", {"n": 1})["uri"]
        assert artifacts.read(uri).owner == "u-42"


def test_the_sweep_deletes_expired_rows_and_the_command_says_how_many(host):
    old = artifacts.save("q", {"n": 1}, owner="u-42")["uri"]
    new = artifacts.save("q", {"n": 2}, owner="u-42")["uri"]
    Row.objects.filter(artifact_id=old.split("//")[1]).update(expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(ArtifactRefused, match="expired"):
        artifacts.read(old, reader="u-42")
    out = StringIO()
    call_command("canopy_artifacts_sweep", stdout=out)
    assert "deleted 1" in out.getvalue()
    assert Row.objects.count() == 1 and artifacts.read(new, reader="u-42").payload == {"n": 2}


def test_unconfigured_artifacts_are_off(world):
    with override_settings(CANOPY_HOST={"SIGNING_KEY": private_pem(world.host_key)}):
        with pytest.raises(HostNotConfigured):
            artifacts.save("q", {}, owner="u-42")


def test_site_json_declares_the_artifact_scheme(host):
    response = Client().get("/canopy/site.json")
    got = site_mod.verify_site_document(response.content.decode(), [host.host_key.public_key()],
                                        site_name="connect-labs")
    assert got.site.resource_schemes == ("labs-result",)
    assert artifacts.template()["uriTemplate"].startswith("labs-result://")


def test_update_tells_canopy_and_a_failure_to_tell_never_breaks_the_update(host):
    uri = artifacts.save("draft", {"v": 1}, owner="u-42")["uri"]
    sent = []

    def post(url, payload, *, headers, **_kw):
        sent.append(headers["Authorization"].removeprefix("Bearer "))
        return 200, b'{"notified": 1}', {}

    with mock.patch("canopy_sdk.fetch.post_json", side_effect=post):
        assert artifacts.update(uri, {"v": 2}) is True
    claims = resources.verify_updated_notification(sent[0], [host.host_key.public_key()],
                                                   audiences=[resources.updated_url(CANOPY)])
    assert claims["uris"] == [uri] and claims["canopy_workspace"] == "connect"
    with mock.patch("canopy_sdk.fetch.post_json", side_effect=OSError("down")):
        assert artifacts.update(uri, {"v": 3}) is True
    assert artifacts.read(uri, reader="u-42").payload == {"v": 3}
