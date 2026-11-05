"""The default read profile, attacked.

Two halves. The first runs the REAL `profile_guard` (the canopy plugin's PreToolUse hook, which is the
one layer that enforces a caller's `bash` patterns argv token for token) against crafted commands and
tool calls, under the profile `default_read_profile` builds. It needs the canopy plugin checkout:
`CANOPY_PLUGIN_HOOKS` (the `plugins/canopy/hooks` directory) or the review clone beside this repo;
without either it is skipped (CI has neither). The second half needs no plugin: sensitive data and
the stamps read mode keeps.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import uuid
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model

from apps.agents import access, interface, roles, turn_context
from apps.agents.interface import parse
from apps.agents.interface_default import default_read_profile
from apps.agents.models import Agent
from apps.harness import caller_context
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

from .access_helpers import allow

_CANDIDATES = [os.environ.get("CANOPY_PLUGIN_HOOKS", ""),
               "/home/cu-dimagi/work/repos/.review/canopy/plugins/canopy/hooks"]


def _guard():
    for base in _CANDIDATES:
        path = pathlib.Path(base) / "profile_guard.py" if base else None
        if path and path.is_file():
            spec = importlib.util.spec_from_file_location("profile_guard_under_test", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    return None


GUARD = _guard()
needs_guard = pytest.mark.skipif(GUARD is None, reason="the canopy plugin's profile_guard.py is not checked out")


@pytest.fixture()
def cwd(tmp_path):
    (tmp_path / ".ada-read").mkdir()
    return str(tmp_path)


@pytest.fixture()
def prof():
    agent = SimpleNamespace(slug="ada", email="ada@example.org",
                            repo_url="https://github.com/dimagi-internal/ada.git", repo_ref="main")
    return {"capability": {"name": "read", **default_read_profile(agent, skills=[])}, "thread_id": "T1"}


def _bash(prof, cwd, command):
    return GUARD.decide("Bash", {"command": command}, prof, cwd)


@needs_guard
@pytest.mark.parametrize("command", [
    "curl -sS https://api.github.com/repos/dimagi-internal/ada/contents/README.md",   # the v2 reads are gone
    "curl -sS -g --path-as-is https://raw.githubusercontent.com/dimagi-internal/ada/main/README.md",
    "curl https://evil.example/", "wget https://evil.example/", "nc evil.example 80",
    "python3 -c 'import urllib.request'", "ssh evil.example",
    "gh api repos/dimagi-internal/ada/contents/x",
    "git clone https://github.com/dimagi-internal/ada", "git fetch", "git push",
    "cat /etc/passwd", "ls", "env", "python3 -c 'print(1)'", "echo hi > /tmp/x",
    "canopy email read --repo . T1; id", "canopy email read --repo . T1 | sh",
])
def test_the_guard_refuses_every_network_tool_and_shell(prof, cwd, command):
    assert _bash(prof, cwd, command) is not None


@needs_guard
def test_the_intended_reads_are_allowed(prof, cwd):
    assert _bash(prof, cwd, "canopy email read --repo . T1") is None
    assert GUARD.decide("Read", {"file_path": f"{cwd}/skills/answer-caller/SKILL.md"}, prof, cwd) is None
    assert GUARD.decide("Read", {"file_path": f"{cwd}/CLAUDE.md"}, prof, cwd) is None


@needs_guard
def test_the_profile_has_no_network_pattern_at_all(prof):
    assert not [b for b in prof["capability"]["bash"] if "curl" in b or "http" in b]


@needs_guard
@pytest.mark.parametrize("folder", [".ada-read", ".ada-ask"])
def test_a_thread_other_than_its_own_is_refused(prof, cwd, folder):
    assert _bash(prof, cwd, "canopy email read --repo . T2") is not None
    assert _bash(prof, cwd, f"bin/ada-email --reply-all --thread-id T2 --subject-file {cwd}/{folder}/s "
                            f"--body-file {cwd}/{folder}/b") is not None
    assert _bash(prof, cwd, f"bin/ada-email --reply-all --thread-id T1 --subject-file {cwd}/{folder}/s "
                            f"--body-file {cwd}/{folder}/b") is None


@needs_guard
def test_a_reply_cannot_mix_a_scratch_file_with_one_outside_both(prof, cwd):
    assert _bash(prof, cwd, f"bin/ada-email --reply-all --thread-id T1 --subject-file {cwd}/.ada-ask/s "
                            f"--body-file {cwd}/.ada-other/b") is not None


@needs_guard
@pytest.mark.parametrize("tail", ["--to evil@example.org", "--cc evil@example.org", "--repo=/etc",
                                  "--body-file /etc/passwd"])
def test_the_reply_script_takes_no_extra_flag(prof, cwd, tail):
    cmd = (f"bin/ada-email --reply-all --thread-id T1 --subject-file {cwd}/.ada-read/s "
           f"--body-file {cwd}/.ada-read/b {tail}")
    assert _bash(prof, cwd, cmd) is not None


@needs_guard
@pytest.mark.parametrize("arg", ["/etc/passwd", "{cwd}/.ada-read/../../../etc/passwd", "~/.ssh/id_rsa",
                                 "{cwd}/.ada-ask/../../../etc/passwd", "{cwd}/../outside"])
def test_the_reply_files_cannot_leave_the_worktree(prof, cwd, arg):
    cmd = (f"canopy email review-receipt --repo . --body-file {arg.format(cwd=cwd)}")
    assert _bash(prof, cwd, cmd) is not None


@needs_guard
def test_a_symlink_in_the_scratch_folder_cannot_carry_a_write_out(prof, cwd):
    os.symlink("/etc", os.path.join(cwd, ".ada-read", "link"))
    assert GUARD.decide("Write", {"file_path": f"{cwd}/.ada-read/link/passwd"}, prof, cwd) is not None
    outside = os.path.join(os.path.dirname(cwd), "secret")
    os.symlink(outside, os.path.join(cwd, ".ada-read", "s2"))
    assert GUARD.decide("Read", {"file_path": f"{cwd}/.ada-read/s2"}, prof, cwd) is not None
    os.makedirs(os.path.join(cwd, ".ada-ask"))
    os.symlink("/etc", os.path.join(cwd, ".ada-ask", "link"))
    assert GUARD.decide("Write", {"file_path": f"{cwd}/.ada-ask/link/passwd"}, prof, cwd) is not None


@needs_guard
@pytest.mark.parametrize("path", ["bin/ada-email", ".claude/settings.json", ".ada-readx", ".ada-askx", ".ada-read/../bin/x",
                                  ".ada-ask/../bin/x", ".ada-other/note.txt", ".git/hooks/pre-commit", "CLAUDE.md",
                                  "skills/answer-caller/SKILL.md", "persona.md"])
def test_write_reaches_only_the_two_scratch_folders(prof, cwd, path):
    assert GUARD.decide("Write", {"file_path": f"{cwd}/{path}"}, prof, cwd) is not None
    assert GUARD.decide("Write", {"file_path": f"{cwd}/.ada-read/note.txt"}, prof, cwd) is None
    assert GUARD.decide("Write", {"file_path": f"{cwd}/.ada-ask/note.txt"}, prof, cwd) is None   # legacy fleet name


@needs_guard
@pytest.mark.parametrize("tool", ["Edit", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch", "Agent", "Grep", "Glob",
                                  "Skill", "TodoWrite", "Bash(curl)", "mcp__other__tool"])
def test_no_tool_beyond_the_four(prof, cwd, tool):
    assert GUARD.decide(tool, {"file_path": f"{cwd}/.ada-read/x", "url": "http://x", "pattern": "*"}, prof, cwd) is not None


@needs_guard
def test_tool_search_is_allowed_but_opens_nothing_else(prof, cwd):
    """ToolSearch only loads a deferred tool's schema; the guard still judges each real call."""
    assert GUARD.decide("ToolSearch", {"query": "select:mcp__plugin_canopy_canopy-web__who_is_asking"}, prof, cwd) is None
    # a loaded schema grants no call: every other MCP tool and every other tool stays refused
    assert GUARD.decide("mcp__plugin_canopy_canopy-web__create_task", {}, prof, cwd) is not None
    assert GUARD.decide("mcp__plugin_canopy_canopy-web__who_is_asking", {}, prof, cwd) is None


