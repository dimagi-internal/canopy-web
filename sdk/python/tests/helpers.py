"""Shared test scaffolding: a host, a canopy client, and signed things."""
from __future__ import annotations

import asyncio
import json
import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from canopy_sdk import consumer, contract
from canopy_sdk.host import ClientKeyResolver, GrantHandler, HostConfig, ResourceVerifier
from canopy_sdk.keys import generate_private_key, private_pem
from canopy_sdk.stores import MemoryCache, MemoryJtiStore, MemoryTokenStore

BASE = "https://labs.example.org"
ISSUER = BASE
TOKEN_ENDPOINT = f"{BASE}/o/token/"
RESOURCE = f"{BASE}/mcp/"
CANOPY = f"{BASE}/canopy"
CLIENT_ID = f"{CANOPY}/oauth/client.json"
JWKS_URI = f"{CANOPY}/oauth/jwks.json"
SCOPE_TOOLS = {"marketplace:read": {"marketplace_orgs_get", "marketplace_rounds_list"}}


def ec_jwk(private) -> dict:
    return ECAlgorithm.to_jwk(private.public_key(), as_dict=True)


class World:
    """One host + one canopy client, wired to each other in memory."""

    def __init__(self, *, active_subjects=("42",)):
        self.host_key = generate_private_key("EdDSA")
        self.client = consumer.ClientCredentials(CLIENT_ID, generate_private_key("EdDSA"),
                                                 ec.generate_private_key(ec.SECP256R1()))
        self.client_jwk = {**self.client.jwks()["keys"][0]}
        self.config = HostConfig(
            signing_key=private_pem(self.host_key), canopy_base_url=CANOPY, app_name="connect-labs",
            issuer=ISSUER, resource=RESOURCE, token_endpoint=TOKEN_ENDPOINT,
            canopy_client_id=CLIENT_ID, scope_tools=SCOPE_TOOLS)
        self.documents = {CLIENT_ID: self.client.metadata(JWKS_URI), JWKS_URI: self.client.jwks()}
        self.fetches: list[str] = []
        self.jti_store = MemoryJtiStore()
        self.token_store = MemoryTokenStore()
        self.active = set(active_subjects)
        self.resolver = ClientKeyResolver(fetch_json=self._fetch, cache=MemoryCache())
        self.handler = GrantHandler(self.config, jti_store=self.jti_store, token_store=self.token_store,
                                    client_keys=self.resolver, subject_active=self.active.__contains__)
        self.verifier = ResourceVerifier(self.config, token_store=self.token_store,
                                         replay_store=MemoryJtiStore(),
                                         subject_active=self.active.__contains__)

    def _fetch(self, url):
        self.fetches.append(url)
        document = self.documents[url]
        if isinstance(document, Exception):
            raise document
        return document

    # --- signed things -----------------------------------------------------------

    def client_assertion(self, **overrides) -> str:
        now = int(time.time())
        claims = {"iss": CLIENT_ID, "sub": CLIENT_ID, "aud": ISSUER, "iat": now, "exp": now + 60,
                  "jti": str(uuid.uuid4())}
        claims.update(overrides)
        return jwt.encode(claims, self.client.client_key, algorithm="EdDSA",
                          headers={"kid": self.client_jwk["kid"]})

    def proof(self, htm="POST", htu=TOKEN_ENDPOINT, access_token=None, key=None, jwk=None, alg="ES256",
              headers=None, **overrides) -> str:
        claims = {"htm": htm, "htu": htu, "iat": int(time.time()), "jti": str(uuid.uuid4())}
        if access_token is not None:
            claims["ath"] = contract.ath(access_token)
        claims.update(overrides)
        return jwt.encode(claims, key or self.client.dpop_key, algorithm=alg,
                          headers=headers or {"typ": "dpop+jwt",
                                              "jwk": jwk or ec_jwk(self.client.dpop_key)})

    def id_jag(self, sub="42", *, key=None, alg="EdDSA", headers=None, **overrides) -> str:
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": ISSUER, "sub": sub, "client_id": CLIENT_ID, "resource": RESOURCE,
                  "scope": "marketplace:read", "iat": now, "exp": now + 120, "jti": str(uuid.uuid4())}
        claims.update(overrides)
        return jwt.encode(claims, key or self.host_key, algorithm=alg,
                          headers=headers or {"kid": self.config.kid, "typ": contract.ID_JAG_TYP})

    def form(self, *, assertion=None, client_assertion=None, extra=None, drop=()) -> dict:
        data = {
            "grant_type": contract.JWT_BEARER_GRANT,
            "assertion": assertion if assertion is not None else self.id_jag(),
            "client_id": CLIENT_ID,
            "client_assertion_type": contract.CLIENT_ASSERTION_TYPE,
            "client_assertion": client_assertion if client_assertion is not None else self.client_assertion(),
            "resource": RESOURCE,
        }
        data.update(extra or {})
        for name in drop:
            data.pop(name, None)
        return data

    def redeem(self, proof="default", **form_kwargs):
        if proof == "default":
            proof = self.proof()
        return self.handler.handle(self.form(**form_kwargs), proof)


def call_asgi(app, method: str, path: str, *, headers: dict | None = None, body: bytes = b""):
    """Drive an ASGI app once; returns ``(status, headers, body)``."""

    async def main():
        scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(),
                 "query_string": b"", "headers": [(k.lower().encode(), v.encode())
                                                   for k, v in (headers or {}).items()],
                 "scheme": "https", "server": ("labs.example.org", 443), "http_version": "1.1"}
        sent = {"status": None, "headers": [], "body": b""}
        delivered = False

        async def receive():
            nonlocal delivered
            if delivered:
                await asyncio.sleep(3600)
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                sent["status"] = message["status"]
                sent["headers"] = message.get("headers", [])
            elif message["type"] == "http.response.body":
                sent["body"] += message.get("body", b"")

        await app(scope, receive, send)
        return sent["status"], {k.decode(): v.decode() for k, v in sent["headers"]}, sent["body"]

    return asyncio.run(main())


def echo_app(seen: list):
    """An ASGI app that records the scope it was handed and answers 200 JSON."""

    async def app(scope, receive, send):
        from canopy_sdk.host import delegated_principal, presented_dpop_jkt

        seen.append({"headers": dict(scope["headers"]), "jkt": presented_dpop_jkt.get(),
                     "principal": delegated_principal.get()})
        body = json.dumps({"ok": True}).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

    return app
