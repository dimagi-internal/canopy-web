"""Every runner runs the pinned adapter: `install_adapter` installs ADAPTER_VERSION
and replaces another version under its prefix, so a box that installed the adapter
once does not stay on whatever npm's latest was that day."""
import json

from canopy_acp import client
from canopy_acp.client import ADAPTER_PACKAGE, ADAPTER_VERSION, install_adapter


def _installed(prefix, version):
    pkg = prefix / "lib" / "node_modules" / ADAPTER_PACKAGE / "dist"
    pkg.mkdir(parents=True)
    (pkg / "index.js").write_text("")
    (pkg.parent / "package.json").write_text(json.dumps({"version": version}))


def _box(monkeypatch, *, on_path=None):
    runs = []
    which = {"claude-agent-acp": on_path, "node": "/usr/bin/node", "npm": "/usr/bin/npm"}
    monkeypatch.setattr(client.shutil, "which", lambda n: which.get(n))
    monkeypatch.setattr(client.subprocess, "run", lambda cmd, **kw: runs.append(cmd))
    return runs


def test_a_fresh_box_installs_the_pinned_version(tmp_path, monkeypatch):
    runs = _box(monkeypatch)
    assert install_adapter(tmp_path) == "installed"
    assert runs[0][-1] == f"{ADAPTER_PACKAGE}@{ADAPTER_VERSION}"


def test_another_version_under_the_prefix_is_replaced(tmp_path, monkeypatch):
    _installed(tmp_path, "0.93.0")
    runs = _box(monkeypatch, on_path=str(tmp_path / "bin" / "claude-agent-acp"))
    assert install_adapter(tmp_path) == "upgraded from 0.93.0"
    assert runs[0][-1] == f"{ADAPTER_PACKAGE}@{ADAPTER_VERSION}"


def test_the_pinned_version_is_left_alone(tmp_path, monkeypatch):
    _installed(tmp_path, ADAPTER_VERSION)
    runs = _box(monkeypatch)
    assert install_adapter(tmp_path) == "present"
    assert runs == []


def test_an_adapter_outside_the_prefix_is_someone_else_s(tmp_path, monkeypatch):
    runs = _box(monkeypatch, on_path="/usr/bin/claude-agent-acp")
    assert install_adapter(tmp_path) == "present"
    assert runs == []


def test_the_bundle_pins_the_same_version():
    import pathlib
    versions = json.loads((pathlib.Path(__file__).resolve().parents[2]
                           / "packaging" / "versions.json").read_text())
    assert versions["acp_adapter"]["version"] == ADAPTER_VERSION