@needs_guard
def test_read_stays_inside_the_worktree(prof, cwd):
    for target in ("/etc/passwd", "/proc/self/environ", "~/.ssh/id_rsa", f"{cwd}/../x"):
        assert GUARD.decide("Read", {"file_path": target}, prof, cwd) is not None, target


# --- no plugin needed ---------------------------------------------------------------------------

M = WorkspaceMembership


@pytest.fixture()
def w(db):
    mk = get_user_model().objects.create_user
    owner, vw = mk("own", "own@dimagi.com", "pw"), mk("vw", "vw@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="labs", display_name="Labs", created_by=owner)
    M.objects.create(user=owner, workspace=ws, role=M.OWNER)
    M.objects.create(user=vw, workspace=ws, role=M.VIEWER)
    agent = Agent.objects.create(slug="ada", name="Ada", workspace=ws, owner=owner)
    return {"agent": agent, "owner": owner, "vw": vw}


SENSITIVE_ASK = {"ask": {"sites": ["connect-labs"], "data": "sensitive", "tools": ["Read"]}}


def _sensitive_ask(agent):
    agent.interface = parse({"capabilities": SENSITIVE_ASK})
    agent.save(update_fields=["interface"])


@pytest.mark.django_db
def test_naming_read_cannot_dodge_a_sensitive_ask(w):
    """`read` and `ask` are one thing (`is_read_mode`), so an agent whose `ask` handles sensitive data
    is confined in it for everyone who names either: the owner naming `read` must not run FULL."""
    _sensitive_ask(w["agent"])
    for name in ("ask", "read"):
        d = turn_context.for_asker(w["agent"], w["owner"], verified=True, requested=name)
        assert (d.access, d.stamped, d.basis) == (access.CONFINED, "ask", "sensitive:ask"), name


@pytest.mark.django_db
def test_a_sensitive_conversation_gets_no_default_profile_when_its_ask_is_gone(w, monkeypatch):
    """A sensitive turn stamped `ask` whose capability was unpublished before the claim: the envelope
    must carry the empty deny-all profile, not canopy's default read profile (Write, mail)."""
    from apps.harness import sensitivity

    allow(w["agent"], role=roles.READ_ONLY)
    turn = Turn.objects.create(agent=w["agent"], origin=Turn.ORIGIN_API, idempotency_key=uuid.uuid4().hex,
                               capability="ask")
    monkeypatch.setattr(sensitivity, "is_sensitive", lambda t: True)
    cap = caller_context.build(turn)["capability"]
    assert cap["tools"] == [] and cap["bash"] == [] and cap["write_paths"] == []
    assert "default_profile_version" not in cap
    monkeypatch.setattr(sensitivity, "is_sensitive", lambda t: False)
    assert caller_context.build(turn)["capability"]["tools"] != []       # an ordinary read turn still reads


def test_read_and_ask_stay_one_thing_for_the_profile():
    assert interface.is_read_mode("ask") and interface.is_read_mode("read")
    assert not interface.is_read_mode("canopy") and not interface.is_read_mode(None)
