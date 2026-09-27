# dimagi-canopy — the canopy SDK for Python

The Python half of the canopy SDK: what a site needs to embed a canopy agent
and let it act **as the site's visitor**, and what canopy itself uses on the
other end of the same wire.

| Package | Where | What |
|---|---|---|
| `canopy-client` | npm | layer 1 — framework-free browser transport |
| `canopy-ui` | npm | the shared React kit (chat, presence, shell) |
| **`dimagi-canopy`** | this directory | the server-side contract: host + canopy halves |

All three implement **host grant contract v1** (`canopy_sdk.contract.CONTRACT_VERSION == "1"`).

- **Import name: `canopy_sdk` — fixed.** The distribution name (`dimagi-canopy`)
  may still change before it is published to an index. A rename will only ever
  touch your requirements line, never your code.
- Not `canopy-sdk`: that PyPI name belongs to an unrelated project — the same
  lesson as the `@canopy` npm scope.

## Install

Until it is on an index, install from a release tag (each version bump that
reaches `main` is tagged `dimagi-canopy-v<version>` and gets a GitHub Release
with the sdist and wheel attached):

```
# requirements.txt / pyproject — from the tag
dimagi-canopy @ git+https://github.com/dimagi-internal/canopy-web@dimagi-canopy-v0.2.0#subdirectory=sdk/python

# or from the Release's wheel
dimagi-canopy @ https://github.com/dimagi-internal/canopy-web/releases/download/dimagi-canopy-v0.2.0/dimagi_canopy-0.2.0-py3-none-any.whl
```

Add the `django` extra (`dimagi-canopy[django] @ ...`) for `canopy_sdk.django`.
The core needs only PyJWT + `cryptography`; outbound fetches use the standard
library, address-pinned.

## What is in it

| Module | For | Key names |
|---|---|---|
| `canopy_sdk.contract` | both sides | `CONTRACT_VERSION`, `ID_JAG_TYP`, `JWT_BEARER_GRANT`, `CLIENT_ASSERTION_TYPE`, lifetime caps, `ASSERTION_ALGORITHMS`, `GRANT_ALGORITHMS`, `jwk_thumbprint`, `ath`, `metadata_url`, `normalize_htu` |
| `canopy_sdk.host` | a host site | `HostConfig`, `sign_visitor_assertion`, `issue_id_jag`, `arrival_payload`, `mint_contact_token`, `PageTokens`, `GrantHandler`, `ResourceVerifier`, `DPoPGate`, `authorization_server_metadata`, `protected_resource_metadata` |
| `canopy_sdk.consumer` | canopy | `verify_visitor_assertion`, `check_id_jag`, `ClientCredentials`, `client_assertion`, `dpop_proof`, `request_token`, `parse_token_response`, `redeem_id_jag` |
| `canopy_sdk.stores` | a host | `JtiStore` (also the DPoP replay cache), `TokenStore`, `IssuedToken`, in-memory implementations |
| `canopy_sdk.fetch` | a host | SSRF-safe `get_json` / `post_form` (https only, vetted + pinned addresses, no redirects, bounded) |
| `canopy_sdk.django` | a Django host | settings (`CANOPY_HOST`), views, models + migration, the DPoP ASGI gate, `{% canopy_panel %}` |
| `canopy_sdk.conformance` | anyone | `check_metadata`, `check_jwks`, `check_client`, `check_grant`, `check_mcp`, `run`; pytest fixtures |

## A Django host in five steps

```python
# settings.py
INSTALLED_APPS += ["canopy_sdk.django"]          # app label: canopy_host
MIDDLEWARE += ["canopy_sdk.django.middleware.CanopyPageTokenMiddleware"]  # optional
CANOPY_HOST = {
    "SIGNING_KEY": env("CANOPY_SIGNING_KEY"),     # Ed25519 or P-256 PEM — never RSA/HMAC
    "CANOPY_BASE_URL": "https://labs.connect.dimagi.com/canopy",
    "APP_NAME": "connect-labs",                   # your Connected-site name in canopy
    "AGENT_SLUG": "ace",
    # The grant: all four present turns it on.
    "CLIENT_ID": "https://labs.connect.dimagi.com/canopy/oauth/client.json",
    "ISSUER": "https://labs.connect.dimagi.com",
    "RESOURCE": "https://labs.connect.dimagi.com/mcp/",
    "TOKEN_ENDPOINT": "https://labs.connect.dimagi.com/o/token/",
    "SCOPE_TOOLS": {"marketplace:read": ["marketplace_orgs_get", "marketplace_rounds_list"]},
    "PAGE_SCOPES": {"marketplace:network": ["marketplace:read"]},   # URL name -> scopes
    "USER_CLAIMS": "myapp.canopy.user_claims",    # vouch for email_verified on purpose
}
```

