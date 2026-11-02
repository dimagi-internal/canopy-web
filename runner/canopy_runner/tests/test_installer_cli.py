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


def _with_log_file(log, body):
    saved = sys.stdout, sys.stderr, list(logging.getLogger().handlers)
    try:
        main._log_to_file(str(log))
        body()
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        sys.stdout, sys.stderr = saved[0], saved[1]
        for h in logging.getLogger().handlers:
            h.close()
        logging.basicConfig(handlers=saved[2], force=True)


def test_log_file_takes_logging_stdout_and_stderr(tmp_path):
    log = tmp_path / "logs" / "runner.log"

    def body():
        logging.getLogger("canopy_runner").info("hello from the runner")
        print("a stray print")
        print("a traceback line", file=sys.stderr)

    _with_log_file(log, body)
    text = log.read_text()
    assert "hello from the runner" in text and "a stray print" in text and "traceback" in text


def test_the_runner_log_is_capped(tmp_path, monkeypatch):
    """runner.log reached 160 MB because nothing rolled it while the process ran."""
    monkeypatch.setattr(main, "LOG_ROLL_BYTES", 2_000)
    monkeypatch.setattr(main, "LOG_BACKUPS", 2)
    log = tmp_path / "runner.log"

    def body():
        for i in range(500):
            logging.getLogger("canopy_runner").info("line %04d %s", i, "x" * 50)
            print("stray", i)

    _with_log_file(log, body)
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["runner.log", "runner.log.1", "runner.log.2"], files
    assert all(p.stat().st_size <= 2_000 + 200 for p in tmp_path.iterdir())
    assert "line 0499" in log.read_text()
