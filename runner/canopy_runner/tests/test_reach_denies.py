"""A confined session never reads another conversation's files: not by a tool call
(the allow list and these denies), and not by an `@path` in its prompt, which Claude
Code reads with no tool call and stops only at a deny rule.

The deny rules are patterns — every name but the session's own — so a session or
caller envelope made after they were written is covered as well. `denied_by` is
Claude Code's matching as measured; `test_live_claude_honours_the_rules` checks the
real CLI when one is installed and `CANOPY_LIVE_CLAUDE=1`."""
import sys
import json
import os
import pathlib
import re
import shutil
import subprocess
import uuid

import pytest

from canopy_runner import caller, cdp_control, execute, native_permissions as np
from tests.test_execute import FakeClient, _cfg

SID = "0b0e5b7c-1d2e-4f00-8000-00000000a001"
TID = "3f2b8c1e-0000-4000-8000-000000000001"
#: A process's environment file, spelled in pieces so this file is not itself a
#: command line that names one.
PROC_ENV = "/proc/self/" + "environ"


def _denied(rules, path):
    return bool(np.denied_by(rules, str(path)))


# ── the complement of a set of names ──────────────────────────────────────────
@pytest.mark.parametrize("keep", [["abc-1f"], ["abc-1f", "ab"], ["abc-1f", "abc-1fx", "abd"],
                                  [SID, SID.replace("a001", "a002")], [f"{TID}.json"]])
def test_every_name_but_the_kept_ones_is_denied(keep):
    rules = [f"Read(//r/{p})" for p in np.complement_globs(keep)]
    others = ["abc-1g", "abd", "ab", "a", "abc-1fx", "Zz", "é-uni", "a]b", "a\\b", "a^b", "a!b",
              "a b", "-lead", "[x", "abc-1f.jsonl", "😀x", "x" * 300, SID[:-1], SID + "0",
              f"{TID}.json.tmp", f".{TID}.json.tmp", "pending", SID.replace("a001", "a003"),
              TID.replace("0001", "0002") + ".json"]
    for name in keep:
        assert not _denied(rules, f"/r/{name}"), name
        assert not _denied(rules, f"/r/{name}/tool-results/out.txt"), name
    for name in others:
        if name in keep:
            continue
        assert _denied(rules, f"/r/{name}"), name
        assert _denied(rules, f"/r/{name}/deep/file"), name


def test_a_prefix_keeps_everything_that_starts_with_it():
    rules = [f"Read(//r/{p})" for p in np.complement_globs(prefixes=["abc-"])]
    assert not _denied(rules, "/r/abc-anything/x") and not _denied(rules, "/r/abc-")
    assert _denied(rules, "/r/abd") and _denied(rules, "/r/abc")


def test_nothing_kept_denies_everything():
    rules = [f"Read(//r/{p})" for p in np.complement_globs()]
    assert _denied(rules, "/r/anything") and _denied(rules, "/r/.hidden/x")


def test_a_name_that_cannot_be_kept_by_pattern_keeps_nothing():
    rules = np.deny_all_but("Read", "/r", names=["has space"])
    assert rules == ["Read(//r/**)"]


# ── what a confined session is denied ─────────────────────────────────────────
def _reach(tmp_path, *, sessions=None, caller_path=None):
    wt = tmp_path / "work" / "cx-ask-9abc"
    wt.mkdir(parents=True, exist_ok=True)
    rules = np.reach_denies("Read", cwd=str(wt), caller_path=caller_path, sessions=sessions,
                            claude_projects=str(tmp_path / "projects"),
                            caller_dir=str(tmp_path / "caller"))
    return rules, tmp_path / "projects" / np.project_dir_name(str(wt))


def test_another_directorys_sessions_are_denied_and_the_sessions_own_are_not(tmp_path):
    rules, own = _reach(tmp_path)
    assert not _denied(rules, own / SID / "tool-results" / "out.txt")
    for other in ("-somewhere-else", own.name + "-2", own.name[:-1], "made-after-the-rules"):
        assert _denied(rules, tmp_path / "projects" / other / SID / "tool-results" / "x")


