"""The installer's commands on the CLI, and `run --log-file` (pythonw has no console)."""
from __future__ import annotations

import logging
import sys

from canopy_runner import main


def test_the_new_commands_parse():
    p = main._build_parser()
    a = p.parse_args(["setup", "--no-pair", "--base-url", "https://x.test"])
    assert (a.command, a.no_pair, a.base_url) == ("setup", True, "https://x.test")
    a = p.parse_args(["self-update", "--if-stale", "--config", "c.json", "--log-file", "u.log"])
    assert (a.command, a.if_stale, a.config, a.log_file) == ("self-update", True, "c.json", "u.log")
    a = p.parse_args(["pair", "--browser"])
    assert a.browser and not a.allow_software
    a = p.parse_args(["run", "--config", "c.json", "--log-file", "r.log"])
    assert a.log_file == "r.log"


def test_log_file_takes_logging_stdout_and_stderr(tmp_path, monkeypatch):
    log = tmp_path / "logs" / "runner.log"
    old = log.with_name("runner.log")
    log.parent.mkdir()
    old.write_text("x" * 10)
    monkeypatch.setattr(main, "LOG_ROLL_BYTES", 5)
    saved = sys.stdout, sys.stderr, list(logging.getLogger().handlers)
    try:
        main._log_to_file(str(log))
        logging.getLogger("canopy_runner").info("hello from the runner")
        print("a stray print")
        print("a traceback line", file=sys.stderr)
        sys.stdout.flush()
    finally:
        stream = sys.stdout
        sys.stdout, sys.stderr = saved[0], saved[1]
        logging.basicConfig(handlers=saved[2], force=True)
        stream.close()
    text = log.read_text()
    assert "hello from the runner" in text and "a stray print" in text and "traceback" in text
    assert log.with_name("runner.log.1").read_text() == "x" * 10, "an oversized log is rolled"
