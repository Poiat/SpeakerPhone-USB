import os
import shlex
import shutil
import tempfile

import updater

from updater import ApplyResult, parse_checksums


def test_parse_checksums_standard_format():
    text = (
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  "
        "./SpeakerPhone-USB-Linux-x86_64.AppImage\n"
        "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB *otro.bin\n"
        "linea-invalida\n"
    )
    checksums = parse_checksums(text)
    assert checksums["SpeakerPhone-USB-Linux-x86_64.AppImage"] == "a" * 64
    assert checksums["otro.bin"] == "b" * 64
    assert len(checksums) == 2


def test_parse_checksums_empty():
    assert parse_checksums("") == {}
    assert parse_checksums(None) == {}


def test_apply_result_defaults():
    result = ApplyResult(True, "ok")
    assert result.ok is True
    assert result.restart_launched is False
    assert result.folder == ""


def _temp_download(tmp_path):
    folder = tmp_path / "speakerphone-update-test"
    folder.mkdir()
    package = folder / "pkg.bin"
    package.write_bytes(b"data")
    return folder, package


def test_apply_update_removes_temp_download_on_success(monkeypatch, tmp_path):
    folder, package = _temp_download(tmp_path)
    monkeypatch.setattr(updater.sys, "platform", "linux")
    monkeypatch.setattr(
        updater,
        "_apply_linux",
        lambda path, name: ApplyResult(True, "ok", restart_launched=True),
    )

    result = updater.apply_update(str(package), "pkg.bin")

    assert result.ok
    assert not folder.exists()


def test_apply_update_keeps_download_on_failure(monkeypatch, tmp_path):
    folder, package = _temp_download(tmp_path)
    monkeypatch.setattr(updater.sys, "platform", "linux")
    monkeypatch.setattr(
        updater,
        "_apply_linux",
        lambda path, name: ApplyResult(False, "no", folder=str(folder)),
    )

    result = updater.apply_update(str(package), "pkg.bin")

    assert not result.ok
    assert folder.exists()


def test_windows_update_script_uses_random_path(monkeypatch, tmp_path):
    target = tmp_path / "SpeakerPhone.exe"
    target.write_bytes(b"old")
    package = tmp_path / "pkg.bin"
    package.write_bytes(b"new")
    captured = {}

    def fake_popen(args, **_kwargs):
        captured["batch_path"] = args[-1]
        with open(args[-1], encoding="utf-8") as handle:
            captured["batch"] = handle.read()
        return object()

    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.sys, "executable", str(target))
    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)

    result = updater._apply_windows(str(package), "pkg.bin")
    batch_path = captured["batch_path"]
    try:
        assert result.ok
        assert os.path.basename(batch_path).startswith("speakerphone-update-")
        assert batch_path != os.path.join(
            tempfile.gettempdir(), "speakerphone-update.cmd"
        )
        assert str(target) in captured["batch"]
    finally:
        os.unlink(batch_path)


def test_macos_update_script_uses_private_dir(monkeypatch, tmp_path):
    target = tmp_path / "SpeakerPhone.app"
    target.mkdir()
    package = tmp_path / "pkg.zip"
    package.write_bytes(b"zip")
    captured = {}

    def fake_ditto(args, **_kwargs):
        workdir = args[-1]
        captured["workdir"] = workdir
        os.makedirs(os.path.join(workdir, "SpeakerPhone.app"))
        return object()

    def fake_popen(args, **_kwargs):
        captured["script_path"] = args[-1]
        with open(args[-1], encoding="utf-8") as handle:
            captured["script"] = handle.read()
        return object()

    monkeypatch.setattr(updater, "_mac_bundle_path", lambda: str(target))
    monkeypatch.setattr(updater.subprocess, "run", fake_ditto)
    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)

    result = updater._apply_macos(str(package), "pkg.zip")
    script_path = captured["script_path"]
    script_dir = os.path.dirname(script_path)
    try:
        assert result.ok
        assert script_path != os.path.join(
            tempfile.gettempdir(), "speakerphone-update.sh"
        )
        assert os.path.basename(script_dir).startswith("speakerphone-update-run-")
        assert os.path.dirname(script_dir) == tempfile.gettempdir()
        assert f"rm -rf {shlex.quote(script_dir)}" in captured["script"]
    finally:
        shutil.rmtree(script_dir, ignore_errors=True)
        shutil.rmtree(captured["workdir"], ignore_errors=True)


def test_apply_update_does_not_remove_other_directories(monkeypatch, tmp_path):
    folder = tmp_path / "instaladores"
    folder.mkdir()
    package = folder / "pkg.bin"
    package.write_bytes(b"data")
    monkeypatch.setattr(updater.sys, "platform", "linux")
    monkeypatch.setattr(updater, "_apply_linux", lambda path, name: ApplyResult(True, "ok"))

    updater.apply_update(str(package), "pkg.bin")

    assert folder.exists()
