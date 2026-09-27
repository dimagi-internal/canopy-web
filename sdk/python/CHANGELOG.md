# Changelog — dimagi-canopy

The import name is `canopy_sdk` and does not change with the distribution name.

## 0.2.0 — 2026-09-27

- `canopy_sdk.conformance.check_client` — does a host accept canopy as its
  client, provable WITHOUT the host's signing key: the real `private_key_jwt`
  assertion and DPoP proof, carrying an ID-JAG signed by a throwaway key.
  `invalid_grant` means the client authenticated; `invalid_client` means it
  did not; a 200 means the host accepted a grant it never issued. Nothing is
  spent at the host. `run()` includes it when given `credentials` and no
  `id_jag`. canopy-web's Connected sites "Test connection" uses it.

## 0.1.0 — 2026-09-27

First release. Implements host grant contract v1 (`CONTRACT_VERSION = "1"`).

- `canopy_sdk.contract` — the contract's constants and pure helpers, defined once.
- `canopy_sdk.host` — the host half, extracted from connect-labs PR #2060 and
  generalised: visitor assertion + ID-JAG signing, the page registry and page
  token, the jwt-bearer `GrantHandler`, the MCP-side `ResourceVerifier` and ASGI
  `DPoPGate`, metadata helpers.
- `canopy_sdk.consumer` — canopy's half, extracted from canopy-web
  `apps/tokens/`: visitor-assertion and ID-JAG verification, the client
  assertion and DPoP proofs, token request/response handling.
- `canopy_sdk.django` — optional Django wiring (settings, views, models +
  migration, the DPoP gate, `{% canopy_panel %}`).
- `canopy_sdk.conformance` — live checks and opt-in pytest fixtures.
- `canopy_sdk.fetch` — SSRF-safe, address-pinned outbound fetches (stdlib only).
