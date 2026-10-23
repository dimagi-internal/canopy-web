"""A credential bundle as canopy-web serves it: one ordered `cascade` list.

Values ride on the entries as if the box had already opened them from its vault
(`_with_vault_values`), so a test sees the cascade the box stages. Tokens are keyed
"1", "2", … (labels `token:1`, `token:2`), the API key "k" (label `api-key:k`).
"""
from __future__ import annotations


def bundle(*tokens: str, api_key: str = "", logins: list[str] | None = None, **extra) -> dict:
    cascade = [{"key": k, "kind": "login"} for k in logins or []]
    cascade += [{"key": str(i), "kind": "token", "value": v} for i, v in enumerate(tokens, 1)]
    if api_key:
        cascade.append({"key": "k", "kind": "api_key", "value": api_key})
    out = {"cascade": cascade, **extra}
    if logins is not None:
        out["logins"] = [{"key": k} for k in logins]
    return out
