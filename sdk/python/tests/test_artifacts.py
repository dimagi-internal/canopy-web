"""Host-held results, framework-free: ``canopy_sdk.resources`` (the link, the scheme,
the signed "it changed") and ``canopy_sdk.host.artifacts`` (save, read, ACL, expiry,
paging, summarize)."""
from __future__ import annotations

import json

import pytest
from canopy_sdk import resources, site
from canopy_sdk.host import artifacts as art
from canopy_sdk.host.artifacts import ArtifactRefused, Artifacts, MemoryArtifactStore, summarize_rows
from canopy_sdk.keys import generate_private_key

CANOPY = "https://canopy.example.org"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(clock):
    return Artifacts(MemoryArtifactStore(), scheme="labs-result", ttl=3600, clock=clock)


TABLE = {"columns": ["name", "visits"], "rows": [[f"w{i}", i] for i in range(250)]}


# --- the link ------------------------------------------------------------------------


def test_save_returns_an_mcp_resource_link(store):
    link = store.save("u-42", "query", TABLE, tool_call_id="call-1", name="Visits by worker",
                      description="250 rows")
    assert link["type"] == "resource_link" and link["uri"].startswith("labs-result://")
    assert link["name"] == "Visits by worker" and link["mimeType"] == "application/json"
    assert link["description"] == "250 rows"
    assert resources.validate_link(link, ["labs-result"]) == link


def test_validate_link_keeps_an_address_and_drops_everything_else():
    block = {"type": "resource_link", "uri": "labs-result://abc", "name": "r", "mime_type": "text/csv",
             "_meta": {"rows": [["amina"]]}, "annotations": {"x": 1}, "size": 9}
    assert resources.validate_link(block, ["labs-result"]) == {
        "type": "resource_link", "uri": "labs-result://abc", "name": "r", "mimeType": "text/csv"}


@pytest.mark.parametrize("block", [
    {"type": "text", "text": "labs-result://abc"},
    {"type": "resource_link", "uri": "other://abc", "name": "r", "mimeType": "a/b"},       # undeclared
    {"type": "resource_link", "uri": "https://evil.test/x", "name": "r", "mimeType": "a/b"},  # reserved
    {"type": "resource_link", "uri": "data:text/plain,amina", "name": "r", "mimeType": "a/b"},
    {"type": "resource_link", "uri": "labs-result://a b", "name": "r", "mimeType": "a/b"},
    {"type": "resource_link", "uri": "labs-result://" + "x" * 3000, "name": "r", "mimeType": "a/b"},
    {"type": "resource_link", "uri": "labs-result://abc", "name": "", "mimeType": "a/b"},
    {"type": "resource_link", "uri": "labs-result://abc", "name": "r" * 500, "mimeType": "a/b"},
    {"type": "resource_link", "uri": "labs-result://abc", "name": "r", "mimeType": "nope"},
    {"type": "resource", "resource": {"uri": "labs-result://abc", "text": "amina"}},
    "labs-result://abc",
])
def test_validate_link_refuses_what_is_not_a_declared_link(block):
    assert resources.validate_link(block, ["labs-result"]) is None


def test_validate_links_dedupes_and_caps():
    blocks = [{"type": "resource_link", "uri": f"labs-result://{i % 30}", "name": "r", "mimeType": "a/b"}
              for i in range(60)]
    out = resources.validate_links(blocks, ["labs-result"])
    assert len(out) == resources.MAX_LINKS and len({b["uri"] for b in out}) == len(out)


@pytest.mark.parametrize("scheme", ["https", "http", "data", "javascript", "ui", "file", "", "1abc", "a b"])
def test_a_host_cannot_claim_a_scheme_that_means_something_else(scheme):
    with pytest.raises(resources.ResourceError):
        resources.check_scheme(scheme)
    with pytest.raises((ValueError, resources.ResourceError)):
        Artifacts(MemoryArtifactStore(), scheme=scheme)


def test_site_json_declares_resource_schemes():
    key = generate_private_key("EdDSA")
    doc = site.site_document("connect-labs", site={"origins": ["https://labs.example.org"],
                                                   "resource_schemes": ["Labs-Result", "labs-result"]})
    assert doc["site"]["resource_schemes"] == ["labs-result"]
    got = site.verify_site_document(site.sign_site_document(key, doc), [key.public_key()],
                                    site_name="connect-labs")
    assert got.site.resource_schemes == ("labs-result",)
    with pytest.raises(site.SiteDocumentError):
        site.site_document("connect-labs", site={"resource_schemes": ["https"]})


# --- reading: owner, ACL, expiry ---------------------------------------------------------


def test_the_owner_reads_their_artifact(store):
    link = store.save("u-42", "query", {"n": 1})
    got = store.read(link["uri"], "u-42")
    assert got.payload == {"n": 1} and got.owner == "u-42" and got.kind == "query"


