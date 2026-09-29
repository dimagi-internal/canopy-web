# Changelog — dimagi-canopy

The import name is `canopy_sdk` and does not change with the distribution name.

## 0.4.0 — 2026-09-28

The live probe: canopy can exercise a REAL grant against a host on a schedule,
without a visitor. Minor: new public API, nothing removed, contract version
unchanged (every new field is an optional extension).

- **`canopy_sdk.host.ProbeIdentity` + `ProbeHandler`** — a host configures a
  dedicated low-privilege probe principal, one read-only scope, one tool (+
  arguments), an optional out-of-scope `denied_tool` and page key. The probe
  endpoint accepts ONLY canopy's configured client (`private_key_jwt`, verified
  by the token endpoint's own code, `aud` = issuer or the probe URL) plus a
  DPoP proof; refuses any request naming a principal, tool, arguments, or a
  scope/resource other than the probe's; spends every `jti` only after all
  checks; and returns a real ID-JAG for the probe principal (≤ 300 s,
  single-use `jti`, marked `canopy_probe: true`). `HostConfig(probe=...)`,
  `HostConfig.probe_enabled`. Unconfigured = `ProbeDisabled` (404).
- **Metadata** — `authorization_server_metadata` adds
  `canopy_probe_endpoint` while a probe is configured.
- **Django** — `CANOPY_HOST["PROBE"]` (`ENDPOINT`, `SUBJECT` or
  `SUBJECT_RESOLVER`, `SCOPE`, `TOOL`, `ARGUMENTS`, `DENIED_TOOL`, `PAGE`),
  `views.probe_endpoint` at `canopy_host:probe` (`probe/`). A broken `PROBE`
  block logs and turns the probe off — never the grant.
- **`issue_id_jag(..., extra_claims=)`** — claims beside the contract's, which
  always win.
- **`GrantHandler` logs `probe=True`** for a probe ID-JAG it redeems.
- **`canopy_sdk.conformance.live`** — `request_probe`, `check_live_grant`
  (probe → normal redemption → token material), and the three assertions:
  `check_probe_tool` (a), `check_out_of_scope_refused` (b),
  `check_requires_dpop` (c: no proof, a stranger's key, a plain bearer);
  `run_live` for all of it.

## 0.3.0 — 2026-09-27

Fixes for the gaps three hosts (connect-labs, ace-web, canopy-web) found
moving onto the SDK. Minor, not patch: new public API. Nothing is removed.

- **The DPoP gate never 500s on configuration.** `DPoPGate` answers 401
  `invalid_dpop_proof` when its verifier factory raises `HostNotConfigured`
  (or cannot be built at all), and non-DPoP traffic still passes through
  untouched, so `canopy_sdk.django.asgi.dpop_gate` is safe to install while the
  grant is off. `canopy_sdk.django.asgi.resource_verifier()` now raises
  `HostNotConfigured` whenever the grant is off (not only without a key), so a
  token issued while it was on stops resolving. New `resolve_delegated(raw,
  jkt)` for a host's bearer verifier: `None`, never an exception, while off.
  `dpop_gate(..., run_sync=)` takes a host's own sync runner. The Django
  metadata views 404 (were 500) while the grant is off; the panel-token view
  503s (was 500) on an unreadable key.
- **`CANOPY_HOST` may be a callable** (or a dotted path to one) resolved on
  every read, for values that derive from settings overridden later. A plain
  dict still works; what the SDK reads is always a real `dict`.
- **`PANEL_TOKEN_URL_NAME`** — name a URL of the host's own for the panel to
  mint at, reversed per request.
- **`panel.html` renders its options through `json_script`**, not `escapejs`,
  so the token URL reaches the page literally (`=` stayed `\u003D` before).
  A host that overrides the template gets a new `canopy_panel.options` dict.
- **`mint_contact_token` returns canopy's whole response** (`kind`,
  `host_grant`, `contact_id`, …), defaulting `kind`/`host_grant` for an older
  canopy. The panel-token view passes `kind` on to the browser. `MintFailed`
  carries `status` (canopy's HTTP status for a refusal, else `None`).
- **SPA page keys — two modes of one API.** `canopy_sdk.host.PageRegistry`
  (key mode: the browser names a page key, or a path `patterns` recognises;
  read-only scopes only unless `writable_scopes`) and `PageTokens` (signed
  mode, now a subclass) share `scopes_for(value, user_id)`. Django:
  `CANOPY_HOST["PAGE_MODE"] = "key"` + optional `PAGE_PATTERNS` /
  `WRITABLE_SCOPES`, `conf.page_registry()`, `conf.page_scopes(value, user)`.
- Docs: opt in to the pytest fixtures with `-p canopy_sdk.conformance.pytest_plugin`
  (or `addopts`); `pytest_plugins` works only in the rootdir conftest.

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
