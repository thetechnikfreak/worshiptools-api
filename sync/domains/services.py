import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from ct_client import ChurchToolsClient
from sync.models import CanonicalService
from sync.state import SyncState
from worshiptools_api import Worshiptools_API

logger = logging.getLogger(__name__)

class ServicesSyncDomain:
    def __init__(self, ct_client: ChurchToolsClient, wt_api: Worshiptools_API, state: SyncState, config: Dict[str, Any]):
        self.ct = ct_client
        self.wt = wt_api
        self.state = state
        self.config = config
        self.tz = ZoneInfo(config.get("churchtools", {}).get("sync_timezone", "Europe/Berlin"))
        self.cal_name = config.get("churchtools", {}).get("calendar", "Gottesdienst")
        self.service_types_map = config.get("service_types", {})
        self.default_template = config.get("default_template", "Gottesdienst")
        self.tie_window_seconds = config.get("sync", {}).get("tie_window_seconds", 60)
        self.delete_mode = config.get("sync", {}).get("delete_mode", "off")
        self.missing_runs_before_delete = config.get("sync", {}).get("missing_runs_before_delete", 2)

    def _to_local_time(self, iso_utc_str: str) -> str:
        dt = datetime.fromisoformat(iso_utc_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(self.tz).strftime("%Y-%m-%dT%H:%M:%S")

    def _to_utc_iso(self, local_time_str: str) -> str:
        dt_local = datetime.fromisoformat(local_time_str).replace(tzinfo=self.tz)
        return dt_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def load_wt_service_type_names(self) -> Dict[str, str]:
        """Returns {type_id: type_name}."""
        resp = self.wt.get("meta/service-types")
        if isinstance(resp, dict):
            resp = resp.get("data") or resp.get("docs") or []
        return {str(t["id"]): str(t["name"]) for t in resp if "id" in t and "name" in t}

    def fetch_ct_services(self, from_date: str, to_date: str) -> Tuple[int, List[CanonicalService]]:
        cal_id = self.ct.get_calendar_id_by_name(self.cal_name)
        if not cal_id:
            raise ValueError(f"Calendar '{self.cal_name}' not found in ChurchTools.")

        events = self.ct.get_events(from_date, to_date, include_services=True)
        canonical_list = []

        for ev in events:
            ev_cal = ev.get("calendar") or {}
            ev_cal_id = ev_cal.get("domainIdentifier") or ev_cal.get("id")
            if str(ev_cal_id) != str(cal_id):
                continue

            event_id = str(ev["id"])
            appt_id = ev.get("appointmentId")
            appt = self.ct.get_appointment(cal_id, appt_id) if appt_id else {}

            title = (appt.get("title") or ev.get("name") or "").strip()
            subtitle = (appt.get("subtitle") or "").strip()
            start_utc = ev.get("startDate") or appt.get("startDate")
            if not start_utc:
                continue

            local_time = self._to_local_time(start_utc)
            mod_utc = ev.get("meta", {}).get("modifiedDate") or appt.get("meta", {}).get("modifiedDate") or ""
            is_canceled = ev.get("isCanceled", False)

            canonical_list.append(CanonicalService(
                id=event_id,
                type_name=title,
                name=subtitle,
                start_time=local_time,
                notes=appt.get("description") or ev.get("description") or "",
                mod_timestamp=mod_utc,
                is_canceled=is_canceled
            ))

        return cal_id, canonical_list

    def fetch_wt_services(self, today_str: str) -> List[CanonicalService]:
        type_names = self.load_wt_service_type_names()
        resp = self.wt.get("service", params={"past": "hidden", "today": today_str, "rows": 100})
        docs = resp.get("response", {}).get("docs", []) if isinstance(resp, dict) else []

        canonical_list = []
        for s in docs:
            wt_id = str(s["id"])
            type_id = str(s.get("type", ""))
            type_name = type_names.get(type_id, type_id)
            name = s.get("name") or ""
            times = s.get("times") or []
            if not times:
                continue
            # Ignore multi-service storage containers that have many dates
            if len(times) > 3:
                continue
            first_time = times[0][:19]
            mod_ts = s.get("mod") or ""

            canonical_list.append(CanonicalService(
                id=wt_id,
                type_name=type_name,
                name=name,
                start_time=first_time,
                notes="",
                mod_timestamp=mod_ts,
                is_canceled=s.get("archived", False)
            ))

        return canonical_list

    def sync(self, from_date: str, to_date: str, dry_run: bool = False) -> Dict[str, Any]:
        result = {
            "created_in_ct": [],
            "created_in_wt": [],
            "updated_in_ct": [],
            "updated_in_wt": [],
            "deleted_in_ct": [],
            "deleted_in_wt": [],
            "skipped": 0,
            "conflicts_resolved": []
        }

        cal_id, ct_services = self.fetch_ct_services(from_date, to_date)
        wt_services = self.fetch_wt_services(from_date)

        ct_by_id = {s.id: s for s in ct_services}
        wt_by_id = {s.id: s for s in wt_services}

        links = self.state.get_links("service", active_only=True)
        linked_ct_ids = set()
        linked_wt_ids = set()

        wt_type_map = {v.lower(): k for k, v in self.load_wt_service_type_names().items()}

        # 1. Process existing links
        for link in links:
            ct_id = link["ct_id"]
            wt_id = link["wt_id"]
            linked_ct_ids.add(ct_id)
            linked_wt_ids.add(wt_id)

            ct_item = ct_by_id.get(ct_id)
            wt_item = wt_by_id.get(wt_id)

            # Check for deletions
            if not ct_item and wt_item:
                count = self.state.mark_missing("service", ct_id, wt_id)
                if count >= self.missing_runs_before_delete and self.delete_mode in ("both", "wt_only"):
                    result["deleted_in_wt"].append({"wt_id": wt_id, "name": wt_item.name})
                    if not dry_run:
                        self.wt.delete_service(wt_id)
                        self.state.mark_deleted("service", ct_id, wt_id)
                continue

            if not wt_item and ct_item:
                count = self.state.mark_missing("service", ct_id, wt_id)
                if count >= self.missing_runs_before_delete and self.delete_mode in ("both", "ct_only"):
                    result["deleted_in_ct"].append({"ct_id": ct_id, "name": ct_item.name})
                    if not dry_run:
                        self.ct.delete_event(int(ct_id))
                        self.state.mark_deleted("service", ct_id, wt_id)
                continue

            if not ct_item and not wt_item:
                continue

            # Both items exist: Compare hashes
            ct_hash = ct_item.compute_hash()
            wt_hash = wt_item.compute_hash()
            stored_ct_hash = link["ct_hash"]
            stored_wt_hash = link["wt_hash"]

            ct_changed = (ct_hash != stored_ct_hash)
            wt_changed = (wt_hash != stored_wt_hash)

            if not ct_changed and not wt_changed:
                result["skipped"] += 1
                continue

            if ct_changed and not wt_changed:
                # Push CT -> WT
                result["updated_in_wt"].append({"wt_id": wt_id, "name": ct_item.name, "type": ct_item.type_name, "time": ct_item.start_time})
                if not dry_run:
                    type_uuid = wt_type_map.get(ct_item.type_name.lower())
                    self.wt.update_service(wt_id, name=ct_item.name, times=[ct_item.start_time], service_type_id=type_uuid)
                    self.state.upsert_link("service", ct_id, wt_id, ct_hash, wt_hash, ct_item.mod_timestamp, wt_item.mod_timestamp)
                continue

            if wt_changed and not ct_changed:
                # Push WT -> CT
                result["updated_in_ct"].append({"ct_id": ct_id, "name": wt_item.name, "type": wt_item.type_name, "time": wt_item.start_time})
                if not dry_run:
                    ev = self.ct.get_event(int(ct_id))
                    appt_id = ev.get("appointmentId")
                    if appt_id:
                        start_utc = self._to_utc_iso(wt_item.start_time)
                        self.ct.update_appointment(cal_id, appt_id, title=wt_item.type_name, subtitle=wt_item.name, start_iso=start_utc)
                    self.state.upsert_link("service", ct_id, wt_id, ct_hash, wt_hash, ct_item.mod_timestamp, wt_item.mod_timestamp)
                continue

            # Both changed: Resolve conflict via LWW + 60s CT advantage
            ct_dt = datetime.fromisoformat(ct_item.mod_timestamp.replace("Z", "+00:00")) if ct_item.mod_timestamp else datetime.min.replace(tzinfo=timezone.utc)
            wt_dt = datetime.fromisoformat(wt_item.mod_timestamp.replace("Z", "+00:00")) if wt_item.mod_timestamp else datetime.min.replace(tzinfo=timezone.utc)
            diff_seconds = abs((ct_dt - wt_dt).total_seconds())

            if diff_seconds <= self.tie_window_seconds or ct_dt > wt_dt:
                # CT Wins
                winner = "CT"
                result["conflicts_resolved"].append({"action": "CT_won", "ct_id": ct_id, "wt_id": wt_id})
                result["updated_in_wt"].append({"wt_id": wt_id, "name": ct_item.name, "type": ct_item.type_name})
                if not dry_run:
                    type_uuid = wt_type_map.get(ct_item.type_name.lower())
                    self.wt.update_service(wt_id, name=ct_item.name, times=[ct_item.start_time], service_type_id=type_uuid)
                    self.state.upsert_link("service", ct_id, wt_id, ct_hash, wt_hash, ct_item.mod_timestamp, wt_item.mod_timestamp)
            else:
                # WT Wins
                winner = "WT"
                result["conflicts_resolved"].append({"action": "WT_won", "ct_id": ct_id, "wt_id": wt_id})
                result["updated_in_ct"].append({"ct_id": ct_id, "name": wt_item.name, "type": wt_item.type_name})
                if not dry_run:
                    ev = self.ct.get_event(int(ct_id))
                    appt_id = ev.get("appointmentId")
                    if appt_id:
                        start_utc = self._to_utc_iso(wt_item.start_time)
                        self.ct.update_appointment(cal_id, appt_id, title=wt_item.type_name, subtitle=wt_item.name, start_iso=start_utc)
                    self.state.upsert_link("service", ct_id, wt_id, ct_hash, wt_hash, ct_item.mod_timestamp, wt_item.mod_timestamp)

        # 2. Check unlinked items: Try matching by date + start time (+/- 15 min) or create
        unlinked_ct = [s for s in ct_services if s.id not in linked_ct_ids]
        unlinked_wt = [s for s in wt_services if s.id not in linked_wt_ids]

        # Match unlinked by same date (same day) & matching type or subtitle
        matched_wt_ids = set()
        for ct_item in unlinked_ct:
            match = None
            ct_dt = datetime.fromisoformat(ct_item.start_time)
            for wt_item in unlinked_wt:
                if wt_item.id in matched_wt_ids:
                    continue
                wt_dt = datetime.fromisoformat(wt_item.start_time)
                # Same day and same type (or exact same title/subtitle)
                if ct_dt.date() == wt_dt.date():
                    if ct_item.type_name.lower() == wt_item.type_name.lower() or (ct_item.name and ct_item.name.lower() == wt_item.name.lower()):
                        match = wt_item
                        break
            if match:
                matched_wt_ids.add(match.id)
                result["conflicts_resolved"].append({
                    "action": "linked_existing_by_date",
                    "ct_id": ct_item.id,
                    "wt_id": match.id,
                    "date": str(ct_dt.date())
                })
                self.state.upsert_link("service", ct_item.id, match.id, ct_item.compute_hash(), match.compute_hash(),
                                      ct_item.mod_timestamp, match.mod_timestamp)
            else:
                # CT event has no counterpart in WT -> Create in WT
                type_uuid = wt_type_map.get(ct_item.type_name.lower())
                if type_uuid:
                    result["created_in_wt"].append({"ct_id": ct_item.id, "type": ct_item.type_name, "time": ct_item.start_time, "name": ct_item.name})
                    if not dry_run:
                        created = self.wt.create_service(service_type_id=type_uuid, times=[ct_item.start_time], name=ct_item.name)
                        new_wt_id = created["id"]
                        self.state.upsert_link("service", ct_item.id, new_wt_id, ct_item.compute_hash(), "", ct_item.mod_timestamp, "")

        # Remaining unlinked WT services -> Create in CT
        for wt_item in unlinked_wt:
            if wt_item.id in matched_wt_ids:
                continue
            result["created_in_ct"].append({"wt_id": wt_item.id, "type": wt_item.type_name, "time": wt_item.start_time, "name": wt_item.name})
            if not dry_run:
                start_utc = self._to_utc_iso(wt_item.start_time)
                # Assume 1 hour default duration
                dt_end = datetime.fromisoformat(wt_item.start_time).replace(tzinfo=self.tz)
                end_utc = datetime.fromtimestamp(dt_end.timestamp() + 3600, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                appt_res = self.ct.create_appointment_and_event(
                    calendar_id=cal_id,
                    start_iso=start_utc,
                    end_iso=end_utc,
                    title=wt_item.type_name,
                    subtitle=wt_item.name
                )
                new_ct_event_id = str(appt_res.get("appointment", {}).get("calculatedDates", {})
                                     .get(list(appt_res.get("appointment", {}).get("calculatedDates", {}).keys())[0], {})
                                     .get("event", {}).get("domainIdentifier", "")) if "appointment" in appt_res else ""
                if not new_ct_event_id and "id" in appt_res:
                    new_ct_event_id = str(appt_res["id"])

                self.state.upsert_link("service", new_ct_event_id or "pending", wt_item.id, "", wt_item.compute_hash(), "", wt_item.mod_timestamp)

        return result
