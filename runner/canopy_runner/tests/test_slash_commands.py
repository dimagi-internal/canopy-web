"""What the box reports for the chat composer's `/` autocomplete: its account's
skills and commands, its ENABLED plugins' (named `plugin:command`), and what ACP
announced — sent only when the list changes."""
import json

import pytest
from canopy_runner import slash_commands


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _home(tmp_path):
    claude = tmp_path / ".claude"
    _write(claude / "skills" / "browse" / "SKILL.md",
           "---\nname: browse\ndescription: \"Drive a \\\"real\\\" browser.\"\n---\nbody")
    _write(claude / "skills" / "internal" / "SKILL.md",
           "---\nname: internal\nuser-invocable: false\n---\n")
    _write(claude / "commands" / "deploy.md",
           "---\nargument-hint: <env>\n---\n# Deploy the app\nmore")
    hal = tmp_path / "plugins" / "hal"
    _write(hal / ".claude-plugin" / "plugin.json", json.dumps({"name": "hal"}))
    _write(hal / "skills" / "turn" / "SKILL.md",
           "---\nname: turn\ndescription: |\n  Do a full\n  Hal turn.\n---\n")
    off = tmp_path / "plugins" / "off"
    _write(off / "commands" / "x.md", "x")
    _write(claude / "plugins" / "installed_plugins.json", json.dumps({"plugins": {
        "hal@hal": [{"installPath": str(hal)}],
        "off@off": [{"installPath": str(off)}],
    }}))
    _write(claude / "settings.json", json.dumps({"enabledPlugins": {"hal@hal": True, "off@off": False}}))
    return tmp_path


def test_scan_reads_skills_commands_and_enabled_plugins(tmp_path, monkeypatch):
    monkeypatch.setattr(slash_commands, "_acp_seen", {})
    rows = {c["name"]: c for c in slash_commands.scan(_home(tmp_path))}
    assert sorted(rows) == ["browse", "deploy", "hal:turn", "internal"]
    assert rows["internal"]["internal"] is True
    assert "internal" not in rows["browse"] and "internal" not in rows["hal:turn"]
    assert rows["browse"]["description"] == 'Drive a "real" browser.'
    assert rows["deploy"] == {"name": "deploy", "description": "Deploy the app",
                              "args_hint": "<env>", "source": "user"}
    assert rows["hal:turn"]["description"] == "Do a full Hal turn."
    assert rows["hal:turn"]["source"] == "plugin"


def test_what_acp_announced_is_included_and_wins_a_clash(tmp_path, monkeypatch):
    monkeypatch.setattr(slash_commands, "_acp_seen", {})
    slash_commands.remember_acp([
        {"name": "compact", "description": "Compact the conversation", "input": {"hint": "focus"}},
        {"name": "hal:turn", "description": "from ACP"},
        {"name": "bad name"},
    ])
    rows = {c["name"]: c for c in slash_commands.scan(_home(tmp_path))}
    assert rows["compact"] == {"name": "compact", "description": "Compact the conversation",
                               "args_hint": "focus", "source": "acp"}
    assert rows["hal:turn"]["description"] == "from ACP"
    assert "bad name" not in rows


def _plugin_skill(home, name, header):
    hal = home / "plugins" / "hal"
    _write(hal / "skills" / name / "SKILL.md", f"---\nname: {name}\n{header}\ndescription: words\n---\n")


@pytest.mark.parametrize("header", [
    "user-invocable: false",
    "user-invocable: false   # internal: Claude invokes it; it is not a human-launchable /command",
    "user-invocable: False",
    'user-invocable: "false"  # quoted, then a comment',
])
def test_every_way_of_writing_the_marker_says_internal(tmp_path, monkeypatch, header):
    # The fleet's skills write the marker with a trailing comment; read as raw text
    # that value was not "false", so all of them stayed on the `/` menu.
    monkeypatch.setattr(slash_commands, "_acp_seen", {})
    home = _home(tmp_path)
    _plugin_skill(home, "shipping", header)
    rows = {c["name"]: c for c in slash_commands.scan(home)}
    assert rows["hal:shipping"]["internal"] is True
    assert rows["hal:shipping"]["description"] == "words"


