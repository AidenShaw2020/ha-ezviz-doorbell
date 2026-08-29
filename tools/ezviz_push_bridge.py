"""EZVIZ push -> Home Assistant webhook most.

HA integrace 'ezviz' umi jen 30s cloud polling a zvoneni vubec nezachyti.
Tento skript se pripoji na EZVIZ MQTT push (stejny kanal, co pouziva mobilni
appka) a kazdou udalost preposle do HA jako webhook.

Diagnosticky rezim (bez --webhook-url) jen vypisuje udalosti, aby sis nasel
alert_type_code pro stisk tlacitka.

Pouziti:
  python ezviz_push_bridge.py -u <ucet> -p <heslo>
  python ezviz_push_bridge.py -u <ucet> -p <heslo> \
      --webhook-url http://homeassistant.local:8123/api/webhook/ezviz_push
"""

from __future__ import annotations

import argparse
from getpass import getpass
import json
import logging
from pathlib import Path
import sys
import time
from typing import Any

import requests

from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import EzvizAuthVerificationCode, PyEzvizError

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s"
)
_LOGGER = logging.getLogger("ezviz_bridge")

LOG_FILE = Path("ezviz_push.jsonl")


def build_handler(
    webhook_url: str | None, serial_filter: str | None
) -> Any:
    """Vytvori callback pro prichozi MQTT zpravy."""

    def handler(msg: dict[str, Any]) -> None:
        ext = msg.get("ext")
        ext = ext if isinstance(ext, dict) else {}

        serial = ext.get("device_serial")
        if serial_filter and serial != serial_filter:
            _LOGGER.debug("Preskakuji zarizeni %s", serial)
            return

        payload = {
            "serial": serial,
            "device_name": ext.get("device_name"),
            "alert_type_code": ext.get("alert_type_code"),
            "time": ext.get("time"),
            "channel_no": ext.get("channel_no"),
            "is_encrypted": ext.get("is_encrypted"),
            "pic_url": ext.get("default_pic_url"),
            "msg_id": ext.get("msgId"),
            "alert": msg.get("alert"),
            "raw": msg,
        }

        _LOGGER.info(
            ">>> UDALOST  serial=%s  alert_type_code=%s  alert=%s  cas=%s",
            payload["serial"],
            payload["alert_type_code"],
            payload["alert"],
            payload["time"],
        )

        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

        if not webhook_url:
            return

        try:
            resp = requests.post(webhook_url, json=payload, timeout=10)
            if resp.status_code >= 400:
                _LOGGER.warning(
                    "Webhook vratil HTTP %s: %s", resp.status_code, resp.text[:200]
                )
        except requests.RequestException as err:
            _LOGGER.error("Webhook selhal: %r", err)

    return handler


def login(args: argparse.Namespace) -> EzvizClient:
    """Prihlasi se do EZVIZ cloudu, s podporou MFA."""
    username = args.username or input("EZVIZ ucet: ")
    password = args.password or getpass("EZVIZ heslo: ")

    client = EzvizClient(username, password, args.region)
    try:
        client.login()
    except EzvizAuthVerificationCode:
        code = input("Vyzadovan MFA kod: ").strip()
        client.login(sms_code=int(code) if code.isdigit() else None)
    return client


def main(argv: list[str] | None = None) -> int:
    """Vstupni bod."""
    parser = argparse.ArgumentParser(prog="ezviz_push_bridge")
    parser.add_argument("-u", "--username", help="EZVIZ ucet")
    parser.add_argument("-p", "--password", help="EZVIZ heslo")
    parser.add_argument("-r", "--region", default="apiieu.ezvizlife.com")
    parser.add_argument(
        "--webhook-url",
        help="HA webhook URL. Bez nej skript jen vypisuje udalosti (diagnostika).",
    )
    parser.add_argument(
        "--serial", help="Posilat jen udalosti tohoto seznamu (jedno seriove cislo)."
    )
    args = parser.parse_args(argv)

    if not args.webhook_url:
        _LOGGER.info(
            "DIAGNOSTICKY REZIM: udalosti se jen vypisuji do konzole "
            "a do %s. Zazvon a sleduj alert_type_code.",
            LOG_FILE,
        )

    handler = build_handler(args.webhook_url, args.serial)

    while True:
        mqtt_client = None
        try:
            client = login(args)
            mqtt_client = client.get_mqtt_client(on_message_callback=handler)
            mqtt_client.connect()
            _LOGGER.info("Pripojeno k EZVIZ push. Ctrl+C pro ukonceni.")
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            _LOGGER.info("Koncim.")
            if mqtt_client:
                mqtt_client.stop()
            return 0
        except (PyEzvizError, OSError) as err:
            _LOGGER.error("Spojeni selhalo (%r), zkousim znovu za 60 s", err)
            if mqtt_client:
                try:
                    mqtt_client.stop()
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