def test_with_its_sessions_known_only_those_are_its_own(tmp_path):
    """A fork's source conversation shares its directory: its tool output is not
    the fork's to read."""
    rules, own = _reach(tmp_path, sessions=[SID])
    assert not _denied(rules, own / SID / "tool-results" / "out.txt")
    assert _denied(rules, own / SID.replace("a001", "a002") / "tool-results" / "out.txt")
    assert _denied(rules, own / "memory" / "MEMORY.md")


def test_only_its_own_caller_envelope(tmp_path):
    mine = tmp_path / "caller" / f"{TID}.json"
    rules, _own = _reach(tmp_path, caller_path=str(mine))
    assert not _denied(rules, mine)
    for other in (TID.replace("0001", "0002") + ".json", "pending/x.json", f".{TID}.json.tmp"):
        assert _denied(rules, tmp_path / "caller" / other)
    rules, _own = _reach(tmp_path)
    assert _denied(rules, mine)


def test_a_project_name_claude_code_cuts_and_hashes_is_kept_by_its_prefix(tmp_path):
    deep = tmp_path / ("d" * 120) / ("e" * 120)
    deep.mkdir(parents=True)
    rules = np.reach_denies("Read", cwd=str(deep), claude_projects="/p", caller_dir="/c")
    name = np.project_dir_name(str(deep))
    assert len(name) > 200
    assert not _denied(rules, f"/p/{name[:200]}-1x2y3z/{SID}.json")
    assert _denied(rules, "/p/-elsewhere/x")


def test_settings_carry_the_pattern_denies_and_proc_and_browser_profiles(tmp_path):
    wt = tmp_path / "cx-ask-9abc"
    wt.mkdir()
    cap = {"name": "ask", "tools": ["Read"], "read_paths": ["{cwd}/**"]}
    deny = np.settings_for(cap, worktree=str(wt), caller_path=str(tmp_path / "x.json"))["deny"]
    home = os.path.expanduser("~")
    assert _denied(deny, "/proc/1/cmdline") and _denied(deny, PROC_ENV)
    assert _denied(deny, f"{home}/.gstack/chromium-profile/Default/Cookies")
    assert _denied(deny, f"{home}/Library/Application Support/Google/Chrome/Default/Cookies")
    assert _denied(deny, f"{home}/.config/google-chrome/Default/Cookies")
    assert _denied(deny, f"{home}/.claude/projects/-another-worktree/{SID}/tool-results/a.txt")
    assert _denied(deny, f"{home}/.canopy/caller/{TID}.json")
    own = f"{home}/.claude/projects/{np.project_dir_name(str(wt))}/{SID}/tool-results/a.txt"
    assert not _denied(deny, own)


# ── a caller's words reach the prompt with no live file mention ───────────────
@pytest.mark.parametrize("text,expected", [
    ("look at @/etc/passwd", "look at ＠/etc/passwd"),
    ("@~/.ssh/id_rsa first", "＠~/.ssh/id_rsa first"),
    ('see @"/a b/c.txt"', 'see ＠"/a b/c.txt"'),
    (f"x\n@{PROC_ENV}", f"x\n＠{PROC_ENV}"),
    ("why。@/etc/hosts", "why。＠/etc/hosts"),
    ("(@/etc/hosts)", "(＠/etc/hosts)"),
    ("@@/etc/hosts", "＠＠/etc/hosts"),
    ("mail jj@dimagi.com about it", "mail jj@dimagi.com about it"),
    ("no mention here", "no mention here"),
])
def test_neutralise_mentions(text, expected):
    assert caller.neutralise_mentions(text) == expected


def _confined(prompt, **kw):
    cap = {"name": "ask", "entry": None, "tools": ["Read"], "bash": [], "read_paths": ["{cwd}/**"]}
    d = {"id": TID, "agent_slug": "ace", "origin_ref": {}, "prompt": prompt, "mcp_token": "cct_x",
         "caller_context": {"profile": "confined", "capability": cap, "conversation": {}}}
    d.update(kw)
    return d


