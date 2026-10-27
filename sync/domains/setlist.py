import logging
from typing import Any, Dict, List, Optional
from ct_client import ChurchToolsClient
from sync.state import SyncState
from worshiptools_api import Worshiptools_API

logger = logging.getLogger(__name__)

class SetlistSyncDomain:
    def __init__(self, ct_client: ChurchToolsClient, wt_api: Worshiptools_API, state: SyncState, config: Dict[str, Any]):
        self.ct = ct_client
        self.wt = wt_api
        self.state = state
        self.config = config

    def sync_service_setlist(self, ct_event_id: int, wt_service_id: str, calendar_id: int, dry_run: bool = False) -> Dict[str, Any]:
        """Reads songs from WT service cues and updates CT event agenda."""
        # Check if agenda in ChurchTools is locked or finalized by user
        existing_agenda = self.ct.get_agenda(ct_event_id)
        if existing_agenda and (existing_agenda.get("isLocked") or existing_agenda.get("isFinal")):
            logger.info(f"Agenda for CT event {ct_event_id} is locked/finalized in ChurchTools. Skipping overwrite.")
            return {
                "status": "skipped",
                "reason": "agenda_locked_in_churchtools",
                "ct_event_id": ct_event_id,
                "wt_service_id": wt_service_id,
                "items_count": len(existing_agenda.get("items", []))
            }

        doc = self.wt.get_firestore_document(f"cuelists/{wt_service_id}")
        data_fields = doc.get("fields", {}).get("data", {}).get("mapValue", {}).get("fields", {})
        cues = data_fields.get("cues", {}).get("arrayValue", {}).get("values", [])
        song_meta = data_fields.get("songMeta", {}).get("mapValue", {}).get("fields", {})

        song_links = {l["wt_id"]: l["ct_id"] for l in self.state.get_links("song", active_only=True)}

        agenda_items = []
        for cue in cues:
            f = cue.get("mapValue", {}).get("fields", {})
            title = f.get("displayName", {}).get("stringValue") or ""
            cue_type = f.get("cue_type", {}).get("stringValue", "")
            fg = f.get("cue_foreground", {}).get("mapValue", {}).get("fields", {})
            fg_type = fg.get("type", {}).get("stringValue", "")
            wt_song_id = fg.get("text", {}).get("stringValue")

            # In WorshipTools, cues with cue_type == 'g' (Groups/Sections like 'Werbung', 'Main')
            # or explicit headers are section headers.
            if cue_type == "g" or (not fg_type and title in ("Werbung", "Main")):
                agenda_items.append({
                    "title": title,
                    "type": "header"
                })
                continue

            # Extract key/notes from songMeta if available
            key = ""
            notes = ""
            if wt_song_id and wt_song_id in song_meta:
                meta_item = song_meta[wt_song_id].get("mapValue", {}).get("fields", {})
                key = meta_item.get("key", {}).get("stringValue", "")
                notes = meta_item.get("notes", {}).get("stringValue", "")

            if fg_type == "text" and wt_song_id:
                ct_song_id = song_links.get(wt_song_id)
                if ct_song_id:
                    # Fetch song's default arrangement id
                    ct_song_data = self.ct.request("GET", f"songs/{ct_song_id}")
                    arrs = ct_song_data.get("arrangements") or []
                    if not arrs:
                        new_arr = self.ct.request("POST", f"songs/{ct_song_id}/arrangements", json={"name": "Standard-Arrangement", "isDefault": True})
                        arr_id = new_arr.get("id")
                    else:
                        arr_id = arrs[0].get("id")
                    agenda_items.append({
                        "title": title,
                        "type": "song",
                        "duration": 300,
                        "songId": int(ct_song_id),
                        "arrangementId": int(arr_id),
                        "note": f"Tonart: {key}" if key else notes
                    })
                else:
                    # Song not linked yet in CT songs library, add as text item
                    agenda_items.append({
                        "title": f"Lied: {title}",
                        "type": "text",
                        "duration": 300,
                        "note": f"Tonart: {key}" if key else notes
                    })
            elif fg_type == "bible":
                agenda_items.append({
                    "title": title,
                    "type": "text",
                    "duration": 180,
                    "note": "Schriftlesung"
                })
            elif title.lower() == "predigt":
                agenda_items.append({
                    "title": "Predigt",
                    "type": "text",
                    "duration": 1200,
                    "note": ""
                })
            elif title:
                agenda_items.append({
                    "title": title,
                    "type": "text",
                    "duration": 120,
                    "note": ""
                })

        if not agenda_items:
            return {"status": "no_cues", "items_count": 0}

        if not dry_run:
            try:
                self.ct.set_agenda(event_id=ct_event_id, calendar_id=calendar_id, items=agenda_items)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 403:
                    return {
                        "status": "skipped",
                        "reason": "agenda_locked_or_forbidden",
                        "ct_event_id": ct_event_id,
                        "wt_service_id": wt_service_id
                    }
                raise

        return {
            "status": "synced",
            "ct_event_id": ct_event_id,
            "wt_service_id": wt_service_id,
            "items_count": len(agenda_items),
            "songs_count": len([i for i in agenda_items if i.get("type") == "song"])
        }
