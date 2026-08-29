"""Postavi custom_components/ezviz jako fork core integrace + push udalosti.

Stahne zdrojaky core integrace 'ezviz' z home-assistant/core pro TVOJI verzi HA,
prilepi k nim push.py + event.py a aplikuje tri chirurgicke patche:

  1. manifest.json  -> custom integration, pyezvizapi 1.0.5.0 (kvuli MQTT push)
  2. __init__.py    -> registruje platformu EVENT a drzi MQTT push spojeni
  3. config_flow.py -> selhany RTSP test uz neblokuje vytvoreni kameroveho entry

Pouziti:
  python build_ezviz_fork.py --ha-version 2026.8.0 --out ./custom_components/ezviz

Verzi HA najdes v Nastaveni -> O aplikaci (napr. 2026.8.0).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import urllib.error
import urllib.request

GITHUB_API = (
    "https://api.github.com/repos/home-assistant/core/contents/"
    "homeassistant/components/ezviz?ref={ref}"
)
LIB_PIN = "pyezvizapi==1.0.5.0"
HERE = Path(__file__).parent


class PatchError(RuntimeError):
    """Kotva pro patch nenalezena nebo nejednoznacna."""


def fetch_json(url: str) -> list[dict]:
    """Stahne JSON z GitHub API."""
    req = urllib.request.Request(url, headers={"User-Agent": "ezviz-fork-build"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_bytes(url: str) -> bytes:
    """Stahne soubor."""
    req = urllib.request.Request(url, headers={"User-Agent": "ezviz-fork-build"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def patch(text: str, anchor: str, replacement: str, name: str) -> str:
    """Nahradi kotvu, ale jen kdyz je v souboru prave jednou."""
    count = text.count(anchor)
    if count != 1:
        raise PatchError(
            f"Patch '{name}': kotva nalezena {count}x (ocekavano 1x). "
            "Core integrace se zmenila - patch je potreba rucne upravit."
        )
    return text.replace(anchor, replacement)


def patch_manifest(raw: bytes, ha_version: str) -> bytes:
    """Udela z core manifestu manifest custom integrace."""
    data = json.loads(raw.decode("utf-8"))
    data["version"] = f"{ha_version}+doorbell.1"
    data["requirements"] = [LIB_PIN]
    return (json.dumps(data, indent=2) + "\n").encode("utf-8")


INIT_PUSH_START = '''        entry.runtime_data = coordinator

        # Real time udalosti (zvoneni!) chodi jen pres EZVIZ push,
        # polling je nikdy neuvidi.
        push_manager = EzvizPushManager(hass, ezviz_client)
        await push_manager.async_start()
        coordinator.push_manager = push_manager'''

INIT_UNLOAD_ANCHOR = '''    sensor_type = entry.data[CONF_TYPE]

    return await hass.config_entries.async_unload_platforms('''

INIT_UNLOAD_PATCHED = '''    sensor_type = entry.data[CONF_TYPE]

    if sensor_type == ATTR_TYPE_CLOUD:
        push_manager = getattr(entry.runtime_data, "push_manager", None)
        if push_manager is not None:
            await push_manager.async_stop()

    return await hass.config_entries.async_unload_platforms('''


def patch_init(text: str) -> str:
    """Zaregistruje platformu EVENT a nastartuje/zastavi push spojeni."""
    text = patch(
        text,
        "from .coordinator import EzvizConfigEntry, EzvizDataUpdateCoordinator",
        "from .coordinator import EzvizConfigEntry, EzvizDataUpdateCoordinator\n"
        "from .push import EzvizPushManager",
        "import push manageru",
    )
    text = patch(
        text,
        "        Platform.CAMERA,",
        "        Platform.CAMERA,\n        Platform.EVENT,",
        "registrace platformy EVENT",
    )
    text = patch(
        text,
        "        entry.runtime_data = coordinator",
        INIT_PUSH_START,
        "start push spojeni",
    )
    return patch(
        text,
        INIT_UNLOAD_ANCHOR,
        INIT_UNLOAD_PATCHED,
        "zastaveni push spojeni",
    )


CF_ANCHOR = '''            # Attempt an authenticated RTSP DESCRIBE request.
            _test_camera_rtsp_creds(data)'''

CF_PATCHED = '''            # Attempt an authenticated RTSP DESCRIBE request.
            # Battery doorbells hibernate and often expose no RTSP server at
            # all. A failure here must not block the entry: the verification
            # code is still needed to decrypt the alarm snapshots shown by
            # the image entity.
            try:
                _test_camera_rtsp_creds(data)
            except (AuthTestResultFailed, InvalidHost, OSError) as err:
                _LOGGER.warning(
                    "RTSP check failed for %s (%s). Creating the entry anyway,"
                    " but the verification code was NOT validated - watch the"
                    " log for decrypt warnings",
                    data[ATTR_SERIAL],
                    err,
                )'''


def patch_config_flow(text: str) -> str:
    """Selhany RTSP test uz nesmi zablokovat vytvoreni kameroveho entry."""
    return patch(text, CF_ANCHOR, CF_PATCHED, "nepovinny RTSP test")


def patch_strings(raw: bytes) -> bytes:
    """Doplni preklad pro novou event entitu."""
    data = json.loads(raw.decode("utf-8"))
    entity = data.setdefault("entity", {})
    entity.setdefault("event", {})["alerts"] = {
        "name": "Alerts",
        "state_attributes": {
            "event_type": {
                "state": {
                    "ring": "Doorbell pressed",
                    "motion": "Motion detected",
                    "alarm": "Alarm",
                }
            }
        },
    }
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def validate(out: Path) -> bool:
    """Overi, ze se kazdy vysledny .py soubor da naparsovat.

    Stahovani ze site neni spolehlive - pokud proxy, cache nebo mirror vrati
    porusenou verzi zdrojaku, HA by integraci jen tise odmitl nacist s
    nejasnou chybou v logu. Radsi to zachytime tady.
    """
    broken: list[str] = []
    for path in sorted(out.glob("*.py")):
        try:
            compile(path.read_bytes(), str(path), "exec")
        except SyntaxError as err:
            broken.append(f"{path.name}:{err.lineno}: {err.msg}")

    if broken:
        print(
            "\nCHYBA: stazene zdrojaky nejsou validni Python:",
            file=sys.stderr,
        )
        for item in broken:
            print(f"  {item}", file=sys.stderr)
        print(
            "\nStazeny obsah je poskozeny - integraci NEINSTALUJ.\n"
            "Zkus jinou sit/VPN nebo stahni zdrojaky rucne z\n"
            "https://github.com/home-assistant/core/tree/<verze>/"
            "homeassistant/components/ezviz",
            file=sys.stderr,
        )
        return False

    print("\nKontrola syntaxe: vsechny soubory OK")
    return True


def main(argv: list[str] | None = None) -> int:
    """Vstupni bod."""
    parser = argparse.ArgumentParser(prog="build_ezviz_fork")
    parser.add_argument(
        "--ha-version",
        required=True,
        help="Verze tveho HA, napr. 2026.8.0 (Nastaveni -> O aplikaci)",
    )
    parser.add_argument(
        "--out",
        default="./custom_components/ezviz",
        help="Cilovy adresar (default: ./custom_components/ezviz)",
    )
    args = parser.parse_args(argv)

    out = Path(args.out)
    if out.exists():
        print(f"Cilovy adresar {out} uz existuje, mazu jeho obsah.")
        shutil.rmtree(out)
    out.mkdir(parents=True)

    print(f"Stahuji core integraci ezviz z tagu {args.ha_version} ...")
    try:
        listing = fetch_json(GITHUB_API.format(ref=args.ha_version))
    except urllib.error.HTTPError as err:
        print(
            f"CHYBA: nepodarilo se najit tag '{args.ha_version}' ({err}).\n"
            "Zkontroluj verzi HA v Nastaveni -> O aplikaci.",
            file=sys.stderr,
        )
        return 1

    patched = []
    for item in listing:
        if item.get("type") != "file":
            continue
        name = item["name"]
        raw = fetch_bytes(item["download_url"])

        if name == "manifest.json":
            raw = patch_manifest(raw, args.ha_version)
            patched.append(name)
        elif name == "strings.json":
            raw = patch_strings(raw)
            patched.append(name)
        elif name == "__init__.py":
            raw = patch_init(raw.decode("utf-8")).encode("utf-8")
            patched.append(name)
        elif name == "config_flow.py":
            raw = patch_config_flow(raw.decode("utf-8")).encode("utf-8")
            patched.append(name)

        (out / name).write_bytes(raw)
        print(f"  {name}")

    for extra in ("push.py", "event.py"):
        source = HERE / extra
        if not source.exists():
            print(f"CHYBA: chybi {source}", file=sys.stderr)
            return 1
        shutil.copy(source, out / extra)
        print(f"  {extra}  (novy)")

    if not validate(out):
        return 1

    print(f"\nHotovo. Zpatchovano: {', '.join(patched)}")
    print(f"Zkopiruj cely adresar {out} do /config/custom_components/ v HA")
    print("a restartuj Home Assistant.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
