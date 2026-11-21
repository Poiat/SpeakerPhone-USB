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
