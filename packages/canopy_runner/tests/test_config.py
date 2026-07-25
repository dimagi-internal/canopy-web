import json
from pathlib import Path

from canopy_runner.config import Config


def test_load_config_with_token_file(tmp_path: Path):
    token_file = tmp_path / "tok"
    token_file.write_text("sekret\n")
    cfg_file = tmp_path / "runner.json"
    cfg_file.write_text(json.dumps({
        "base_url": "https://labs.example.com/canopy",
        "token": f"@{token_file}",
        "runner_id": "r-1",
        "emdash_db": str(tmp_path / "emdash4.db"),
        # Extra/legacy keys (e.g. a leftover automation_ids from the retired inject
        # executor) are ignored by load, not rejected — keep the runner bootable across
        # a config that predates their removal.
        "automation_ids": {"echo": "auto-1"},
    }))
    cfg = Config.load(cfg_file)
    assert cfg.token == "sekret"
    assert cfg.base_url == "https://labs.example.com/canopy"
    assert not hasattr(cfg, "automation_ids")  # dropped field, silently ignored on load
    assert cfg.poll_seconds == 5  # default (low, for snappy turn claims)


def test_session_report_limit_defaults_to_100():
    """Separate from session_tail_count (30): that bounds expensive TRANSCRIPT reads,
    this bounds how many tasks are reported at all. Truncation here auto-archives."""
    cfg = Config(base_url="http://x", token="t", runner_id="r", emdash_db="/db")
    assert cfg.session_report_limit == 100
    assert cfg.session_tail_count == 30
