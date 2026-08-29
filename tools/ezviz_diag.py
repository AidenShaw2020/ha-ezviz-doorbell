"""Diagnostika Ezviz zvonku pro HA integraci.

Pouziti:  python ezviz_diag.py <SERIOVE_CISLO>
Heslo se zada interaktivne, nikam se neuklada.
"""
import getpass
import json
import sys

import requests
from pyezvizapi.client import EzvizClient

serial = sys.argv[1] if len(sys.argv) > 1 else input("Seriove cislo zvonku: ").strip()
account = input("EZVIZ ucet (email/telefon): ").strip()
password = getpass.getpass("EZVIZ heslo: ")
region = input("API region [apiieu.ezvizlife.com]: ").strip() or "apiieu.ezvizlife.com"

client = EzvizClient(account, password, region)
client.login()

info = client.get_device_infos(serial)

status = info.get("STATUS") or {}
optionals = status.get("optionals") or {}
dev = info.get("deviceInfos") or {}

print("\n=== ZARIZENI ===")
print("name              :", dev.get("name"))
print("deviceCategory    :", dev.get("deviceCategory"))
print("deviceSubCategory :", dev.get("deviceSubCategory"))
print("status (1=online) :", dev.get("status"))
print("supportExt keys   :", sorted((dev.get("supportExt") or {}).keys()))

print("\n=== SIFROVANI ===")
print("STATUS.isEncrypt  :", status.get("isEncrypt"), "  <-- 1 = snimky jsou sifrovane")

print("\n=== SWITCH (co HA muze udelat jako prepinac) ===")
for s in info.get("SWITCH") or []:
    print(f"  type={s.get('type'):<5} enable={s.get('enable')}")

print("\n=== STATUS.optionals ===")
print(json.dumps(optionals, indent=2, ensure_ascii=False)[:2000])

print("\n=== POSLEDNI ALARM ===")
msg = None
try:
    resp = client.get_device_messages_list(serials=serial, limit=5, date="", end_time="")
    items = resp.get("message") or resp.get("messages") or []
    msg = next((m for m in items if m.get("deviceSerial") == serial), None)
    print("zdroj: unifiedmsg, polozek:", len(items))
except Exception as err:  # noqa: BLE001
    print("unifiedmsg selhalo:", repr(err))

pic_url = None
if msg:
    print(json.dumps(msg, indent=2, ensure_ascii=False)[:2000])
    pic_url = msg.get("pic") or (msg.get("ext") or {}).get("pics")
else:
    try:
        alarms = client.get_alarminfo(serial, limit=5)
        print("zdroj: /v3/alarms, totalResults:",
              (alarms.get("page") or {}).get("totalResults"))
        first = (alarms.get("alarms") or [None])[0]
        if first:
            print(json.dumps(first, indent=2, ensure_ascii=False)[:2000])
            pic_url = first.get("picUrl")
    except Exception as err:  # noqa: BLE001
        print("get_alarminfo selhalo:", repr(err))

print("\n=== SNIMEK ===")
if not pic_url:
    print("Zadna URL snimku -> HA nema co zobrazit.")
else:
    url = pic_url.split(";")[0]
    print("URL:", url)
    r = requests.get(url, timeout=20)
    print("HTTP:", r.status_code, "| bytu:", len(r.content),
          "| content-type:", r.headers.get("content-type"))
    head = r.content[:16]
    if head == b"hikencodepicture":
        print(">>> SNIMEK JE SIFROVANY. HA ho zobrazi jen s overovacim kodem zarizeni.")
    elif r.content[:3] == b"\xff\xd8\xff":
        print(">>> Snimek je normalni JPEG (nesifrovany).")
    else:
        print(">>> Neznamy format, prvnich 16 bytu:", head)
