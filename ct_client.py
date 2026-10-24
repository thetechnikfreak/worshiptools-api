import logging
from typing import Any, Dict, List, Optional, Set
import requests

logger = logging.getLogger(__name__)

class ChurchToolsClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Login {self.token}",
            "Accept": "application/json"
        })

    def request(self, method: str, endpoint: str, **kwargs) -> Any:
        url = f"{self.base_url}/api/{endpoint.lstrip('/')}"
        timeout = kwargs.pop("timeout", 30)
        resp = self.session.request(method, url, timeout=timeout, **kwargs)
        if not resp.ok:
            logger.error(f"ChurchTools API {method} {endpoint} failed ({resp.status_code}): {resp.text[:500]}")
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.text.strip():
            return None
        data = resp.json()
        return data.get("data") if isinstance(data, dict) and "data" in data else data

    def get_calendars(self) -> List[Dict[str, Any]]:
        return self.request("GET", "calendars") or []

    def get_calendar_id_by_name(self, name: str) -> Optional[int]:
        target = name.strip().lower()
        for cal in self.get_calendars():
            cal_name = (cal.get("name") or cal.get("nameTranslated") or "").strip().lower()
            if cal_name == target:
                return cal.get("id")
        return None

    def get_events(self, from_date: str, to_date: str, include_services: bool = True) -> List[Dict[str, Any]]:
        params = {"from": from_date, "to": to_date}
        if include_services:
            params["include"] = "eventServices"
        return self.request("GET", "events", params=params) or []

    def get_event(self, event_id: int) -> Dict[str, Any]:
        return self.request("GET", f"events/{event_id}?include=eventServices")

    def get_appointment(self, calendar_id: int, appointment_id: int) -> Dict[str, Any]:
        res = self.request("GET", f"calendars/{calendar_id}/appointments/{appointment_id}")
        return res.get("appointment") if isinstance(res, dict) and "appointment" in res else res

    def get_event_templates(self) -> List[Dict[str, Any]]:
        return self.request("GET", "eventtemplates") or []

    def create_appointment_and_event(self, calendar_id: int, start_iso: str, end_iso: str,
                                    title: str, subtitle: Optional[str] = None,
                                    description: Optional[str] = None) -> Dict[str, Any]:
        """Creates an appointment (which in turn creates the linked CT event)."""
        payload = {
            "title": title,
            "subtitle": subtitle or "",
            "description": description or "",
            "startDate": start_iso,
            "endDate": end_iso,
            "allDay": False,
            "isInternal": False
        }
        res = self.request("POST", f"calendars/{calendar_id}/appointments", json=payload)
        return res

    def update_appointment(self, calendar_id: int, appointment_id: int,
                           title: Optional[str] = None, subtitle: Optional[str] = None,
                           start_iso: Optional[str] = None, end_iso: Optional[str] = None,
                           description: Optional[str] = None) -> Dict[str, Any]:
        appt = self.get_appointment(calendar_id, appointment_id)
        payload = {
            "title": title if title is not None else appt.get("title", ""),
            "subtitle": subtitle if subtitle is not None else (appt.get("subtitle") or ""),
            "description": description if description is not None else (appt.get("description") or ""),
            "startDate": start_iso if start_iso is not None else appt.get("startDate"),
            "endDate": end_iso if end_iso is not None else appt.get("endDate"),
            "allDay": appt.get("allDay", False),
            "isInternal": appt.get("isInternal", False),
            "version": appt.get("version", 1)
        }
        return self.request("PUT", f"calendars/{calendar_id}/appointments/{appointment_id}", json=payload)

    def delete_appointment(self, calendar_id: int, appointment_id: int):
        return self.request("DELETE", f"calendars/{calendar_id}/appointments/{appointment_id}")

    def delete_event(self, event_id: int):
        return self.request("DELETE", f"events/{event_id}")

    # Agenda endpoints
    def get_agenda(self, event_id: int) -> Optional[Dict[str, Any]]:
        try:
            return self.request("GET", f"events/{event_id}/agenda")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise

    def set_agenda(self, event_id: int, calendar_id: int, items: List[Dict[str, Any]], name: str = "Agenda") -> Dict[str, Any]:
        payload = {
            "name": name,
            "series": "",
            "isTemplate": False,
            "calendarId": calendar_id,
            "items": items
        }
        return self.request("PUT", f"events/{event_id}/agenda", json=payload)

    def delete_agenda(self, event_id: int):
        try:
            return self.request("DELETE", f"events/{event_id}/agenda")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise

    # Songs endpoints
    def get_songs(self) -> List[Dict[str, Any]]:
        # Paged or full retrieval
        songs = []
        page = 1
        while True:
            res = self.request("GET", f"songs?page={page}&limit=100")
            if not res:
                break
            songs.extend(res)
            if len(res) < 100:
                break
            page += 1
        return songs

    def create_song(self, name: str, author: str = "", ccli: str = "",
                    key: Optional[str] = None, bpm: Optional[str] = None) -> Dict[str, Any]:
        payload = {
            "name": name,
            "author": author or "",
            "ccli": ccli or "",
            "categoryId": 2  # Verschiedenes
        }
        res = self.request("POST", "songs", json=payload)
        song_id = res.get("id")
        
        # Ensure default arrangement exists
        arr_payload = {
            "name": "Standard-Arrangement",
            "isDefault": True,
            "keyOfArrangement": key or "",
            "bpm": bpm or ""
        }
        arr_res = self.request("POST", f"songs/{song_id}/arrangements", json=arr_payload)
        res["default_arrangement_id"] = arr_res.get("id")
        return res

    def delete_song(self, song_id: int):
        return self.request("DELETE", f"songs/{song_id}")
