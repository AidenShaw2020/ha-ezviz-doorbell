# EZVIZ Doorbell

Home Assistant support for EZVIZ doorbells that the built-in `ezviz` integration
cannot handle: the **doorbell press as its own event**, told apart from motion,
a **live camera** for a device with no RTSP server, a **wake button** for one
that hibernates on battery, **decrypted snapshots** you can leave encryption
switched on for, and the **full set of device entities** — battery, switches,
work mode, firmware and the rest.

There are two ways to install it. They do the same job and do not need each
other.

| | **Integration** (`custom_components/ezviz_doorbell`) | **Add-on** (`ezviz_doorbell_push`) |
| --- | --- | --- |
| Live video | a real `camera` entity | a URL you paste into a Generic Camera |
| Two factor code | a step in the setup dialog | an option you type in and restart for |
| Needs an MQTT broker | no | yes |
| Install | HACS or a copy into `config/custom_components` | Add-on Store, from this repository |
| Updates | with a Home Assistant restart | independently, from the Supervisor |

**The integration is the one to use.** The add-on came first, still works, and
is documented in [ezviz_doorbell_push/DOCS.md](ezviz_doorbell_push/DOCS.md); it
will stay until the integration has been proven on real hardware.

Both run beside the built-in `ezviz` integration and change nothing about it.

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

This project downloads the snapshot itself and decrypts it with the key the
cloud hands its own account, so **image encryption can stay on** in the EZVIZ
app.

### The same failed RTSP check costs you everything else

That camera config entry is also what carries the switches, the sensors and the
live view. A battery doorbell that never passes the RTSP check therefore ends up
with almost no entities at all. This project builds them from the cloud API
instead, which is the same place the EZVIZ app reads them from, so no RTSP
server has to exist.

---

## What you get

One device per camera, with:

| Platform | Entities |
| --- | --- |
| `camera` | **Live video** and stills, taken on demand |
| `event` | **Doorbell** (ring only), **Motion** (motion only), **Alerts** (every event, with the raw code) |
| `binary_sensor` | Doorbell button, Motion detected, Online, Video encryption, Alarm schedule |
| `sensor` | Battery, Last event, Last ring, Last motion, Last alarm type, Last alarm time, Seconds since last trigger, Wi-Fi signal, Wi-Fi network, Local IP, WAN IP, PIR status, Storage capacity |
| `switch` | Motion detection, Alarm notifications, Doorbell notifications, plus every hardware switch the device reports (status light, audio, sleep, infrared, human detection, tamper alarm, …) |
| `number` | Detection sensitivity |
| `select` | Work mode, Night vision, Image style, Alarm sound, Detection type |
| `button` | **Wake camera**, Take snapshot, Reboot |
| `siren` | Sounds the camera's own alarm — only for devices that say they can |
| `image` | Last snapshot, decrypted |
| `update` | Firmware, with an Install button |

The hardware switches are built from what the device itself reports, so you get
the ones your model has and no placeholders for the ones it does not. Entity
names are translated; Czech is included.

## Install the integration

**HACS:** add `https://github.com/AidenShaw2020/ha-ezviz-doorbell` as a custom
repository of type *Integration*, install **EZVIZ Doorbell**, restart Home
Assistant.

**By hand:** copy `custom_components/ezviz_doorbell` into your
`config/custom_components/`, restart Home Assistant.

Then **Settings → Devices & services → Add integration → EZVIZ Doorbell** and
sign in with the account the doorbell is registered to. If EZVIZ wants a two
factor code it emails one and the dialog asks for it; that is a one time step,
because the session is stored and refreshed from then on. Needs Home Assistant
2024.12 or newer.

### Options

**Settings → Devices & services → EZVIZ Doorbell → Configure**:

- **Seconds between message polls** (default 5) — a press only ever arrives by
  polling, so this is its worst case delay. See below for why.
- **Seconds between device status refreshes** (default 60) — battery, switches
  and the rest.
- **Offer live video** (default on) — turns the camera's stream on or off.
- **Seconds between still pictures** (default 3) — each one is a round trip to
  the cloud and wakes the camera, so short intervals cost battery.
- **Extra alert codes that mean a ring / motion** — comma separated, only needed
  for a model whose codes are not recognised yet.
- **Cameras to include** — only devices EZVIZ files as a doorbell are used
  unless something is picked here; tick a camera to bring it in as well. (If
  nothing on the account calls itself a doorbell, everything is used and the log
  says so.)
Verification codes are not here — they are part of the account's configuration,
under **⋮ → Reconfigure**. See below.

