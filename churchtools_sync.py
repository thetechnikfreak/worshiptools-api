"""
ChurchTools -> WorshipTools: legt fuer ChurchTools-Events Gottesdienste in
WorshipTools an (nur Erstellen, keine Lieder).

Umgebungsvariablen:
  CT_URL                z.B. https://meinegemeinde.church.tools
  CT_TOKEN              Login-Token (ChurchTools: /api/person/<id>/logintoken)
  WT_SERVICE_TYPE_ID    UUID des WorshipTools-Gottesdiensttyps
  CT_CALENDAR_IDS       optional, kommagetrennt, z.B. "2,5" (leer = alle)
  CT_NAME_CONTAINS      optional, nur Events deren Name das enthaelt, z.B. "Gottesdienst"
  SYNC_DAYS_AHEAD       optional, Standard 60
  SYNC_TIMEZONE         optional, Standard Europe/Berlin
  SYNC_STATE_FILE       optional, Standard /data/sync_state.json (als Volume mounten!)
  WORSHIPTOOLS_EMAIL / WORSHIPTOOLS_PASSWORD / WORSHIPTOOLS_ACCOUNT_ID
                        (oder WT_EMAIL / WT_PASSWORD / WT_ACCOUNT_ID)

Aufruf:
  python churchtools_sync.py --dry-run   # zeigt nur, was angelegt wuerde
  python churchtools_sync.py             # legt neue Gottesdienste an
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

CT_URL = os.environ.get("CT_URL", "").rstrip("/")
CT_TOKEN = os.environ.get("CT_TOKEN", "")
WT_SERVICE_TYPE_ID = os.environ.get("WT_SERVICE_TYPE_ID", "")
CT_CALENDAR_IDS = {c.strip() for c in os.environ.get("CT_CALENDAR_IDS", "").split(",") if c.strip()}
CT_NAME_CONTAINS = os.environ.get("CT_NAME_CONTAINS", "").strip().lower()
DAYS_AHEAD = int(os.environ.get("SYNC_DAYS_AHEAD", "60"))
TZ = ZoneInfo(os.environ.get("SYNC_TIMEZONE", "Europe/Berlin"))
STATE_FILE = os.environ.get("SYNC_STATE_FILE", "/data/sync_state.json")


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"events": {}}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def fetch_ct_events():
    """Holt die Events der naechsten DAYS_AHEAD Tage aus ChurchTools."""
    today = datetime.now(TZ).date()
    params = {
        "from": today.isoformat(),
        "to": (today + timedelta(days=DAYS_AHEAD)).isoformat(),
    }
    resp = requests.get(
        f"{CT_URL}/api/events",
        params=params,
        headers={"Authorization": f"Login {CT_TOKEN}", "Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def event_calendar_id(ev):
    cal = ev.get("calendar") or {}
    return str(cal.get("domainIdentifier") or cal.get("id") or "")


def event_matches(ev):
    if CT_CALENDAR_IDS and event_calendar_id(ev) not in CT_CALENDAR_IDS:
        return False
    name = (ev.get("name") or ev.get("caption") or "").lower()
    if CT_NAME_CONTAINS and CT_NAME_CONTAINS not in name:
        return False
    return True


def to_local_time(iso_str):
    """ChurchTools liefert UTC (z.B. 2026-10-25T08:00:00Z).
    WorshipTools speichert lokale Zeit ohne Zeitzone (2026-10-25T10:00:00)."""
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ).strftime("%Y-%m-%dT%H:%M:%S")


def sync(wt, dry_run=False):
    """wt = Worshiptools_API-Instanz. Gibt eine Zusammenfassung zurueck."""
    state = load_state()
    result = {"created": [], "skipped": 0, "filtered": 0, "errors": []}

    for ev in fetch_ct_events():
        ct_id = str(ev.get("id"))
        if not event_matches(ev):
            result["filtered"] += 1
            continue
        if ct_id in state["events"]:
            result["skipped"] += 1
            continue

        name = ev.get("name") or ev.get("caption") or "Gottesdienst"
        start = ev.get("startDate")
        if not start:
            result["errors"].append({"ct_id": ct_id, "error": "kein startDate"})
            continue
        local_time = to_local_time(start)

        if dry_run:
            result["created"].append({"ct_id": ct_id, "name": name, "time": local_time, "dry_run": True})
            continue

        try:
            created = wt.create_service(
                service_type_id=WT_SERVICE_TYPE_ID,
                times=[local_time],
                name=name,
            )
            state["events"][ct_id] = {
                "wt_id": created["id"],
                "name": name,
                "time": local_time,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            save_state(state)  # sofort speichern, damit nichts doppelt angelegt wird
            result["created"].append({"ct_id": ct_id, "name": name, "time": local_time, "wt_id": created["id"]})
        except Exception as e:  # ein fehlerhaftes Event soll die anderen nicht blockieren
            result["errors"].append({"ct_id": ct_id, "name": name, "error": str(e)})

    return result


def _env(*names):
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    return None


if __name__ == "__main__":
    for var in ("CT_URL", "CT_TOKEN", "WT_SERVICE_TYPE_ID"):
        if not os.environ.get(var):
            sys.exit(f"Umgebungsvariable {var} fehlt")

    from worshiptools_api import Worshiptools_API

    api = Worshiptools_API(
        _env("WORSHIPTOOLS_EMAIL", "WT_EMAIL"),
        _env("WORSHIPTOOLS_PASSWORD", "WT_PASSWORD"),
        _env("WORSHIPTOOLS_ACCOUNT_ID", "WT_ACCOUNT_ID"),
    )
    print(json.dumps(sync(api, dry_run="--dry-run" in sys.argv), indent=2, ensure_ascii=False))
