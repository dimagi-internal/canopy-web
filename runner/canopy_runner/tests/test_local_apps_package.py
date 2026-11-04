"""A package runs as the person on their computer, so these pin what the manifest and the
zip rules refuse, and that packing the same folder gives the same bytes."""
import io
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("fcntl")  # local apps are macOS only; symlinks and modes differ on Windows

from canopy_runner.local_apps import manifest as manifest_mod  # noqa: E402
from canopy_runner.local_apps import package  # noqa: E402

LOCAL_APPS = Path(__file__).resolve().parents[3] / "local_apps"


def good(**changes):
    raw = {"key": "my-app", "name": "My app", "version": "0.1.0", "entry": ["{python}", "app.py"],
           "platforms": ["macos"], "max_minutes": 60}
    raw.update(changes)
    return raw


def write_app(folder: Path, **changes):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps(good(**changes)))
    (folder / "app.py").write_text("print('hi')\n")
    return folder


def test_a_minimal_manifest_gets_every_optional_field_filled():
    out = manifest_mod.validate(good())
    assert out["consumes"] == [] and out["canopy"] == [] and out["notice"] == ""
    assert out["disclosure"] == {"reads": [], "sends": []}


@pytest.mark.parametrize("change", [
    {"key": "Bad"}, {"key": "a"}, {"key": "ends-"}, {"key": "../evil"}, {"key": "a" * 41},
    {"version": "1.x"}, {"version": "1.0.0-beta"}, {"version": 1},
    {"entry": []}, {"entry": "app.py"}, {"entry": ["app.py", "{python}"]}, {"entry": [""]},
    {"platforms": ["windows"]}, {"platforms": "macos"},
    {"consumes": "transcript"}, {"provides": ["x" * 41]},
    {"max_minutes": 0}, {"max_minutes": 481}, {"max_minutes": True}, {"max_minutes": 1.5},
    {"notice": 3}, {"disclosure": {"reads": "a thing"}}, {"disclosure": {"other": []}},
    {"settings": {"a": {"type": "number"}}}, {"settings": {"Bad Name": {"type": "text"}}},
    {"checks": [{"label": "x", "run": ["y"]}]}, {"checks": [{"label": "x", "run": [], "fix": "z"}]},
    {"canopy": ["sessions.delete"]}, {"surprise": 1},
])
def test_refuses(change):
    with pytest.raises(ValueError):
        manifest_mod.validate(good(**change))


@pytest.mark.parametrize("missing", manifest_mod.REQUIRED)
def test_refuses_a_missing_required_field(missing):
    raw = good()
    del raw[missing]
    with pytest.raises(ValueError):
        manifest_mod.validate(raw)


def test_pack_is_deterministic_whatever_the_folder_order_and_times(tmp_path):
    one = write_app(tmp_path / "one")
    two = write_app(tmp_path / "two")
    for folder, names in ((one, ["b.txt", "a.txt"]), (two, ["a.txt", "b.txt"])):
        for name in names:
            (folder / name).write_text(name)
    for path in one.iterdir():
        os.utime(path, (1_000_000, 1_000_000))
    _, first = package.pack(one)
    _, second = package.pack(two)
    assert first == second
    assert package.sha256(first) == package.sha256(package.pack(one)[1])
    assert zipfile.ZipFile(io.BytesIO(first)).namelist() == ["a.txt", "app.py", "b.txt", "manifest.json"]


def test_pack_leaves_out_tests_caches_and_dotfiles(tmp_path):
    folder = write_app(tmp_path / "app")
    for rel in ("tests/test_x.py", "__pycache__/x.pyc", ".git/config", ".env", "lib/tests/t.py", "lib/ok.py"):
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    names = zipfile.ZipFile(io.BytesIO(package.pack(folder)[1])).namelist()
    assert names == ["app.py", "lib/ok.py", "manifest.json"]


def test_pack_refuses_a_symlink_to_a_file_or_a_folder(tmp_path):
    folder = write_app(tmp_path / "app")
    (folder / "link").symlink_to(Path.home() / ".ssh")
    with pytest.raises(package.PackageError, match="symlink"):
        package.pack(folder)
    (folder / "link").unlink()
    (tmp_path / "secret").write_text("x")
    (folder / "link.txt").symlink_to(tmp_path / "secret")
    with pytest.raises(package.PackageError, match="symlink"):
        package.pack(folder)