### It runs alongside the built-in EZVIZ integration

Both talk to EZVIZ through the same library, `pyezvizapi`, and they need
different versions of it: Home Assistant pins `1.0.0.7` for the built-in
integration, while the push channel, on-demand snapshots, the wake request and
the cloud stream only exist from `1.0.5.0`. There is one `site-packages` per
Home Assistant, so installed side by side the two versions overwrite each other
on every restart, and whichever integration lost the race that time breaks.

So this integration does not install the library at all. It carries its own
copy in [custom_components/ezviz_doorbell/vendor/](custom_components/ezviz_doorbell/vendor/),
imports it from there under its own name, and leaves whatever is in
`site-packages` alone. Keep the built-in integration, remove it, install them in
either order — none of it matters any more.

The bundled copy is unmodified pyezvizapi 1.0.5.0, Apache-2.0, from
[RenierM26/pyEzvizApi](https://github.com/RenierM26/pyEzvizApi), with its
licence beside it. Its own dependencies — `requests`, `xmltodict`,
`pycryptodome`, `paho-mqtt` — are declared unpinned, so they cannot start the
same fight.

## A ring is not an alarm, and neither is motion

EZVIZ delivers both down the same pipe and files both as an alarm, which is why
they are so easily confused. They are separated three ways here.

**Separate entities.** `event.<device>_doorbell` fires only for a press,
`event.<device>_motion` only for motion, and the momentary
`binary_sensor.<device>_doorbell_button` and
`binary_sensor.<device>_motion_detected` mirror them for cards and blueprints
that expect a binary sensor. `event.<device>_alerts` still carries everything,
raw code included, so nothing is lost.

**Wider classification.** Each message is decided in this order, and the reason
is written to the log next to the event:

1. a message that carries a call status is a ring — EZVIZ models a doorbell
   press as an incoming call, not an alarm
2. the alert code, looked up in the table for the path it arrived on
3. the codes you added in the options
4. the alert code looked up in the *other* path's table
5. the wording of the title EZVIZ sends with the message, matched whole-word
   against a list that covers English, Czech, German, French and Spanish
6. anything still unrecognised stays `alarm`, with its raw code in the log and
   in the event attributes

So a model whose codes nobody has catalogued yet is still classified correctly
from its own message text, and a code you identify from the log can be mapped
permanently in the options — no code change.

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
an incoming **call**, and the MQTT push channel carries **alarms**. That is why
the push side can report itself connected, subscribed and granted QoS 2, and
still never deliver a ring: it is not on that channel at all. Motion still
arrives over push, when it arrives.

So the same message feed the official app reads is polled as well, and anything
new is emitted — tagged `"source": "poll"` in the event attributes so the two
paths stay distinguishable. A detection seen on both is emitted once.

#### Burst polling keeps the ring quick without polling hard all day

Polling every few seconds around the clock to catch an event that happens twice
a day is wasteful, but a slow poll makes the doorbell feel broken. So the poller
has two speeds.

Motion *does* arrive over push, instantly — and someone reaching for the button
has nearly always tripped motion on the way in. Any push is therefore treated as
an early warning: it wakes the poller immediately and drops it to one poll a
second for the next 30 seconds, which is where the press lands. Outside that
window it idles back to the configured interval.

In practice the ring shows up about a second after the press, while the steady
state stays at one request every few seconds. A ring with no preceding motion
falls back to the slow path, so the interval still sets the worst case.

The first sweep after a start only records what already exists, so old messages
are not replayed as fresh events.

## Live video without RTSP

A battery doorbell has no RTSP server, so the usual route to a camera entity is
closed. What the EZVIZ app plays instead is the cloud stream, and the same
stream is opened here, remuxed to MPEG-TS with the FFmpeg that Home Assistant
already ships, and served back to the stream component as a URL of Home
Assistant's own. From the dashboard it is simply a camera.

### Video encryption, and the key that gets past it

An encrypted camera streams too. Only the first 4 KB of each video frame is
encrypted, so the stream is decrypted as it arrives rather than fetched, waited
for and decrypted in one lump — which is what the EZVIZ app does, and why it can
play an encrypted camera live when a "download the clip first" approach cannot.

What it needs is the key, and usually it has one: EZVIZ hands most accounts the
key to their own camera over its API, and this integration asks for it as the
device is read. Nothing has to be typed in.

Some accounts are refused — a shared device, or one EZVIZ answers with
`好友不存在` / `重复申请分享`, both of which mean "no". The key is then the code
printed on the device's own label, and it goes in under **Settings → Devices &
services → EZVIZ Doorbell → ⋮ → Reconfigure**, one box per camera, named by the
serial that is printed on the same label. It takes effect immediately, and it
decrypts that camera's snapshots as well as its video.

With encryption on and no key, no stream is offered at all and the camera shows
stills: a play button that can only fail makes Home Assistant retry it endlessly
and fills the log. Home Assistant raises a repair saying which camera and what
to do about it, and clears it once there is a key. **Settings → Devices &
services → EZVIZ Doorbell → ⋮ → Download diagnostics** lists, per camera,
whether encryption is on, whether a key is configured and whether live video is
being offered at all. The other way out is switching video encryption off for that
device in the EZVIZ app, which needs no key at all. Image encryption is a
separate setting and can stay on either way.

Note that a dashboard card shows a still by default even when live video is
available — click the camera, or set `camera_view: live` on the card, to make it
play. Whether the entity offers a stream at all is in its `supported_features`
attribute: `1` means it does, `0` means stills only.

One more thing worth knowing: **a cloud stream is a cloud stream.** It costs
bandwidth at both ends and takes longer to start than RTSP would. Switch *Offer
live video* off to drop it and keep the stills.

## Waking a sleeping camera

A battery doorbell hibernates between events, and while it sleeps it answers
nothing — no live view, no fresh snapshot. The **Wake camera** button does what
the app does when you open the live view, as three separate requests: it turns
the sleep switch off, asks the cloud to keep the device awake, and takes a
picture, which is what actually pulls the device onto the network. Each step is
allowed to fail on its own — which of them a model honours depends on its
firmware — and the log says which ones were accepted.

Opening the camera or asking for a snapshot already asks the cloud to delay
sleep, so most of the time nothing needs pressing.

## When EZVIZ asks for a code again

The cloud ties a session to a terminal identity derived from the machine's MAC
address, and a Home Assistant container gets a new one when it is recreated — on
an update, say. When that happens EZVIZ drops the session and Home Assistant
raises a **Reauthentication needed** notification: click it, retype the
password, and paste the emailed code if one is asked for.

## Alert type codes

| Code | Path | Event type |
| --- | --- | --- |
| `subType 2701` | poll | `ring` — doorbell button |
| any message with a `callingStatus` | poll | `ring` |
| `alert_type_code 10120` | push | `motion` — AI human detection |
| `alert_type_code 10000` | push | `motion` — PIR |
| `alert_type_code 0` | push | `ring` |
| anything else | either | matched on the message text, else `alarm` with the raw code kept in the attributes |

`2701` and `10120` were captured from a live EP8x; `0` and `10000` come from
[RenierM26/ha-ezviz#112](https://github.com/RenierM26/ha-ezviz/issues/112) on a
DP1C. Anything unmapped is logged with its raw code — add it in the options, or
to `PUSH_ALERT_TYPES` / `POLL_SUBTYPES` in `const.py` if you would rather it
shipped with the integration.

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
            image: /api/camera_proxy/camera.front_door
```

The doorbell event entity only ever fires for a press, so no condition on the
event type is needed. Use `event.front_door_motion` for motion.

---

## Status

The **add-on** is confirmed working against an EP8x: the ring is delivered and
mapped to `ring`, its encrypted snapshot decrypts, and motion arrives over push
as AI human detection almost instantly.

The **integration** is the same logic rebuilt on Home Assistant's own platforms,
and it has not yet been run against a real doorbell — the classification, the
wake sequence and the cloud stream are carried over, but the config flow, the
entities and the camera want a first outing before anyone calls them proven. If
something misbehaves, the log names the device and what was refused.

Which of the cloud calls a given model answers varies by firmware. Everything is
built from what the device itself reports, and a request a device refuses is
logged rather than retried.

What *is* covered is that the integration loads and behaves, against Home
Assistant itself (2026.2) with the cloud mocked out - every platform's
entities appear, a ring reaches the doorbell entity without touching motion, a
detection arriving on both paths fires once, and the switches and the wake
button reach the API calls they should:

```bash
pip install -r requirements-test.txt
pytest
```

## tools/

Standalone scripts, useful before or instead of installing anything. Both prompt
for credentials interactively and store nothing.

- `ezviz_diag.py` — dumps a device's pagelist, switch list, `isEncrypt` flag and
  latest alarm, then downloads the snapshot and reports whether it is encrypted.
- `ezviz_push_bridge.py` — a plain push listener forwarding to a Home Assistant
  webhook. Handy for discovering your alert codes.
- `make_icons.py` — resizes `assets/` into the add-on's `icon.png` and
  `logo.png`.

## License

Apache-2.0.
