# EZVIZ Doorbell Push

A Home Assistant **add-on** that gives EZVIZ doorbells the things the built-in
integration cannot: a real-time **doorbell press event** told apart from motion,
a **decrypted snapshot** you can keep encryption switched on for, a **live view**
for a camera with no RTSP server, a **wake button** for a hibernating battery
device, and the **full set of device entities** — battery, switches, work mode,
firmware and the rest.

It runs beside the built-in `ezviz` integration and changes nothing about it.
Nothing is replaced, nothing is overridden — remove the add-on and you are back
where you started.

> **There is now an integration that does the same job**, and it does the two
> things this add-on cannot: it creates a real camera entity for the live view
> instead of handing you a URL, and it asks for the two factor code in a dialog
> rather than through an option you have to restart for. It also needs no MQTT
> broker. See the [repository README](https://github.com/AidenShaw2020/ha-ezviz-doorbell).
> This add-on stays supported; run whichever suits you, but not both against the
> same doorbell, or every event will arrive twice.

---

## Why

### A doorbell press never reaches Home Assistant

The built-in integration is `cloud_polling` on a 30 second interval and ships no
`event` platform, so there is nothing for a button press to arrive on. It also
polls only the *alarm* feed, and a ring is not an alarm — see below — so the
press stays invisible even between polls. Reported upstream in
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

### The same failed RTSP check costs you everything else

That camera config entry is also what carries the switches, the sensors and the
live view. A battery doorbell that never passes the RTSP check therefore ends up
with almost no entities at all. This add-on builds them from the cloud API
instead, which is the same place the EZVIZ app reads them from, so no RTSP
server has to exist.

---

## What you get

Entities are created automatically through MQTT discovery — no YAML. Everything
below appears under one device per camera.

| Platform | Entities |
| --- | --- |
| `event` | **Doorbell** (ring only), **Motion** (motion only), **Alerts** (every event, with the raw code) |
| `binary_sensor` | Doorbell button, Motion detected, Online, Video encryption, Alarm schedule |
| `sensor` | Battery, Last event, Last ring, Last motion, Last alarm type, Last alarm time, Seconds since last trigger, Wi-Fi signal, Wi-Fi network, Local IP, WAN IP, Firmware, PIR status, Storage capacity, Live stream URL, Snapshot URL |
| `switch` | Motion detection, Alarm notifications, Doorbell notifications, plus every hardware switch the device reports (status light, audio, sleep, infrared, human detection, tamper alarm, …) |
| `number` | Detection sensitivity |
| `select` | Work mode, Night vision, Image style, Alarm sound, Detection type |
| `button` | **Wake camera**, Take snapshot, Refresh status, Reboot |
| `siren` | Siren — sounds the camera's own alarm |
| `image` | Last snapshot, decrypted |
| `update` | Firmware, with an Install button |

The hardware switches are announced from what the device itself reports, so you
get the ones your model has and no placeholders for the ones it does not.

Event entities carry `alert_type_code`, `alert`, `time`, `msg_id` and `source`
as attributes.

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
poll_interval: 5      # seconds; 0 disables the polling fallback
status_interval: 60   # seconds between device status refreshes; 0 disables them
live_stream: true     # serve live video over HTTP, see below
snapshot_interval: 3  # seconds between frames of the MJPEG fallback
ring_codes: []        # extra alert codes to treat as a ring
motion_codes: []      # extra alert codes to treat as motion
log_level: info
```

`live_stream_token` and `live_stream_url_base` override the live view's
generated token and its published base URL. `mqtt_host` / `mqtt_port` /
`mqtt_username` / `mqtt_password` are optional and only needed for a broker the
Supervisor does not manage.

## A ring is not an alarm, and neither is motion

Both used to arrive as the generic `alarm` event type, which made the two
indistinguishable in an automation. They are now separated three ways.

**Separate entities.** `event.<device>_doorbell` fires only for a press,
`event.<device>_motion` only for motion, and the momentary
`binary_sensor.<device>_doorbell_button` and
`binary_sensor.<device>_motion_detected` mirror them for cards and blueprints
that expect a binary sensor. `event.<device>_alerts` still carries everything,
raw code included, so nothing is lost.

**Wider classification.** Each message is now decided in this order, and the
reason is written to the log next to the event:

1. a message that carries a call status is a ring — EZVIZ models a doorbell
   press as an incoming call, not an alarm
2. the alert code, looked up in the table for the path it arrived on
3. the codes you added under `ring_codes` / `motion_codes`
4. the alert code looked up in the *other* path's table
5. the wording of the title EZVIZ sends with the message, matched whole-word
   against a list that covers English, Czech, German, French and Spanish
6. anything still unrecognised stays `alarm`, with its raw code in the log and
   in the event attributes

So a model whose codes nobody has catalogued yet is still classified correctly
from its own message text, and a code you identify from the log can be mapped
permanently by adding it to `ring_codes` or `motion_codes` — no rebuild.

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

> **Changing the default does not change your install.** The Supervisor keeps
> the options you already saved, so an add-on update never rewrites
> `poll_interval` for you. If rings feel slow, check the value in the
> Configuration tab against the `poll_interval=` figure the add-on logs on
> startup.

#### Burst polling keeps the ring quick without polling hard all day

Polling every few seconds around the clock to catch an event that happens twice
a day is wasteful, but a slow poll makes the doorbell feel broken. So the poller
has two speeds.

Motion *does* arrive over push, instantly — and someone reaching for the button
has nearly always tripped motion on the way in. Any push is therefore treated as
an early warning: it wakes the poller immediately and drops it to one poll a
second for the next 30 seconds, which is where the press lands. Outside that
window it idles back to `poll_interval`.

In practice the ring shows up about a second after the press, while the steady
state stays at one request every `poll_interval` seconds. A ring with no
preceding motion falls back to the slow path, so `poll_interval` still sets the
worst case — `0` switches polling off entirely.

The first sweep after a start only records what already exists, so old messages
are not replayed as fresh events.

## Live view without RTSP

A battery doorbell has no RTSP server, so there is nothing for a camera entity
to point at. What the EZVIZ app plays instead is the cloud stream, and the
add-on can open the same stream, remux it with FFmpeg and serve it on an
ordinary URL. It listens on port **8099** and offers four URLs per camera:

| URL | What it is |
| --- | --- |
| `/<serial>/live.ts` | the cloud live stream as MPEG-TS — the real thing |
| `/<serial>/snapshot.jpg` | one freshly captured picture, taken on demand |
| `/<serial>/mjpeg` | those captures as an MJPEG stream, one every `snapshot_interval` seconds |
| `/<serial>/last.jpg` | the most recent alarm snapshot, already decrypted |

The full URLs, token included, are published as the **Live stream URL** and
**Snapshot URL** sensors, and listed on the add-on's own index page at
`http://<your-home-assistant>:8099/?token=…`.

### Turning that into a camera entity

MQTT discovery has a `camera` platform, but it only takes images — there is no
field in it for a stream source. No add-on can therefore create a video camera
entity by itself; that takes an integration, which is why the built-in `ezviz`
integration can show one and this add-on hands you a URL instead. Wiring it up
is a one-time paste:

1. **Start the add-on and open its log.** On the first status refresh it prints
   both values for every camera, ready to copy:

   ```
   Live video for Front door is ready. ... paste
       Still Image URL:    http://<add-on>:8099/D1234567/snapshot.jpg?token=…
       Stream Source URL:  http://<add-on>:8099/D1234567/live.ts?token=…
   ```

   The same two values are always available as the **Snapshot URL** and **Live
   stream URL** sensors on the device.

2. **Settings → Devices & services → Add integration → Generic Camera.**
3. Paste the first value into *Still Image URL* and the second into *Stream
   Source URL*.
4. Leave *Username* and *Password* empty — the token in the URL is the
   authentication — and leave *RTSP transport protocol* alone; it is ignored for
   an HTTP source.
5. Confirm the preview. You now have a `camera.` entity with live video,
   snapshots and recording, usable on any picture card.

Home Assistant validates both URLs during that step, which means it fetches a
picture — expect it to take a few seconds while a sleeping doorbell wakes up.

Notes worth knowing before you judge the result:

- **Video encryption limits it to a clip.** An encrypted cloud stream has to be
  collected in full before it can be decrypted, so with encryption on the add-on
  serves a 15 second clip rather than a continuous stream. Switch video
  encryption off in the EZVIZ app for a real live view. Image encryption is
  separate and can stay on — snapshots are decrypted either way.
- **Every URL carries a token.** It is generated once, kept in the add-on's
  storage, and included in the published URLs. Set `live_stream_token` to pin
  your own.
- **The published URL uses the add-on's container hostname**, which is what
  Home Assistant itself resolves. To reach it from a browser on your network,
  use your Home Assistant host's address and the port shown in the add-on's
  Network panel, or set `live_stream_url_base` to the address you want
  published.
- **A cloud stream is a cloud stream.** It costs bandwidth on both ends and it
  is slower to start than RTSP would be. Set `live_stream: false` to switch the
  server off entirely.

## Waking a sleeping camera

A battery doorbell hibernates between events, and while it sleeps it answers
nothing — no live view, no fresh snapshot. The **Wake camera** button does what
the app does when you open the live view, as three separate requests: it turns
the sleep switch off, asks the cloud to keep the device awake, and takes a
picture, which is what actually pulls the device onto the network. Each step is
allowed to fail on its own — which of them a model honours depends on its
firmware — and the log says which ones were accepted.

The live stream and the snapshot URL ask the cloud to delay sleep before they
open, so opening the camera in Home Assistant usually wakes it without pressing
anything.

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

| Code | Path | Event type |
| --- | --- | --- |
| `subType 2701` | poll | `ring` — doorbell button |
| any message with a `callingStatus` | poll | `ring` |
| `alert_type_code 10120` | push | `motion` — AI human detection |
| `alert_type_code 10000` | push | `motion` — PIR |
| `alert_type_code 0` | push | `ring` |
| anything else | either | matched on the message text, else `alarm` with the raw code kept in the attributes |

Motion arrives over push within a second or two; a ring usually only arrives by
polling, about a second after the press thanks to the burst described above.
Both paths are live at once, so events carry `"source": "push"` or
`"source": "poll"`, and a detection seen on both is emitted once — the second
copy is suppressed and logged as a skipped duplicate.

`2701` and `10120` were captured from a live EP8x; `0` and `10000` come from
[RenierM26/ha-ezviz#112](https://github.com/RenierM26/ha-ezviz/issues/112) on a
DP1C. Anything unmapped is logged with its raw code — add it to `ring_codes` or
`motion_codes` in the options, or to `PUSH_ALERT_TYPES` / `POLL_SUBTYPES` in
`const.py` if you would rather it shipped with the add-on.

## Automation example

```yaml
automation:
  - alias: "Doorbell pressed"
    triggers:
      - trigger: state
        entity_id: event.front_door_doorbell
    actions:
      - action: notify.mobile_app_phone
        data:
          message: "Someone is at the door"
          data:
            image: /api/camera_proxy/image.front_door_last_snapshot
```

The doorbell event entity only ever fires for a press, so no condition on the
event type is needed. Use `event.front_door_motion` for motion.

---

## Status

Confirmed working against an EP8x: the ring is delivered, mapped to `ring`, and
its encrypted snapshot decrypts. It arrives over the polling path — the alarm
push channel stays silent for a ring, for the reason described above.

Motion is confirmed too, arriving over push almost instantly as AI human
detection. Anything unmapped is still reported as `alarm` with its raw code in
the log and in the event attributes, which is what you need to extend the maps.

The device entities, the live view and the wake button are built on the same
cloud API the EZVIZ app uses, through `pyezvizapi`. Which of them a given model
answers varies by firmware: everything is announced from what the device itself
reports, and a request a device refuses is logged rather than retried.

## tools/

Standalone scripts, useful before or instead of installing the add-on. Both
prompt for credentials interactively and store nothing.

- `ezviz_diag.py` — dumps a device's pagelist, switch list, `isEncrypt` flag and
  latest alarm, then downloads the snapshot and reports whether it is encrypted.
- `ezviz_push_bridge.py` — the same push listener as a plain script, forwarding
  to a Home Assistant webhook. Handy for discovering your alert codes.
- `make_icons.py` — resizes `assets/` into the add-on's `icon.png` and
  `logo.png`.

## Notes

The add-on logs in from its own container, which gives it a `featureCode`
distinct from Home Assistant's, so it does not disturb the built-in
integration's session or the EZVIZ app's push notifications.

## License

Apache-2.0.
