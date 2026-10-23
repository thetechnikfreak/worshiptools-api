"""
ChurchTools -> WorshipTools: creates services in WorshipTools for ChurchTools
events (create only, no songs).

Rules:
  * Only events from the calendar named "Gottesdienst" are synced
    (CT_CALENDAR_NAMES, comma separated). If that calendar cannot be found,
    NOTHING is synced (fail closed).
  * The WorshipTools service type is looked up by NAME: the event template
    name (or, as a fallback, the event name) is matched against the names in
    WorshipTools' meta/service-types. WT_SERVICE_TYPE_ID is only a fallback.

Environment variables:
  CT_URL                e.g. https://mychurch.church.tools
  CT_TOKEN              ChurchTools login token (/api/person/<id>/logintoken)
  CT_CALENDAR_NAMES     default "Gottesdienst"
  WT_SERVICE_TYPE_ID    optional fallback UUID if no name matches
  SYNC_DAYS_AHEAD       default 60
  SYNC_TIMEZONE         default Europe/Berlin
  SYNC_STATE_FILE       default /data/sync_state.json (mount as a volume!)
  WORSHIPTOOLS_EMAIL / WORSHIPTOOLS_PASSWORD / WORSHIPTOOLS_ACCOUNT_ID
                        (or WT_EMAIL / WT_PASSWORD / WT_ACCOUNT_ID)

Usage:
  python churchtools_sync.py --dry-run   # only shows what would be created
  python churchtools_sync.py             # creates new services
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

CT_URL = os.environ.get("CT_URL", "").rstrip("/")
CT_TOKEN = os.environ.get("CT_TOKEN", "")
CT_CALENDAR_NAMES = {
    c.strip().lower()
    for c in os.environ.get("CT_CALENDAR_NAMES", "Gottesdienst").split(",")
    if c.strip()
}
WT_FALLBACK_TYPE_ID = os.environ.get("WT_SERVICE_TYPE_ID", "")
DAYS_AHEAD = int(os.environ.get("SYNC_DAYS_AHEAD", "60"))
TZ = ZoneInfo(os.environ.get("SYNC_TIMEZONE", "Europe/Berlin"))
STATE_FILE = os.environ.get("SYNC_STATE_FILE", "/data/sync_state.json")

# Fields where ChurchTools might carry the event template name (first hit wins).
TEMPLATE_FIELDS = ("eventTemplate", "eventTemplateName", "template", "templateName")


# ---------- state ----------

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


# ---------- ChurchTools ----------

def _ct_get(path, params=None):
    resp = requests.get(
        f"{CT_URL}/api/{path}",
        params=params,
        headers={"Authorization": f"Login {CT_TOKEN}", "Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def resolve_calendar_ids():
    """Looks up the IDs of the allowed calendars (e.g. 'Gottesdienst') by name."""
    ids = set()
    for cal in _ct_get("calendars"):
        name = (cal.get("name") or cal.get("title") or "").strip().lower()
        if name in CT_CALENDAR_NAMES:
            ids.add(str(cal.get("id")))
    return ids


def _date_range():
    today = datetime.now(TZ).date()
    return {
        "from": today.isoformat(),
        "to": (today + timedelta(days=DAYS_AHEAD)).isoformat(),
    }


def fetch_ct_events():
    return _ct_get("events", _date_range())


def fetch_calendar_appointment_ids(calendar_ids):
    """IDs of all appointments in the allowed calendars (GET /calendars/{id}/appointments).

    An event links to its appointment via appointmentId. This makes the calendar
    filter reliable even if the event itself carries no calendar object.
    Returns (ids, warning or None).
    """
    ids = set()
    try:
        for cal_id in calendar_ids:
            for appt in _ct_get(f"calendars/{cal_id}/appointments", _date_range()):
                # Series entries may be wrapped as {"base": {...}, "calculated": {...}}
                appt_id = appt.get("id") or (appt.get("base") or {}).get("id")
                if appt_id is not None:
                    ids.add(str(appt_id))
    except requests.RequestException as e:
        return ids, f"could not load appointments of the calendar ({e}); using event.calendar only"
    return ids, None


def event_in_allowed_calendar(ev, allowed_ids, allowed_appointment_ids=frozenset()):
    # 1) calendar object on the event (Event.calendar: domainIdentifier + title)
    cal = ev.get("calendar") or {}
    cal_id = str(cal.get("domainIdentifier") or cal.get("id") or "")
    if cal_id and cal_id in allowed_ids:
        return True
    cal_name = (cal.get("name") or cal.get("title") or cal.get("caption") or "").strip().lower()
    if cal_name and cal_name in CT_CALENDAR_NAMES:
        return True
    # 2) fallback: the event's appointment belongs to an allowed calendar
    appt_id = ev.get("appointmentId")
    return appt_id is not None and str(appt_id) in allowed_appointment_ids


def event_template_name(ev):
    for field in TEMPLATE_FIELDS:
        val = ev.get(field)
        if isinstance(val, dict):
            val = val.get("name") or val.get("title") or val.get("caption")
        if val:
            return str(val).strip()
    return ""


def to_local_time(iso_str):
    """ChurchTools sends UTC (2026-10-25T08:00:00Z); WorshipTools stores local
    time without timezone (2026-10-25T10:00:00)."""
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ).strftime("%Y-%m-%dT%H:%M:%S")


# ---------- WorshipTools service types ----------

def load_service_types(wt):
    """Returns {lowercase name: service type id} from meta/service-types."""
    resp = wt.get("meta/service-types")
    types = {}

    def add(fallback_id, obj):
        name = obj.get("name") or obj.get("title") or obj.get("label")
        sid = obj.get("id") or obj.get("uuid") or fallback_id
        if name and sid:
            types[str(name).strip().lower()] = str(sid)

    if isinstance(resp, dict):
        for key in ("data", "docs", "serviceTypes", "types", "items"):
            if isinstance(resp.get(key), list):
                resp = resp[key]
                break
    if isinstance(resp, list):
        for obj in resp:
            if isinstance(obj, dict):
                add(None, obj)
    elif isinstance(resp, dict):
        for key, obj in resp.items():
            if isinstance(obj, dict):
                add(key, obj)
    return types


def resolve_service_type(types, template_name, event_name):
    """Template name first, then event name. Exact match, then 'contains'."""
    for candidate in (template_name, event_name):
        c = (candidate or "").strip().lower()
        if not c:
            continue
        if c in types:
            return types[c]
        for type_name, type_id in types.items():
            if type_name in c or c in type_name:
                return type_id
    return WT_FALLBACK_TYPE_ID or None


# ---------- sync ----------

def sync(wt, dry_run=False):
    """wt = Worshiptools_API instance. Returns a summary dict."""
    result = {"created": [], "skipped": 0, "filtered": 0, "errors": []}

    allowed_ids = resolve_calendar_ids()
    if not allowed_ids:
        result["errors"].append({
            "error": f"calendar {sorted(CT_CALENDAR_NAMES)} not found in ChurchTools - nothing synced"
        })
        return result

    allowed_appointment_ids, warning = fetch_calendar_appointment_ids(allowed_ids)
    if warning:
        result["warning"] = warning

    state = load_state()
    types = load_service_types(wt)
    result["service_types_found"] = sorted(types.keys())

    for ev in fetch_ct_events():
        ct_id = str(ev.get("id"))

        if ev.get("isCanceled") or not event_in_allowed_calendar(ev, allowed_ids, allowed_appointment_ids):
            result["filtered"] += 1
            continue
        if ct_id in state["events"]:
            result["skipped"] += 1
            continue

        name = ev.get("name") or ev.get("caption") or "Gottesdienst"
        template = event_template_name(ev)
        start = ev.get("startDate")
        if not start:
            result["errors"].append({"ct_id": ct_id, "name": name, "error": "no startDate"})
            continue

        type_id = resolve_service_type(types, template, name)
        if not type_id:
            result["errors"].append({
                "ct_id": ct_id, "name": name, "template": template,
                "error": "no matching WorshipTools service type",
            })
            continue

        local_time = to_local_time(start)
        entry = {"ct_id": ct_id, "name": name, "template": template or None,
                 "type_id": type_id, "time": local_time}

        if dry_run:
            entry["dry_run"] = True
            result["created"].append(entry)
            continue

        try:
            created = wt.create_service(service_type_id=type_id, times=[local_time], name=name)
            state["events"][ct_id] = {
                "wt_id": created["id"], "name": name, "time": local_time,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            save_state(state)  # save right away so nothing is created twice
            entry["wt_id"] = created["id"]
            result["created"].append(entry)
        except Exception as e:  # one broken event must not block the others
            result["errors"].append({"ct_id": ct_id, "name": name, "error": str(e)})

    return result


def _env(*names):
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    return None


if __name__ == "__main__":
    for var in ("CT_URL", "CT_TOKEN"):
        if not os.environ.get(var):
            sys.exit(f"Environment variable {var} is missing")

    from worshiptools_api import Worshiptools_API

    api = Worshiptools_API(
        _env("WORSHIPTOOLS_EMAIL", "WT_EMAIL"),
        _env("WORSHIPTOOLS_PASSWORD", "WT_PASSWORD"),
        _env("WORSHIPTOOLS_ACCOUNT_ID", "WT_ACCOUNT_ID"),
    )
    print(json.dumps(sync(api, dry_run="--dry-run" in sys.argv), indent=2, ensure_ascii=False))
