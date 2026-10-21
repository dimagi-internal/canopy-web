"""HTML -> markdown, a whole content region.

Article extractors (trafilatura) guess which part of a page is the article and, on long
pages, guess short: on GiveWell's site they dropped lists and whole later sections (half
of some posts). A publisher's CMS nearly always puts the article in a known region
(`#content-area`, `main`, `article`), and an API returns only the article, so neither
needs guessing: convert the whole region, keeping headings (# levels), tables and
footnotes.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

DROP = ["script", "style", "noscript", "nav", "button", "input", "select", "textarea", "iframe", "svg", "header", "footer"]
#: Where most sites put the article; a curator passes its own.
CONTENT = "#content-area, #content, main, article"


class _MD(MarkdownConverter):
    def convert_a(self, el, text, *args, **kw):  # keep link text, drop the URL
        return text

    def convert_img(self, el, text, *args, **kw):
        alt = (el.get("alt") or "").strip()
        return f"[image: {alt}]" if alt else ""


def to_markdown(html: str | bytes, selector: str | None = None, title_suffix: str = "") -> tuple[str, str]:
    """Returns (title, markdown). `selector`: CSS selectors tried in order for the content
    region (else the body); `title_suffix` (e.g. "| GiveWell") is cut from the title."""
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if title_suffix:
        title = title.replace(title_suffix, "")
    root = None
    if selector:
        for sel in selector.split(","):
            root = soup.select_one(sel.strip())
            if root:
                break
    root = root or soup.body or soup
    for t in root(DROP):
        t.decompose()
    for t in root.select(".visually-hidden, .sr-only, .share, .social-share, .print-link"):
        t.decompose()
    md = _MD(heading_style="ATX", bullets="-", strip=None).convert_soup(root)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"[ \t]{2,}", " ", md)
    return title.strip(), md.strip()


def page(html: bytes, selector: str = CONTENT, title_suffix: str = "") -> tuple[str, str]:
    return to_markdown(html, selector, title_suffix)


def fragment(html: str) -> str:
    return to_markdown(f"<html><body>{html}</body></html>")[1]
