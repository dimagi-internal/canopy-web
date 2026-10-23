"""What the box reports for the chat composer's `/` autocomplete: its account's
skills and commands, its ENABLED plugins' (named `plugin:command`), and what ACP
announced — sent only when the list changes."""
import json

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
    assert sorted(rows) == ["browse", "deploy", "hal:turn"]
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
