"""A runner on an address canopy has left moves itself — and only then.

canopy moved to canopy.dimagi.com; every paired laptop's runner.json still
names labs.connect.dimagi.com/canopy. The heartbeat reply carries the canonical
address, and the runner moves ONLY off a known former address, ONLY to https,
ONLY once the new address answers as this runner, and ONLY while idle — a
runner that followed any address a server named could be stranded by one.
"""
import json
import os
from types import SimpleNamespace

from canopy_runner import rebase

OLD = "https://labs.connect.dimagi.com/canopy"
NEW = "https://canopy.dimagi.com"


def _cfg(tmp_path, base=OLD):
    path = tmp_path / "runner.json"
    path.write_text(json.dumps({"base_url": base, "token": "t", "runner_id": "r-1", "emdash_db": "x"}))
    os.chmod(path, 0o600)
    return SimpleNamespace(base_url=base, token="t", runner_id="r-1", config_path=str(path)), path


def _client(rows):
    class C:
        def __init__(self, base, token):
            self.base = base

        def _call(self, method, path, body=None, *, retry=True):
            if isinstance(rows, Exception):
                raise rows
            return 200, rows
    return C


def test_moves_off_the_old_address_and_keeps_the_file_private(tmp_path):
    cfg, path = _cfg(tmp_path)
    assert rebase.observe({"canonical_base_url": NEW}, cfg, idle=True, client_cls=_client([{"id": "r-1"}]))
    assert json.loads(path.read_text())["base_url"] == NEW
    assert path.stat().st_mode & 0o777 == 0o600


def test_stays_on_any_address_that_is_not_a_former_one(tmp_path):
    cfg, path = _cfg(tmp_path, base="http://localhost:8000")
    assert not rebase.observe({"canonical_base_url": NEW}, cfg, idle=True, client_cls=_client([{"id": "r-1"}]))
    assert json.loads(path.read_text())["base_url"] == "http://localhost:8000"


def test_never_moves_to_a_non_https_address(tmp_path):
    cfg, _ = _cfg(tmp_path)
    assert not rebase.observe({"canonical_base_url": "http://canopy.dimagi.com"}, cfg, idle=True,
                              client_cls=_client([{"id": "r-1"}]))


def test_never_moves_to_an_address_that_does_not_know_this_runner(tmp_path):
    cfg, path = _cfg(tmp_path)
    assert not rebase.observe({"canonical_base_url": NEW}, cfg, idle=True, client_cls=_client([{"id": "other"}]))
    assert not rebase.observe({"canonical_base_url": NEW}, cfg, idle=True, client_cls=_client(OSError("down")))
    assert json.loads(path.read_text())["base_url"] == OLD


def test_waits_until_idle(tmp_path):
    cfg, _ = _cfg(tmp_path)
    assert not rebase.observe({"canonical_base_url": NEW}, cfg, idle=False, client_cls=_client([{"id": "r-1"}]))


def test_an_older_server_that_names_no_address_changes_nothing(tmp_path):
    cfg, _ = _cfg(tmp_path)
    assert not rebase.observe({}, cfg, idle=True, client_cls=_client([{"id": "r-1"}]))
