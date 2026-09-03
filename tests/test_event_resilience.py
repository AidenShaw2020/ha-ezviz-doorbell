"""Regression tests for event handling across EZVIZ cloud outages."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezviz_doorbell.const import (
    CONF_REGION,
    DEFAULT_REGION,
    DOMAIN,
)
from custom_components.ezviz_doorbell.vendor.pyezvizapi.exceptions import PyEzvizError

from .conftest import SERIAL
from .test_setup import RAW_STATUS


async def _setup(
    hass: HomeAssistant, ezviz_client: MagicMock
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="cloud@example.invalid",
        unique_id="cloud@example.invalid",
        data={
            CONF_USERNAME: "cloud@example.invalid",
            CONF_PASSWORD: "secret",
            CONF_REGION: DEFAULT_REGION,
        },
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

    entry.runtime_data._settle_until = 0
    return entry


async def test_status_outage_does_not_make_event_entity_unavailable(
    hass: HomeAssistant, ezviz_client: MagicMock
) -> None:
    """The status REST call is unrelated to the push/message event paths."""
    entry = await _setup(hass, ezviz_client)
    coordinator = entry.runtime_data

    await coordinator._async_handle_push(
        {
            "alert": "AI Human Detection",
            "ext": {
                "device_serial": SERIAL,
                "alert_type_code": 10120,
                "msgId": "before-outage",
            },
        }
    )
    await hass.async_block_till_done()

    before = hass.states.get("event.front_door_motion")
    assert before is not None
    assert before.state != STATE_UNAVAILABLE

    ezviz_client.get_device_infos.side_effect = PyEzvizError("cloud offline")
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    after = hass.states.get("event.front_door_motion")
    assert after is not None
    assert after.state == before.state
    assert after.state != STATE_UNAVAILABLE


async def test_replayed_push_does_not_retrigger_event_entity(
    hass: HomeAssistant, ezviz_client: MagicMock
) -> None:
    """A reconnect may replay the last MQTT message with the same msgId."""
    entry = await _setup(hass, ezviz_client)
    coordinator = entry.runtime_data

    message = {
        "alert": "AI Human Detection",
        "ext": {
            "device_serial": SERIAL,
            "alert_type_code": 10120,
            "msgId": "replayed-after-reconnect",
            "time": "2026-09-03 09:56:42",
        },
    }

    await coordinator._async_handle_push(message)
    await hass.async_block_till_done()
    first = hass.states.get("event.front_door_motion").state

    # Simulate a reconnect after the coordinator's short cross-path dedupe
    # memory has expired.
    coordinator._recent.clear()
    await coordinator._async_handle_push(message)
    await hass.async_block_till_done()

    assert hass.states.get("event.front_door_motion").state == first

    # A genuinely new message must still trigger normally.
    coordinator._recent.clear()
    new_message = {
        **message,
        "ext": {
            **message["ext"],
            "msgId": "genuinely-new",
            "time": "2026-09-03 09:57:10",
        },
    }
    await coordinator._async_handle_push(new_message)
    await hass.async_block_till_done()

    assert hass.states.get("event.front_door_motion").state != first
