# Host grant contract v1 (canopy-web ↔ a host site, first host: connect-labs)

Design: canopy-web `docs/superpowers/specs/2026-09-26-embedded-caller-delegation-design.md`
(PR #985). This file pins the WIRE CONTRACT so both sides can be built in parallel.
Principle: **the host (which signed the user in) issues the grant; canopy only redeems.**
Canopy never holds a key a host trusts to assert a user.

## Identifiers
- Canopy client_id (a CIMD URL, MCP 2026-07-28 client registration):
  `https://labs.connect.dimagi.com/canopy/oauth/client.json`
  (generally `{CANOPY_PUBLIC_BASE}/oauth/client.json`). Served by canopy, public, JSON:
  `{"client_id": <that url>, "client_name": "canopy", "jwks_uri": "{base}/oauth/jwks.json",
    "token_endpoint_auth_method": "private_key_jwt",
    "grant_types": ["urn:ietf:params:oauth:grant-type:jwt-bearer"],
    "dpop_bound_access_tokens": true}`
- Canopy JWKS: `{CANOPY_PUBLIC_BASE}/oauth/jwks.json` — the CLIENT-AUTH public key(s)
  (`use: sig`, with `kid`). Algorithms: EdDSA (Ed25519) or ES256. Never HMAC.
- Host issuer / authorization server: the host's existing RFC 8414 metadata
  (connect-labs: `connect_labs/mcp/oauth.py::authorization_server_metadata`). Canopy
  discovers `token_endpoint` from it and validates `issuer`.
- Host MCP resource: the host's RFC 9728 `resource` (connect-labs: `oauth.resource_url()`).

## 1. Host issues an ID-JAG at arrival
Where: in the SAME server-to-server call where the host already signs the visitor
assertion for canopy (connect-labs `connect_labs/labs/canopy.py`, canopy
`apps/tokens/contact_api.py` / `apps/tokens/assertions.py`). The mint request body
gains one optional field: `"id_jag": "<compact JWS>"`. Absent = today's behaviour.

ID-JAG (draft-ietf-oauth-identity-assertion-authz-grant):
- header: `typ: "oauth-id-jag+jwt"`, `alg` EdDSA|ES256, `kid` = the host's existing
  signing key (the one canopy already verifies assertions with).
- claims:
  - `iss` = host issuer; `aud` = host issuer (the host grants for its own AS)
  - `sub` = the host's own id for the visitor — MUST equal the assertion's `sub`
  - `client_id` = canopy's client_id (above)
  - `resource` = host MCP resource URL
  - `scope` = space-separated scopes for THIS PAGE, decided server-side by the host
    from its own route registry (never from anything the browser sent). Default
    read-only. An unregistered page gets NO id_jag.
  - `iat`, `exp` (≤ 300s after iat), `jti` (unique)
- The page's identity reaches the host's token endpoint as a SERVER-signed page
  token embedded in the rendered HTML (host-internal detail; canopy never sees it).

## 2. Canopy redeems it (RFC 7523 jwt-bearer + private_key_jwt + DPoP)
`POST {host token_endpoint}` form-encoded:
- `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`
- `assertion=<the ID-JAG>`
- `client_id=<canopy client_id>`
- `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`
- `client_assertion=<JWS signed by canopy's client key: iss=sub=client_id,
   aud=host issuer, iat, exp ≤ 60s, jti>`
- `resource=<host MCP resource>` (RFC 8707), optional `scope` (subset only)
- header `DPoP: <proof>` (RFC 9449; canopy's DPoP key, distinct from the client key;
  `htm=POST`, `htu=token_endpoint`, `iat`, `jti`)

Host MUST: accept only the configured canopy client_id (setting, e.g.
`CANOPY_CLIENT_ID`); fetch its CIMD → `jwks_uri` (cache ≤ 1h, SSRF-safe: https only,
no private IPs) and verify `client_assertion`; verify the ID-JAG with its OWN key;
check `aud`==its issuer, `client_id`==authenticated client, `resource` matches, `exp`,
single-use `jti` (both JWTs); verify the DPoP proof.
Response 200 JSON: `{"access_token": "...", "token_type": "DPoP", "expires_in": ≤900,
"scope": "..."}` — **no refresh_token**. The token is bound to the DPoP key's JWK
thumbprint (`cnf.jkt`), to `sub` (the visitor), `client_id` (canopy), the scope, and
carries actor `act: {"sub": "<canopy client_id>"}`. Errors: RFC 6749 JSON
(`invalid_grant`, `invalid_client`, `invalid_dpop_proof`, `invalid_scope`).

## 3. Canopy calls the host MCP as the visitor (canopy-web is the gateway)
Streamable HTTP to the host MCP resource with
`Authorization: DPoP <access_token>` + `DPoP: <proof>` (`htm`, `htu`=MCP URL,
`ath`=b64url(SHA-256(access_token)), `iat`, `jti`; nonce support optional in v1).
Informational header `Canopy-Actor: <agent slug>` (host logs it; not trusted for authz).
Host MCP MUST: for a token carrying `cnf.jkt`, require and verify DPoP (replay cache on
proof `jti`, `iat` within ±60s); run the tool AS `sub`; allow only tools mapped to the
token's scopes; audit `sub`, `act`, client, actor header. Tokens WITHOUT `cnf` (normal
OAuth sign-ins, PATs) are unchanged — direct users of the host MCP see no difference.

## Freshness
Canopy re-obtains an ID-JAG whenever the widget re-mints its token (the widget
re-mints on expiry; canopy should ask it to at ≤ 5 min cadence while open). When
the access token has expired and no fresh ID-JAG arrived, a host call in that
visitor's turn FAILS ("I need you back on the page"); it never falls back to the
agent's own credential.

## Scopes (connect-labs v1)
`marketplace:read` → the read-only marketplace tools
(`marketplace_orgs_get`, `marketplace_rounds_list`). The host owns the
scope→tool and route→scope maps.
