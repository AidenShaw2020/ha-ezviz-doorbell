"""Adding an EZVIZ account, and changing how it is polled afterwards.

The add-on this integration grew out of asked for a two factor code through a
YAML option, which meant editing configuration and restarting to get past a
dialog the cloud only ever shows once. Here it is a step in the flow, and the
same step comes back on its own if EZVIZ ever asks again.

EZVIZ says quite precisely why it turned a login down - a wrong password, a
code that has expired, an account it has locked after too many tries - and all
of it arrives as the text of one exception type. That text is what decides
which error the form shows, and it is logged either way, because a login that
fails silently is the one thing nobody can debug.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

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
from .vendor.pyezvizapi.client import EzvizClient
from .vendor.pyezvizapi.exceptions import EzvizAuthVerificationCode, InvalidURL, PyEzvizError

_LOGGER = logging.getLogger(__name__)

CONF_CODE = "code"
CONF_RESEND = "resend"

ACCOUNT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Required(CONF_REGION, default=DEFAULT_REGION): cv.string,
    }
)

MFA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_CODE, default=""): cv.string,
        vol.Optional(CONF_RESEND, default=False): cv.boolean,
    }
)


def _error_for(message: str) -> tuple[str, str]:
    """Return the form field and error key for what EZVIZ said.

    The library reports every one of these as a plain ``PyEzvizError`` whose
    text is the only thing telling them apart, so the text is what is matched.
    Anything unrecognised is reported as unknown rather than guessed at.
    """
    lowered = message.lower()
    if "mfa code is invalid" in lowered:
        return CONF_CODE, "invalid_code"
    if "locked" in lowered:
        return "base", "account_locked"
    if "incorrect username" in lowered:
        return CONF_USERNAME, "invalid_auth"
    if "incorrect password" in lowered:
        return CONF_PASSWORD, "invalid_auth"
    return "base", "unknown"


class EzvizDoorbellConfigFlow(ConfigFlow, domain=DOMAIN):
    """Walk through adding one EZVIZ account."""

    VERSION = 1

    def __init__(self) -> None:
        """Start with nothing entered and no session."""
        self._account: dict[str, Any] = {}
        # One client for the whole flow. EZVIZ binds a two factor code to the
        # terminal that asked for it, so the step that asks and the step that
        # answers have to be the same client.
        self._client: EzvizClient | None = None

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the EZVIZ account."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._account = dict(user_input)
            self._client = None
            await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
            self._abort_if_unique_id_configured()

            if (result := await self._async_login(errors)) is not None:
                return result

        return self.async_show_form(
            step_id="user", data_schema=ACCOUNT_SCHEMA, errors=errors
        )

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the code EZVIZ has just emailed.

        A code is short lived, and one that has expired can only be replaced by
        asking for another - so the form can do that rather than making anyone
        start the whole flow again.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            code = str(user_input.get(CONF_CODE) or "").strip()

            if user_input.get(CONF_RESEND):
                if await self._async_send_code():
                    errors["base"] = "code_sent"
                else:
                    errors["base"] = "code_not_sent"
            elif not code.isdigit():
                errors[CONF_CODE] = "invalid_code"
            elif (result := await self._async_login(errors, code=int(code))) is not None:
                return result

        return self.async_show_form(
            step_id="mfa", data_schema=MFA_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start again for an account whose session EZVIZ has dropped."""
        self._account = dict(entry_data)
        self._client = None
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the password, which is usually all that is needed."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._account = {**self._account, **user_input}
            self._client = None
            if (result := await self._async_login(errors)) is not None:
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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> OptionsFlow:
        """Return the options flow."""
        return EzvizDoorbellOptionsFlow()

    # ------------------------------------------------------------------
    # Logging in
    # ------------------------------------------------------------------

    def _login(self, code: int | None) -> dict[str, Any]:
        """Log in and return the session (executor thread).

        Raises:
            EzvizAuthVerificationCode: If EZVIZ wants a two factor code, which
                it emails as it raises this.
            PyEzvizError: If it turns the login down, with its reason as text.
        """
        if self._client is None:
            self._client = EzvizClient(
                self._account[CONF_USERNAME],
                self._account[CONF_PASSWORD],
                self._account.get(CONF_REGION, DEFAULT_REGION),
            )
        # login() hands the session back in every version of the library, while
        # export_token() only exists in the newer ones - and reaching for it on
        # an older one turned a login that had just succeeded into an unhandled
        # AttributeError, which is a miserable way to lose a two factor code.
        token = self._client.login(sms_code=code)
        if not token and hasattr(self._client, "export_token"):
            token = self._client.export_token()
        return dict(token or {})

    async def _async_login(
        self, errors: dict[str, str], code: int | None = None
    ) -> ConfigFlowResult | None:
        """Log in, and return the finished flow if that worked.

        Returns None when the caller should show its form again, with either an
        error filled in or the two factor step queued up.
        """
        try:
            token = await self.hass.async_add_executor_job(self._login, code)

        except EzvizAuthVerificationCode:
            if code is not None:
                # The code was refused at the point of use rather than by the
                # login itself, which means it is no longer good.
                _LOGGER.error("EZVIZ did not accept the two factor code")
                errors[CONF_CODE] = "invalid_code"
                return None
            _LOGGER.debug("EZVIZ wants a two factor code; it has emailed one")
            return await self.async_step_mfa()

        except InvalidURL as err:
            _LOGGER.error("Could not reach the EZVIZ region address: %s", err)
            errors["base"] = "invalid_region"
            return None

        except PyEzvizError as err:
            # Everything EZVIZ says about a refused login arrives here, and it
            # is worth reading, so it is logged whether or not it is recognised.
            field, reason = _error_for(str(err))
            _LOGGER.error("EZVIZ refused the login: %s", err)
            errors[field] = reason
            return None

        except OSError as err:
            _LOGGER.error("Could not reach EZVIZ: %s", err)
            errors["base"] = "cannot_connect"
            return None

        except Exception:  # noqa: BLE001
            # Nothing should reach this, and if something does, the log is the
            # only way anyone finds out what it was.
            _LOGGER.exception("Unexpected error logging in to EZVIZ")
            errors["base"] = "unknown"
            return None

        data = {**self._account, CONF_TOKEN: token}

        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates=data
            )

        return self.async_create_entry(title=self._account[CONF_USERNAME], data=data)

    async def _async_send_code(self) -> bool:
        """Ask EZVIZ for a fresh two factor code."""

        def _send() -> bool:
            if self._client is None:
                return False
            self._client.send_mfa_code()
            return True

        try:
            sent = await self.hass.async_add_executor_job(_send)
        except (PyEzvizError, OSError) as err:
            _LOGGER.error("Could not ask EZVIZ for a new code: %s", err)
            return False
        return sent


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
