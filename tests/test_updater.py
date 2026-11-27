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


def test_apply_update_does_not_remove_other_directories(monkeypatch, tmp_path):
    folder = tmp_path / "instaladores"
    folder.mkdir()
    package = folder / "pkg.bin"
    package.write_bytes(b"data")
    monkeypatch.setattr(updater.sys, "platform", "linux")
    monkeypatch.setattr(updater, "_apply_linux", lambda path, name: ApplyResult(True, "ok"))

    updater.apply_update(str(package), "pkg.bin")

    assert folder.exists()