def test_pack_refuses_binaries(tmp_path):
    folder = write_app(tmp_path / "app")
    (folder / "blob").write_bytes(b"\x00\x01\x02")
    with pytest.raises(package.PackageError, match="binary"):
        package.pack(folder)
    (folder / "blob").write_bytes(b"\xff\xfe\xfa")
    with pytest.raises(package.PackageError, match="UTF-8"):
        package.pack(folder)


def test_pack_refuses_too_many_files_and_too_much(tmp_path):
    folder = write_app(tmp_path / "app")
    for i in range(package.MAX_FILES):
        (folder / f"f{i}.txt").write_text("x")
    with pytest.raises(package.PackageError, match="files"):
        package.pack(folder)

    folder = write_app(tmp_path / "big")
    (folder / "big.txt").write_text("x" * (package.MAX_UNPACKED + 1))
    with pytest.raises(package.PackageError, match="unpacked"):
        package.pack(folder)

    folder = write_app(tmp_path / "packed")
    (folder / "big.txt").write_text("x" * (package.MAX_PACKED + 1))
    with pytest.raises(package.PackageError, match="over"):
        package.pack(folder)


def zipped(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data, attr in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = attr
            archive.writestr(info, data)
    return buffer.getvalue()


REGULAR = (stat.S_IFREG | 0o644) << 16


@pytest.mark.parametrize("name", ["../evil.py", "a/../../evil.py", "/etc/evil", "a\\..\\evil", "C:/evil"])
def test_unpack_refuses_traversal_and_writes_nothing(tmp_path, name):
    target = tmp_path / "out" / "1.0"
    with pytest.raises(package.PackageError, match="unsafe"):
        package.unpack(zipped([("fine.py", "x", REGULAR), (name, "x", REGULAR)]), target)
    assert not target.exists()
    assert not (tmp_path / "evil.py").exists()
    assert list((tmp_path / "out").iterdir()) == []


def test_unpack_refuses_a_symlink_entry(tmp_path):
    with pytest.raises(package.PackageError, match="symlink"):
        package.unpack(zipped([("link", "/etc/passwd", (stat.S_IFLNK | 0o777) << 16)]), tmp_path / "out")


def test_unpack_refuses_binaries_duplicates_and_junk(tmp_path):
    with pytest.raises(package.PackageError, match="binary"):
        package.unpack(zipped([("a.bin", b"\x00", REGULAR)]), tmp_path / "a")
    with pytest.raises(package.PackageError, match="twice"):
        package.unpack(zipped([("A.py", "x", REGULAR), ("a.py", "y", REGULAR)]), tmp_path / "b")
    with pytest.raises(package.PackageError, match="not a zip"):
        package.unpack(b"not a zip", tmp_path / "c")


def test_unpack_counts_what_comes_out_not_what_the_zip_claims(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bomb.txt", "x" * (package.MAX_UNPACKED + 1))
    with pytest.raises(package.PackageError, match="unpacked"):
        package.unpack(buffer.getvalue(), tmp_path / "out")


def test_unpack_refuses_too_many_files(tmp_path):
    entries = [(f"f{i}.txt", "x", REGULAR) for i in range(package.MAX_FILES + 1)]
    with pytest.raises(package.PackageError, match="files"):
        package.unpack(zipped(entries), tmp_path / "out")


def test_unpack_is_read_only_and_can_still_be_replaced_and_removed(tmp_path):
    folder = write_app(tmp_path / "app")
    (folder / "lib").mkdir()
    (folder / "lib" / "x.py").write_text("x")
    (folder / "run.sh").write_text("#!/bin/sh\n")
    (folder / "run.sh").chmod(0o755)
    target = tmp_path / "versions" / "0.1.0"
    data = package.pack(folder)[1]
    package.unpack(data, target)
    assert (target / "lib" / "x.py").read_text() == "x"
    assert stat.S_IMODE((target / "app.py").stat().st_mode) == 0o444
    assert stat.S_IMODE((target / "run.sh").stat().st_mode) == 0o555
    assert stat.S_IMODE(target.stat().st_mode) == 0o555
    assert stat.S_IMODE((target / "lib").stat().st_mode) == 0o555
    package.unpack(data, target)
    assert (target / "app.py").exists()
    package.remove_tree(target)
    assert not target.exists()


@pytest.mark.parametrize("name", ["meeting-assistant", "superwhisper-transcript"])
def test_the_first_apps_validate_and_pack_without_their_tests(name):
    folder = LOCAL_APPS / name
    manifest, first = package.pack(folder)
    assert manifest["key"] == name
    assert first == package.pack(folder)[1]
    assert not any("tests" in n for n in zipfile.ZipFile(io.BytesIO(first)).namelist())
