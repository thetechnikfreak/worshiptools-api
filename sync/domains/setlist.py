import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import requests
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

    def _hash_ct_agenda(self, items: List[Dict[str, Any]]) -> str:
        """Compute stable hash for ChurchTools agenda items."""
        clean = []
        for it in items:
            clean.append({
                "title": (it.get("title") or "").strip(),
                "type": it.get("type", "text"),
                "songId": it.get("songId"),
                "arrId": it.get("arrangementId"),
                "note": (it.get("note") or "").strip()
            })
        return hashlib.md5(json.dumps(clean, sort_keys=True).encode("utf-8")).hexdigest()

    def _hash_wt_cues(self, cues: List[Dict[str, Any]]) -> str:
        """Compute stable hash for WorshipTools cuelist cues."""
        clean = []
        for cue in cues:
            f = cue.get("mapValue", {}).get("fields", {})
            title = (f.get("displayName", {}).get("stringValue") or "").strip()
            cue_type = f.get("cue_type", {}).get("stringValue", "")
            fg = f.get("cue_foreground", {}).get("mapValue", {}).get("fields", {})
            fg_type = fg.get("type", {}).get("stringValue", "")
            text = fg.get("text", {}).get("stringValue", "")
            clean.append({"title": title, "cue_type": cue_type, "fg_type": fg_type, "text": text})
        return hashlib.md5(json.dumps(clean, sort_keys=True).encode("utf-8")).hexdigest()

    def _parse_wt_cues_to_ct_items(self, cues: List[Dict[str, Any]], song_meta: Dict[str, Any], song_links: Dict[str, str]) -> List[Dict[str, Any]]:
        """Converts WorshipTools cues into ChurchTools agenda items."""
        agenda_items = []
        for cue in cues:
            f = cue.get("mapValue", {}).get("fields", {})
            title = f.get("displayName", {}).get("stringValue") or ""
            cue_type = f.get("cue_type", {}).get("stringValue", "")
            fg = f.get("cue_foreground", {}).get("mapValue", {}).get("fields", {})
            fg_type = fg.get("type", {}).get("stringValue", "")
            wt_song_id = fg.get("text", {}).get("stringValue")

            if cue_type == "g" or (not fg_type and title in ("Werbung", "Main")):
                agenda_items.append({
                    "title": title,
                    "type": "header"
                })
                continue

            key = ""
            notes = ""
            if wt_song_id and wt_song_id in song_meta:
                meta_item = song_meta[wt_song_id].get("mapValue", {}).get("fields", {})
                key = meta_item.get("key", {}).get("stringValue", "")
                notes = meta_item.get("notes", {}).get("stringValue", "")

            if fg_type == "text" and wt_song_id:
                ct_song_id = song_links.get(wt_song_id)
                if ct_song_id:
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
        return agenda_items

    def _push_ct_agenda_to_wt(self, wt_service_id: str, ct_items: List[Dict[str, Any]], existing_doc: Dict[str, Any], song_links_rev: Dict[str, str]) -> Dict[str, Any]:
        """Converts ChurchTools agenda items into WorshipTools cues and patches Firestore cuelist."""
        existing_data = existing_doc.setdefault("fields", {}).setdefault("data", {}).setdefault("mapValue", {}).setdefault("fields", {})
        existing_cues = existing_data.get("cues", {}).get("arrayValue", {}).get("values", [])
        
        # Index existing cues by title so we can reuse presentation/slide IDs and custom background settings
        cues_by_title = {}
        for c in existing_cues:
            f = c.get("mapValue", {}).get("fields", {})
            t = (f.get("displayName", {}).get("stringValue") or "").strip().lower()
            if t and t not in cues_by_title:
                cues_by_title[t] = c

        new_cues_values = []
        for item in ct_items:
            title = (item.get("title") or "").strip()
            item_type = item.get("type", "text")
            title_lower = title.lower()

            # If existing cue matches this title, preserve it (e.g. retains slide presentations and background settings)
            if title_lower in cues_by_title:
                new_cues_values.append(cues_by_title[title_lower])
                continue

            cue_id = str(uuid.uuid4())
            if item_type == "header":
                new_cues_values.append({
                    "mapValue": {
                        "fields": {
                            "cue_id": {"stringValue": cue_id},
                            "displayName": {"stringValue": title},
                            "cue_type": {"stringValue": "g"},
                            "cue_name": {"stringValue": title},
                            "cue_foreground": {"mapValue": {"fields": {"type": {"stringValue": ""}}}},
                            "cue_background": {
                                "mapValue": {
                                    "fields": {
                                        "type": {"stringValue": ""},
                                        "data": {"mapValue": {"fields": {"inherit": {"booleanValue": False}}}}
                                    }
                                }
                            },
                            "cue_options": {"mapValue": {}},
                            "apps": {"arrayValue": {}}
                        }
                    }
                })
            elif item_type == "song":
                ct_song_id = str(item.get("songId") or "")
                wt_song_id = song_links_rev.get(ct_song_id, "")
                new_cues_values.append({
                    "mapValue": {
                        "fields": {
                            "cue_id": {"stringValue": cue_id},
                            "displayName": {"stringValue": title},
                            "cue_type": {"stringValue": ""},
                            "cue_foreground": {
                                "mapValue": {
                                    "fields": {
                                        "type": {"stringValue": "text"},
                                        "text": {"stringValue": wt_song_id}
                                    }
                                }
                            },
                            "cue_background": {
                                "mapValue": {
                                    "fields": {
                                        "type": {"stringValue": ""},
                                        "data": {"mapValue": {"fields": {"inherit": {"booleanValue": True}}}}
                                    }
                                }
                            },
                            "cue_options": {"mapValue": {}},
                            "apps": {"arrayValue": {}}
                        }
                    }
                })
            else:
                new_cues_values.append({
                    "mapValue": {
                        "fields": {
                            "cue_id": {"stringValue": cue_id},
                            "displayName": {"stringValue": title},
                            "cue_type": {"stringValue": ""},
                            "cue_foreground": {"mapValue": {"fields": {"type": {"stringValue": ""}}}},
                            "cue_background": {
                                "mapValue": {
                                    "fields": {
                                        "type": {"stringValue": ""},
                                        "data": {"mapValue": {"fields": {"inherit": {"booleanValue": True}}}}
                                    }
                                }
                            },
                            "cue_options": {"mapValue": {}},
                            "apps": {"arrayValue": {}}
                        }
                    }
                })

        existing_data["cues"] = {"arrayValue": {"values": new_cues_values}}
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"
        existing_doc["fields"]["mod"] = {"timestampValue": now_ts}

        # Patch to Firestore
        fb_token = self.wt.get_firebase_id_token()
        url = f"https://firestore.googleapis.com/v1/projects/worship-extreme-datastore/databases/(default)/documents/accounts/{self.wt.account_id}/cuelists/{wt_service_id}"
        resp = requests.patch(url, headers={"Authorization": f"Bearer {fb_token}", "Content-Type": "application/json"}, json=existing_doc)
        resp.raise_for_status()
        return resp.json()

    def sync_service_setlist(self, ct_event_id: int, wt_service_id: str, calendar_id: int, dry_run: bool = False) -> Dict[str, Any]:
        """
        True bidirectional sync between ChurchTools Agenda and WorshipTools Cuelist.
        - Preserves manual edits made in ChurchTools (never blindly overwrites them!).
        - If ChurchTools agenda is modified, syncs changes to WorshipTools.
        - If WorshipTools cuelist is modified, syncs changes to ChurchTools.
        - If neither has changed, skips without any API calls.
        """
        # 1. Fetch ChurchTools Agenda
        ct_agenda = self.ct.get_agenda(ct_event_id)
        if ct_agenda and (ct_agenda.get("isLocked") or ct_agenda.get("isFinal")):
            logger.info(f"Agenda for CT event {ct_event_id} is locked/finalized in ChurchTools. Skipping overwrite.")
            return {
                "status": "skipped",
                "reason": "agenda_locked_in_churchtools",
                "ct_event_id": ct_event_id,
                "wt_service_id": wt_service_id,
                "items_count": len(ct_agenda.get("items", []))
            }

        ct_items = ct_agenda.get("items", []) if ct_agenda else []
        ct_hash = self._hash_ct_agenda(ct_items)
        ct_mod = (ct_agenda.get("meta", {}).get("modifiedDate") or "") if ct_agenda else ""

        # 2. Fetch WorshipTools Cuelist
        doc = self.wt.get_firestore_document(f"cuelists/{wt_service_id}")
        data_fields = doc.get("fields", {}).get("data", {}).get("mapValue", {}).get("fields", {})
        cues = data_fields.get("cues", {}).get("arrayValue", {}).get("values", [])
        song_meta = data_fields.get("songMeta", {}).get("mapValue", {}).get("fields", {})
        wt_hash = self._hash_wt_cues(cues)
        wt_mod = doc.get("fields", {}).get("mod", {}).get("timestampValue", "")

        # 3. Read Stored Sync State
        link = self.state.get_link_by_ct("setlist", str(ct_event_id))
        stored_ct_hash = link.get("ct_hash") if link else None
        stored_wt_hash = link.get("wt_hash") if link else None

        # Song links mapping (both directions)
        song_links_wt_to_ct = {l["wt_id"]: l["ct_id"] for l in self.state.get_links("song", active_only=True)}
        song_links_ct_to_wt = {l["ct_id"]: l["wt_id"] for l in self.state.get_links("song", active_only=True)}

        # 4. Decision: Has anything changed?
        if stored_ct_hash is not None and stored_wt_hash is not None:
            ct_changed = (ct_hash != stored_ct_hash)
            wt_changed = (wt_hash != stored_wt_hash)

            if not ct_changed and not wt_changed:
                return {
                    "status": "unchanged",
                    "ct_event_id": ct_event_id,
                    "wt_service_id": wt_service_id,
                    "items_count": len(ct_items)
                }

            if ct_changed and not wt_changed:
                # User modified ChurchTools agenda -> sync changes to WorshipTools!
                logger.info(f"ChurchTools agenda for event {ct_event_id} was modified by user. Syncing to WorshipTools.")
                if not dry_run:
                    updated_doc = self._push_ct_agenda_to_wt(wt_service_id, ct_items, doc, song_links_ct_to_wt)
                    new_cues = updated_doc.get("fields", {}).get("data", {}).get("mapValue", {}).get("fields", {}).get("cues", {}).get("arrayValue", {}).get("values", [])
                    new_wt_hash = self._hash_wt_cues(new_cues)
                    new_wt_mod = updated_doc.get("fields", {}).get("mod", {}).get("timestampValue", "")
                    self.state.upsert_link("setlist", str(ct_event_id), wt_service_id, ct_hash=ct_hash, wt_hash=new_wt_hash, ct_mod=ct_mod, wt_mod=new_wt_mod)

                return {
                    "status": "synced_ct_to_wt",
                    "ct_event_id": ct_event_id,
                    "wt_service_id": wt_service_id,
                    "items_count": len(ct_items),
                    "action": "preserved_and_synced_churchtools_changes"
                }

            if wt_changed and not ct_changed:
                # User modified WorshipTools cuelist -> sync changes to ChurchTools!
                logger.info(f"WorshipTools cuelist {wt_service_id} was modified. Syncing to ChurchTools.")
                parsed_agenda_items = self._parse_wt_cues_to_ct_items(cues, song_meta, song_links_wt_to_ct)
                if not dry_run and parsed_agenda_items:
                    self.ct.set_agenda(event_id=ct_event_id, calendar_id=calendar_id, items=parsed_agenda_items)
                    new_ct_agenda = self.ct.get_agenda(ct_event_id)
                    new_ct_items = new_ct_agenda.get("items", []) if new_ct_agenda else parsed_agenda_items
                    new_ct_hash = self._hash_ct_agenda(new_ct_items)
                    new_ct_mod = (new_ct_agenda.get("meta", {}).get("modifiedDate") or "") if new_ct_agenda else ""
                    self.state.upsert_link("setlist", str(ct_event_id), wt_service_id, ct_hash=new_ct_hash, wt_hash=wt_hash, ct_mod=new_ct_mod, wt_mod=wt_mod)

                return {
                    "status": "synced_wt_to_ct",
                    "ct_event_id": ct_event_id,
                    "wt_service_id": wt_service_id,
                    "items_count": len(parsed_agenda_items)
                }

            # Both changed (Conflict) -> compare modification timestamps
            if ct_mod and wt_mod and ct_mod > wt_mod:
                # ChurchTools is newer!
                logger.info(f"Conflict on event {ct_event_id}: CT ({ct_mod}) is newer than WT ({wt_mod}). Syncing CT -> WT.")
                if not dry_run:
                    updated_doc = self._push_ct_agenda_to_wt(wt_service_id, ct_items, doc, song_links_ct_to_wt)
                    new_cues = updated_doc.get("fields", {}).get("data", {}).get("mapValue", {}).get("fields", {}).get("cues", {}).get("arrayValue", {}).get("values", [])
                    new_wt_hash = self._hash_wt_cues(new_cues)
                    new_wt_mod = updated_doc.get("fields", {}).get("mod", {}).get("timestampValue", "")
                    self.state.upsert_link("setlist", str(ct_event_id), wt_service_id, ct_hash=ct_hash, wt_hash=new_wt_hash, ct_mod=ct_mod, wt_mod=new_wt_mod)
                return {
                    "status": "conflict_resolved_ct_newer",
                    "ct_event_id": ct_event_id,
                    "wt_service_id": wt_service_id,
                    "items_count": len(ct_items)
                }
            else:
                # WorshipTools is newer!
                logger.info(f"Conflict on event {ct_event_id}: WT ({wt_mod}) is newer than CT ({ct_mod}). Syncing WT -> CT.")
                parsed_agenda_items = self._parse_wt_cues_to_ct_items(cues, song_meta, song_links_wt_to_ct)
                if not dry_run and parsed_agenda_items:
                    self.ct.set_agenda(event_id=ct_event_id, calendar_id=calendar_id, items=parsed_agenda_items)
                    new_ct_agenda = self.ct.get_agenda(ct_event_id)
                    new_ct_items = new_ct_agenda.get("items", []) if new_ct_agenda else parsed_agenda_items
                    new_ct_hash = self._hash_ct_agenda(new_ct_items)
                    new_ct_mod = (new_ct_agenda.get("meta", {}).get("modifiedDate") or "") if new_ct_agenda else ""
                    self.state.upsert_link("setlist", str(ct_event_id), wt_service_id, ct_hash=new_ct_hash, wt_hash=wt_hash, ct_mod=new_ct_mod, wt_mod=wt_mod)
                return {
                    "status": "conflict_resolved_wt_newer",
                    "ct_event_id": ct_event_id,
                    "wt_service_id": wt_service_id,
                    "items_count": len(parsed_agenda_items)
                }

        # 5. Initial sync (first time this service is synced with hash tracking)
        # If ChurchTools already has an agenda and it is newer or WT has no cues:
        if ct_items and (not cues or (ct_mod and wt_mod and ct_mod > wt_mod)):
            logger.info(f"Initial setlist sync for event {ct_event_id}: CT has agenda ({len(ct_items)} items). Syncing CT -> WT.")
            if not dry_run:
                updated_doc = self._push_ct_agenda_to_wt(wt_service_id, ct_items, doc, song_links_ct_to_wt)
                new_cues = updated_doc.get("fields", {}).get("data", {}).get("mapValue", {}).get("fields", {}).get("cues", {}).get("arrayValue", {}).get("values", [])
                new_wt_hash = self._hash_wt_cues(new_cues)
                new_wt_mod = updated_doc.get("fields", {}).get("mod", {}).get("timestampValue", "")
                self.state.upsert_link("setlist", str(ct_event_id), wt_service_id, ct_hash=ct_hash, wt_hash=new_wt_hash, ct_mod=ct_mod, wt_mod=new_wt_mod)
            return {
                "status": "initial_synced_ct_to_wt",
                "ct_event_id": ct_event_id,
                "wt_service_id": wt_service_id,
                "items_count": len(ct_items)
            }
        else:
            logger.info(f"Initial setlist sync for event {ct_event_id}: WT cues ({len(cues)}) -> CT.")
            parsed_agenda_items = self._parse_wt_cues_to_ct_items(cues, song_meta, song_links_wt_to_ct)
            if not dry_run and parsed_agenda_items:
                self.ct.set_agenda(event_id=ct_event_id, calendar_id=calendar_id, items=parsed_agenda_items)
                new_ct_agenda = self.ct.get_agenda(ct_event_id)
                new_ct_items = new_ct_agenda.get("items", []) if new_ct_agenda else parsed_agenda_items
                new_ct_hash = self._hash_ct_agenda(new_ct_items)
                new_ct_mod = (new_ct_agenda.get("meta", {}).get("modifiedDate") or "") if new_ct_agenda else ""
                self.state.upsert_link("setlist", str(ct_event_id), wt_service_id, ct_hash=new_ct_hash, wt_hash=wt_hash, ct_mod=new_ct_mod, wt_mod=wt_mod)
            return {
                "status": "initial_synced_wt_to_ct",
                "ct_event_id": ct_event_id,
                "wt_service_id": wt_service_id,
                "items_count": len(parsed_agenda_items)
            }
