"""The default read profile, pinned EXACTLY.

Read mode (spec 2026-11-03-agent-read-mode-and-doors-design.md) gives every agent a confined profile
built in code. A change here widens or narrows every agent at once, so this test spells the whole
thing out: a diff to it needs a person's review, and `READ_PROFILE_VERSION` must be bumped with it.
"""
from types import SimpleNamespace

from apps.agents import interface
from apps.agents.interface_default import READ_MODE_STAMP, READ_PROFILE_VERSION, default_read_profile


def _agent(**kw):
    base = {"slug": "ada", "email": "", "repo_url": "", "repo_ref": "main"}
    return SimpleNamespace(**{**base, **kw})


def test_bare_agent_exact():
    assert default_read_profile(_agent(), skills=[]) == {
        "description": "Read mode: ada answers questions from its own repo and docs, "
                       "and cannot change anything but draft its reply.",
        "entry": None,
        "tools": ["Read", "Write", "ToolSearch", "mcp__*canopy-web__who_is_asking"],
        "bash": [],
        "read_paths": ["{cwd}/**"],
        "write_paths": ["{cwd}/.ada-read/*", "{cwd}/.ada-ask/*"],
        "sites": [],
        "ceiling": [],
    }


def test_no_tool_the_cloud_runner_build_lacks():
    # The cloud Claude Code build has no Grep or Glob tool (live probe: "No such tool available").
    tools = default_read_profile(_agent(), skills=[])["tools"]
    assert "Grep" not in tools and "Glob" not in tools


def test_mailbox_adds_exactly_the_email_reply_rules_for_both_scratch_folders():
    got = default_read_profile(_agent(email="ada@example.org"), skills=[])
    assert got["bash"] == [
        "canopy email read --repo . {thread_id}",
        "canopy email review-receipt --repo . --body-file {cwd}/.ada-read/*",
        "canopy email review-receipt --repo . --body-file {cwd}/.ada-ask/*",
        "bin/ada-email --reply-all --thread-id {thread_id} "
        "--subject-file {cwd}/.ada-read/* --body-file {cwd}/.ada-read/*",
        "bin/ada-email --reply-all --thread-id {thread_id} "
        "--subject-file {cwd}/.ada-ask/* --body-file {cwd}/.ada-ask/*",
    ]


def test_a_repo_adds_nothing_the_worktree_already_gives():
    # Most agent repos are private (unauthenticated GitHub reads 404) and the session's cwd is a
    # worktree of the repo, so `Read {cwd}/**` is the whole repo read: no curl, whatever repo_url is.
    for url in ("https://github.com/dimagi-internal/ada.git", "git@github.com:o/r.git", ""):
        assert default_read_profile(_agent(repo_url=url), skills=[])["bash"] == []


def test_no_network_tool_and_no_gh_ever():
    got = default_read_profile(_agent(email="a@b.c", repo_url="git@github.com:o/r.git"), skills=[])
    assert all(b.startswith(("canopy email ", "bin/ada-email ")) for b in got["bash"])
    assert not any("curl" in b or "http" in b or b.startswith("gh ") for b in got["bash"])


def test_tool_search_is_the_only_tool_added_since_v2():
    tools = default_read_profile(_agent(), skills=[])["tools"]
    assert set(tools) - {"Read", "Write", "mcp__*canopy-web__who_is_asking"} == {"ToolSearch"}


def test_writes_reach_only_the_two_scratch_folders():
    # `-ask` is the legacy fleet name (the fleet's answer-caller skills draft there), `-read` the new one.
    got = default_read_profile(_agent(email="a@b.c"), skills=[])
    assert got["write_paths"] == ["{cwd}/.ada-read/*", "{cwd}/.ada-ask/*"]
    for rule in got["bash"]:
        for token in rule.split():
            if "{cwd}" in token:
                assert token.startswith(("{cwd}/.ada-read/", "{cwd}/.ada-ask/")), rule


def test_entry_only_when_the_catalog_has_answer_caller():
    assert default_read_profile(_agent(), skills=["answer-caller"])["entry"] == \
        "/ada:answer-caller --thread {thread_id}"
    for other in (["ask"], ["policy-qa"], ["answer-caller-v2"], []):
        assert default_read_profile(_agent(), skills=other)["entry"] is None


def test_it_is_a_valid_profile_and_pinned_version():
    got = default_read_profile(_agent(email="a@b.c", repo_url="https://github.com/o/r"), skills=["answer-caller"])
    parsed = interface.parse({"capabilities": {READ_MODE_STAMP: got}})["capabilities"][READ_MODE_STAMP]
    assert parsed["tools"] == got["tools"] and parsed["bash"] == got["bash"]
    assert parsed["write_paths"] == got["write_paths"]
    assert READ_PROFILE_VERSION == 4


def test_a_fresh_dict_each_call():
    a = default_read_profile(_agent(), skills=[])
    a["tools"].append("Bash")
    assert "Bash" not in default_read_profile(_agent(), skills=[])["tools"]
