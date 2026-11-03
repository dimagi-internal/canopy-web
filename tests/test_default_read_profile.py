"""The default read profile, pinned EXACTLY.

Read mode (spec 2026-11-03-agent-read-mode-and-doors-design.md) gives every agent a confined profile
built in code. A change here widens or narrows every agent at once, so this test spells the whole
thing out: a diff to it needs a person's review, and `READ_PROFILE_VERSION` must be bumped with it.
"""
from types import SimpleNamespace

from apps.agents import interface
from apps.agents.interface_default import READ_PROFILE_VERSION, default_read_profile


def _agent(**kw):
    base = {"slug": "ada", "email": "", "repo_url": "", "repo_ref": "main"}
    return SimpleNamespace(**{**base, **kw})


def test_bare_agent_exact():
    assert default_read_profile(_agent(), skills=[]) == {
        "description": "Ask ada a question. It answers from its own repo and docs; "
                       "it cannot change anything but draft its reply.",
        "entry": None,
        "tools": ["Read", "Grep", "Glob", "Write", "mcp__*canopy-web__who_is_asking"],
        "bash": [],
        "read_paths": ["{cwd}/**"],
        "write_paths": ["{cwd}/.ada-read/*"],
        "input": {},
        "pages": [],
        "sites": [],
        "ceiling": [],
    }


def test_mailbox_adds_exactly_the_email_reply_rules():
    got = default_read_profile(_agent(email="ada@example.org"), skills=[])
    assert got["bash"] == [
        "canopy email read --repo . {thread_id}",
        "canopy email review-receipt --repo . --body-file {cwd}/.ada-read/*",
        "bin/ada-email --reply-all --thread-id {thread_id} "
        "--subject-file {cwd}/.ada-read/* --body-file {cwd}/.ada-read/*",
    ]


def test_repo_adds_exactly_unauthenticated_curl_reads_of_its_own_repo():
    got = default_read_profile(_agent(repo_url="https://github.com/dimagi-internal/ada.git"), skills=[])
    assert got["bash"] == [
        'curl -sS -H "Accept: application/vnd.github.raw" https://api.github.com/repos/dimagi-internal/ada/contents/*',
        "curl -sS https://api.github.com/repos/dimagi-internal/ada/contents/*",
        "curl -sS https://api.github.com/repos/dimagi-internal/ada/git/trees/main?recursive=1",
        "curl -sS https://raw.githubusercontent.com/dimagi-internal/ada/main/*",
    ]


def test_no_gh_and_no_other_host_ever():
    got = default_read_profile(_agent(email="a@b.c", repo_url="git@github.com:o/r.git", repo_ref="x y"),
                               skills=[])
    assert not any(b.startswith("gh ") for b in got["bash"])
    assert all(b.startswith(("canopy email ", "bin/ada-email ", "curl -sS ")) for b in got["bash"])
    assert "git/trees/main?recursive=1" in " ".join(got["bash"])   # an unsafe ref falls back to main


def test_a_repo_that_is_not_on_github_gets_no_reads():
    assert default_read_profile(_agent(repo_url="https://example.org/x/y.git"), skills=[])["bash"] == []


def test_entry_only_when_the_catalog_has_answer_caller():
    assert default_read_profile(_agent(), skills=["answer-caller"])["entry"] == \
        "/ada:answer-caller --thread {thread_id}"
    for other in (["ask"], ["policy-qa"], ["answer-caller-v2"], []):
        assert default_read_profile(_agent(), skills=other)["entry"] is None


def test_it_is_a_valid_capability_and_pinned_version():
    got = default_read_profile(_agent(email="a@b.c", repo_url="https://github.com/o/r"), skills=["answer-caller"])
    parsed = interface.parse({"capabilities": {"ask": got}})["capabilities"]["ask"]
    assert parsed["tools"] == got["tools"] and parsed["bash"] == got["bash"]
    assert parsed["write_paths"] == got["write_paths"]
    assert READ_PROFILE_VERSION == 1


def test_a_fresh_dict_each_call():
    a = default_read_profile(_agent(), skills=[])
    a["tools"].append("Bash")
    assert "Bash" not in default_read_profile(_agent(), skills=[])["tools"]
