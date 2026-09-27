"""Fetching without SSRF, and canopy's keys as a host learns them.

Ported from connect-labs' "Fetching client metadata without SSRF" tests.
"""
from __future__ import annotations

import socket
from unittest import mock

import pytest

from canopy_sdk import fetch
from canopy_sdk.host import ClientKeyResolver, MetadataError
from canopy_sdk.stores import MemoryCache


@pytest.mark.parametrize("url", [
    "http://canopy.example/client.json",
    "https://user:pw@canopy.example/client.json",
    "https://canopy.example:8443/client.json",
    "https://127.0.0.1/client.json",
    "https://169.254.169.254/latest/meta-data/",
    "https://10.0.0.5/jwks.json",
    "https://[::1]/jwks.json",
    "file:///etc/passwd",
    "https://canopy.example/a b",
    "https://" + "a" * 3000,
])
def test_urls_that_are_not_public_https_are_refused(url):
    with pytest.raises(fetch.FetchError):
        fetch.vet_url(url)


@pytest.mark.parametrize("addresses", [
    ["10.1.2.3"], ["169.254.169.254"], ["127.0.0.1"], ["93.184.216.34", "192.168.1.1"],
    ["::ffff:10.0.0.1"], ["100.64.0.1"],
])
def test_a_host_resolving_to_a_private_address_is_not_connected_to(addresses):
    infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 443)) for a in addresses]
    with mock.patch.object(fetch.socket, "getaddrinfo", return_value=infos), \
            mock.patch.object(fetch.socket, "create_connection") as connect:
        with pytest.raises(fetch.FetchError):
            fetch.get_json("https://canopy.example/client.json")
    connect.assert_not_called()


def test_a_public_host_is_connected_to_at_the_vetted_address_with_tls_to_the_name():
    infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    with mock.patch.object(fetch.socket, "getaddrinfo", return_value=infos), \
            mock.patch.object(fetch.socket, "create_connection", side_effect=OSError("stop")) as connect:
        with pytest.raises(fetch.FetchError):
            fetch.get_json("https://canopy.example/client.json")
    assert connect.call_args.args[0] == ("93.184.216.34", 443), "no second lookup to be rebound"


def test_an_unresolvable_host_is_a_fetch_error():
    with mock.patch.object(fetch.socket, "getaddrinfo", side_effect=OSError("nx")):
        with pytest.raises(fetch.FetchError):
            fetch.get_json("https://nowhere.example/x")


# --- ClientKeyResolver ------------------------------------------------------------------

CLIENT = "https://canopy.example/oauth/client.json"
JWKS = "https://canopy.example/oauth/jwks.json"


def _resolver(documents, calls):
    def get(url):
        calls.append(url)
        value = documents[url]
        if isinstance(value, Exception):
            raise value
        return value

    return ClientKeyResolver(fetch_json=get, cache=MemoryCache())


def _docs(keys):
    return {CLIENT: {"client_id": CLIENT, "jwks_uri": JWKS, "token_endpoint_auth_method": "private_key_jwt"},
            JWKS: {"keys": keys}}


def test_metadata_is_cached():
    calls = []
    resolver = _resolver(_docs([{"kid": "a", "kty": "OKP"}]), calls)
    resolver.signing_keys(CLIENT)
    resolver.signing_keys(CLIENT)
    assert calls == [CLIENT, JWKS]


def test_a_fetch_error_becomes_a_metadata_error():
    resolver = _resolver({CLIENT: fetch.FetchError("timed out")}, [])
    with pytest.raises(MetadataError):
        resolver.signing_keys(CLIENT)


@pytest.mark.parametrize("change", [
    {"client_id": "https://other/c.json"}, {"token_endpoint_auth_method": "none"}, {"jwks_uri": None},
])
def test_a_document_that_does_not_describe_the_client_is_refused(change):
    docs = _docs([])
    docs[CLIENT] = {**docs[CLIENT], **change}
    with pytest.raises(MetadataError):
        _resolver(docs, []).signing_keys(CLIENT)


def test_a_jwks_without_keys_is_refused():
    with pytest.raises(MetadataError):
        _resolver({**_docs([]), JWKS: {"keys": "nope"}}, []).signing_keys(CLIENT)


def test_the_cache_is_never_longer_than_the_contracts_hour():
    assert ClientKeyResolver(cache_seconds=86400).cache_seconds == 3600
