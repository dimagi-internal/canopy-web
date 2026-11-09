# Host grant contract v1

The wire contract between canopy-web and a host site is written in
**[`sdk/python/README.md` → "Host grant contract v1"](../../sdk/python/README.md#host-grant-contract-v1)**,
next to the code that implements both halves of it.

Every value in it is a constant in `canopy_sdk/contract.py`, which both canopy-web
(`apps/embedding/`, `apps/host_grants/`) and a host (`canopy_sdk.host`) import, so there is one
definition. `tests/test_sdk_round_trip.py` runs the two halves against each other;
`tests/test_embedding_doc_is_true.py` pins the README's wire constants to that module.

In one line: the host issues an ID-JAG (`typ: oauth-id-jag+jwt`) for its own MCP server at
arrival; canopy redeems it with the RFC 7523 jwt-bearer grant (`private_key_jwt` + DPoP) at the
host's token endpoint, then calls the host's MCP as the visitor with `Authorization: DPoP` and a
`Canopy-Actor` header. The SDK pin a host installs is in the README's install section.
