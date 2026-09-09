"""HTTP cache policy for the built SPA, served by WhiteNoise.

The SPA is two kinds of file with opposite needs, and WhiteNoise's default
``max-age=60`` on everything gets both of them wrong:

* ``index.html`` (and ``sw.js``) must be CURRENT. It is the only file that says
  which hashed assets this build uses, so a stale copy renders a stale app —
  and a minute of that is a minute in which the only way to see a deploy is a
  hard refresh. That is one half of the bug this module exists for; the other
  half was the service worker serving its precached shell (see
  ``frontend/vite.config.ts``). Fixing only the service worker left a 60-second
  window where a fresh visit still rendered the previous build — measured in a
  browser against a two-deploy reproduction, not reasoned about.

* ``assets/index-D2OGee1N.js`` and friends can never change: the hash IS the
  content, and a new build writes a new name. Revalidating them every 60
  seconds is pure round-trips.

So: hashed ⇒ cache forever, everything else ⇒ revalidate every time. A
revalidation of a 2 KB shell is one conditional request answered with 304, and
it is what makes "I opened the page" mean "I am on the current build".
"""
from __future__ import annotations

import re

# Vite writes `assets/<name>-<hash>.<ext>`, where <hash> is base64url. The name
# can itself contain dashes (`geist-latin-wght-normal-Dm3htQBi.woff2`), so the
# hash is anchored as the LAST dash-separated segment before the extension.
_HASHED_ASSET = re.compile(r"/assets/[^/]+-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$")

IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"


def is_hashed_asset(url: str) -> bool:
    """True for a build artifact whose name already encodes its content."""
    return bool(_HASHED_ASSET.search(url))


def add_cache_headers(headers, path: str, url: str) -> None:
    """WhiteNoise ``add_headers_function`` hook — overwrites its Cache-Control.

    Deliberately a total rule rather than a list of exceptions: a file we have
    not thought about (a new icon, ``manifest.webmanifest``, ``sw-push.js``,
    ``workbox-<hash>.js``) lands on "revalidate", which costs a 304 and can
    never serve something stale. Only a name that provably encodes its own
    content opts into being cached forever.
    """
    headers["Cache-Control"] = IMMUTABLE if is_hashed_asset(url) else REVALIDATE
