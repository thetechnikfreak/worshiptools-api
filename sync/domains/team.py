import hashlib
import json
import logging
from typing import Any, Dict, List, Optional
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

    def load_wt_roles_lookup(self) -> Dict[str, str]:
        """Returns {role_name.lower(): role_id}."""
        roles = self.wt.get("meta/roles") or []
        mapping = {}
        for cat in roles:
            for r in cat.get("roles", []):
                mapping[r["name"].strip().lower()] = r["id"]
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
                # Also index by reverse "lastName firstName"
                rev = f"{ln} {fn}".strip().lower()
                if rev not in mapping:
                    mapping[rev] = uid
        return mapping

    def get_existing_wt_assignments(self, wt_service_id: str) -> Dict[str, str]:
        """
        Reads currently assigned volunteers from WorshipTools Firestore subcollection.
        Returns {(user_id, role_id): doc_id}.
        """
        try:
            fb_token = self.wt.get_firebase_id_token()
            url = (
                f"https://firestore.googleapis.com/v1/projects/worship-extreme-datastore/"
                f"databases/(default)/documents/accounts/{self.wt.account_id}/cuelists/{wt_service_id}/people"
            )
            resp = requests.get(url, headers={"Authorization": f"Bearer {fb_token}"})
            if resp.status_code != 200:
                return {}
            
            existing = {}
            docs = resp.json().get("documents", [])
            for d in docs:
                doc_id = d["name"].split("/")[-1]
                fields = d.get("fields", {})
                u_id = fields.get("user", {}).get("stringValue")
                r_id = fields.get("role", {}).get("stringValue")
                if u_id and r_id:
                    existing[(u_id, r_id)] = doc_id
            return existing
        except Exception as e:
            logger.warning(f"Error fetching existing WT assignments: {e}")
            return {}

    def sync_service_team(self, ct_event_id: int, wt_service_id: str, dry_run: bool = False) -> Dict[str, Any]:
        """Reads team assignments from CT eventServices and syncs to WT service people."""
        event_data = self.ct.get_event(ct_event_id)
        ct_services = event_data.get("eventServices") or []

        # CT service definitions lookup (serviceId -> name)
        ct_services_def = {s["id"]: s["name"] for s in self.ct.request("GET", "services")}
        wt_roles = self.load_wt_roles_lookup()
        wt_people = self.load_wt_people_lookup()
        existing_assignments = self.get_existing_wt_assignments(wt_service_id)

        synced_assignments = []
        desired_pairs = set()

        for slot in ct_services:
            srv_id = slot.get("serviceId")
            srv_name = ct_services_def.get(srv_id, "")
            person_obj = slot.get("person") or {}
            person_name = slot.get("name") or person_obj.get("title")

            if not person_name:
                continue

            person_name = str(person_name).strip()

            # Find matching WT role
            target_wt_role_name = self.role_map.get(srv_name, srv_name)
            wt_role_id = wt_roles.get(target_wt_role_name.lower())

            # Find matching WT user ID
            wt_user_id = self.people_map.get(person_name)
            if not wt_user_id:
                wt_user_id = wt_people.get(person_name.lower())

            # Fallback: clean extra whitespace
            if not wt_user_id:
                clean_name = " ".join(person_name.split()).lower()
                wt_user_id = wt_people.get(clean_name)

            action = "skipped"
            if wt_user_id and wt_role_id:
                pair = (wt_user_id, wt_role_id)
                desired_pairs.add(pair)

                if pair in existing_assignments:
                    action = "already_synced"
                else:
                    if not dry_run:
                        try:
                            self.wt.post(
                                f"service/{wt_service_id}/people",
                                json={"user": wt_user_id, "role": wt_role_id, "status": "accepted"}
                            )
                            action = "created"
                            existing_assignments[pair] = "new"
                        except Exception as e:
                            logger.error(f"Error creating WT volunteer for {person_name}: {e}")
                            action = f"error: {e}"
                    else:
                        action = "dry_run_create"
            elif not wt_user_id:
                action = "user_not_found_in_wt"
            elif not wt_role_id:
                action = f"role_not_mapped ({target_wt_role_name})"

            synced_assignments.append({
                "ct_service_name": srv_name,
                "person": person_name,
                "wt_user_id": wt_user_id,
                "wt_role_name": target_wt_role_name,
                "wt_role_id": wt_role_id,
                "action": action
            })

        # Remove deleted assignments from WT if configured
        deleted_count = 0
        delete_mode = self.config.get("sync", {}).get("delete_mode", "off")
        if delete_mode in ("both", "wt_only") and not dry_run:
            for (curr_user, curr_role), doc_id in existing_assignments.items():
                if (curr_user, curr_role) not in desired_pairs and doc_id != "new":
                    try:
                        self.wt.delete(f"service/{wt_service_id}/people/{doc_id}")
                        deleted_count += 1
                    except Exception as e:
                        logger.warning(f"Error deleting obsolete WT assignment {doc_id}: {e}")

        successful_assignments = [
            a for a in synced_assignments
            if a.get("action") in ("created", "already_synced", "dry_run_create")
        ]

        return {
            "ct_event_id": ct_event_id,
            "wt_service_id": wt_service_id,
            "assignments_count": len(successful_assignments),
            "assignments": synced_assignments,
            "removed_obsolete_count": deleted_count
        }
