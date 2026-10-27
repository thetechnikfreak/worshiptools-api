import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Set, Tuple
import requests
from ct_client import ChurchToolsClient
from sync.state import SyncState
from worshiptools_api import Worshiptools_API

logger = logging.getLogger(__name__)

class TeamSyncDomain:
    def __init__(self, ct_client: ChurchToolsClient, wt_api: Worshiptools_API, state: SyncState, config: Dict[str, Any]):
        self.ct = ct_client
        self.wt = wt_api
        self.state = state
        self.config = config
        self.role_map = config.get("role_map", {})
        self.people_map = config.get("people_map", {})
        self._ct_person_cache: Optional[Dict[str, int]] = None

    def load_wt_roles_lookup(self) -> Dict[str, str]:
        """Returns {role_name.lower(): role_id}."""
        roles = self.wt.get("meta/roles") or []
        mapping = {}
        for cat in roles:
            for r in cat.get("roles", []):
                mapping[r["name"].strip().lower()] = r["id"]
        return mapping

    def load_wt_roles_id_lookup(self) -> Dict[str, str]:
        """Returns {role_id: role_name}."""
        roles = self.wt.get("meta/roles") or []
        mapping = {}
        for cat in roles:
            for r in cat.get("roles", []):
                mapping[r["id"]] = r["name"].strip()
        return mapping

    def load_wt_people_lookup(self) -> Dict[str, str]:
        """Returns {full_name.lower(): user_id}."""
        people = self.wt.get("people") or []
        mapping = {}
        for p in people:
            uid = p.get("id")
            fn = (p.get("firstName") or "").strip()
            ln = (p.get("lastName") or "").strip()
            full = f"{fn} {ln}".strip().lower()
            if full and uid:
                mapping[full] = uid
                rev = f"{ln} {fn}".strip().lower()
                if rev not in mapping:
                    mapping[rev] = uid
        return mapping

    def load_wt_people_id_lookup(self) -> Dict[str, str]:
        """Returns {user_id: full_name}."""
        people = self.wt.get("people") or []
        mapping = {}
        for p in people:
            uid = p.get("id")
            fn = (p.get("firstName") or "").strip()
            ln = (p.get("lastName") or "").strip()
            full = f"{fn} {ln}".strip()
            if full and uid:
                mapping[uid] = full
        return mapping

    def load_ct_person_cache(self) -> Dict[str, int]:
        """Builds a lookup of ChurchTools {full_name.lower(): person_id}."""
        if self._ct_person_cache is not None:
            return self._ct_person_cache

        cache: Dict[str, int] = {}
        # 1. Read from people_map config if provided
        for name, val in self.people_map.items():
            name_clean = str(name).strip().lower()
            if isinstance(val, dict) and "ct_id" in val:
                cache[name_clean] = int(val["ct_id"])
            elif isinstance(val, int):
                cache[name_clean] = val
            elif isinstance(val, str) and val.isdigit():
                cache[name_clean] = int(val)

        # 2. Learn from existing ChurchTools events
        try:
            events = self.ct.get_events(from_date="2026-10-01", to_date="2026-12-31")
            for ev in events:
                for s in ev.get("eventServices", []):
                    p = s.get("person") or {}
                    p_name = s.get("name") or p.get("title")
                    p_id = p.get("domainIdentifier") or s.get("personId")
                    if p_name and p_id:
                        clean_n = str(p_name).strip().lower()
                        cache[clean_n] = int(p_id)
                        parts = clean_n.split()
                        if len(parts) == 2:
                            cache[f"{parts[1]} {parts[0]}"] = int(p_id)
        except Exception as e:
            logger.warning(f"Error loading CT person cache from events: {e}")

        self._ct_person_cache = cache
        return cache

    def get_existing_wt_assignments_details(self, wt_service_id: str) -> List[Dict[str, str]]:
        """
        Reads currently assigned volunteers from WorshipTools Firestore subcollection.
        Returns list of dicts: [{'doc_id': ..., 'user_id': ..., 'role_id': ..., 'status': ...}].
        """
        try:
            fb_token = self.wt.get_firebase_id_token()
            url = (
                f"https://firestore.googleapis.com/v1/projects/worship-extreme-datastore/"
                f"databases/(default)/documents/accounts/{self.wt.account_id}/cuelists/{wt_service_id}/people"
            )
            resp = requests.get(url, headers={"Authorization": f"Bearer {fb_token}"})
            if resp.status_code != 200:
                return []
            
            volunteers = []
            docs = resp.json().get("documents", [])
            for d in docs:
                doc_id = d["name"].split("/")[-1]
                fields = d.get("fields", {})
                u_id = fields.get("user", {}).get("stringValue")
                r_id = fields.get("role", {}).get("stringValue")
                status = fields.get("status", {}).get("stringValue", "pending")
                if u_id and r_id:
                    volunteers.append({
                        "doc_id": doc_id,
                        "user_id": u_id,
                        "role_id": r_id,
                        "status": status
                    })
            return volunteers
        except Exception as e:
            logger.warning(f"Error fetching existing WT assignments: {e}")
            return []

    def get_existing_wt_assignments(self, wt_service_id: str) -> Dict[Tuple[str, str], str]:
        """Backwards compatibility: Returns {(user_id, role_id): doc_id}."""
        details = self.get_existing_wt_assignments_details(wt_service_id)
        return {(d["user_id"], d["role_id"]): d["doc_id"] for d in details}

    def _normalize_pair(self, person_name: str, ct_service_name: str) -> Tuple[str, str]:
        return (person_name.strip().lower(), ct_service_name.strip().lower())

    def sync_service_team(self, ct_event_id: int, wt_service_id: str, dry_run: bool = False) -> Dict[str, Any]:
        """
        State-tracked bidirectional team synchronization:
        - Detects additions in ChurchTools and pushes to WorshipTools.
        - Detects additions in WorshipTools and pushes to ChurchTools.
        - Detects unassignments/removals in either system and synchronizes the removal
          to the other system (without resurrection loops).
        """
        event_data = self.ct.get_event(ct_event_id)
        ct_services = event_data.get("eventServices") or []

        # Service definitions lookup (serviceId -> name and name.lower -> serviceId)
        ct_services_list = self.ct.request("GET", "services") or []
        ct_services_def = {s["id"]: s["name"] for s in ct_services_list}
        ct_name_to_srv_id = {s["name"].strip().lower(): s["id"] for s in ct_services_list}

        wt_roles = self.load_wt_roles_lookup()
        wt_roles_by_id = self.load_wt_roles_id_lookup()
        wt_people = self.load_wt_people_lookup()
        wt_people_by_id = self.load_wt_people_id_lookup()
        ct_person_cache = self.load_ct_person_cache()

        # Build reverse role map: WT role name -> CT service name
        reverse_role_map = {}
        for ct_name, wt_name in self.role_map.items():
            reverse_role_map[wt_name.strip().lower()] = ct_name.strip()

        # Parse current ChurchTools state
        # ct_current_pairs: set of (person_name.lower(), ct_srv_name.lower())
        ct_current_pairs: Set[Tuple[str, str]] = set()
        ct_slot_by_pair: Dict[Tuple[str, str], int] = {}
        empty_slots: Dict[int, List[int]] = {}
        slot_counts: Dict[int, int] = {}

        for slot in ct_services:
            slot_id = slot.get("id")
            srv_id = slot.get("serviceId")
            srv_name = ct_services_def.get(srv_id, "")
            slot_counts[srv_id] = slot_counts.get(srv_id, 0) + 1

            person_obj = slot.get("person") or {}
            person_name = slot.get("name") or person_obj.get("title")

            if person_name:
                p_name_clean = str(person_name).strip()
                pair_key = self._normalize_pair(p_name_clean, srv_name)
                ct_current_pairs.add(pair_key)
                ct_slot_by_pair[pair_key] = slot_id

                # Cache person ID if present
                p_id = person_obj.get("domainIdentifier") or slot.get("personId")
                if p_id:
                    ct_person_cache[p_name_clean.lower()] = int(p_id)
            else:
                empty_slots.setdefault(srv_id, []).append(slot_id)

        # Parse current WorshipTools state
        wt_volunteer_details = self.get_existing_wt_assignments_details(wt_service_id)
        # wt_current_pairs: set of (person_name.lower(), ct_srv_name.lower())
        wt_current_pairs: Set[Tuple[str, str]] = set()
        wt_doc_by_pair: Dict[Tuple[str, str], str] = {}
        wt_vol_by_pair: Dict[Tuple[str, str], Dict[str, str]] = {}

        for vol in wt_volunteer_details:
            u_id = vol["user_id"]
            r_id = vol["role_id"]
            p_name = wt_people_by_id.get(u_id, "")
            r_name = wt_roles_by_id.get(r_id, "")
            if p_name and r_name:
                ct_srv = reverse_role_map.get(r_name.lower(), r_name)
                pair_key = self._normalize_pair(p_name, ct_srv)
                wt_current_pairs.add(pair_key)
                wt_doc_by_pair[pair_key] = vol["doc_id"]
                wt_vol_by_pair[pair_key] = {
                    "user_id": u_id,
                    "role_id": r_id,
                    "doc_id": vol["doc_id"],
                    "person_name": p_name,
                    "role_name": r_name,
                    "ct_service_name": ct_srv
                }

        # Retrieve last synced state from sqlite database
        link = self.state.get_link_by_ct("team", str(ct_event_id))
        last_synced_pairs: Set[Tuple[str, str]] = set()
        has_baseline = False
        if link and link.get("ct_hash"):
            try:
                loaded = json.loads(link["ct_hash"])
                last_synced_pairs = set(tuple(p) for p in loaded)
                has_baseline = True
            except Exception as e:
                logger.warning(f"Error parsing last synced team pairs: {e}")

        actions_taken = []
        synced_count = 0
        removed_count = 0

        # If we have a stored baseline, compute deltas:
        if has_baseline:
            # 1. Removals:
            # Removed in ChurchTools:
            removed_in_ct = (last_synced_pairs - ct_current_pairs) & wt_current_pairs
            for pair in removed_in_ct:
                p_name, srv_name = pair
                doc_id = wt_doc_by_pair.get(pair)
                if doc_id:
                    if not dry_run:
                        try:
                            self.wt.delete(f"service/{wt_service_id}/people/{doc_id}")
                            actions_taken.append({
                                "direction": "ct_to_wt",
                                "action": "removed_in_wt",
                                "person": p_name,
                                "ct_service_name": srv_name
                            })
                            removed_count += 1
                        except Exception as e:
                            logger.error(f"Error deleting WT assignment {doc_id} for {p_name}: {e}")
                    else:
                        actions_taken.append({
                            "direction": "ct_to_wt",
                            "action": "dry_run_remove_wt",
                            "person": p_name,
                            "ct_service_name": srv_name
                        })
                # Remove from wt_current_pairs so we don't re-add
                wt_current_pairs.discard(pair)

            # Removed in WorshipTools:
            removed_in_wt = (last_synced_pairs - wt_current_pairs) & ct_current_pairs
            for pair in removed_in_wt:
                p_name, srv_name = pair
                slot_id = ct_slot_by_pair.get(pair)
                if slot_id:
                    if not dry_run:
                        try:
                            self.ct.request("DELETE", f"events/{ct_event_id}/eventservices/{slot_id}")
                            actions_taken.append({
                                "direction": "wt_to_ct",
                                "action": "removed_in_ct",
                                "person": p_name,
                                "ct_service_name": srv_name
                            })
                            removed_count += 1
                        except Exception as e:
                            logger.error(f"Error unassigning CT slot {slot_id} for {p_name}: {e}")
                    else:
                        actions_taken.append({
                            "direction": "wt_to_ct",
                            "action": "dry_run_remove_ct",
                            "person": p_name,
                            "ct_service_name": srv_name
                        })
                # Remove from ct_current_pairs so we don't re-add
                ct_current_pairs.discard(pair)

        # 2. Additions / Syncs:
        # ChurchTools -> WorshipTools additions:
        to_create_in_wt = ct_current_pairs - wt_current_pairs
        for pair in to_create_in_wt:
            p_name, srv_name = pair
            target_wt_role_name = self.role_map.get(srv_name, srv_name)
            wt_role_id = wt_roles.get(target_wt_role_name.lower())

            wt_user_id = self.people_map.get(p_name) or wt_people.get(p_name)
            if not wt_user_id:
                clean_name = " ".join(p_name.split()).lower()
                wt_user_id = wt_people.get(clean_name)

            if wt_user_id and wt_role_id:
                if not dry_run:
                    try:
                        self.wt.post(
                            f"service/{wt_service_id}/people",
                            json={"user": wt_user_id, "role": wt_role_id, "status": "accepted"}
                        )
                        actions_taken.append({
                            "direction": "ct_to_wt",
                            "action": "created_in_wt",
                            "person": p_name,
                            "ct_service_name": srv_name,
                            "wt_role_name": target_wt_role_name
                        })
                        synced_count += 1
                        wt_current_pairs.add(pair)
                    except Exception as e:
                        logger.error(f"Error creating WT volunteer for {p_name}: {e}")
                else:
                    actions_taken.append({
                        "direction": "ct_to_wt",
                        "action": "dry_run_create_wt",
                        "person": p_name,
                        "ct_service_name": srv_name,
                        "wt_role_name": target_wt_role_name
                    })
                    synced_count += 1
            else:
                actions_taken.append({
                    "direction": "ct_to_wt",
                    "action": "skipped_wt_lookup_missing",
                    "person": p_name,
                    "ct_service_name": srv_name
                })

        # WorshipTools -> ChurchTools additions:
        to_create_in_ct = wt_current_pairs - ct_current_pairs
        for pair in to_create_in_ct:
            p_name, srv_name = pair
            target_ct_srv_id = ct_name_to_srv_id.get(srv_name.lower())
            if not target_ct_srv_id:
                actions_taken.append({
                    "direction": "wt_to_ct",
                    "action": f"service_not_found_in_ct ({srv_name})",
                    "person": p_name
                })
                continue

            if not dry_run:
                try:
                    target_slot_id = None
                    if empty_slots.get(target_ct_srv_id):
                        target_slot_id = empty_slots[target_ct_srv_id].pop(0)
                    else:
                        # Add a new slot in ChurchTools
                        curr_cnt = slot_counts.get(target_ct_srv_id, 0)
                        new_cnt = curr_cnt + 1
                        self.ct.request(
                            "PUT",
                            f"events/{ct_event_id}/eventservices",
                            json={"services": [{"serviceId": target_ct_srv_id, "count": new_cnt}]}
                        )
                        slot_counts[target_ct_srv_id] = new_cnt
                        refreshed_ev = self.ct.get_event(ct_event_id)
                        for s in refreshed_ev.get("eventServices", []):
                            if s.get("serviceId") == target_ct_srv_id:
                                p_obj = s.get("person") or {}
                                if not (s.get("name") or p_obj.get("title")):
                                    target_slot_id = s.get("id")
                                    break

                    if target_slot_id:
                        ct_pid = ct_person_cache.get(p_name.lower())
                        if not ct_pid:
                            parts = p_name.split()
                            if len(parts) == 2:
                                ct_pid = ct_person_cache.get(f"{parts[1]} {parts[0]}".lower())

                        put_payload = {"isAccepted": True}
                        if ct_pid:
                            put_payload["personId"] = ct_pid
                            put_payload["name"] = None
                        else:
                            put_payload["name"] = p_name
                            put_payload["personId"] = None

                        self.ct.request("PUT", f"events/{ct_event_id}/eventservices/{target_slot_id}", json=put_payload)
                        actions_taken.append({
                            "direction": "wt_to_ct",
                            "action": "created_in_ct",
                            "person": p_name,
                            "ct_service_name": srv_name
                        })
                        synced_count += 1
                        ct_current_pairs.add(pair)
                except Exception as e:
                    logger.error(f"Error creating CT volunteer for {p_name} in {srv_name}: {e}")
            else:
                actions_taken.append({
                    "direction": "wt_to_ct",
                    "action": "dry_run_create_ct",
                    "person": p_name,
                    "ct_service_name": srv_name
                })
                synced_count += 1

        # Matching pairs that were already in both
        already_synced_pairs = ct_current_pairs & wt_current_pairs
        for pair in already_synced_pairs:
            p_name, srv_name = pair
            actions_taken.append({
                "direction": "bidirectional",
                "action": "already_synced",
                "person": p_name,
                "ct_service_name": srv_name
            })
            synced_count += 1

        # 3. Save new combined baseline in SQLite SyncState
        if not dry_run:
            final_active_pairs = sorted(list(ct_current_pairs & wt_current_pairs))
            state_data = json.dumps(final_active_pairs)
            self.state.upsert_link(
                domain="team",
                ct_id=str(ct_event_id),
                wt_id=wt_service_id,
                ct_hash=state_data,
                wt_hash=state_data
            )

        return {
            "ct_event_id": ct_event_id,
            "wt_service_id": wt_service_id,
            "assignments_count": synced_count,
            "removed_count": removed_count,
            "actions": actions_taken
        }
