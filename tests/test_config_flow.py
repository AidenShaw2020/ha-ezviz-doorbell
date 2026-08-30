"""Signing in, which is where a config flow usually goes wrong.

EZVIZ reports a wrong password, an expired two factor code and a locked
account all as the same exception type, telling them apart only by the text
inside it. These tests are about that text ending up as the right message on
the right field - and about the log always saying what happened, because the
first version of this flow failed silently and left nothing to go on.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest

from custom_components.ezviz_doorbell.const import (
    CONF_REGION,
    CONF_TOKEN,
    DEFAULT_REGION,
    DOMAIN,
)

# The integration carries its own copy of the library, so these have to be the
# exception classes it will actually be catching.
from custom_components.ezviz_doorbell.vendor.pyezvizapi.exceptions import (  # noqa: E402
    EzvizAuthVerificationCode,
    PyEzvizError,
)

ACCOUNT = {
    CONF_USERNAME: "cloud@example.invalid",
    CONF_PASSWORD: "secret",
    CONF_REGION: DEFAULT_REGION,
}
TOKEN = {"session_id": "session", "api_url": "api"}


@pytest.fixture
def client() -> MagicMock:
    """Return the EZVIZ client the flow will build."""
    client = MagicMock()
    client.login.return_value = TOKEN
    client.export_token.return_value = TOKEN
    return client


@pytest.fixture(autouse=True)
def skip_setup():
    """Keep these tests about the flow, not about what it starts."""
    with patch(
        "custom_components.ezviz_doorbell.async_setup_entry", return_value=True
    ) as mock:
        yield mock


async def _start(hass: HomeAssistant) -> str:
    """Open the flow and return its id."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result["flow_id"]


async def test_a_plain_login(hass: HomeAssistant, client: MagicMock) -> None:
    """An account without two factor goes straight in."""
    flow_id = await _start(hass)
    with patch(
        "custom_components.ezviz_doorbell.config_flow.EzvizClient",
        return_value=client,
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, ACCOUNT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == ACCOUNT[CONF_USERNAME]
    assert result["data"][CONF_TOKEN] == TOKEN


async def test_two_factor(hass: HomeAssistant, client: MagicMock) -> None:
    """EZVIZ asks for a code, and the same client has to answer it.

    The cloud binds a code to the terminal that asked for it, so a second
    client would be handed a code that was never meant for it.
    """
    client.login.side_effect = [EzvizAuthVerificationCode("code required"), TOKEN]

    flow_id = await _start(hass)
    with patch(
        "custom_components.ezviz_doorbell.config_flow.EzvizClient",
        return_value=client,
    ) as constructor:
        result = await hass.config_entries.flow.async_configure(flow_id, ACCOUNT)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "mfa"

        result = await hass.config_entries.flow.async_configure(
            flow_id, {"code": "123456", "resend": False}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert constructor.call_count == 1, "the code must go to the client that asked"
    client.login.assert_called_with(sms_code=123456)


async def test_a_refused_code_says_so(
    hass: HomeAssistant, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """A code EZVIZ rejects belongs on the code field, and in the log."""
    client.login.side_effect = [
        EzvizAuthVerificationCode("code required"),
        PyEzvizError("The MFA code is invalid, please try again."),
    ]

    flow_id = await _start(hass)
    with (
        patch(
            "custom_components.ezviz_doorbell.config_flow.EzvizClient",
            return_value=client,
        ),
        caplog.at_level(logging.ERROR),
    ):
        await hass.config_entries.flow.async_configure(flow_id, ACCOUNT)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"code": "000000", "resend": False}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa"
    assert result["errors"] == {"code": "invalid_code"}
    assert "The MFA code is invalid" in caplog.text


async def test_a_new_code_can_be_asked_for(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """A code expires, and starting the whole flow again to get one is silly."""
    client.login.side_effect = [EzvizAuthVerificationCode("code required"), TOKEN]

    flow_id = await _start(hass)
    with patch(
        "custom_components.ezviz_doorbell.config_flow.EzvizClient",
        return_value=client,
    ):
        await hass.config_entries.flow.async_configure(flow_id, ACCOUNT)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"code": "", "resend": True}
        )

    client.send_mfa_code.assert_called_once()
    assert result["step_id"] == "mfa"
    assert result["errors"] == {"base": "code_sent"}


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("The user is locked.", {"base": "account_locked"}),
        ("Incorrect Password.", {CONF_PASSWORD: "invalid_auth"}),
        ("Incorrect Username.", {CONF_USERNAME: "invalid_auth"}),
        ("Login error: {'code': 1234}", {"base": "unknown"}),
    ],
)
async def test_what_ezviz_said_reaches_the_form(
    hass: HomeAssistant,
    client: MagicMock,
    caplog: pytest.LogCaptureFixture,
    message: str,
    expected: dict[str, str],
) -> None:
    """Each refusal lands on the field it is about, and in the log."""
    client.login.side_effect = PyEzvizError(message)

    flow_id = await _start(hass)
    with (
        patch(
            "custom_components.ezviz_doorbell.config_flow.EzvizClient",
            return_value=client,
        ),
        caplog.at_level(logging.ERROR),
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, ACCOUNT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == expected
    assert message in caplog.text


async def test_an_unexpected_failure_is_logged(
    hass: HomeAssistant, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Whatever else happens, it must not vanish."""
    client.login.side_effect = KeyError("meta")

    flow_id = await _start(hass)
    with (
        patch(
            "custom_components.ezviz_doorbell.config_flow.EzvizClient",
            return_value=client,
        ),
        caplog.at_level(logging.ERROR),
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, ACCOUNT)

    assert result["errors"] == {"base": "unknown"}
    assert "Unexpected error logging in to EZVIZ" in caplog.text
    assert "KeyError" in caplog.text


async def test_a_library_without_export_token(hass: HomeAssistant) -> None:
    """The older pyezvizapi has no export_token, and login() is enough.

    Reaching for it there turned a login that had just succeeded into an
    unhandled AttributeError - the flow died, the log said nothing, and the two
    factor code that had just been spent was gone.
    """
    old_client = MagicMock(spec=["login", "send_mfa_code"])
    old_client.login.return_value = TOKEN

    flow_id = await _start(hass)
    with patch(
        "custom_components.ezviz_doorbell.config_flow.EzvizClient",
        return_value=old_client,
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, ACCOUNT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKEN] == TOKEN
