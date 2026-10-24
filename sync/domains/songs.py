import logging
from typing import Any, Dict, List, Optional
from ct_client import ChurchToolsClient
from sync.state import SyncState
from worshiptools_api import Worshiptools_API

logger = logging.getLogger(__name__)

class SongsSyncDomain:
    def __init__(self, ct_client: ChurchToolsClient, wt_api: Worshiptools_API, state: SyncState, config: Dict[str, Any]):
        self.ct = ct_client
        self.wt = wt_api
        self.state = state
        self.config = config

    def sync(self, dry_run: bool = False) -> Dict[str, Any]:
        result = {
            "seeded_in_ct": [],
            "matched_existing": [],
            "skipped": 0,
            "errors": []
        }

        # 1. Fetch CT songs
        ct_songs = self.ct.get_songs()
        ct_by_ccli = {str(s.get("ccli")).strip(): s for s in ct_songs if s.get("ccli")}
        ct_by_name = {s.get("name", "").strip().lower(): s for s in ct_songs if s.get("name")}

        # 2. Fetch WT songs (batch fetch all songs from Solr index)
        resp = self.wt.get("song", params={"rows": 1000})
        docs = resp.get("response", {}).get("docs", []) if isinstance(resp, dict) else []

        links = {l["wt_id"]: l for l in self.state.get_links("song", active_only=True)}

        for wt_song in docs:
            wt_id = str(wt_song.get("id"))
            name = str(wt_song.get("name") or "").strip()
            artist = str(wt_song.get("artist") or "").strip()
            ccli = str(wt_song.get("ccli") or "").strip()
            key = str(wt_song.get("key") or "")
            tempo = str(wt_song.get("tempo") or "") if wt_song.get("tempo") else ""

            if wt_id in links:
                result["skipped"] += 1
                continue

            # Try matching with existing CT song
            match_ct = None
            if ccli and ccli in ct_by_ccli:
                match_ct = ct_by_ccli[ccli]
            elif name.lower() in ct_by_name:
                match_ct = ct_by_name[name.lower()]

            if match_ct:
                ct_id = str(match_ct["id"])
                result["matched_existing"].append({"wt_id": wt_id, "ct_id": ct_id, "name": name})
                self.state.upsert_link("song", ct_id, wt_id, "", "", "", "")
            else:
                # Seed song into CT
                result["seeded_in_ct"].append({"wt_id": wt_id, "name": name, "artist": artist, "ccli": ccli})
                if not dry_run:
                    try:
                        created = self.ct.create_song(name=name, author=artist, ccli=ccli, key=key, bpm=tempo)
                        ct_id = str(created["id"])
                        self.state.upsert_link("song", ct_id, wt_id, "", "", "", "")
                    except Exception as e:
                        result["errors"].append({"wt_id": wt_id, "name": name, "error": str(e)})

        return result
