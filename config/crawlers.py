"""What crawlers may read: ``/robots.txt`` and ``/sitemap.xml``.

Only Google Search indexes canopy, and only the public product site (``config/public_site.py``):
the ``Googlebot`` group allows exactly the pages in the site build (plus their ``/site/`` files
and the Google site-verification files) and disallows the rest. Every other crawler, named AI
crawlers included, is disallowed everything. The AI tokens are also named one by one: a
product token such as ``Google-Extended`` (Gemini training and grounding) has no crawler of its
own and is honoured only when named, and naming the rest says what is meant rather than leaving
it to the ``*`` fallback.

What robots.txt cannot do: Google Search's own AI features (AI Overviews, AI Mode) read what
``Googlebot`` indexes; only ``nosnippet`` keeps a page out of them, and that also drops its
search snippet.

The page list is read from the build, like the site itself, so a new site page is allowed and
listed in the sitemap without touching this file. Both answer signed out
(``apps/common/middleware.py::_PUBLIC_PAGES``).
"""
from __future__ import annotations

from xml.sax.saxutils import escape

from django.conf import settings
from django.http import HttpRequest, HttpResponse

from config import public_site

# Crawlers that collect for AI models or answer engines, and the product tokens that opt a
# site out of AI use. Disallowed everything.
AI_AGENTS = (
    "Google-Extended",
    "GoogleOther",
    "Google-CloudVertexBot",
    "Applebot-Extended",
    "GPTBot",
    "OAI-SearchBot",
    "ChatGPT-User",
    "ClaudeBot",
    "Claude-User",
    "Claude-SearchBot",
    "anthropic-ai",
    "CCBot",
    "PerplexityBot",
    "Perplexity-User",
    "Meta-ExternalAgent",
    "Meta-ExternalFetcher",
    "FacebookBot",
    "Amazonbot",
    "Bytespider",
    "cohere-ai",
    "cohere-training-data-crawler",
    "MistralAI-User",
    "DuckAssistBot",
    "AI2Bot",
    "Diffbot",
    "YouBot",
    "Timpibot",
    "omgili",
    "ImagesiftBot",
)


def _base() -> str:
    return (settings.CANOPY_PUBLIC_BASE_URL or "").rstrip("/")


def _site_pages() -> list[str]:
    return sorted(public_site.pages())


def robots_txt(request: HttpRequest) -> HttpResponse:
    lines = ["# Google Search indexes the public product site only.", "User-agent: Googlebot"]
    # `$` ends the match: "/" must not allow everything under it.
    lines += [f"Allow: {path}$" for path in _site_pages()]
    lines.append(f"Allow: {public_site.ASSET_PREFIX}")
    lines.append("Allow: /favicon.svg$")
    lines += [f"Allow: /{name}$" for name in settings.GOOGLE_SITE_VERIFICATION_FILES or []]
    lines += ["Disallow: /", ""]
    lines.append("# No AI training, grounding or answer engines.")
    lines += [f"User-agent: {agent}" for agent in AI_AGENTS]
    lines += ["Disallow: /", ""]
    lines += ["# Nothing else.", "User-agent: *", "Disallow: /", ""]
    lines.append(f"Sitemap: {_base()}/sitemap.xml")
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain; charset=utf-8")


def sitemap_xml(request: HttpRequest) -> HttpResponse:
    base = _base()
    urls = "".join(f"<url><loc>{escape(base + path)}</loc></url>" for path in _site_pages())
    body = ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>')
    return HttpResponse(body, content_type="application/xml; charset=utf-8")
