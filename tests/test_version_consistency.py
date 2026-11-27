from protocol import HEADER_SIZE, STATUS_SIZE, VERSION
from scripts.check_version import android_protocol


def test_android_protocol_constants_match_python():
    protocol = android_protocol()
    assert protocol["PROTOCOL_VERSION"] == VERSION
    assert protocol["HEADER_SIZE"] == HEADER_SIZE
    assert protocol["STATUS_SIZE"] == STATUS_SIZE
