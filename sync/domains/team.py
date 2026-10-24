import hashlib
import json
import logging
from typing import Any, Dict, List, Optional
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
        roles = self.wt.get("meta/roles")
        mapping = {}
        for cat in roles:
            for r in cat.get("roles", []):
                mapping[r["name"].strip().lower()] = r["id"]
        return mapping

    def sync_service_team(self, ct_event_id: int, wt_service_id: str, dry_run: bool = False) -> Dict[str, Any]:
        """Reads team assignments from CT eventServices and syncs to WT cuelist people subcollection."""
        event_data = self.ct.get_event(ct_event_id)
        ct_services = event_data.get("eventServices") or []

        # CT service definitions lookup (serviceId -> name)
        ct_services_def = {s["id"]: s["name"] for s in self.ct.request("GET", "services")}
        wt_roles = self.load_wt_roles_lookup()

        synced_assignments = []
        for slot in ct_services:
            srv_id = slot.get("serviceId")
            srv_name = ct_services_def.get(srv_id, "")
            person_obj = slot.get("person") or {}
            person_name = slot.get("name") or person_obj.get("title")

            if not person_name:
                continue

            # Find matching WT role
            target_wt_role_name = self.role_map.get(srv_name, srv_name)
            wt_role_id = wt_roles.get(target_wt_role_name.lower())

            synced_assignments.append({
                "ct_service_name": srv_name,
                "person": person_name,
                "wt_role_name": target_wt_role_name,
                "wt_role_id": wt_role_id
            })

        return {
            "ct_event_id": ct_event_id,
            "wt_service_id": wt_service_id,
            "assignments_count": len(synced_assignments),
            "assignments": synced_assignments
        }