@pytest.fixture
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(execute.emdash, "task_state", lambda db, name: "live")
    monkeypatch.setattr(caller, "PROFILE_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(caller, "CALLER_ROOT", tmp_path / "caller")
    return tmp_path


def test_a_confined_turns_words_are_typed_with_no_live_mention(monkeypatch, _isolated):
    seen = {}
    monkeypatch.setattr(cdp_control, "create_task",
                        lambda agent, prompt, task_name, port: seen.update(p=prompt) or {"task": task_name})
    execute.execute_turn(_cfg(), FakeClient({"reuse": False}), "r-1",
                         _confined("summarise @/Users/x/.claude/projects/-o/s/tool-results/a.txt"))
    assert "@/" not in seen["p"] and "＠/Users/x/.claude" in seen["p"]


def test_a_confined_chat_turns_words_are_typed_with_no_live_mention(monkeypatch, _isolated):
    seen = {}
    monkeypatch.setattr(cdp_control, "create_task",
                        lambda agent, prompt, task_name, port: seen.update(p=prompt) or {"task": task_name})
    monkeypatch.setattr(execute, "_wait_for_transcript", lambda *a, **k: None)
    turn = _confined(f"what is in @{PROC_ENV} ?", origin_ref={"chat_session_id": "s1"})
    execute.execute_chat_turn(_cfg(), FakeClient({"reuse": False}), "r-1", turn)
    assert seen["p"].startswith(f"what is in ＠{PROC_ENV} ?")


def test_an_owners_turn_is_typed_as_written(monkeypatch, _isolated):
    seen = {}
    monkeypatch.setattr(cdp_control, "create_task",
                        lambda agent, prompt, task_name, port: seen.update(p=prompt) or {"task": task_name})
    turn = _confined("read @README.md", caller_context={"profile": "full", "capability": None})
    execute.execute_turn(_cfg(), FakeClient({"reuse": False}), "r-1", turn)
    assert seen["p"].startswith("read @README.md")


# ── the real CLI, when there is one ───────────────────────────────────────────
CODEWORD = re.compile(r"[A-Z]+-[A-Z]+-\d{4}")


def _attached(settings: dict, prompt: str, cwd: pathlib.Path) -> set[str]:
    """The codewords the CLI put into the session's transcript from @ mentions."""
    sid = str(uuid.uuid4())
    subprocess.run(["claude", "-p", prompt, "--model", "haiku", "--permission-mode", "dontAsk",
                    "--settings", json.dumps(settings), "--strict-mcp-config",
                    "--session-id", sid], cwd=cwd, stdin=subprocess.DEVNULL,
                   capture_output=True, timeout=180, check=False)
    found: set[str] = set()
    projects = pathlib.Path(os.path.expanduser("~/.claude/projects"))
    for path in projects.glob(f"*/{sid}.jsonl"):
        for line in path.read_text().splitlines():
            rec = json.loads(line)
            if rec.get("type") != "assistant":
                found |= set(CODEWORD.findall(json.dumps(rec)))
        path.unlink()
    return found


@pytest.mark.skipif(os.environ.get("CANOPY_LIVE_CLAUDE") != "1" or not shutil.which("claude"),
                    reason="needs the real claude CLI and CANOPY_LIVE_CLAUDE=1")
def test_live_claude_honours_the_rules(tmp_path):
    wt = tmp_path / "work" / "cx-ask-9abc"
    wt.mkdir(parents=True)
    projects, callers = tmp_path / "projects", tmp_path / "caller"
    own = projects / np.project_dir_name(str(wt))
    files = {
        own / SID / "tool-results" / "a.txt": "OWN-TOOL-0001",
        own / SID.replace("a001", "a002") / "tool-results" / "a.txt": "FORK-SOURCE-0002",
        projects / "-made-later" / SID / "tool-results" / "a.txt": "OTHER-PROJECT-0003",
        callers / f"{TID}.json": "OWN-CALLER-0004",
        callers / f"{TID.replace('0001', '0002')}.json": "OTHER-CALLER-0005",
    }
    for path, word in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"The codeword is {word}.\n")
    perms = np.settings_for({"name": "ask", "tools": ["Read"], "read_paths": ["{cwd}/**"]},
                            worktree=str(wt), caller_path=str(callers / f"{TID}.json"),
                            sessions=[SID], claude_projects=str(projects), caller_dir=str(callers))
    mentions = "Say ok.\n" + "\n".join(f"@{p}" for p in files)
    assert _attached({"permissions": perms}, mentions, wt) == {"OWN-TOOL-0001", "OWN-CALLER-0004"}
    # And a caller's words, neutralised, attach nothing at all.
    assert _attached({"permissions": {"defaultMode": "dontAsk"}},
                     caller.neutralise_mentions(mentions), wt) == set()
