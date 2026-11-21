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
