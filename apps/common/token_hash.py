"""How a token-shaped secret is stored: its sha256, hex.

Every bearer, code, ticket and key canopy issues is kept only as this digest and
looked up by it, so there is one function for it rather than a `hashlib` line
per model. Not for content digests (ETags, dedupe keys) — those are not secrets
and may change shape freely.
"""
from __future__ import annotations

import hashlib


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
