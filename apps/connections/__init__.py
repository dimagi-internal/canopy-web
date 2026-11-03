"""OAuth connections (Salesforce, Google), sealed to their owner.

THE CONTRACT. canopy-web holds no token it can read or use. At connect it
exchanges the code, runs the policy checks and seals the refresh token to the
owner's vault key at once; after that it stores ciphertext, relays it to the
machines the owner's signed grant statement allows, and never refreshes or
revokes. Machines refresh and write back (one lease holder at a time), cloud
boxes re-seal for newly allowed machines and revoke at the provider. The app's
client secret (`ConnectionApp`, Fernet) is the one secret canopy-web keeps, and
it leaves only in a material response to an authenticated machine.

WHERE THINGS LIVE
- `services.py` connect and disconnect; the audit/log vocabulary (`record()`)
- `sharing.py` the signed statement and the envelope sealed to it
- `recipients.py` who may open a connection; `wire.py` the envelope wire format
- `tokens.py` / `proofs.py` material, lease, write-back; device proof
- `keeper.py` re-seal, revoke confirmation, the sweeps
- `providers/` one module per provider

AUDIT. One vocabulary, `services.AUDITED`: `connected`, `disconnected`,
`shared`, `granted`, `grant_revoked`, `token_issued` (names `to`, the `runner`
and `machine`, and the `turn`), `refreshed` (`rotated`), `resealed`, `revoked`,
`revoke_unconfirmed`, `lease_expired_unwritten`, `reconnect_required`,
`app_configured`, `app_removed`. Never a token, code, verifier or client secret
in any event, log line or response other than the material response's `app`.
"""