```python
# urls.py
from canopy_sdk.django.views import jwt_bearer_view
from oauth2_provider.views import TokenView       # if you already run django-oauth-toolkit

urlpatterns += [
    path("canopy/", include("canopy_sdk.django.urls")),   # jwks.json, panel-token/, token/
    path("o/token/", jwt_bearer_view(TokenView.as_view())),  # jwt-bearer here, the rest to DOT
]
```

```python
# asgi.py — in front of your MCP app
from canopy_sdk.django.asgi import dpop_gate, resource_verifier
from canopy_sdk.host import presented_dpop_jkt
mcp_app = dpop_gate(mcp_app)

# ...and in your MCP bearer-token verifier, after your own PATs/OAuth tokens:
principal = resource_verifier().resolve(raw_token, presented_dpop_jkt.get())
if principal:            # a delegated token: run AS principal.subject,
    ...                  # limited to principal.allowed_tools (principal.filter_tools(tools))
```

```django
{# the page #}
{% load canopy_host %}
{% canopy_panel resource="labs-marketplace://orgs" backing_tool="marketplace_orgs_get" visible_ids=slugs %}
```

Then `migrate`, merge `canopy_sdk.host.authorization_server_metadata(config, your_doc)`
into your RFC 8414 document (and `protected_resource_metadata` into RFC 9728), and
run the conformance checks below.

Without Django, the same pieces are plain objects: build a `HostConfig`, sign
with `arrival_payload`, serve `GrantHandler(...).handle(form, dpop_header)` from
your token endpoint, and wrap your MCP ASGI app in `DPoPGate(app, verifier)`.
Provide a `JtiStore` and `TokenStore` shared across workers that **fail closed**.

**canopy-web is a host too.** Nothing assumes the host is not canopy:
issuer, resource, client id and every URL are configuration. canopy-web mounts
`HostConfig`, `GrantHandler`, `ResourceVerifier`, `DPoPGate` and the
`canopy_sdk.django` stores in-process so agents on canopy's own pages call its
OWN MCP (`/api/mcp/`) as the visitor — see canopy-web `apps/tokens/self_host.py`
and `docs/architecture/embedding-a-canopy-agent.md`.

## Conformance

```python
from canopy_sdk import conformance
report = conformance.run(
    "https://labs.example.org", "https://labs.example.org/mcp/",
    jwks_url="https://labs.example.org/labs/canopy/jwks.json",
    id_jag=test_id_jag, credentials=client_the_host_accepts,   # optional: grant + MCP round trip
)
print(report); report.raise_for_failures()
```

Without the host's signing key there is no ID-JAG to redeem, but whoever holds
canopy's CLIENT keys can still prove the host accepts canopy as its client:

```python
report = conformance.check_client(issuer, resource, credentials=canopy_client_credentials)
# client_accepted: invalid_grant for a throwaway-signed ID-JAG = the client got through
```

That is what canopy-web's **Connected sites → Test connection** runs, beside
`check_metadata` and `check_jwks`, from canopy's own server.

Network is used only inside those calls, through replaceable transports. For
your own CI, opt in to the fixtures:

```python
# conftest.py
pytest_plugins = ["canopy_sdk.conformance.pytest_plugin"]
# fixtures: canopy_client, canopy_client_documents, canopy_redeem, canopy_mcp_headers,
#           canopy_live (skipped unless --canopy-issuer / --canopy-resource are given)
```

canopy-web's own CI runs this package's host half against its real arrival and
redemption code (`tests/test_sdk_round_trip.py`), so the two sides cannot drift.

---

## Host grant contract v1

Design: canopy-web `docs/superpowers/specs/2026-09-26-embedded-caller-delegation-design.md`.
This section pins the WIRE CONTRACT so both sides can be built in parallel.
Principle: **the host (which signed the user in) issues the grant; canopy only redeems.**
Canopy never holds a key a host trusts to assert a user. Every value below is a
constant in `canopy_sdk.contract`.

### Identifiers
- Canopy client_id (a CIMD URL, MCP 2026-07-28 client registration):
  `https://labs.connect.dimagi.com/canopy/oauth/client.json`
  (generally `{CANOPY_PUBLIC_BASE}/oauth/client.json`). Served by canopy, public, JSON:
  `{"client_id": <that url>, "client_name": "canopy", "jwks_uri": "{base}/oauth/jwks.json",
    "token_endpoint_auth_method": "private_key_jwt",
    "grant_types": ["urn:ietf:params:oauth:grant-type:jwt-bearer"],
    "dpop_bound_access_tokens": true}`
- Canopy JWKS: `{CANOPY_PUBLIC_BASE}/oauth/jwks.json` — the CLIENT-AUTH public key(s)
  (`use: sig`, with `kid`). Algorithms: EdDSA (Ed25519) or ES256. Never HMAC.
