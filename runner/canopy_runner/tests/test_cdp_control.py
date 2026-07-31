"""Wrapper-level tests for the emdash CDP control (the sidecar itself needs a live
emdash on the debug port — validated separately)."""
import json
import urllib.error
from types import SimpleNamespace

import pytest

from canopy_runner import cdp_control


def _fake_run(stdout, returncode=0, stderr=""):
    def run(cmd, capture_output, text, timeout):
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)
    return run


def test_list_tasks_parses(monkeypatch):
    monkeypatch.setattr(cdp_control.subprocess, "run",
                        _fake_run(json.dumps({"ok": True, "tasks": ["a", "b"], "projects": ["echo"]})))
    r = cdp_control.list_tasks()
    assert r["tasks"] == ["a", "b"] and r["projects"] == ["echo"]


def test_open_send_ok(monkeypatch):
    monkeypatch.setattr(cdp_control.subprocess, "run",
                        _fake_run(json.dumps({"ok": True, "action": "sent", "task": "T"})))
    assert cdp_control.open_and_send("T", "hi there")["action"] == "sent"


def test_interrupt_calls_run_with_task_and_port(monkeypatch):
    """interrupt() must reach the sidecar's `interrupt` command with {task, port} — the
    same call shape open_and_send uses (matching _run's convention: command name + a
    plain args dict; port default matches open_and_send's default of 9222)."""
    captured = {}

    def fake_run(command, args, **kwargs):
        captured["command"] = command
        captured["args"] = args
        return {"ok": True, "task": args["task"]}

    monkeypatch.setattr(cdp_control, "_run", fake_run)
    result = cdp_control.interrupt("my-task")
    assert captured["command"] == "interrupt"
    assert captured["args"] == {"task": "my-task", "port": 9222}
    assert result["task"] == "my-task"


def test_interrupt_passes_a_non_default_port(monkeypatch):
    captured = {}
    monkeypatch.setattr(cdp_control, "_run",
                        lambda command, args, **k: captured.update(args=args) or {"ok": True})
    cdp_control.interrupt("my-task", port=9333)
    assert captured["args"] == {"task": "my-task", "port": 9333}


def test_interrupt_ok(monkeypatch):
    monkeypatch.setattr(cdp_control.subprocess, "run",
                        _fake_run(json.dumps({"ok": True, "task": "T"})))
    assert cdp_control.interrupt("T")["task"] == "T"


def test_sidecar_error_raises_cdperror(monkeypatch):
    monkeypatch.setattr(cdp_control.subprocess, "run",
                        _fake_run(json.dumps({"ok": False, "error": 'no existing task "X"'})))
    with pytest.raises(cdp_control.CDPError, match="no existing task"):
        cdp_control.open_and_send("X", "hi")


def test_non_json_output_raises(monkeypatch):
    monkeypatch.setattr(cdp_control.subprocess, "run", _fake_run("kaboom not json", stderr="trace"))
    with pytest.raises(cdp_control.CDPError, match="non-JSON"):
        cdp_control.list_tasks()


