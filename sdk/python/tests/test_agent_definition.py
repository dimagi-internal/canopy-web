"""The stored-agent definition contract: the manifest root both sides compute, and the
file policy canopy-web enforces on every write."""
from __future__ import annotations

import hashlib
import json

import pytest

from canopy_sdk import agent_definition as ad

SLUG = "connect-helper"


def _f(path, content="hello\n", executable=False, **kw):
    return {"path": path, "content": content, "executable": executable, **kw}


def test_manifest_root_is_the_sha256_of_the_sorted_compact_manifest():
    files = [_f("b.md", "B"), _f("CLAUDE.md", "A")]
    entries = [ad.manifest_entry(f["path"], f["content"]) for f in files]
    rows = sorted(entries, key=lambda r: r["path"])
    expected = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert ad.manifest_root(entries) == expected
    # Order of the input never matters; the stored {path: {...}} shape gives the same root.
    assert ad.manifest_root(list(reversed(entries))) == expected
    tree = {e["path"]: {k: v for k, v in e.items() if k != "path"} for e in entries}
    assert ad.manifest_root(tree) == expected
    assert ad.manifest_root_of_files(files) == expected


def test_the_executable_bit_is_part_of_the_identity():
    a = ad.manifest_root([ad.manifest_entry("bin/x-email", "#!/bin/sh\n", True)])
    b = ad.manifest_root([ad.manifest_entry("bin/x-email", "#!/bin/sh\n", False)])
    assert a != b


@pytest.mark.parametrize("path", [
    "CLAUDE.md", "persona.md", "README.md", "skills/worker-chat/SKILL.md",
    "skills/worker-chat/refs/notes.md", "skills/worker-chat/data/x.json", "skills/a/b/c.yaml",
    "config/agent.json", "config/gating.json", "config/allowlist.txt",
    "tests/scenarios/agree.json", "tests/x.yml", "docs/design.md", "docs/deep/er.md",
])
def test_editable_paths(path):
    assert ad.editable(path)
    assert ad.check_tree([_f(path)], slug=SLUG) == []


@pytest.mark.parametrize("path,code", [
    ("../etc/passwd", "bad_path"),
    ("/etc/passwd", "bad_path"),
    ("skills\\x\\SKILL.md", "bad_path"),
    ("skills/x/\x00.md", "bad_path"),
    ("skills//SKILL.md", "bad_path"),
    (".git/config", "bad_path"),
    ("skills/x/.git/HEAD", "bad_path"),
    ("hooks/hooks.json", "hooks"),
    ("hooks/pre.sh", "hooks"),
    (".claude-plugin/plugin.json", "server_generated"),
    (".claude/settings.json", "server_generated"),
    (f"bin/{SLUG}-email", "server_generated"),
    (".gitignore", "server_generated"),
    ("bin/_env.py", "not_editable"),
    ("scripts/x.py", "not_editable"),
    (".github/workflows/test.yml", "not_editable"),
    ("tests/test_x.py", "not_editable"),
    ("skills/x/run.sh", "not_editable"),
    ("docs/x.txt", "not_editable"),
    ("config/other.json", "not_editable"),
    (".mcp.json", "not_editable"),
    ("skills/x/.hidden.md", "not_editable"),
    ("skills/X/SKILL.md", "not_editable"),
])
def test_refused_paths(path, code):
    problems = ad.check_tree([_f(path)], slug=SLUG)
    assert [p.code for p in problems] == [code]


def test_generated_paths_are_allowed_only_in_canopys_own_check():
    files = [_f(".claude-plugin/plugin.json", "{}"), _f(f"bin/{SLUG}-email", "#!/usr/bin/env python3\n", True)]
    assert ad.check_tree(files, slug=SLUG, allow_generated=True) == []
    assert {p.code for p in ad.check_tree(files, slug=SLUG)} == {"server_generated"}


def test_only_the_email_shim_is_executable():
    assert [p.code for p in ad.check_tree([_f("CLAUDE.md", executable=True)], slug=SLUG)] == ["executable"]
    other = ad.check_tree([_f("bin/other-email", executable=True)], slug=SLUG, allow_generated=True)
    assert "not_editable" in [p.code for p in other]


