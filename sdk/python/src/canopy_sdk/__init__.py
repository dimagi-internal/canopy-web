"""dimagi-canopy — the Python half of the canopy SDK.

The import name is ``canopy_sdk`` and it is FIXED: the distribution name
(``dimagi-canopy``) may still change, and a rename must only ever touch a host's
requirements line, never its code.

Modules:

* ``canopy_sdk.contract`` — the host grant contract, defined once.
* ``canopy_sdk.host`` — what a host site runs (signing, the token endpoint, the
  MCP-side DPoP verifier).
* ``canopy_sdk.consumer`` — canopy's side (verifying, redeeming, signing proofs).
* ``canopy_sdk.django`` — optional Django wiring for the host half.
* ``canopy_sdk.conformance`` — checks against a live host, plus pytest fixtures.
* ``canopy_sdk.ondemand`` — optional (``ondemand`` extra): run a dedicated
  capability on a stopped-when-idle EC2 instance.
"""
from .contract import CONTRACT_VERSION

__version__ = "0.7.2"

__all__ = ["CONTRACT_VERSION", "__version__"]
