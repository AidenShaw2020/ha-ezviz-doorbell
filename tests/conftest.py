"""Fixtures for the EZVIZ Doorbell tests."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest
import pytest_socket

# Home Assistant's harness blocks sockets so that a test cannot reach the
# network by accident. On Windows the event loop needs one for its own wakeup
# pipe, and it is created before any fixture or hook of ours could lift the
# block, so there the block is never applied. On Linux it stays on, which is
# where it is worth having.
if sys.platform == "win32":
    pytest_socket.disable_socket = lambda *args, **kwargs: None

SERIAL = "D1234567"

# One device as the EZVIZ pagelist reports it, trimmed to the keys the
# integration actually reads.
PAGELIST_DEVICE = {
    "deviceInfos": {
        "deviceSerial": SERIAL,
        "name": "Front door",
        "version": "V5.3.4 build 240101",
        "status": 1,
        "deviceCategory": "BDoorBell",
        "deviceSubCategory": "CP4",
        "mac": "aa:bb:cc",
        "offlineNotify": 0,
        "channelNumber": 1,
    },
    "STATUS": {
        "globalStatus": 1,
        "isEncrypt": 1,
        "pirStatus": 1,
        "alarmSoundMode": 0,
        "optionals": {
            "powerRemaining": 74,
            "diskCapacity": "30436",
            "batteryCameraWorkMode": 0,
            "NightVision_Model": {"graphicType": 2, "luminance": 100},
            "Alarm_DetectHumanCar": {"type": 1},
            "display_mode": {"mode": 1},
        },
    },
    "WIFI": {"ssid": "home", "signal": 84},
    "CONNECTION": {"localIp": "192.168.1.5", "netIp": "8.8.8.8", "localRtspPort": 0},
    "NODISTURB": {"alarmEnable": 0, "callingEnable": 0},
    "SWITCH": [
        {"type": 3, "enable": True},
        {"type": 21, "enable": False},
        {"type": 22, "enable": True},
        {"type": 200, "enable": True},
        {"type": 4242, "enable": False},
    ],
    "UPGRADE": {"isNeedUpgrade": 0},
    "resourceInfos": [{"resourceId": "res1", "deviceSerial": SERIAL}],
    "TIME_PLAN": {},
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load this repository's custom_components."""
    return


@pytest.fixture
def ezviz_client() -> MagicMock:
    """Return a stand-in for the EZVIZ cloud."""
    client = MagicMock()
    client.export_token.return_value = {"session_id": "session", "api_url": "api"}
    client.get_device_infos.return_value = {SERIAL: PAGELIST_DEVICE}
    client.get_detection_sensibility.return_value = 4
    client.get_device_messages_list.return_value = {"message": []}
    client.get_mqtt_client.return_value = MagicMock()
    return client