def test_anyone_else_is_refused_unless_the_hosts_acl_says_yes(clock):
    seen = []

    def may_read(reader, artifact):
        seen.append((reader, artifact.owner))
        return reader == "colleague"

    store = Artifacts(MemoryArtifactStore(), scheme="labs-result", may_read=may_read, clock=clock)
    uri = store.save("u-42", "query", {"n": 1})["uri"]
    assert store.read(uri, "colleague").payload == {"n": 1}
    for reader in ("stranger", None):
        with pytest.raises(ArtifactRefused) as exc:
            store.read(uri, reader)
        assert exc.value.code == "forbidden"
    assert ("colleague", "u-42") in seen


def test_an_acl_that_raises_says_no(clock):
    def boom(reader, artifact):
        raise RuntimeError("acl down")

    store = Artifacts(MemoryArtifactStore(), scheme="labs-result", may_read=boom, clock=clock)
    uri = store.save("u-42", "q", {})["uri"]
    with pytest.raises(ArtifactRefused, match="forbidden"):
        store.read(uri, "someone")


def test_an_expired_artifact_reads_as_expired_and_the_sweep_deletes_it(store, clock):
    uri = store.save("u-42", "q", {"n": 1}, ttl=60)["uri"]
    keep = store.save("u-42", "q", {"n": 2})["uri"]
    clock.t += 61
    with pytest.raises(ArtifactRefused) as exc:
        store.read(uri, "u-42")
    assert exc.value.code == "expired"
    assert store.sweep() == 1
    with pytest.raises(ArtifactRefused, match="not_found"):
        store.read(uri, "u-42")
    assert store.read(keep, "u-42").payload == {"n": 2}


@pytest.mark.parametrize("uri", ["other://abc", "labs-result://", "labs-result://abc/def", "nonsense"])
def test_a_uri_that_is_not_one_of_ours_is_bad_uri(store, uri):
    with pytest.raises(ArtifactRefused) as exc:
        store.read(uri, "u-42")
    assert exc.value.code == "bad_uri"


def test_an_unknown_id_is_not_found(store):
    with pytest.raises(ArtifactRefused) as exc:
        store.read("labs-result://nope", "u-42")
    assert exc.value.code == "not_found"


def test_save_needs_an_owner_and_stores_what_json_holds(store):
    import datetime

    with pytest.raises(ValueError):
        store.save("", "q", {})
    uri = store.save("u-42", "q", {"when": datetime.date(2026, 10, 1), "row": ("a", 1)})["uri"]
    assert store.read(uri, "u-42").payload == {"when": "2026-10-01", "row": ["a", 1]}


# --- contents: what resources/read answers ------------------------------------------------


def test_contents_pages_a_table_and_says_what_it_is(store):
    uri = store.save("u-42", "query", TABLE, tool_call_id="call-1")["uri"]
    [first] = store.contents(uri, "u-42")
    body = json.loads(first["text"])
    meta = first["_meta"]["canopy/artifact"]
    assert first["uri"] == uri and first["mimeType"] == "application/json"
    assert body["columns"] == ["name", "visits"] and len(body["rows"]) == 100
    assert meta == {**meta, "kind": "query", "tool_call_id": "call-1", "snapshot": True, "page": 1,
                    "page_size": 100, "pages": 3, "row_count": 250}
    [last] = store.contents(f"{uri}?page=3&page_size=100", "u-42")
    assert json.loads(last["text"])["rows"][-1] == ["w249", 249]
    [small] = store.contents(f"{uri}?page=2&page_size=10", "u-42")
    assert json.loads(small["text"])["rows"][0] == ["w10", 10]
    [clamped] = store.contents(f"{uri}?page=99&page_size=bogus", "u-42")
    assert clamped["_meta"]["canopy/artifact"]["page"] == 3


def test_contents_of_a_non_table_is_the_whole_payload(store):
    uri = store.save("u-42", "chart", {"mark": "bar"})["uri"]
    [c] = store.contents(uri, "u-42")
    assert json.loads(c["text"]) == {"mark": "bar"} and "page" not in c["_meta"]["canopy/artifact"]


def test_on_read_serves_fresh_and_says_so(clock):
    store = Artifacts(MemoryArtifactStore(), scheme="labs-result", clock=clock,
                      on_read=lambda a, reader: {"n": a.payload["n"] + 1})
    uri = store.save("u-42", "q", {"n": 1})["uri"]
    [c] = store.contents(uri, "u-42")
    assert json.loads(c["text"]) == {"n": 2} and c["_meta"]["canopy/artifact"]["snapshot"] is False


