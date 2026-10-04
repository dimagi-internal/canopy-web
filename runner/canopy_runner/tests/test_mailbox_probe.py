"""Which mailboxes a box reads is DISCOVERED, not hand-configured (canopy-web#1087).

A runner with `"mailboxes": {}` in runner.json — what pairing writes — read no
mail at all, while the push doorbell kept ringing it for every message. These pin
the replacement: candidates from canopy-web, tokens from `gog auth tokens list`,
proof from a one-message search, and runner.json entries kept as overrides.

No test here shells out: every gog call goes through an injected runner.
"""
import json
import logging
import subprocess
from types import SimpleNamespace

import pytest
from canopy_runner import mailbox_probe

ACE = "ace@dimagi-ai.com"
ECHO = "echo@dimagi-ai.com"


class Gog:
    """A fake `subprocess.run` for gog: a token store and which pairs can search."""

    def __init__(self, tokens=(), works=(), tokens_rc=0, tokens_err=""):
        self.tokens = list(tokens)          # [(client, mailbox)]
        self.works = set(works)             # {(client, mailbox)} whose search succeeds
        self.tokens_rc = tokens_rc
        self.tokens_err = tokens_err
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[:4] == ["gog", "auth", "tokens", "list"]:
            out = json.dumps({"keys": [f"token:{c}:{m}" for c, m in self.tokens]})
            return subprocess.CompletedProcess(argv, self.tokens_rc,
                                               out if not self.tokens_rc else "",
                                               self.tokens_err)
        if argv[:3] == ["gog", "gmail", "search"]:
            account = argv[argv.index("--account") + 1]
            client = argv[argv.index("--client") + 1]
            assert argv[argv.index("--max") + 1] == "1", "the proof is ONE message"
            if (client, account) in self.works:
                return subprocess.CompletedProcess(argv, 0, '{"threads": []}', "")
            return subprocess.CompletedProcess(argv, 1, "", f"No auth for gmail {account}")
        raise AssertionError(f"unexpected gog call {argv}")

    def searches(self):
        return [(a[a.index("--client") + 1], a[a.index("--account") + 1])
                for a in self.calls if a[:3] == ["gog", "gmail", "search"]]