def test_symlinks_and_duplicates_are_refused():
    assert [p.code for p in ad.check_tree([_f("CLAUDE.md", symlink=True)], slug=SLUG)] == ["symlink"]
    assert [p.code for p in ad.check_tree([_f("CLAUDE.md"), _f("CLAUDE.md")], slug=SLUG)] == ["duplicate"]


def test_caps():
    big = "x" * (ad.MAX_FILE_BYTES + 1)
    assert [p.code for p in ad.check_tree([_f("CLAUDE.md", big)], slug=SLUG)] == ["too_large"]
    many = [_f(f"docs/d{i}.md") for i in range(ad.MAX_FILES + 1)]
    assert [p.code for p in ad.check_tree(many, slug=SLUG)] == ["too_many_files"]
    chunk = "y" * (200 * 1024)
    heavy = [_f(f"docs/d{i}.md", chunk) for i in range(11)]
    assert [p.code for p in ad.check_tree(heavy, slug=SLUG)] == ["tree_too_large"]


def test_content_must_be_utf8_text():
    assert [p.code for p in ad.check_tree([_f("CLAUDE.md", b"\xff\xfe")], slug=SLUG)] == ["not_utf8"]


@pytest.mark.parametrize("text", [
    "token: ghp_" + "a" * 36,
    "github_pat_" + "B" * 40,
    "key sk-ant-" + "c" * 40,
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "cpat_" + "d" * 30,
    "-----BEGIN RSA PRIVATE KEY-----\nMII\n-----END RSA PRIVATE KEY-----",
    "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjM0In0" + ".c2lnbmF0dXJlc2ln",
    "https://bob:hunter22@example.com/x",
    "Authorization: Bearer " + "e" * 30,
    "xoxb-" + "1" * 20,
    "OCS_API_KEY" + "=abc123def456ghi789",
])
def test_secret_shaped_content_is_refused(text):
    problems = ad.check_tree([_f("skills/x/SKILL.md", f"---\nname: x\n---\n{text}\n")], slug=SLUG)
    assert [p.code for p in problems] == ["secret"]


@pytest.mark.parametrize("text", [
    "Set GITHUB_TOKEN=$(canopy cred get github)",
    "OCS_API_KEY=<your key>",
    "API_KEY: your-api-key-here",
    "the TOKEN: required field",
    "PASSWORD = {{ vault.password }}",
    "a link https://example.com/path and bearer tokens in general",
])
def test_placeholders_and_prose_are_not_secrets(text):
    assert ad.secret_kinds(text) == []


def test_policy_is_data():
    p = ad.policy()
    assert p["max_files"] == 200 and p["max_file_bytes"] == 256 * 1024 and p["max_tree_bytes"] == 2 * 1024 * 1024
    assert "skills/<name>/SKILL.md" in p["editable"]
    assert "bin/<slug>-email" in p["generated"]


def test_the_shared_vector_the_cli_pins():
    entries = [ad.manifest_entry("CLAUDE.md", b"# helper\n"),
               ad.manifest_entry("bin/helper-email", b"#!/bin/sh\n", True)]
    assert ad.manifest_root(entries) == "b90fd0b7d22806c3d6402f4996bb0b1bcd2a1978b40769aefca00be68b1b97d4"


def test_git_url_and_its_recognition():
    url = ad.git_url("https://canopy.dimagi.com/", "connect", "connect-helper")
    assert url == "https://canopy.dimagi.com/git/connect/connect-helper.git"
    assert ad.is_git_url(url, "https://canopy.dimagi.com")
    assert ad.is_git_url(url + "/", "https://canopy.dimagi.com/")
    for other in ("https://github.com/dimagi-internal/connect-helper.git",
                  "https://canopy.dimagi.com/git/connect/connect-helper",
                  "https://canopy.dimagi.com/git/connect-helper.git",
                  "https://canopy.dimagi.com/git/a/b/c.git",
                  "https://elsewhere.example/git/connect/connect-helper.git",
                  "canopy://connect/connect-helper", ""):
        assert not ad.is_git_url(other, "https://canopy.dimagi.com"), other
    assert not ad.is_git_url(url, "")
