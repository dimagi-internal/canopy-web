# Host grant contract v1 — moved to the SDK

The wire contract between canopy-web and a host site (first host: connect-labs)
now lives with the code that implements both halves of it:

**[`sdk/python/README.md` → "Host grant contract v1"](../../sdk/python/README.md#host-grant-contract-v1)**

Every value in it is a constant in `canopy_sdk.contract` (package `dimagi-canopy`,
import name `canopy_sdk`), which both canopy-web (`apps/tokens/`) and a host
(`canopy_sdk.host`) import — so there is one definition, and
`tests/test_sdk_round_trip.py` runs the two halves against each other in
canopy-web's CI. `tests/test_embedding_doc_is_true.py` pins the README's wire
constants to that module.

In one line: the host issues an ID-JAG (`typ: oauth-id-jag+jwt`) for its own MCP
server at arrival; canopy redeems it with the RFC 7523 jwt-bearer grant
(`private_key_jwt` + DPoP) at the host's token endpoint, then calls the host's
MCP as the visitor with `Authorization: DPoP` and a `Canopy-Actor` header. A host
installs the SDK from a release tag:

```
dimagi-canopy @ git+https://github.com/dimagi-internal/canopy-web@dimagi-canopy-v0.3.0#subdirectory=sdk/python
```

Design: `docs/superpowers/specs/2026-09-26-embedded-caller-delegation-design.md`.