def test_node_missing_raises(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(cdp_control.subprocess, "run", boom)
    with pytest.raises(cdp_control.CDPError, match="node not found"):
        cdp_control.list_tasks()


def test_host_id_has_user_and_host():
    h = cdp_control.host_id()
    assert "@" in h and len(h) > 2


# --------------------------------------------------------------------------------------
# cdp_healthy — the claim preflight; a green probe means create/reuse will connect
# --------------------------------------------------------------------------------------


class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_cdp_healthy_true_on_200(monkeypatch):
    monkeypatch.setattr(cdp_control.urllib.request, "urlopen",
                        lambda url, timeout: _Resp(200))
    assert cdp_control.cdp_healthy(port=9222) is True


def test_cdp_healthy_false_on_non_200(monkeypatch):
    monkeypatch.setattr(cdp_control.urllib.request, "urlopen",
                        lambda url, timeout: _Resp(500))
    assert cdp_control.cdp_healthy() is False


def test_cdp_healthy_false_on_connection_refused(monkeypatch):
    def refused(url, timeout):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(cdp_control.urllib.request, "urlopen", refused)
    assert cdp_control.cdp_healthy() is False


def test_cdp_healthy_false_on_timeout(monkeypatch):
    def slow(url, timeout):
        raise TimeoutError("timed out")  # a TimeoutError IS an OSError — must be caught
    monkeypatch.setattr(cdp_control.urllib.request, "urlopen", slow)
    assert cdp_control.cdp_healthy() is False


# --------------------------------------------------------------------------------------
# host_id — the reuse OWNERSHIP key; must be stable or session continuity silently dies
# --------------------------------------------------------------------------------------

def test_host_id_pins_the_first_value_it_computes(monkeypatch, tmp_path):
    pin = tmp_path / "host-id"
    monkeypatch.setattr(cdp_control, "HOST_ID_PATH", pin)
    monkeypatch.setattr(cdp_control.socket, "gethostname", lambda: "Jonathans-MacBook-Pro.local")
    monkeypatch.setattr(cdp_control.getpass, "getuser", lambda: "jjackson")
    assert cdp_control.host_id() == "jjackson@Jonathans-MacBook-Pro.local"
    assert pin.read_text().strip() == "jjackson@Jonathans-MacBook-Pro.local"


def test_host_id_survives_a_macos_hostname_flap(monkeypatch, tmp_path):
    """THE bug (proved live 2026-07-15): macOS flaps gethostname() between the Bonjour
    and DHCP names. reusable_by() compares this value by EQUALITY, so every flap
    orphaned every SessionLink recorded under the other name — reuse silently returned
    false and each thread got a fresh cold session, with no error logged anywhere."""
    pin = tmp_path / "host-id"
    monkeypatch.setattr(cdp_control, "HOST_ID_PATH", pin)
    monkeypatch.setattr(cdp_control.getpass, "getuser", lambda: "jjackson")

    monkeypatch.setattr(cdp_control.socket, "gethostname", lambda: "Jonathans-MacBook-Pro.local")
    first = cdp_control.host_id()
    # ...macOS renames the host out from under us (observed 3x each way in one day)
    monkeypatch.setattr(cdp_control.socket, "gethostname", lambda: "Jonathans-MBP.localdomain")
    assert cdp_control.host_id() == first      # ownership key unchanged -> reuse survives


def test_host_id_degrades_to_the_live_value_when_the_pin_is_unwritable(monkeypatch, tmp_path):
    """An unwritable pin must not crash the runner — fall back to the flappy live value
    (no worse than before) rather than refusing to heartbeat."""
    unwritable = tmp_path / "no-such-dir" / "x" / "host-id"
    monkeypatch.setattr(cdp_control, "HOST_ID_PATH", unwritable)
    monkeypatch.setattr(cdp_control.socket, "gethostname", lambda: "H")
    monkeypatch.setattr(cdp_control.getpass, "getuser", lambda: "u")
    def boom(*a, **k):
        raise OSError("read-only fs")
    monkeypatch.setattr(cdp_control.Path, "mkdir", boom)
    assert cdp_control.host_id() == "u@H"


def test_host_id_ignores_a_blank_pin(monkeypatch, tmp_path):
    pin = tmp_path / "host-id"
    pin.write_text("   \n")
    monkeypatch.setattr(cdp_control, "HOST_ID_PATH", pin)
    monkeypatch.setattr(cdp_control.socket, "gethostname", lambda: "H2")
    monkeypatch.setattr(cdp_control.getpass, "getuser", lambda: "u2")
    assert cdp_control.host_id() == "u2@H2"


def test_read_terminal_returns_the_rendered_text(monkeypatch):
    """The screen is the only place an emdash session's dialog exists."""
    calls = {}

    def fake_run(command, args, **kw):
        calls["command"], calls["args"] = command, args
        return {"ok": True, "task": args["task"], "text": " Do you want to proceed?\n ❯ 1. Yes\n   2. No"}

    monkeypatch.setattr(cdp_control, "_run", fake_run)
    text = cdp_control.read_terminal("agent-x")
    assert calls["command"] == "read-term"
    assert calls["args"]["task"] == "agent-x"
    assert "Do you want to proceed?" in text


def test_read_terminal_tolerates_an_empty_screen(monkeypatch):
    monkeypatch.setattr(cdp_control, "_run", lambda *a, **k: {"ok": True})
    assert cdp_control.read_terminal("agent-x") == ""


def test_send_keys_passes_each_key_separately(monkeypatch):
    """Not insertText: a stray digit typed into the PROMPT of a session that is
    not showing a menu is worse than a failed answer."""
    calls = {}
    monkeypatch.setattr(cdp_control, "_run",
                        lambda c, a, **k: (calls.update(command=c, args=a), {"ok": True})[1])
    cdp_control.send_keys("agent-x", ["3", "\r"])
    assert calls["command"] == "send-keys"
    assert calls["args"]["keys"] == ["3", "\r"]


# --- sidecar provisioning (spec 2026-07-28: the runner is installed, so its node
# deps cannot ship in the wheel and are provisioned per-install) -----------------


def test_ensure_sidecar_deps_is_a_noop_when_already_present(monkeypatch):
    """Called at every daemon start, so the common path must not shell out."""
    monkeypatch.setattr(cdp_control, "sidecar_deps_installed", lambda: True)
    monkeypatch.setattr(cdp_control.subprocess, "run",
                        lambda *a, **k: pytest.fail("must not run npm when deps are present"))
    cdp_control.ensure_sidecar_deps()


def test_ensure_sidecar_deps_installs_next_to_the_sidecar(monkeypatch):
    """Deps must land BESIDE the .mjs, not in a shared directory: NODE_PATH is
    consulted for CommonJS only and this sidecar is `type: module`, so Node
    resolves its bare import by walking up from the file."""
    calls = {}
    state = {"present": False}

    def fake_run(cmd, cwd=None, **kwargs):
        calls["cmd"] = cmd
        calls["cwd"] = cwd
        state["present"] = True
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(cdp_control, "sidecar_deps_installed", lambda: state["present"])
    monkeypatch.setattr(cdp_control.subprocess, "run", fake_run)
    cdp_control.ensure_sidecar_deps()

    assert calls["cmd"][:2] == ["npm", "install"]
    assert calls["cwd"] == str(cdp_control.SIDECAR.parent)


def test_ensure_sidecar_deps_reports_a_missing_npm_actionably(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("npm")

    monkeypatch.setattr(cdp_control, "sidecar_deps_installed", lambda: False)
    monkeypatch.setattr(cdp_control.subprocess, "run", boom)
    with pytest.raises(cdp_control.CDPError) as exc:
        cdp_control.ensure_sidecar_deps()
    assert "install-sidecar" in str(exc.value)


def test_ensure_sidecar_deps_fails_when_npm_claims_success_but_installs_nothing(monkeypatch):
    """A zero exit is not proof. Re-check the dep, or the daemon starts believing
    it is provisioned and dies later at the first CDP call instead."""
    monkeypatch.setattr(cdp_control, "sidecar_deps_installed", lambda: False)
    monkeypatch.setattr(cdp_control.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout="", stderr="", returncode=0))
    with pytest.raises(cdp_control.CDPError):
        cdp_control.ensure_sidecar_deps()


# -- the evaluated JS must be valid, which node --check cannot tell us ---------

def _evaluate_bodies():
    """Every string this sidecar hands to page.evaluate, as Playwright sees it."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "canopy_runner" / "cdp"
           / "emdash_control.mjs").read_text()
    helpers = {name: (re.search(rf"const {name} = String\.raw`([\s\S]*?)`;", src) or [None, ""])[1]
               for name in ("ACTIVE_TERM_FN", "COMPOSER_FN")}
    bodies = []
    for m in re.findall(r"page\.evaluate\(String\.raw`([\s\S]*?)`\)", src):
        for name, helper_src in helpers.items():
            m = m.replace("${" + name + "}", helper_src)
        bodies.append(m)
    return bodies


def test_every_evaluate_string_is_valid_javascript():
    """`node --check` passes on a file whose TEMPLATE contents are broken, because
    the template is a valid string literal either way. What Playwright receives is
    the PROCESSED text — and a plain template ate the escapes: `\\n` became a real
    newline (unterminated string -> "SyntaxError: Invalid or unexpected token" at
    runtime) and `\\s` collapsed to a literal `s`, turning every `\\s*` into "zero
    or more letter s". The collision guard silently stopped matching and nothing
    in CI noticed. String.raw is what prevents it; this test is what catches a
    regression.
    """
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:                       # CI without node: nothing to assert
        return
    bodies = _evaluate_bodies()
    assert bodies, "no page.evaluate(String.raw`…`) sites found — did the shape change?"
    for i, body in enumerate(bodies, start=1):
        check = subprocess.run(
            [node, "-e", "new Function('return ' + require('fs').readFileSync(0, 'utf8'))"],
            input=body, capture_output=True, text=True, timeout=30)
        assert check.returncode == 0, f"evaluate #{i} is invalid JS: {check.stderr[:300]}"


def test_the_evaluate_sites_use_string_raw():
    """A plain backtick here reintroduces the escape-eating bug."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "canopy_runner" / "cdp"
           / "emdash_control.mjs").read_text()
    assert "page.evaluate(`" not in src, "use String.raw` for evaluate source"
    assert len(re.findall(r"page\.evaluate\(String\.raw`", src)) >= 4


