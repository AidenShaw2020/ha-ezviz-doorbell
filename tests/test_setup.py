"""Does the integration load, and does a ring reach the right entity?

These are the two questions no amount of reading the code answers. The cloud
itself is mocked out; what is exercised is the wiring - the config entry, the
coordinator, every platform, and the path an event takes from a polled message
to the entity an automation would trigger on.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezviz_doorbell.const import (
    CONF_REGION,
    CONF_STATUS_INTERVAL,
    DEFAULT_REGION,
    DOMAIN,
)

from .conftest import PAGELIST_DEVICE, SERIAL

# The status EzvizCamera would compose from the pagelist above.
RAW_STATUS = {
    "serial": SERIAL,
    "name": "Front door",
    "version": "V5.3.4 build 240101",
    "status": 1,
    "device_category": "BDoorBell",
    "device_sub_category": "CP4",
    "upgrade_available": False,
    "upgrade_in_progress": False,
    "alarm_notify": True,
    "alarm_schedules_enabled": False,
    "alarm_sound_mod": "SOFT",
    "encrypted": True,
    "local_ip": "192.168.1.5",
    "wan_ip": "8.8.8.8",
    "battery_level": 74,
    "PIR_Status": 1,
    "Seconds_Last_Trigger": 0,
    "last_alarm_time": "2026-08-30 08:00:00",
    "last_alarm_type_name": "Doorbell",
    "push_notify_alarm": True,
    "push_notify_call": True,
    "Alarm_DetectHumanCar": 1,
    "diskCapacity": ["30436"],
    "battery_camera_work_mode": 0,
    "optionals": PAGELIST_DEVICE["STATUS"]["optionals"],
    "WIFI": PAGELIST_DEVICE["WIFI"],
    "switches": {3: True, 21: False, 22: True, 200: True, 4242: False},
}


@pytest.fixture
async def loaded_entry(
    hass: HomeAssistant, ezviz_client: MagicMock
) -> MockConfigEntry:
    """Set up the integration against the mocked cloud."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="cloud@example.invalid",
        unique_id="cloud@example.invalid",
        data={
            CONF_USERNAME: "cloud@example.invalid",
            CONF_PASSWORD: "secret",
            CONF_REGION: DEFAULT_REGION,
        },
        options={CONF_STATUS_INTERVAL: 60},
    )
    entry.add_to_hass(hass)

    camera = MagicMock()
    camera.return_value.status.return_value = RAW_STATUS
    with (
        patch(
            "custom_components.ezviz_doorbell.coordinator.EzvizClient",
            return_value=ezviz_client,
        ),
        patch("custom_components.ezviz_doorbell.coordinator.EzvizCamera", camera),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    return entry


async def test_entities_are_created(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Every platform should produce its entities for the one device."""
    states = {state.entity_id: state for state in hass.states.async_all()}

    expected = {
        "camera.front_door",
        "event.front_door_doorbell",
        "event.front_door_motion",
        "event.front_door_alerts",
        "binary_sensor.front_door_doorbell_button",
        "binary_sensor.front_door_motion_detected",
        "binary_sensor.front_door_online",
        "sensor.front_door_battery",
        "sensor.front_door_last_ring",
        "sensor.front_door_wi_fi_signal",
        "switch.front_door_motion_detection",
        "switch.front_door_doorbell_notifications",
        "number.front_door_detection_sensitivity",
        "select.front_door_work_mode",
        "button.front_door_wake_camera",
        "siren.front_door_siren",
        "image.front_door_last_snapshot",
        "update.front_door_firmware",
    }
    missing = expected - set(states)
    assert not missing, f"missing {sorted(missing)}; got {sorted(states)}"

    assert states["sensor.front_door_battery"].state == "74"
    assert states["binary_sensor.front_door_online"].state == STATE_ON
    assert states["select.front_door_work_mode"].state == "power_save"
    assert states["number.front_door_detection_sensitivity"].state == "4.0"


async def test_device_switches_follow_the_device(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """A switch appears for each type the device reports, named where known."""
    assert hass.states.get("switch.front_door_status_light").state == STATE_ON
    assert hass.states.get("switch.front_door_sleep").state == STATE_OFF
    # An unknown type is still offered rather than dropped.
    assert hass.states.get("switch.front_door_switch_4242") is not None


async def test_a_ring_reaches_the_doorbell_entity(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """A polled doorbell message must not look like motion."""
    coordinator = loaded_entry.runtime_data
    await coordinator._async_handle_polled(
        {
            "deviceSerial": SERIAL,
            "subType": 2701,
            "title": "Your doorbell is ringing",
            "msgId": "m1",
            "ext": {"callingStatus": 1, "text": "somebody there ring the door"},
        }
    )
    await hass.async_block_till_done()

    doorbell = hass.states.get("event.front_door_doorbell")
    assert doorbell.attributes["event_type"] == "ring"
    assert doorbell.attributes["source"] == "poll"
    assert hass.states.get("binary_sensor.front_door_doorbell_button").state == STATE_ON

    # Motion stays untouched, which is the whole point of splitting them.
    assert hass.states.get("event.front_door_motion").state == "unknown"
    assert (
        hass.states.get("binary_sensor.front_door_motion_detected").state == STATE_OFF
    )
    assert hass.states.get("sensor.front_door_last_event").state == "ring"


async def test_motion_reaches_the_motion_entity(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """A push carrying AI human detection is motion, not a ring."""
    coordinator = loaded_entry.runtime_data
    await coordinator._async_handle_push(
        {
            "alert": "AI Human Detection",
            "ext": {
                "device_serial": SERIAL,
                "alert_type_code": 10120,
                "msgId": "m2",
            },
        }
    )
    await hass.async_block_till_done()

    assert hass.states.get("event.front_door_motion").attributes["event_type"] == (
        "motion"
    )
    assert hass.states.get("binary_sensor.front_door_motion_detected").state == STATE_ON
    assert hass.states.get("event.front_door_doorbell").state == "unknown"


async def test_the_same_event_twice_fires_once(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Push and polling overlap, and the second copy must be dropped."""
    coordinator = loaded_entry.runtime_data
    await coordinator._async_handle_push(
        {
            "alert": "AI Human Detection",
            "ext": {"device_serial": SERIAL, "alert_type_code": 10120, "msgId": "m3"},
        }
    )
    await hass.async_block_till_done()
    first = hass.states.get("event.front_door_motion").state

    await coordinator._async_handle_polled(
        {
            "deviceSerial": SERIAL,
            "subType": 10120,
            "title": "AI Human Detection",
            "msgId": "m4",
        }
    )
    await hass.async_block_till_done()

    assert hass.states.get("event.front_door_motion").state == first


async def test_a_switch_reaches_the_cloud(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, ezviz_client: MagicMock
) -> None:
    """Turning a switch off should call the API it belongs to."""
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.front_door_sleep"},
        blocking=True,
    )
    ezviz_client.switch_status.assert_called_with(SERIAL, 21, False)


async def test_wake_tries_every_route(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, ezviz_client: MagicMock
) -> None:
    """The wake button should try all three ways of reaching the device."""
    ezviz_client.capture_picture.return_value = {"picUrl": "https://pic.invalid/a.jpg"}
    with patch("custom_components.ezviz_doorbell.coordinator.requests.get") as get:
        get.return_value.content = b"\xff\xd8notencrypted"
        get.return_value.raise_for_status.return_value = None
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": "button.front_door_wake_camera"},
            blocking=True,
        )

    ezviz_client.switch_status.assert_any_call(SERIAL, 21, 0)
    ezviz_client.delay_battery_device_sleep.assert_called_with(SERIAL, 1, 1)
    ezviz_client.capture_picture.assert_called_with(SERIAL, 1)


async def test_unload(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> None:
    """Unloading should leave nothing running behind it."""
    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()

    # Home Assistant keeps the entity registered and marks it unavailable
    # rather than removing it, which is what a reload has to look like.
    assert hass.states.get("camera.front_door").state == STATE_UNAVAILABLE
    assert loaded_entry.state is ConfigEntryState.NOT_LOADED
