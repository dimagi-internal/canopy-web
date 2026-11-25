"""Google site-verification files (Search Console / Chrome Web Store verified publisher).

Google proves we control canopy's domain by fetching /google<16 hex>.html and expecting the line
``google-site-verification: <file name>``. The names Google issued are listed in
``settings.GOOGLE_SITE_VERIFICATION_FILES``; any other name is a 404. The file proves ownership
only, carries nothing secret, and must stay up while the verification is in use.
"""

import re

from django.conf import settings
from django.http import Http404, HttpResponse

PATH_RE = re.compile(r"^/(google[0-9a-f]{16}\.html)$")


def is_verification_path(path: str) -> bool:
    m = PATH_RE.match(path)
    return bool(m) and m.group(1) in (settings.GOOGLE_SITE_VERIFICATION_FILES or [])


def google_site_verification(request, name):
    if not is_verification_path(f"/{name}"):
        raise Http404
    return HttpResponse(f"google-site-verification: {name}", content_type="text/html; charset=utf-8")