def test_a_comment_is_only_a_comment_where_yaml_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(slash_commands, "_acp_seen", {})
    home = _home(tmp_path)
    hal = home / "plugins" / "hal" / "skills"
    _write(hal / "a" / "SKILL.md", "---\nname: a\ndescription: \"keeps # this\"  # not this\n---\n")
    _write(hal / "b" / "SKILL.md", "---\nname: b\ndescription: c#sharp stays\n---\n")
    _write(hal / "c" / "SKILL.md", "---\nname: c\ndescription: >  # folded\n  two\n  lines\n---\n")
    _write(hal / "d" / "SKILL.md", "---\nname: d\nuser-invocable: true  # false\n---\n")
    rows = {c["name"]: c for c in slash_commands.scan(home)}
    assert rows["hal:a"]["description"] == "keeps # this"
    assert rows["hal:b"]["description"] == "c#sharp stays"
    assert rows["hal:c"]["description"] == "two lines"
    assert "internal" not in rows["hal:d"]


def test_a_file_saying_internal_beats_what_acp_remembers(tmp_path, monkeypatch):
    # What a session announced stays in `_acp_seen` after the skill is marked, and
    # ACP otherwise wins a clash.
    monkeypatch.setattr(slash_commands, "_acp_seen", {})
    home = _home(tmp_path)
    _plugin_skill(home, "shipping", "user-invocable: false  # internal")
    slash_commands.remember_acp([{"name": "hal:shipping", "description": "from before the marker"},
                                 {"name": "hal:turn", "description": "from ACP"}])
    rows = {c["name"]: c for c in slash_commands.scan(home)}
    assert rows["hal:shipping"]["internal"] is True and rows["hal:shipping"]["source"] == "plugin"
    assert rows["hal:turn"]["source"] == "acp" and "internal" not in rows["hal:turn"]


def _stamp_hal(home, updated="2026-11-04T10:00:00.000Z"):
    path = home / ".claude" / "plugins" / "installed_plugins.json"
    data = json.loads(path.read_text())
    data["plugins"]["hal@hal"][0]["lastUpdated"] = updated
    path.write_text(json.dumps(data))


def test_a_plugins_commands_say_when_it_was_last_updated(tmp_path, monkeypatch):
    monkeypatch.setattr(slash_commands, "_acp_seen", {})
    home = _home(tmp_path)
    _stamp_hal(home)
    slash_commands.remember_acp([{"name": "hal:turn", "description": "from ACP"},
                                 {"name": "hal:deleted-from-the-repo"}, {"name": "compact"}])
    rows = {c["name"]: c for c in slash_commands.scan(home)}
    # On disk: stamped, even when ACP's words won the clash.
    assert rows["hal:turn"]["plugin_updated_at"] == "2026-11-04T10:00:00.000Z"
    assert rows["hal:turn"]["source"] == "acp"
    # Only ACP's memory, and anything that is not a plugin's: no stamp.
    assert "plugin_updated_at" not in rows["hal:deleted-from-the-repo"]
    assert "plugin_updated_at" not in rows["compact"] and "plugin_updated_at" not in rows["browse"]


def test_a_new_install_of_the_same_list_is_a_change_worth_reporting(tmp_path, monkeypatch):
    monkeypatch.setattr(slash_commands, "_acp_seen", {})
    home = _home(tmp_path)
    before = slash_commands.digest(slash_commands.scan(home))
    _stamp_hal(home)
    assert slash_commands.digest(slash_commands.scan(home)) != before


def test_reports_only_when_the_list_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(slash_commands, "_acp_seen", {})
    monkeypatch.setattr(slash_commands, "_state", {"at": 0.0, "sent": ""})
    home, sent, t = _home(tmp_path), [], [1000.0]

    def report():
        return slash_commands.maybe_report(sent.append, home=home, now_fn=lambda: t[0])

    assert report() and len(sent) == 1
    t[0] += slash_commands.INTERVAL_SECONDS
    assert not report()  # unchanged
    _write(home / ".claude" / "commands" / "new.md", "New command")
    t[0] += 10
    assert not report()  # not due yet
    t[0] += slash_commands.INTERVAL_SECONDS
    assert report() and "new" in [c["name"] for c in sent[-1]]


def test_a_failed_send_is_retried_at_the_next_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(slash_commands, "_acp_seen", {})
    monkeypatch.setattr(slash_commands, "_state", {"at": 0.0, "sent": ""})
    home, t = _home(tmp_path), [1000.0]

    def boom(_):
        raise RuntimeError("down")

    assert not slash_commands.maybe_report(boom, home=home, now_fn=lambda: t[0])
    t[0] += slash_commands.INTERVAL_SECONDS
    sent = []
    assert slash_commands.maybe_report(sent.append, home=home, now_fn=lambda: t[0])
