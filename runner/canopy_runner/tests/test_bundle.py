"""Finding the installer's bundle from the interpreter — and nothing for a dev install."""
from __future__ import annotations

import os
import sys

from canopy_runner import bundle, headless

from .bundle_trees import mac_app, mac_python, windows_install


def test_a_developer_install_is_not_bundled():
    assert bundle.root() is None
    assert bundle.app_path() is None and bundle.node_dir() is None
    env = {"PATH": "/usr/bin"}
    assert bundle.activate(env=env) is None and env == {"PATH": "/usr/bin"}
    assert bundle.adapter_prefixes() is None


def test_the_mac_app_bundle(tmp_path):
    app = mac_app(tmp_path)
    exe = mac_python(app)
    rt = app / "Contents" / "Resources" / "runtime"
    assert bundle.root(exe) == rt
    assert bundle.app_path(exe) == app
    assert bundle.node_dir(exe) == rt / "node" / "bin"
    assert bundle.acp_prefix(exe) == rt / "acp"
    assert bundle.info(exe)["sha"] == "a" * 40
    assert bundle.platform(exe) == "macos-arm64"
    assert bundle.windows_app_dir(exe) is None
    env = {"PATH": f"/usr/bin{os.pathsep}{rt / 'node' / 'bin'}"}
    assert bundle.activate(exe, env=env) == rt
    parts = env["PATH"].split(os.pathsep)
    assert parts[:2] == [str(rt / "node" / "bin"), str(rt / "acp" / "bin")]
    assert parts.count(str(rt / "node" / "bin")) == 1


def test_the_windows_install_through_current_is_not_resolved(tmp_path):
    app = windows_install(tmp_path)
    exe = str(app / "current" / "python" / "pythonw.exe")
    assert bundle.root(exe) == app / "current", "never the versioned path the junction names"
    assert bundle.windows_app_dir(exe) == app
    assert bundle.node_dir(exe) == app / "current" / "node"
    assert bundle.app_path(exe) is None
    versioned = str(app / "versions" / ("a" * 12) / "python" / "python.exe")
    assert bundle.windows_app_dir(versioned) == app


def test_host_platform_names():
    assert bundle.host_platform("Darwin", "arm64") == "macos-arm64"
    assert bundle.host_platform("Darwin", "x86_64") == "macos-x86_64"
    assert bundle.host_platform("Windows", "AMD64") == "windows-x86_64"


def test_the_bundled_adapter_is_found_first(tmp_path, monkeypatch):
    prefix = tmp_path / "acp"
    entry = prefix / "lib" / "node_modules" / "@agentclientprotocol" / "claude-agent-acp" / "dist"
    entry.mkdir(parents=True)
    (entry / "index.js").write_text("")
    monkeypatch.setattr(bundle, "adapter_prefixes", lambda executable=None: [prefix])
    monkeypatch.setattr("canopy_acp.client._node", lambda: "/bundle/node")
    monkeypatch.setattr("canopy_acp.client.shutil.which", lambda name: "/elsewhere/" + name)
    assert headless.adapter_argv() == ["/bundle/node", str(entry / "index.js")]
    assert headless.ensure_adapter() is True


def test_build_info_has_a_platform_stamp():
    from canopy_runner import _build_info

    assert _build_info.PLATFORM == "" and sys.modules["canopy_runner._build_info"]
