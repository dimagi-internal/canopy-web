"""canopy's signing keys, as a host learns them: its Client ID Metadata Document
(the ``client_id`` IS that document's URL) → the ``jwks_uri`` it names → keys.

Both fetches are server-side requests to an address somebody else wrote down,
so they go through ``canopy_sdk.fetch`` (https only, vetted addresses, no
redirects, bounded) by default. Cached for at most an hour (the contract's
ceiling), so a rotated key is picked up within that window; a ``kid`` that
misses may force ONE early re-fetch per floor window, so a stream of bad ``kid``
values cannot become a stream of requests at canopy.

Extracted from connect-labs ``connect_labs/mcp/client_metadata.py``.
"""
from __future__ import annotations

from collections.abc import Callable

from .. import contract, fetch
from ..keys import select_key
from ..stores import DocumentCache, MemoryCache


class MetadataError(Exception):
    """canopy's metadata or keys could not be read or do not describe it. The
    grant then fails closed with ``invalid_client``."""


class ClientKeyResolver:
    CACHE_SECONDS = contract.CLIENT_METADATA_MAX_CACHE_SECONDS
    REFETCH_FLOOR_SECONDS = 300

    def __init__(self, *, fetch_json: Callable[[str], dict] | None = None,
                 cache: DocumentCache | None = None, cache_seconds: int | None = None):
        self._fetch = fetch_json or fetch.get_json
        self._cache = cache if cache is not None else MemoryCache()
        self.cache_seconds = min(int(cache_seconds or self.CACHE_SECONDS), self.CACHE_SECONDS)

    def _document(self, url: str, *, fresh: bool) -> dict:
        key = f"canopy-client-metadata:{url}"
        if not fresh:
            cached = self._cache.get(key)
            if isinstance(cached, dict):
                return cached
        try:
            document = self._fetch(url)
        except fetch.FetchError as exc:
            raise MetadataError(str(exc)) from exc
        if not isinstance(document, dict):
            raise MetadataError(f"{url} is not a JSON object")
        self._cache.set(key, document, self.cache_seconds)
        return document

    def signing_keys(self, client_id: str, *, fresh: bool = False) -> list[dict]:
        """The client's JWKS ``keys``, after checking its metadata describes it
        as the contract requires."""
        document = self._document(client_id, fresh=fresh)
        if document.get("client_id") != client_id:
            raise MetadataError("the client metadata document names a different client_id")
        if document.get("token_endpoint_auth_method") != contract.TOKEN_ENDPOINT_AUTH_METHOD:
            raise MetadataError("the client does not authenticate with private_key_jwt")
        jwks_uri = document.get("jwks_uri")
        if not isinstance(jwks_uri, str):
            raise MetadataError("the client metadata document names no jwks_uri")
        jwks = self._document(jwks_uri, fresh=fresh)
        keys = jwks.get("keys")
        if not isinstance(keys, list) or not all(isinstance(k, dict) for k in keys):
            raise MetadataError("the client's JWKS has no keys")
        return keys

    def refetch_allowed(self, client_id: str) -> bool:
        return bool(self._cache.add(f"canopy-client-metadata-refetch:{client_id}", 1,
                                    self.REFETCH_FLOOR_SECONDS))

    def key_for(self, client_id: str, kid) -> dict:
        """The one published signing key named by ``kid``. Raises
        ``MetadataError`` (unreadable) or ``contract.ContractError('unknown_key')``."""
        found = select_key(self.signing_keys(client_id), kid)
        if found is None and self.refetch_allowed(client_id):
            # Perhaps canopy rotated its key since the cache was filled.
            found = select_key(self.signing_keys(client_id, fresh=True), kid)
        if found is None:
            raise contract.ContractError("unknown_key", "no published client key matches the assertion's kid")
        return found
