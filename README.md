# EZVIZ Doorbell Push

A Home Assistant **add-on** that gives EZVIZ doorbells the two things the
built-in integration cannot: a real-time **doorbell press event**, and a
**decrypted snapshot** you can keep encryption switched on for.

It runs beside the built-in `ezviz` integration and changes nothing about it.
Nothing is replaced, nothing is overridden — remove the add-on and you are back
where you started.

---

## Why

### A doorbell press never reaches Home Assistant

The built-in integration is `cloud_polling` on a 30 second interval and ships no
`event` platform. The button press is delivered over the EZVIZ **push** channel
and never appears in the polled alarm feed, so motion gets through and the ring
does not. Reported upstream in
[home-assistant/core#99813](https://github.com/home-assistant/core/issues/99813)
(DP2) and
[home-assistant/core#130339](https://github.com/home-assistant/core/issues/130339)
(EP3x Pro) — the latter closed as stale.

### "Last motion image" is a broken image

EZVIZ encrypts alarm snapshots; the files begin with the magic bytes
`hikencodepicture`. The built-in image entity decrypts them only when a separate
*camera* config entry exists holding the device verification code, and that entry
is created only after a successful authenticated RTSP `DESCRIBE`. A hibernating
battery doorbell fails that check, or has no RTSP server at all, so the entry is
never created, the password stays `None`, and Home Assistant hands the browser
encrypted bytes labelled `image/jpeg`.

This add-on downloads the snapshot itself and decrypts it with the verification
code you give it, so **image encryption can stay on** in the EZVIZ app.

---

## What you get

Entities are created automatically through MQTT discovery — no YAML:

| Entity | Notes |
| --- | --- |
| `event.<device>_alerts` | device class `doorbell`, event types `ring` / `motion` / `alarm` |
| `image.<device>_last_snapshot` | decrypted JPEG from the latest alarm |

The event entity carries `alert_type_code`, `alert`, `time` and `msg_id` as
attributes.

## Install

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add:
   `https://github.com/AidenShaw2020/ha-ezviz-doorbell`
2. Install **EZVIZ Doorbell Push**.
3. Fill in your EZVIZ account in the Configuration tab and start it.

Requires an MQTT broker; the add-on picks up the one the Supervisor already knows
about, so normally there is nothing to configure.

## Options

```yaml
ezviz_username: you@example.com
ezviz_password: your-cloud-password
ezviz_region: apiieu.ezvizlife.com
serials:
  - D1234567          # optional, empty = all devices on the account
verification_codes:
  - serial: D1234567
    code: ABCDEF      # from the device label, needed to decrypt snapshots
mfa_code: ""          # only for the first login, see below
poll_interval: 30     # seconds; 0 disables the polling fallback
log_level: info
```

### Why polling is the path that works for the ring

A button press is not an alarm. Captured from a live EP8x:

```python
{'subType': 2701,
 'title': 'Your doorbell is ringing',
 'ext': {'callingStatus': 1, 'text': 'somebody there ring the door',
         'preTime': 5, 'delayTime': 25},
 'picCrypt': 1, ...}
```

`subType 2701` with a `callingStatus` and call timings — EZVIZ treats a ring as
an incoming **call**, and the MQTT push channel this add-on connects to carries
**alarms**. That is why the push side can report itself connected, subscribed
and granted QoS 2, and still never deliver a ring: it is not on that channel at
all. Motion still arrives over push, when it arrives.

So the add-on also polls the same message feed the official app reads, every
`poll_interval` seconds, and emits anything new — tagged `"source": "poll"` in
the event attributes so the two paths stay distinguishable.

A polled ring is up to `poll_interval` seconds late. `10` is a reasonable
compromise; drop it to `5` if you want the doorbell snappier and don't mind the
extra cloud requests, or set `0` to switch polling off entirely.

The first sweep after a start only records what already exists, so old messages
are not replayed as fresh events.

`mqtt_host` / `mqtt_port` / `mqtt_username` / `mqtt_password` are optional and
only needed for a broker the Supervisor does not manage.

## Two factor authentication

If your EZVIZ account has 2FA, the first start stops with:

```
EZVIZ requires a two factor code, which it has just sent to your email.
```

Paste the digits from that email into **`mfa_code`** in the Configuration tab,
save, and restart the add-on. The session is then written to the add-on's
persistent storage and refreshed automatically from there, so this is a one time
step — clear `mfa_code` again afterwards, the code is single use.

The add-on also pins its own EZVIZ device identity in `/data`. `pyezvizapi`
normally derives that identity from the host MAC address, which a container
changes every time it is recreated — meaning an add-on update would otherwise
invalidate the stored session and ask for a new code.

## Alert type codes

| `alert_type_code` | Event type |
| --- | --- |
| `0` | `ring` — doorbell button (push) |
| `10000` | `motion` — PIR (push) |
| anything else | `alarm`, raw code kept in the attributes |

`subType 2701` maps to `ring` on the polled feed. Push codes observed in
[RenierM26/ha-ezviz#112](https://github.com/RenierM26/ha-ezviz/issues/112), on a
DP1C. If your ring arrives as `alarm`, the log prints the unmapped code and the
event attributes carry it — add it to `ALERT_TYPE_MAP` in `ezviz_push.py`.

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

Written and syntax-checked, **not yet run against a real doorbell**. The alert
code mapping in particular is taken from a DP1C and may differ on other models —
see above for how to correct it.

## tools/

Standalone scripts, useful before or instead of installing the add-on. Both
prompt for credentials interactively and store nothing.

- `ezviz_diag.py` — dumps a device's pagelist, switch list, `isEncrypt` flag and
  latest alarm, then downloads the snapshot and reports whether it is encrypted.
- `ezviz_push_bridge.py` — the same push listener as a plain script, forwarding
  to a Home Assistant webhook. Handy for discovering your alert codes.

## Notes

The add-on logs in from its own container, which gives it a `featureCode`
distinct from Home Assistant's, so it does not disturb the built-in
integration's session or the EZVIZ app's push notifications.

## License

Apache-2.0.