- Host issuer / authorization server: the host's RFC 8414 metadata. Canopy
  discovers `token_endpoint` from it and validates `issuer`.
- Host MCP resource: the host's RFC 9728 `resource`.

### 0. The visitor assertion (arrival)
The host POSTs `{"assertion": <JWS>, "agent_slug": "<agent>"}` to
`{canopy base}/api/auth/contact-token`, server to server. The assertion: signed
with the host's key (EdDSA, ES256 or RS256 — asymmetric only; `kid` = the key's
RFC 7638 thumbprint), `iss` = the host's Connected-site name, `sub` = the host's
OWN id for its signed-in user (never anything the browser sent), `aud` = canopy's
base URL, `iat`, `exp` ≤ 120s after `iat`, a single-use `jti`; optional `name`,
`email`, `email_verified`.

### 1. Host issues an ID-JAG at arrival
In the SAME call, the body gains one optional field: `"id_jag": "<compact JWS>"`.
Absent = no grant.

ID-JAG (draft-ietf-oauth-identity-assertion-authz-grant):
- header: `typ: "oauth-id-jag+jwt"`, `alg` EdDSA|ES256, `kid` = the host's
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
  token embedded in the rendered HTML (host-internal; canopy never sees it —
  `canopy_sdk.host.PageTokens`).

### 2. Canopy redeems it (RFC 7523 jwt-bearer + private_key_jwt + DPoP)
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

Host MUST: accept only the configured canopy client_id; fetch its CIMD → `jwks_uri`
(cache ≤ 1h, SSRF-safe: https only, no private IPs) and verify `client_assertion`;
verify the ID-JAG with its OWN key; check `aud`==its issuer, `client_id`==authenticated
client, `resource` matches, `exp`, single-use `jti` (all three JWTs); verify the DPoP
proof and refuse one signed by the client key.
Response 200 JSON: `{"access_token": "...", "token_type": "DPoP", "expires_in": ≤900,
"scope": "..."}` — **no refresh_token**. The token is bound to the DPoP key's JWK
thumbprint (`cnf.jkt`), to `sub` (the visitor), `client_id` (canopy), the scope, and
carries actor `act: {"sub": "<canopy client_id>"}`. Errors: RFC 6749 JSON
(`invalid_grant`, `invalid_client`, `invalid_dpop_proof`, `invalid_scope`,
`invalid_target`, `unsupported_grant_type`). A host may answer `use_dpop_nonce`
once; canopy retries with a fresh client assertion and proof.

### 3. Canopy calls the host MCP as the visitor (canopy-web is the gateway)
Streamable HTTP to the host MCP resource with
`Authorization: DPoP <access_token>` + `DPoP: <proof>` (`htm`, `htu`=MCP URL,
`ath`=b64url(SHA-256(access_token)), `iat`, `jti`; nonce support optional in v1).
Informational header `Canopy-Actor: <agent slug>` (host logs it; not trusted for authz).
Host MUST: for a token carrying `cnf.jkt`, require and verify DPoP (replay cache on
proof `jti`, `iat` within ±60s); run the tool AS `sub`; allow only tools mapped to the
token's scopes; audit `sub`, `act`, client, actor header. Tokens WITHOUT `cnf` (normal
OAuth sign-ins, PATs) are unchanged — direct users of the host MCP see no difference.

### Freshness
Canopy re-obtains an ID-JAG whenever the widget re-mints its token (the widget
re-mints on expiry; canopy asks it to at ≤ 5 min cadence while open). When the
access token has expired and no fresh ID-JAG arrived, a host call in that
visitor's turn FAILS ("I need you back on the page"); it never falls back to the
agent's own credential.

### Scopes (connect-labs v1)
`marketplace:read` → the read-only marketplace tools
(`marketplace_orgs_get`, `marketplace_rounds_list`). The host owns the
scope→tool and route→scope maps.

### Security properties this package keeps
Asymmetric algorithms only, chosen by the verifier and never read from a
token's header; key shape checked against the algorithm; constant-time
comparison of every identifier; every `jti` single-use, consumed only after
every other check passed, in a store that fails closed; no token, assertion or
proof in any log line, exception message or refusal body; outbound fetches
https-only to vetted, pinned, public addresses with no redirects.

## Versioning and release

`version` in `pyproject.toml` (kept equal to `canopy_sdk.__version__`) is the
release: merging a bump to `main` makes `release-dimagi-canopy.yml` build the
sdist + wheel, tag `dimagi-canopy-v<version>` and publish a GitHub Release. CI
refuses a change under `src/` without a bump. `CONTRACT_VERSION` moves only for a
wire change a deployed peer would notice. See `CHANGELOG.md`.
