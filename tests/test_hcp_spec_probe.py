"""The HCP spec probe notices the schema and a newer draft, and never fails a build.

`scripts/hcp_spec_probe.py` runs weekly (`.github/workflows/hcp-spec-probe.yml`). Its
network is injected here, so the offline cases cover every answer the spec site can
give. What each means for conformance 2.1.1 is in `docs/architecture/hcp-conformance.md`.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

pytestmark = pytest.mark.guard

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("hcp_spec_probe", ROOT / "scripts/hcp_spec_probe.py")
probe_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe_mod)

SHELL = b'<!doctype html><html><body><div id="root"></div><script type="module" src="/assets/app.js"></script></body></html>'


def site(pages: dict):
    """A fake network: url -> (status, content type, body); anything else is a 404."""
    def get(url):
        return pages.get(url, (404, "text/plain", b""))
    return get


def pages(schema=(200, "text/html; charset=utf-8", SHELL), bundle=b"console.log(1)", home=SHELL):
    return {probe_mod.SCHEMA_URL: schema, probe_mod.SITE_URL: (200, "text/html", home),
            "https://hcp.me/assets/app.js": (200, "text/javascript", bundle)}


def test_today_the_schema_url_is_the_html_shell_and_that_is_not_news():
    r = probe_mod.probe(site(pages()), implemented=4)
    assert r["schema"]["state"] == "blocked" and not r["schema"]["news"]
    assert r["site"]["state"] == "no-newer-draft" and not r["site"]["news"]
    assert r["news"] is False


def test_json_with_a_dollar_schema_is_the_schema_being_published():
    body = json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "HCP entry"}).encode()
    r = probe_mod.probe(site(pages(schema=(200, "application/schema+json", body))), implemented=4)
    assert r["schema"]["state"] == "published" and r["news"] is True
    # Even a wrong content type: parseable JSON carrying $schema counts.
    r = probe_mod.probe(site(pages(schema=(200, "text/plain", body))), implemented=4)
    assert r["schema"]["state"] == "published"


def test_a_json_content_type_without_a_schema_is_worth_a_look_but_is_not_claimed_as_the_schema():
    r = probe_mod.probe(site(pages(schema=(200, "application/json", b'{"status": "soon"}'))), implemented=4)
    assert r["schema"]["state"] == "json-without-schema" and r["news"] is True


@pytest.mark.parametrize("answer", [(404, "text/plain", b""), (500, "text/html", b"oops"),
                                    (200, "text/html", b"[not json]"), (301, "text/html", b"")])
def test_any_other_answer_is_not_the_schema(answer):
    assert probe_mod.probe(site(pages(schema=answer)), implemented=4)["schema"]["news"] is False


def test_an_unreachable_site_is_reported_and_is_not_news():
    r = probe_mod.probe(lambda url: (None, "URLError: down", b""), implemented=4)
    assert r["schema"]["state"] == "unreachable" and r["site"]["state"] == "unreachable"
    assert r["news"] is False


def test_a_newer_draft_in_a_script_bundle_is_news_and_the_same_draft_is_not():
    newer = probe_mod.probe(site(pages(bundle=b'x="Reference Specification 1.0-draft-5"')), implemented=4)
    assert newer["site"]["state"] == "newer-draft" and newer["news"] is True
    assert newer["site"]["drafts"] == [5]
    same = probe_mod.probe(site(pages(bundle=b'x="1.0-draft-4"')), implemented=4)
    assert same["site"]["state"] == "no-newer-draft" and same["news"] is False
    page = probe_mod.probe(site(pages(home=SHELL + b"<p>Draft 6</p>")), implemented=4)
    assert page["site"]["state"] == "newer-draft"


def test_the_draft_pattern_reads_the_ways_a_draft_is_written():
    assert probe_mod.drafts_in("1.0-draft-4, Draft 5, draft_6, DRAFT-7.") == {4, 5, 6, 7}
    assert probe_mod.drafts_in("a draftsman, draft copy") == set()


def test_only_the_sites_own_scripts_are_followed():
    html = ('<script src="/assets/a.js"></script><script src="https://hcp.me/b.js"></script>'
            '<script src="https://evil.example/c.js"></script><script src="//cdn.example/d.js"></script>')
    assert probe_mod.bundle_urls(html) == ["https://hcp.me/assets/a.js", "https://hcp.me/b.js"]


def test_the_issue_says_what_closes_conformance_2_1_1():
    body = json.dumps({"$schema": "x"}).encode()
    r = probe_mod.probe(site(pages(schema=(200, "application/schema+json", body))), implemented=4)
    text = probe_mod.body_for(r)
    assert "2.1.1" in text and "hcp.KNOWN_GAPS" in text and "hcp.CONFORMANCE_LEVEL" in text
    assert "validate Tier 1 entries" in text
    assert probe_mod.title_for(r).startswith("HCP spec: ") and "closes conformance 2.1.1" in probe_mod.title_for(r)


def test_it_exits_zero_and_writes_nothing_when_there_is_no_news(monkeypatch, tmp_path):
    monkeypatch.setattr(probe_mod, "fetch", site(pages()))
    real = probe_mod.probe
    monkeypatch.setattr(probe_mod, "probe", lambda: real(probe_mod.fetch, 4))
    out, body = tmp_path / "out", tmp_path / "body.md"
    assert probe_mod.main(["--github-output", str(out), "--body-file", str(body)]) == 0
    assert out.read_text() == "news=false\n" and not body.exists()


def test_it_writes_the_title_and_body_when_there_is_news(monkeypatch, tmp_path):
    schema = json.dumps({"$schema": "x"}).encode()
    monkeypatch.setattr(probe_mod, "fetch", site(pages(schema=(200, "application/json", schema))))
    real = probe_mod.probe
    monkeypatch.setattr(probe_mod, "probe", lambda: real(probe_mod.fetch, 4))
    out, body = tmp_path / "out", tmp_path / "body.md"
    assert probe_mod.main(["--github-output", str(out), "--body-file", str(body)]) == 0
    assert out.read_text().startswith("news=true\ntitle=HCP spec: ")
    assert "2.1.1" in body.read_text()


def test_it_exits_zero_even_when_the_network_is_down(monkeypatch, tmp_path):
    monkeypatch.setattr(probe_mod, "fetch", lambda url: (None, "URLError: down", b""))
    real = probe_mod.probe
    monkeypatch.setattr(probe_mod, "probe", lambda: real(probe_mod.fetch))
    out = tmp_path / "out"
    assert probe_mod.main(["--github-output", str(out)]) == 0
    assert out.read_text() == "news=false\n"


def test_the_draft_it_compares_with_is_the_one_the_audit_names():
    assert probe_mod.implemented_draft() == 4
    audit = (ROOT / "docs/architecture/hcp-conformance.md").read_text()
    assert "1.0-draft-4" in audit


def test_the_workflow_runs_the_probe_on_a_schedule_and_cannot_fail_unrelated_work():
    wf = (ROOT / ".github/workflows/hcp-spec-probe.yml").read_text()
    assert "schedule:" in wf and "cron:" in wf and "workflow_dispatch:" in wf
    assert "scripts/hcp_spec_probe.py" in wf
    assert "issues: write" in wf and "contents: read" in wf
    # Its own workflow: nothing in the PR pipeline waits for it.
    assert "pull_request" not in wf and "push:" not in wf
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "hcp-spec-probe" not in ci and "hcp_spec_probe" not in ci
