import argparse
from datetime import datetime, timedelta
import json
import logging
import os
import sys
from zoneinfo import ZoneInfo
import yaml
from dotenv import load_dotenv

load_dotenv()

from ct_client import ChurchToolsClient
from sync.domains.services import ServicesSyncDomain
from sync.domains.songs import SongsSyncDomain
from sync.state import SyncState
from worshiptools_api import Worshiptools_API

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def main():
    parser = argparse.ArgumentParser(description="Bidirectional sync ChurchTools <-> WorshipTools")
    parser.add_argument("--dry-run", action="store_true", help="Perform a dry run without modifying data")
    parser.add_argument("--domain", default="all", choices=["all", "services", "songs"], help="Domain to sync")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--state-db", default="/data/sync_state.sqlite", help="Path to SQLite state DB")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    ct_url = os.environ.get("CT_URL", "").rstrip("/")
    ct_token = os.environ.get("CT_TOKEN", "")
    wt_email = os.environ.get("WORSHIPTOOLS_EMAIL") or os.environ.get("WT_EMAIL", "")
    wt_password = os.environ.get("WORSHIPTOOLS_PASSWORD") or os.environ.get("WT_PASSWORD", "")
    wt_account_id = os.environ.get("WORSHIPTOOLS_ACCOUNT_ID") or os.environ.get("WT_ACCOUNT_ID", "")

    if not ct_url or not ct_token:
        sys.exit("Error: CT_URL or CT_TOKEN missing in environment.")
    if not wt_email or not wt_password or not wt_account_id:
        sys.exit("Error: WORSHIPTOOLS credentials missing in environment.")

    ct_client = ChurchToolsClient(ct_url, ct_token)
    wt_api = Worshiptools_API(wt_email, wt_password, wt_account_id)
    
    state_file = args.state_db
    # If running locally and /data is not root, fallback to local path
    if state_file.startswith("/data") and not os.path.exists("/data") and os.name == "nt":
        state_file = "data/sync_state.sqlite"

    state = SyncState(state_file)
    legacy_json = os.environ.get("SYNC_STATE_FILE", "/data/sync_state.json")
    if legacy_json.startswith("/data") and not os.path.exists("/data") and os.name == "nt":
        legacy_json = "data/sync_state.json"
    state.migrate_from_json(legacy_json)

    tz = ZoneInfo(config.get("churchtools", {}).get("sync_timezone", "Europe/Berlin"))
    days_ahead = int(config.get("churchtools", {}).get("sync_days_ahead", 60))
    today = datetime.now(tz).date()
    from_date = today.isoformat()
    to_date = (today + timedelta(days=days_ahead)).isoformat()

    results = {}
    if args.domain in ("all", "services"):
        logging.info(f"Starting Services sync (Window: {from_date} to {to_date}, dry_run={args.dry_run})...")
        services_domain = ServicesSyncDomain(ct_client, wt_api, state, config)
        results["services"] = services_domain.sync(from_date, to_date, dry_run=args.dry_run)

    if args.domain in ("all", "songs"):
        logging.info(f"Starting Songs seed/sync (dry_run={args.dry_run})...")
        songs_domain = SongsSyncDomain(ct_client, wt_api, state, config)
        results["songs"] = songs_domain.sync(dry_run=args.dry_run)

    print(json.dumps(results, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