# -- composer detection: the collision guard's core, exercised under node ------
#
# The scan is a pure function over the rendered viewport rows (COMPOSER_FN in
# emdash_control.mjs), so these fixtures ARE the bug reports: the fresh-session
# layout below is the measured live state that made readLine() return '' for a
# composer holding text (canopy-web #521 — composer at row 13 of 38, rows 24-37
# all empty, and the old scan only looked at the last 14 rows).

def _composer_fn_source():
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "canopy_runner" / "cdp"
           / "emdash_control.mjs").read_text()
    m = re.search(r"const COMPOSER_FN = String\.raw`([\s\S]*?)`;", src)
    assert m, "COMPOSER_FN not found in emdash_control.mjs"
    return m.group(1)


def _composer(rows):
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    script = (_composer_fn_source()
              + "\nconst rows = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
              + "\nprocess.stdout.write(JSON.stringify(composerText(rows)));")
    out = subprocess.run([node, "-e", script], input=json.dumps(rows),
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, f"composerText crashed: {out.stderr[:300]}"
    return json.loads(out.stdout)


_RULE = "─" * 90
_STATUS = "  ⏵⏵ bypass permissions on (shift+tab to cycle)"


def _fresh_session(*composer_rows):
    """Measured live 2026-07-29: a fresh session draws its TUI at the TOP of the
    pane — composer around row 13 of 38, everything below the status bar empty."""
    return (["  word: ready. Then do nothing further and wait.", "",
             "⏺ ready", "", "✻ Worked for 3s", "",
             _RULE, *composer_rows, _RULE, _STATUS] + [""] * 24)


def test_fresh_session_planted_text_is_found():
    # THE #521 repro: planted text in a fresh session's composer, far above the
    # bottom of the pane. The old last-14-rows scan returned '' here.
    r = _composer(_fresh_session("❯ hi "))
    assert r == {"found": True, "text": "hi"}


def test_filled_session_bottom_composer():
    rows = (["⏺ some transcript"] * 30
            + [_RULE, "❯ okay so any changes we should do here?", _RULE,
               _STATUS + " · esc to interrupt", "", ""])
    r = _composer(rows)
    assert r == {"found": True, "text": "okay so any changes we should do here?"}


def test_empty_composer_reads_empty_not_missing():
    # An empty composer is '❯' + NBSP + trailing spaces. That is EMPTY (send),
    # never NOT-VISIBLE (refuse) — conflating them would wedge every send.
    r = _composer(_fresh_session("❯   "))
    assert r == {"found": True, "text": ""}


def test_wrapped_composer_joins_rows():
    r = _composer(_fresh_session("❯ a long unsent line that", "  wraps onto more rows"))
    assert r["found"] is True
    assert r["text"] == "a long unsent line that wraps onto more rows"


def test_scrollback_prompt_without_rule_is_not_the_composer():
    # Historical user messages also render with a ❯ prefix, but never with a box
    # rule directly above. A stale frame showing only transcript must read as
    # NOT VISIBLE, not as a collision with an old message.
    rows = ["⏺ doing things", "", "❯ an old user message in scrollback",
            "", "⏺ more output", "✻ Worked for 12m 35s"]
    r = _composer(rows)
    assert r == {"found": False, "text": ""}


def test_clipped_frame_has_no_composer():
    # The live state observed on a just-switched-to pane: transcript ends mid-
    # frame, no composer, no status bar. Must be NOT VISIBLE so open-send fails
    # closed instead of blind-appending into the off-screen composer.
    rows = ["⏺ transcript"] * 38
    r = _composer(rows)
    assert r == {"found": False, "text": ""}


def test_legacy_gt_marker_still_matches():
    r = _composer(_fresh_session("> typed with the old marker"))
    assert r == {"found": True, "text": "typed with the old marker"}


# --------------------------------------------------------------------------------------
# close_task — deletes an emdash task over CDP; absent is success, not TASK_NOT_FOUND
# --------------------------------------------------------------------------------------


def test_close_task_passes_the_task_and_port_through(monkeypatch):
    calls = {}

    def fake_run(command, args, **kwargs):
        calls["command"] = command
        calls["args"] = args
        return {"ok": True, "action": "deleted"}

    monkeypatch.setattr(cdp_control, "_run", fake_run)
    assert cdp_control.close_task("ddd", port=9333) == {"ok": True, "action": "deleted"}
    assert calls["command"] == "close-task"
    assert calls["args"] == {"task": "ddd", "port": 9333}


def test_close_task_reports_an_already_gone_task_as_absent(monkeypatch):
    """A double-tap from the phone, or a task a human just deleted in emdash, both
    land here. Neither is a failure — the desired state already holds."""
    monkeypatch.setattr(
        cdp_control, "_run", lambda c, a, **k: {"ok": True, "action": "absent"}
    )
    assert cdp_control.close_task("gone")["action"] == "absent"