class FakeClient:
    def __init__(self, rows=None, fail=False):
        self.rows = rows if rows is not None else [
            {"address": ACE, "agent_slug": "ace", "watch_topic": ""},
            {"address": ECHO, "agent_slug": "echo", "watch_topic": "projects/p/topics/t"},
        ]
        self.fail = fail
        self.calls = 0

    def runner_mailboxes(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("canopy-web unreachable")
        return self.rows


def _cfg(mailboxes=None, every=600):
    return SimpleNamespace(mailboxes=mailboxes or {}, mailbox_probe_seconds=every)


@pytest.fixture(autouse=True)
def _armed():
    mailbox_probe.reset()   # undo the conftest park: these tests drive the probe
    yield


def test_a_token_that_searches_makes_the_mailbox_readable(caplog):
    gog = Gog(tokens=[("canopy", ACE)], works=[("canopy", ACE)])
    caplog.set_level(logging.INFO, logger="canopy_runner")
    assert mailbox_probe.maybe_probe(_cfg(), FakeClient(), now_fn=lambda: 0, runner=gog)
    assert mailbox_probe.effective_mailboxes(_cfg()) == {
        "ace": {"account": ACE, "client": "canopy"}}
    assert mailbox_probe.readable() == [ACE]
    assert f"mailbox {ACE}: readable via client canopy" in caplog.text
    assert f"mailbox {ECHO}: no token" in caplog.text, "one line per mailbox, every probe"


def test_a_token_whose_search_fails_is_not_readable(caplog):
    gog = Gog(tokens=[("ace", ACE)], works=[])
    caplog.set_level(logging.INFO, logger="canopy_runner")
    mailbox_probe.maybe_probe(_cfg(), FakeClient(), now_fn=lambda: 0, runner=gog)
    assert mailbox_probe.readable() == []
    assert mailbox_probe.effective_mailboxes(_cfg()) == {}
    assert f"mailbox {ACE}: token but search failed: ace: No auth" in caplog.text


def test_the_shared_client_is_tried_first_then_the_next_that_works():
    """echo's live token can sit under `echo` while `canopy` is stale: the first
    client that ANSWERS is the one to read with."""
    gog = Gog(tokens=[("echo", ECHO), ("canopy", ECHO), ("zzz", ECHO)], works=[("echo", ECHO)])
    mailbox_probe.maybe_probe(_cfg(), FakeClient(), now_fn=lambda: 0, runner=gog)
    assert gog.searches() == [("canopy", ECHO), ("echo", ECHO)]
    assert mailbox_probe.effective_mailboxes(_cfg())["echo"]["client"] == "echo"


def test_a_keyring_failure_reports_not_readable_and_never_raises(caplog):
    """A file keyring with no TTY makes `gog auth tokens list` fail. That box
    cannot read mail, and must say so ([]), not crash and not claim unknown."""
    gog = Gog(tokens_rc=1, tokens_err="no TTY available for keyring file backend password prompt")
    caplog.set_level(logging.INFO, logger="canopy_runner")
    mailbox_probe.maybe_probe(_cfg(), FakeClient(), now_fn=lambda: 0, runner=gog)
    assert mailbox_probe.readable() == []
    assert "token store unreadable: no TTY" in caplog.text
    assert gog.searches() == [], "no point searching with a keyring we cannot open"


def test_gog_missing_is_not_a_crash():
    def runner(argv, **kw):
        raise FileNotFoundError("gog")
    mailbox_probe.maybe_probe(_cfg(), FakeClient(), now_fn=lambda: 0, runner=runner)
    assert mailbox_probe.readable() == []


def test_a_runner_json_entry_overrides_its_agent_and_is_probed_too():
    """Nothing existing breaks: a hand-set entry (custom query, pinned client)
    wins for its agent, and its readability is still reported."""
    override = {"ace": {"account": ACE, "client": "ace", "query": "in:inbox from:@dimagi.com"}}
    gog = Gog(tokens=[("canopy", ACE), ("canopy", ECHO)],
              works=[("ace", ACE), ("canopy", ECHO), ("canopy", ACE)])
    cfg = _cfg(override)
    mailbox_probe.maybe_probe(cfg, FakeClient(), now_fn=lambda: 0, runner=gog)
    boxes = mailbox_probe.effective_mailboxes(cfg)
    assert boxes["ace"] == override["ace"]
    assert boxes["echo"] == {"account": ECHO, "client": "canopy"}
    assert mailbox_probe.readable() == [ACE, ECHO]
    assert ("canopy", ACE) not in gog.searches(), "the override's agent is not re-discovered"


def test_an_empty_runner_json_entry_overrides_nothing():
    cfg = _cfg({"ace": {}})
    gog = Gog(tokens=[("canopy", ACE)], works=[("canopy", ACE)])
    mailbox_probe.maybe_probe(cfg, FakeClient(), now_fn=lambda: 0, runner=gog)
    assert mailbox_probe.effective_mailboxes(cfg) == {"ace": {"account": ACE, "client": "canopy"}}


def test_unknown_until_the_first_probe_then_on_a_timer():
    """None until there is an answer — the server keeps ringing an unknown box —
    then one probe per interval, not per tick."""
    assert mailbox_probe.readable() is None
    gog = Gog(tokens=[("canopy", ACE)], works=[("canopy", ACE)])
    client = FakeClient()
    assert mailbox_probe.maybe_probe(_cfg(), client, now_fn=lambda: 100, runner=gog)
    assert not mailbox_probe.maybe_probe(_cfg(), client, now_fn=lambda: 699, runner=gog)
    assert mailbox_probe.maybe_probe(_cfg(), client, now_fn=lambda: 700, runner=gog)
    assert client.calls == 2


def test_an_unreachable_server_keeps_the_last_answer_and_retries_soon():
    gog = Gog(tokens=[("canopy", ACE)], works=[("canopy", ACE)])
    mailbox_probe.maybe_probe(_cfg(), FakeClient(), now_fn=lambda: 0, runner=gog)
    down = FakeClient(fail=True)
    assert not mailbox_probe.maybe_probe(_cfg(), down, now_fn=lambda: 600, runner=gog)
    assert mailbox_probe.readable() == [ACE], "a failed fetch never blanks a working map"
    assert mailbox_probe.maybe_probe(_cfg(), FakeClient(), now_fn=lambda: 600 + 60, runner=gog)


def test_an_older_server_without_agent_slugs_discovers_nothing():
    """Rows without `agent_slug` cannot be mapped to an agent; only overrides run."""
    gog = Gog(tokens=[("canopy", ACE)], works=[("canopy", ACE)])
    mailbox_probe.maybe_probe(_cfg(), FakeClient(rows=[{"address": ACE, "watch_topic": "t"}]),
                              now_fn=lambda: 0, runner=gog)
    assert mailbox_probe.effective_mailboxes(_cfg()) == {}


def test_token_keys_are_parsed_like_canopy_does():
    gog = Gog(tokens=[("canopy", "Ace@Dimagi-AI.com")])
    pairs, why = mailbox_probe.token_pairs(runner=gog)
    assert pairs == {("canopy", ACE)} and why == ""


# ── the loop uses the discovered map ─────────────────────────────────────────


def test_the_inbox_trigger_reads_the_discovered_mailboxes(monkeypatch, tmp_path):
    from canopy_runner import inbox as inbox_mod
    from canopy_runner import main

    gog = Gog(tokens=[("canopy", ACE)], works=[("canopy", ACE)])
    mailbox_probe.maybe_probe(_cfg(), FakeClient(), now_fn=lambda: 0, runner=gog)
    checked = []
    monkeypatch.setattr(inbox_mod, "check_inbox",
                        lambda client, agent, **kw: checked.append((agent, kw["mailbox"],
                                                                    kw["gog_client"])) or {
                            "new": [], "seen": []})
    cfg = SimpleNamespace(mailboxes={}, state_path=str(tmp_path / "runner-state.json"),
                          inbox_poll_seconds=300, inbox_max_threads=8)
    main._maybe_check_inboxes(cfg, client=None, now_fn=lambda: 10_000)
    assert checked == [("ace", ACE, "canopy")], "runner.json `{}` no longer means no mail"


def test_the_heartbeat_carries_the_readable_list_only_once_known():
    from canopy_runner.client import Client

    sent = []
    c = Client("https://x", "t")
    c._call = lambda method, path, body=None: (sent.append(body), (200, {}))[1]
    c.heartbeat("r1", [], code_branch="", code_version="", code_sha="",
                code_committed_at=0, profiles=0)
    assert "mailboxes_readable" not in sent[-1]
    c.heartbeat("r1", [], code_branch="", code_version="", code_sha="",
                code_committed_at=0, profiles=0, mailboxes_readable=[ACE])
    assert sent[-1]["mailboxes_readable"] == [ACE]


def test_the_watch_rearm_ignores_rows_served_without_a_topic(monkeypatch, tmp_path):
    """runner-mailboxes now serves topic-less rows (they are probe candidates);
    the re-arm must keep treating a blank topic as "arm nothing"."""
    from canopy_runner import gmail_watch, main

    armed = []
    monkeypatch.setattr(gmail_watch, "arm", lambda *a, **k: armed.append(a))
    gog = Gog(tokens=[("canopy", ACE)], works=[("canopy", ACE)])
    mailbox_probe.maybe_probe(_cfg(), FakeClient(), now_fn=lambda: 0, runner=gog)
    cfg = SimpleNamespace(mailboxes={}, state_path=str(tmp_path / "runner-state.json"),
                          gmail_watch_topic="")
    main._maybe_rearm_watches(cfg, FakeClient())
    assert armed == [], "ace's row has no topic, and echo is not readable here"



def test_the_fleet_clients_are_tried_before_a_client_named_after_the_agent():
    """canopy and canopy-web are one app (canopy-web's "Connect Google mailbox"
    button mints under the second); a legacy client named after the agent comes after."""
    from canopy_runner.mailbox_probe import _client_order
    assert _client_order({"echo", "canopy-web", "zz"}, "echo") == ["canopy-web", "echo", "zz"]
    assert _client_order({"canopy", "canopy-web", "echo"}, "echo") == ["canopy", "canopy-web", "echo"]
