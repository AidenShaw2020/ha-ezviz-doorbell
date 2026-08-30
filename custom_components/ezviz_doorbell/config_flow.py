"""Adding an EZVIZ account, and changing how it is polled afterwards.

The add-on this integration grew out of asked for a two factor code through a
YAML option, which meant editing configuration and restarting to get past a
dialog the cloud only ever shows once. Here it is a step in the flow, and the
same step comes back on its own if EZVIZ ever asks again.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import (
    EzvizAuthVerificationCode,
    InvalidURL,
    PyEzvizError,
)
import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_LIVE_STREAM,
    CONF_MOTION_CODES,
    CONF_POLL_INTERVAL,
    CONF_REGION,
    CONF_RING_CODES,
    CONF_SNAPSHOT_INTERVAL,
    CONF_STATUS_INTERVAL,
    CONF_TOKEN,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_REGION,
    DEFAULT_SNAPSHOT_INTERVAL,
    DEFAULT_STATUS_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

ACCOUNT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Required(CONF_REGION, default=DEFAULT_REGION): cv.string,
    }
)

MFA_SCHEMA = vol.Schema({vol.Required("code"): cv.string})


def _login(username: str, password: str, region: str, code: int | None) -> dict:
    """Log in and return the session (executor thread).

    Raises:
        EzvizAuthVerificationCode: If EZVIZ wants a two factor code.
        PyEzvizError: If the credentials are refused.
    """
    client = EzvizClient(username, password, region)
    client.login(sms_code=code)
    return client.export_token()


class EzvizDoorbellConfigFlow(ConfigFlow, domain=DOMAIN):
    """Walk through adding one EZVIZ account."""

    VERSION = 1

    def __init__(self) -> None:
        """Start with nothing entered."""
        self._account: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the EZVIZ account."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._account = dict(user_input)
            await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
            self._abort_if_unique_id_configured()

            result = await self._async_try_login(errors)
            if result is not None:
                return result

        return self.async_show_form(
            step_id="user", data_schema=ACCOUNT_SCHEMA, errors=errors
        )

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the code EZVIZ has just emailed."""
        errors: dict[str, str] = {}

        if user_input is not None:
            code = user_input["code"].strip()
            if not code.isdigit():
                errors["code"] = "invalid_code"
            else:
                result = await self._async_try_login(errors, code=int(code))
                if result is not None:
                    return result

        return self.async_show_form(
            step_id="mfa", data_schema=MFA_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start again for an account whose session EZVIZ has dropped."""
        self._account = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the password, which is usually all that is needed."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._account = {**self._account, **user_input}
            result = await self._async_try_login(errors)
            if result is not None:
                return result

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): cv.string,
                    vol.Required(
                        CONF_REGION,
                        default=self._account.get(CONF_REGION, DEFAULT_REGION),
                    ): cv.string,
                }
            ),
            errors=errors,
        )

    async def _async_try_login(
        self, errors: dict[str, str], code: int | None = None
    ) -> ConfigFlowResult | None:
        """Log in, and return the finished flow if that worked.

        Returns None when the caller should show its form again, with either an
        error filled in or the two factor step queued up.
        """
        try:
            token = await self.hass.async_add_executor_job(
                _login,
                self._account[CONF_USERNAME],
                self._account[CONF_PASSWORD],
                self._account.get(CONF_REGION, DEFAULT_REGION),
                code,
            )
        except EzvizAuthVerificationCode:
            if code is not None:
                errors["code"] = "invalid_code"
                return None
            return await self.async_step_mfa()
        except InvalidURL:
            errors["base"] = "invalid_region"
            return None
        except PyEzvizError as err:
            _LOGGER.debug("EZVIZ login failed: %s", err)
            errors["base"] = "invalid_auth"
            return None
        except OSError as err:
            _LOGGER.debug("Could not reach EZVIZ: %s", err)
            errors["base"] = "cannot_connect"
            return None

        data = {**self._account, CONF_TOKEN: token}

        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates=data
            )

        return self.async_create_entry(
            title=self._account[CONF_USERNAME], data=data
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> OptionsFlow:
        """Return the options flow."""
        return EzvizDoorbellOptionsFlow()


class EzvizDoorbellOptionsFlow(OptionsFlow):
    """Change how often the cloud is asked, and what the codes mean."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and save the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3600)),
                vol.Required(
                    CONF_STATUS_INTERVAL,
                    default=options.get(
                        CONF_STATUS_INTERVAL, DEFAULT_STATUS_INTERVAL
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=10, max=86400)),
                vol.Required(
                    CONF_LIVE_STREAM,
                    default=options.get(CONF_LIVE_STREAM, True),
                ): cv.boolean,
                vol.Required(
                    CONF_SNAPSHOT_INTERVAL,
                    default=options.get(
                        CONF_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
                vol.Optional(
                    CONF_RING_CODES,
                    default=options.get(CONF_RING_CODES, ""),
                ): cv.string,
                vol.Optional(
                    CONF_MOTION_CODES,
                    default=options.get(CONF_MOTION_CODES, ""),
                ): cv.string,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