def test_update_replaces_the_payload_and_notifies(clock):
    told = []
    store = Artifacts(MemoryArtifactStore(), scheme="labs-result", clock=clock, notify=told.append)
    uri = store.save("u-42", "draft", {"v": 1})["uri"]
    assert store.update(uri + "?page=2", {"v": 2}) is True
    assert store.read(uri, "u-42").payload == {"v": 2} and told == [[uri]]
    assert store.update("labs-result://gone", {}) is False and told == [[uri]]


def test_template_names_the_scheme(store):
    assert store.template()["uriTemplate"] == "labs-result://{id}{?page,page_size}"


def test_module_level_calls_use_the_configured_store(store):
    with pytest.raises(resources.ResourceError):
        art.configure(None) or art.save("u-42", "q", {})
    art.configure(store)
    try:
        uri = art.save("u-42", "q", {"n": 3})["uri"]
        assert art.read(uri, "u-42").payload == {"n": 3}
        assert json.loads(art.contents(uri, "u-42")[0]["text"]) == {"n": 3}
        assert art.sweep() == 0
    finally:
        art.configure(None)


# --- facts ----------------------------------------------------------------------------------


def test_summarize_rows_gives_the_model_facts_not_the_table():
    facts = summarize_rows(TABLE["columns"], TABLE["rows"], sample=3)
    assert facts == {"row_count": 250, "columns": ["name", "visits"],
                     "sample_rows": [{"name": "w0", "visits": 0}, {"name": "w1", "visits": 1},
                                     {"name": "w2", "visits": 2}], "truncated": True}
    assert summarize_rows([{"name": "a"}], [{"a": 1, "b": 2}], sample=0) == {
        "row_count": 1, "columns": ["a"], "sample_rows": [], "truncated": True}
    assert summarize_rows(["a"], [], sample=5)["truncated"] is False


# --- "it changed": the host's signed notification ---------------------------------------


def test_a_notification_round_trips_and_is_single_use():
    key = generate_private_key("EdDSA")
    url = resources.updated_url(CANOPY)
    token = resources.updated_notification("connect-labs", key, url, ["labs-result://a", "labs-result://a"],
                                           workspace="connect")
    spent = set()

    def spend(claims):
        if claims["jti"] in spent:
            raise resources.ResourceError("replayed", "used")
        spent.add(claims["jti"])

    claims = resources.verify_updated_notification(token, [key.public_key()], audiences=[url], spend_jti=spend)
    assert claims["uris"] == ["labs-result://a"] and claims["iss"] == "connect-labs"
    assert claims["canopy_workspace"] == "connect"
    with pytest.raises(resources.ResourceError, match="used"):
        resources.verify_updated_notification(token, [key.public_key()], audiences=[url], spend_jti=spend)


def test_a_notification_for_another_canopy_or_key_or_typ_is_refused():
    from canopy_sdk import site_agents

    key, other = generate_private_key("EdDSA"), generate_private_key("EdDSA")
    url = resources.updated_url(CANOPY)
    token = resources.updated_notification("connect-labs", key, url, ["labs-result://a"])
    for keys, audiences, code in (([key.public_key()], ["https://other.example.org/x"], "wrong_audience"),
                                  ([other.public_key()], [url], "bad_signature")):
        with pytest.raises(resources.ResourceError) as exc:
            resources.verify_updated_notification(token, keys, audiences=audiences)
        assert exc.value.code == code
    # A site agents request is not a notification, however it is signed.
    request = site_agents.request_assertion("connect-labs", key, url)
    with pytest.raises(resources.ResourceError, match="typ"):
        resources.verify_updated_notification(request, [key.public_key()], audiences=[url])


def test_a_notification_names_at_least_one_and_at_most_fifty_uris():
    key = generate_private_key("EdDSA")
    url = resources.updated_url(CANOPY)
    for uris in ([], [f"labs-result://{i}" for i in range(51)], ["not a uri"]):
        with pytest.raises(resources.ResourceError):
            resources.updated_notification("connect-labs", key, url, uris)


def test_notify_updated_posts_the_signed_notification_and_reads_the_count():
    key = generate_private_key("EdDSA")
    sent = []

    def post(url, payload, *, headers):
        sent.append((url, headers["Authorization"]))
        return 200, b'{"notified": 2}', {}

    assert resources.notify_updated(CANOPY, "connect-labs", key, ["labs-result://a"], post=post) == 2
    url, auth = sent[0]
    assert url == f"{CANOPY}/api/sites/resources-updated"
    claims = resources.verify_updated_notification(auth.removeprefix("Bearer "), [key.public_key()],
                                                   audiences=[url])
    assert claims["uris"] == ["labs-result://a"]

    def refuse(url, payload, *, headers):
        return 401, b'{"detail": "bad_signature: no key"}', {}

    with pytest.raises(resources.ResourceError) as exc:
        resources.notify_updated(CANOPY, "connect-labs", key, ["labs-result://a"], post=refuse)
    assert exc.value.code == "bad_signature"
