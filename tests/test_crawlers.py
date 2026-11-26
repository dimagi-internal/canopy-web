"""What crawlers may read (config/crawlers.py): Google Search gets the public site only, no AI
crawler gets anything, and both files answer a signed-out crawler instead of bouncing it to
Google sign-in (which Search Console reads as the site's robots rules)."""
from __future__ import annotations

import pytest
from django.test import Client

from config import crawlers, public_site


@pytest.fixture
def site(tmp_path, settings):
    dist = tmp_path / "site-dist"
    (dist / "how-it-works").mkdir(parents=True)
    (dist / "site" / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>home</html>")
    (dist / "how-it-works" / "index.html").write_text("<html>how</html>")
    settings.SITE_DIST_DIR = dist
    settings.REQUIRE_AUTH = True
    settings.CANOPY_PUBLIC_BASE_URL = "https://canopy.example"
    settings.GOOGLE_SITE_VERIFICATION_FILES = ["google0123456789abcdef.html"]
    public_site._pages.cache_clear()
    yield dist
    public_site._pages.cache_clear()


def _groups(text: str) -> dict[str, list[str]]:
    """User agent → its rules, as a robots.txt parser groups them."""
    groups: dict[str, list[str]] = {}
    agents: list[str] = []
    in_rules = False
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("Sitemap:"):
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if key == "User-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(value)
            groups[value] = []
        else:
            in_rules = True
            for agent in agents:
                groups[agent].append(f"{key}: {value}")
    return groups


@pytest.mark.django_db
def test_robots_answers_signed_out(site):
    resp = Client().get("/robots.txt")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/plain")


@pytest.mark.django_db
def test_googlebot_reads_the_site_pages_only(site):
    groups = _groups(Client().get("/robots.txt").content.decode())
    assert groups["Googlebot"] == [
        "Allow: /$", "Allow: /how-it-works$", "Allow: /site/", "Allow: /favicon.svg$",
        "Allow: /google0123456789abcdef.html$", "Disallow: /",
    ]


@pytest.mark.django_db
def test_every_other_crawler_and_every_ai_crawler_reads_nothing(site):
    groups = _groups(Client().get("/robots.txt").content.decode())
    for agent in (*crawlers.AI_AGENTS, "*"):
        assert groups[agent] == ["Disallow: /"], agent
    assert "Google-Extended" in groups and "GPTBot" in groups and "ClaudeBot" in groups


@pytest.mark.django_db
def test_robots_names_the_sitemap(site):
    assert "Sitemap: https://canopy.example/sitemap.xml" in Client().get("/robots.txt").content.decode()


@pytest.mark.django_db
def test_sitemap_lists_the_site_pages(site):
    resp = Client().get("/sitemap.xml")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "<loc>https://canopy.example/</loc>" in body
    assert "<loc>https://canopy.example/how-it-works</loc>" in body
    assert body.count("<loc>") == 2
