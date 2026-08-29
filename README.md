# ha-ezviz-doorbell

Real-time doorbell events for the Home Assistant **EZVIZ** integration, plus a fix
for the broken "Last motion image" on battery-powered doorbells (EP8x, DP2, DB2,
EP3x and friends).

This is not a separate integration. It builds a **drop-in fork** of the core
`ezviz` integration into `custom_components/ezviz/`, so it keeps the same domain,
your existing config entries, entity IDs and history. One cloud login, no
duplicate entities.

---

## Why this exists

### 1. A doorbell press never reaches Home Assistant

The core integration is `cloud_polling` with a 30 second interval and ships no
`event` platform at all. The button press is delivered over the EZVIZ **push**
channel and never shows up in the polled alarm feed, so there is nothing for
Home Assistant to see — motion gets through, the ring does not.

Reported upstream in
[home-assistant/core#99813](https://github.com/home-assistant/core/issues/99813)
(DP2) and
[home-assistant/core#130339](https://github.com/home-assistant/core/issues/130339)
(EP3x Pro). The latter was closed as stale, so this is not going to fix itself.

### 2. "Last motion image" shows a broken image

EZVIZ encrypts alarm snapshots — the files start with the magic bytes
`hikencodepicture`. The image entity decrypts them only when both of these hold:

```python
if self.data["encrypted"] and self.alarm_image_password is not None:
    image_data = decrypt_image(response.content, self.alarm_image_password)
```

and `alarm_image_password` comes from a **separate config entry** whose
`unique_id` is the camera serial:

```python
camera = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, serial)
```

That entry is created by the discovery flow — but only *after* a successful
authenticated RTSP `DESCRIBE`. A hibernating battery doorbell either fails that
check or has no RTSP server at all, so the entry is never created, the password
stays `None`, and Home Assistant hands the browser encrypted bytes labelled
`image/jpeg`. Result: a broken image icon.

Turning off image encryption in the EZVIZ app works around it, at the cost of
leaving the stream and cloud snapshots unencrypted.

---

## What the fork changes

| File | Change |
| --- | --- |
| `push.py` | **new** — keeps a long-lived EZVIZ MQTT push connection open and dispatches each decoded message on a per-serial signal |
| `event.py` | **new** — one `event` entity per camera, device class `doorbell`, event types `ring` / `motion` / `alarm` |
| `__init__.py` | registers `Platform.EVENT`, starts the push manager, stops it on unload |
| `config_flow.py` | a failing RTSP check no longer blocks entry creation, so the verification code can be stored and snapshots decrypt |
| `manifest.json` | marks it a custom integration and pins `pyezvizapi==1.0.5.0` |

### Why the library gets pinned

Home Assistant currently ships `pyezvizapi==1.0.0.7`, whose `MQTTClient` is a
bare `threading.Thread` with no message callback and no `ext` decoding. Version
1.0.5.0 adds `EzvizClient.get_mqtt_client(on_message_callback=...)` and decodes
the comma-separated `ext` payload into named fields, including
`alert_type_code`.

Comparing the two client APIs, **no public method was removed** between them and
every method the Home Assistant platforms call still exists, so the bump is
additive. Because the fork replaces the core integration under the same domain,
the core manifest never loads and the two pins cannot conflict.

---

## Install

Find your Home Assistant version under **Settings → About**, then:

```bash
python build_ezviz_fork.py --ha-version 2026.8.3 --out ./custom_components/ezviz
```

The script downloads the core `ezviz` sources **for that exact version**, applies
the patches, drops in `push.py` and `event.py`, and syntax-checks every resulting
file. Copy the produced `custom_components/ezviz/` into your `/config/`, then
restart Home Assistant.

Each patch is anchored to an exact snippet and refuses to apply if that snippet
is not found exactly once — if a future Home Assistant release moves the code,
the build fails loudly instead of producing something subtly broken.

---

## Alert type codes

| `alert_type_code` | Event type |
| --- | --- |
| `0` | `ring` — doorbell button |
| `10000` | `motion` — PIR |
| anything else | `alarm`, with the raw code in the attributes |

Codes observed in
[RenierM26/ha-ezviz#112](https://github.com/RenierM26/ha-ezviz/issues/112).

If your ring arrives as `alarm`, open the event entity, read `alert_type_code`
from its attributes and add it to `ALERT_TYPE_MAP` in `event.py`.

## Automation example

```yaml
automation:
  - alias: "Doorbell pressed"
    triggers:
      - trigger: state
        entity_id: event.doorbell_alerts
        attribute: event_type
        to: ring
    actions:
      - action: notify.mobile_app_phone
        data:
          message: "Someone is at the door"
```

---

## Status

The patcher and both new modules are written and syntax-checked. The **end-to-end
build has not been validated against genuine Home Assistant sources**: in the
environment where this was written, every fetch of `home-assistant/core` returned
sources that are not valid Python — `config_flow.py` and `number.py` both came
back with Python 2 style `except A, B:` clauses, identically over three
independent channels (raw.githubusercontent, jsDelivr, and base64 via the GitHub
API). That is not something that can exist in a released Home Assistant.

So the build script now verifies its own output and refuses to write an
installable component if anything fails to parse. If you hit that error, the
download is corrupted on your side too — do not install it; try another network
or fetch the sources by hand.

## tools/

- `ezviz_diag.py` — dumps a device's pagelist, switch list, `isEncrypt` flag and
  latest alarm, then downloads the snapshot and reports whether it is encrypted.
- `ezviz_push_bridge.py` — standalone EZVIZ push listener that forwards events to
  a Home Assistant webhook. Useful for discovering alert codes before installing
  the fork, or as a fallback if you would rather not replace the integration.

Both prompt for credentials interactively and never store them.

## License

Apache-2.0, matching Home Assistant core. `event.py` and `push.py` are written
against the core integration's internal API and are derived works of it. No core
source is redistributed here — the build script fetches it at build time.
