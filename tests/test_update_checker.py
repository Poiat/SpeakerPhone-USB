import json
from unittest import mock

import update_checker as uc


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


def test_normalize_version():
    assert uc.normalize_version("v4.0.16") == "4.0.16"
    assert uc.normalize_version("0.19") == "0.19.0"
    assert uc.normalize_version("release-1.2.3-rc1") == "1.2.3"
    assert uc.normalize_version("sin version") == ""


def test_version_comparison():
    assert uc.version_tuple("0.18") < uc.version_tuple("0.19")
    assert uc.version_tuple("4.0.16") > uc.version_tuple("0.19")


def test_update_available():
    payload = {"tag_name": "v0.20", "html_url": "https://example.test/0.20"}
    with mock.patch.object(uc.urllib.request, "urlopen", return_value=FakeResponse(payload)):
        result = uc.check_for_update("0.19")
    assert result["status"] == "update"
    assert result["latest"] == "0.20.0"
    assert result["url"] == "https://example.test/0.20"


def test_current_version():
    payload = {"tag_name": "0.19", "html_url": "https://example.test/0.19"}
    with mock.patch.object(uc.urllib.request, "urlopen", return_value=FakeResponse(payload)):
        result = uc.check_for_update("0.19")
    assert result["status"] == "current"


def test_network_error():
    with mock.patch.object(uc.urllib.request, "urlopen", side_effect=OSError("sin red")):
        result = uc.check_for_update("0.19")
    assert result["status"] == "error"
    assert "sin red" in result["error"]


def test_select_asset_per_platform():
    assets = [
        {"name": "SpeakerPhone-USB-Linux-x86_64.AppImage"},
        {"name": "SpeakerPhone-USB-Windows-x86_64.exe"},
        {"name": "SpeakerPhone-USB-macOS-arm64.zip"},
        {"name": "SpeakerPhone-USB-Android.apk"},
        {"name": "SHA256SUMS.txt"},
    ]
    assert uc.select_asset(assets, "linux")["name"].endswith(".AppImage")
    assert uc.select_asset(assets, "win32")["name"].endswith(".exe")
    assert uc.select_asset(assets, "darwin")["name"].endswith(".zip")
    assert uc.find_checksum_asset(assets)["name"] == "SHA256SUMS.txt"
    assert uc.select_asset([], "linux") is None


def test_release_payload_includes_asset_fields():
    payload = {
        "tag_name": "0.21",
        "html_url": "https://example.test/0.21",
        "assets": [
            {
                "name": "SpeakerPhone-USB-Linux-x86_64.AppImage",
                "browser_download_url": "https://example.test/appimage",
                "size": 1234,
            },
            {
                "name": "SHA256SUMS.txt",
                "browser_download_url": "https://example.test/sums",
                "size": 100,
            },
        ],
    }
    with mock.patch.object(
        uc.urllib.request, "urlopen", return_value=FakeResponse(payload)
    ), mock.patch.object(uc, "select_asset", return_value=payload["assets"][0]):
        result = uc.check_for_update("0.20")
    assert result["asset_url"] == "https://example.test/appimage"
    assert result["asset_size"] == 1234
    assert result["checksums_url"] == "https://example.test/sums"
