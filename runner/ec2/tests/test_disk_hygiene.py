"""A cloud runner must not fill its own disk — and must say so before it does.

cloud-ec2-1's 20 GB root reached 100% on 2026-10-04: ~/.claude/plugins/cache
held every plugin version ever installed (33 canopy, 24 ace — about 9 GB) plus
npm/uv download caches, and nothing reported it until turns failed on writes.
Three fixes, one test group each: bootstrap prunes the caches (prune_caches.py,
step 4b), the heartbeat health carries a `disk` check (warn 80%, fail 90%), and
the template's root volume is a parameter defaulting to 40 GB.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
from collections import namedtuple

import pytest

from test_readiness_is_observed import BASH, _fn

EC2 = pathlib.Path(__file__).resolve().parent.parent


def _prune():
    spec = importlib.util.spec_from_file_location("prune_caches", EC2 / "prune_caches.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["prune_caches"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    # Never prune the test machine's real uv cache.
    mod.prune_uv = lambda **kw: None
    return mod


def _cache(home: pathlib.Path, versions: dict[str, list[str]], installed: dict[str, str] | None):
    """versions: {"mp/plugin": [v_oldest, ..., v_newest]}; installed: {"mp/plugin": v}."""
    plugins = home / ".claude" / "plugins"
    for key, vs in versions.items():
        for i, v in enumerate(vs):
            d = plugins / "cache" / key / v
            d.mkdir(parents=True)
            (d / "f").write_text("x" * 100)
            os.utime(d, (1_000_000 + i, 1_000_000 + i))
    if installed is not None:
        record = {"version": 2, "plugins": {
            f"{key.split('/')[1]}@{key.split('/')[0]}": [
                {"scope": "user", "installPath": str(plugins / "cache" / key / v), "version": v}]
            for key, v in installed.items()}}
        (plugins / "installed_plugins.json").write_text(json.dumps(record))
    return plugins / "cache"


def _left(cache: pathlib.Path, key: str) -> set[str]:
    return {p.name for p in (cache / key).iterdir()}


def test_keeps_the_installed_version_and_the_newest_two(tmp_path):
    mod = _prune()
    vs = [f"0.2.{n}" for n in range(500, 533)]  # 33 versions, as on the box
    cache = _cache(tmp_path, {"canopy/canopy": vs, "ace/ace": ["a", "b", "c"]},
                   {"canopy/canopy": "0.2.510", "ace/ace": "c"})
    mod.main(["--home", str(tmp_path), "--npm-max-mb", "999999"])
    assert _left(cache, "canopy/canopy") == {"0.2.510", "0.2.532", "0.2.531"}
    assert _left(cache, "ace/ace") == {"a", "b", "c"}, "under the margin: nothing to prune"


def test_an_unreadable_install_record_prunes_nothing(tmp_path):
    """Guessing what is installed is how a prune deletes the plugin a turn loads."""
    mod = _prune()
    cache = _cache(tmp_path, {"canopy/canopy": [f"v{n}" for n in range(10)]}, None)
    (tmp_path / ".claude" / "plugins" / "installed_plugins.json").write_text("{not json")
    mod.main(["--home", str(tmp_path), "--npm-max-mb", "999999"])
    assert len(_left(cache, "canopy/canopy")) == 10


def test_dry_run_deletes_nothing(tmp_path):
    mod = _prune()
    cache = _cache(tmp_path, {"canopy/canopy": [f"v{n}" for n in range(10)]},
                   {"canopy/canopy": "v9"})
    mod.main(["--home", str(tmp_path), "--dry-run"])
    assert len(_left(cache, "canopy/canopy")) == 10


def test_npm_cache_is_cleaned_only_past_the_threshold(tmp_path, monkeypatch):
    mod = _prune()
    cacache = tmp_path / ".npm" / "_cacache"
    cacache.mkdir(parents=True)
    (cacache / "blob").write_bytes(b"x" * (2 * 2**20))
    ran = []
    monkeypatch.setattr(mod.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    mod.prune_npm(tmp_path, max_mb=10, dry_run=False)
    assert ran == []
    mod.prune_npm(tmp_path, max_mb=1, dry_run=False)
    assert ran == [["npm", "cache", "clean", "--force"]]


def test_a_failing_step_never_fails_the_prune(tmp_path, monkeypatch):
    mod = _prune()
    monkeypatch.setattr(mod, "prune_plugins", lambda *a, **k: 1 / 0)
    assert mod.main(["--home", str(tmp_path)]) == 0


@pytest.mark.skipif(BASH is None, reason="needs bash 4+")
def test_bootstrap_prunes_after_the_plugin_updates_and_only_on_a_full_run(tmp_path):
    """After step 4 (so the version just installed is recorded and kept), and not
    on the --credentials-only timer pass, which can run while a turn is live."""
    log = tmp_path / "order"
    stubs = "\n".join(
        f'{name}() {{ echo {name} >> "{log}"; }}'
        for name in ("step1_tooling", "step2_gog_config", "step3_agents",
                     "step4_claude_plugins", "step4b_prune_caches", "step5_summary",
                     "reinstall_cli_from_marketplace_clone", "log"))
    for flag, expected in ((0, ["step1_tooling", "step2_gog_config", "step3_agents",
                                "step4_claude_plugins", "step4b_prune_caches", "step5_summary"]),
                           (1, ["step2_gog_config", "step3_agents",
                                "reinstall_cli_from_marketplace_clone"])):
        log.write_text("")
        script = f"{stubs}\nCREDENTIALS_ONLY={flag}\n{_fn('main')}\nmain\n"
        subprocess.run([BASH, "-c", script], check=True, timeout=30)
        assert [x for x in log.read_text().split() if x != "log"] == expected


# --- the health check -------------------------------------------------------------

Usage = namedtuple("Usage", "total used free")


@pytest.mark.parametrize("pct,status", [(50, "ok"), (79, "ok"), (80, "warn"), (89, "warn"),
                                        (90, "fail"), (100, "fail")])
def test_disk_check_thresholds(cloud_runner, monkeypatch, pct, status):
    total = 40 * 2**30
    monkeypatch.setattr(cloud_runner.shutil, "disk_usage",
                        lambda p: Usage(total, total * pct // 100, total - total * pct // 100))
    monkeypatch.setattr(cloud_runner, "_slow_checks", lambda: None)
    by = {c["name"]: c for c in cloud_runner.health_report()["checks"]}
    assert by["disk"]["status"] == status
    assert f"{pct}% used" in by["disk"]["detail"]


def test_disk_check_is_reported_before_bootstrap(cloud_runner, monkeypatch):
    """A full disk is often WHY bootstrap never finishes; it cannot wait for it."""
    monkeypatch.setattr(cloud_runner, "_BOOTSTRAPPED_AT", 0.0)
    monkeypatch.setattr(cloud_runner, "_slow_checks", lambda: None)
    names = {c["name"] for c in cloud_runner.health_report()["checks"]}
    assert "disk" in names


def test_an_unreadable_disk_is_a_warning_not_a_lost_beat(cloud_runner, monkeypatch):
    def boom(p):
        raise OSError("nope")
    monkeypatch.setattr(cloud_runner.shutil, "disk_usage", boom)
    monkeypatch.setattr(cloud_runner, "_slow_checks", lambda: None)
    by = {c["name"]: c for c in cloud_runner.health_report()["checks"]}
    assert by["disk"]["status"] == "warn"


def test_the_volume_is_a_parameter_defaulting_to_40():
    text = (EC2 / "runner.cfn.yaml").read_text()
    assert "VolumeSize: !Ref VolumeSize" in text
    block = text[text.index("  VolumeSize:\n"):]
    assert "Default: 40" in block[:200]
