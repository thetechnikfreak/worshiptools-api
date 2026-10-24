import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import yaml
from dotenv import load_dotenv
from flask import Flask, jsonify, request

from ct_client import ChurchToolsClient
from sync.domains.services import ServicesSyncDomain
from sync.domains.setlist import SetlistSyncDomain
from sync.domains.songs import SongsSyncDomain
from sync.domains.team import TeamSyncDomain
from sync.state import SyncState
from worshiptools_api import Worshiptools_API

load_dotenv()

app = Flask(__name__)

# API Key security for incoming webhook/API calls
API_KEY = os.environ.get("SYNC_API_KEY", "")

def check_auth():
    if not API_KEY:
        return True  # If no key configured in .env, endpoints remain open
    
    # Check Bearer token in Authorization header
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token == API_KEY:
            return True
            
    # Check custom X-API-Key header
    api_key_header = request.headers.get("X-API-Key", "").strip()
    if api_key_header == API_KEY:
        return True
        
    # Check query parameter ?api_key=...
    if request.args.get("api_key") == API_KEY:
        return True
        
    return False

# State and clients
CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.yaml")
STATE_DB = os.environ.get("SYNC_STATE_DB", "/data/sync_state.sqlite")
if STATE_DB.startswith("/data") and not os.path.exists("/data") and os.name == "nt":
    STATE_DB = "data/sync_state.sqlite"

state = SyncState(STATE_DB)
legacy_json = os.environ.get("SYNC_STATE_FILE", "/data/sync_state.json")
if legacy_json.startswith("/data") and not os.path.exists("/data") and os.name == "nt":
    legacy_json = "data/sync_state.json"
state.migrate_from_json(legacy_json)

ct_client = ChurchToolsClient(os.environ.get("CT_URL", ""), os.environ.get("CT_TOKEN", ""))
wt_api = Worshiptools_API(
    os.environ.get("WORSHIPTOOLS_EMAIL") or os.environ.get("WT_EMAIL", ""),
    os.environ.get("WORSHIPTOOLS_PASSWORD") or os.environ.get("WT_PASSWORD", ""),
    os.environ.get("WORSHIPTOOLS_ACCOUNT_ID") or os.environ.get("WT_ACCOUNT_ID", ""),
)

sync_lock = threading.Lock()
last_sync_result = {}

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def run_full_sync(dry_run=False):
    global last_sync_result
    if not sync_lock.acquire(blocking=False):
        return {"status": "already_running"}

    started_at = datetime.now(ZoneInfo("UTC")).isoformat()
    try:
        config = load_config()
        tz = ZoneInfo(config.get("churchtools", {}).get("sync_timezone", "Europe/Berlin"))
        days_ahead = int(config.get("churchtools", {}).get("sync_days_ahead", 60))
        today = datetime.now(tz).date()
        from_date = today.isoformat()
        to_date = (today + timedelta(days=days_ahead)).isoformat()

        # 1. Services Domain
        services_domain = ServicesSyncDomain(ct_client, wt_api, state, config)
        srv_res = services_domain.sync(from_date, to_date, dry_run=dry_run)

        # 2. Songs Domain
        songs_domain = SongsSyncDomain(ct_client, wt_api, state, config)
        songs_res = songs_domain.sync(dry_run=dry_run)

        # 3. Setlist & Team Domain for linked services
        setlist_domain = SetlistSyncDomain(ct_client, wt_api, state, config)
        team_domain = TeamSyncDomain(ct_client, wt_api, state, config)

        cal_id = ct_client.get_calendar_id_by_name(config.get("churchtools", {}).get("calendar", "Gottesdienst"))
        links = state.get_links("service", active_only=True)
        setlists_res = []
        teams_res = []
        for l in links:
            ct_id = int(l["ct_id"]) if l["ct_id"].isdigit() else None
            wt_id = l["wt_id"]
            if ct_id and cal_id:
                try:
                    s_res = setlist_domain.sync_service_setlist(ct_id, wt_id, cal_id, dry_run=dry_run)
                    setlists_res.append(s_res)
                except Exception as e:
                    setlists_res.append({"ct_event_id": ct_id, "error": str(e)})

                try:
                    t_res = team_domain.sync_service_team(ct_id, wt_id, dry_run=dry_run)
                    teams_res.append(t_res)
                except Exception as e:
                    teams_res.append({"ct_event_id": ct_id, "error": str(e)})

        finished_at = datetime.now(ZoneInfo("UTC")).isoformat()
        summary = {
            "status": "success",
            "started_at": started_at,
            "finished_at": finished_at,
            "dry_run": dry_run,
            "services": srv_res,
            "songs": songs_res,
            "setlists": setlists_res,
            "teams": teams_res
        }
        if not dry_run:
            state.record_run(started_at, finished_at, summary)
        last_sync_result = summary
        return summary
    finally:
        sync_lock.release()

@app.route("/api/sync", methods=["POST", "GET"])
@app.route("/api/sync-churchtools", methods=["POST", "GET"])
def trigger_sync():
    if not check_auth():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    dry = request.args.get("dry_run") == "1"
    res = run_full_sync(dry_run=dry)
    return jsonify({"success": True, **res})

@app.route("/api/sync/status", methods=["GET"])
def sync_status():
    if not check_auth():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    return jsonify({
        "success": True,
        "last_sync": last_sync_result,
        "is_locked": sync_lock.locked()
    })

@app.route("/api/upload", methods=["POST"])
def upload_file_endpoint():
    """Uploads a file or image to WorshipTools."""
    if not check_auth():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file part in the request"}), 400

    uploaded = request.files["file"]
    if uploaded.filename == "":
        return jsonify({"success": False, "error": "No selected file"}), 400

    filename = uploaded.filename
    is_presentation = filename.lower().endswith((".pptx", ".ppt"))
    is_image = filename.lower().endswith((".jpg", ".jpeg", ".png"))
    is_media = request.form.get("media") == "1" or is_image or is_presentation

    name = request.form.get("name")
    if not name:
        name = filename if is_presentation else os.path.splitext(filename)[0]

    try:
        file_bytes = uploaded.read()
        if is_presentation:
            media_id = request.form.get("media_id") or None
            presentation_info = wt_api.upload_media_presentation(name=name, pptx_bytes=file_bytes, media_id=media_id)
            return jsonify({
                "success": True,
                "type": "presentation",
                **presentation_info
            }), 201
        elif is_media:
            # Upload directly into Presenter media library
            media_info = wt_api.upload_media_image(name=name, image_bytes=file_bytes)
            return jsonify({
                "success": True,
                "type": "media",
                **media_info
            }), 201
        else:
            destination = request.form.get("destination") or None
            content_type = uploaded.content_type or "application/octet-stream"
            storage_path = wt_api.upload_file(
                filename=filename,
                file_bytes=file_bytes,
                destination_path=destination,
                content_type=content_type
            )
            return jsonify({
                "success": True,
                "type": "file",
                "filename": filename,
                "storage_path": storage_path
            }), 201
    except Exception as e:
        logging.error(f"Error uploading file: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/upload-urls", methods=["POST"])
def upload_urls_endpoint():
    """
    Downloads files from URLs (e.g. Canva exports from n8n) and uploads them into WorshipTools.
    Expected JSON:
    {
        "files": [
            { "name": "Title_Ein Zeichen der Hoffnung.png", "url": "https://..." },
            { "name": "Werbung_Ein Zeichen der Hoffnung.pptx", "url": "https://..." }
        ],
        "title": "Optional Title"
    }
    """
    if not check_auth():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json(force=True, silent=True) or {}
    files = data.get("files") or []
    if not files:
        return jsonify({"success": False, "error": "No files provided"}), 400

    import requests as req
    results = []
    errors = []

    for f in files:
        name = f.get("name") or "file"
        url = f.get("url")
        if not url:
            continue

        try:
            resp = req.get(url, timeout=90)
            resp.raise_for_status()
            content = resp.content

            if name.lower().endswith((".pptx", ".ppt")):
                info = wt_api.upload_media_presentation(name=name, pptx_bytes=content)
                results.append({"name": name, "type": "presentation", **info})
            elif name.lower().endswith((".jpg", ".jpeg", ".png")):
                clean_name = os.path.splitext(name)[0]
                info = wt_api.upload_media_image(name=clean_name, image_bytes=content)
                results.append({"name": name, "type": "media", **info})
            else:
                storage_path = wt_api.upload_file(filename=name, file_bytes=content)
                results.append({"name": name, "type": "file", "storage_path": storage_path})
        except Exception as e:
            logging.error(f"Error uploading {name} from {url}: {e}", exc_info=True)
            errors.append({"name": name, "error": str(e)})

    return jsonify({
        "success": len(errors) == 0,
        "count": len(results),
        "uploaded": results,
        "errors": errors if errors else None
    }), (200 if len(results) > 0 else 500)

_service_type_names = {}

def get_wt_service_type_names(force_refresh=False):
    global _service_type_names
    if _service_type_names and not force_refresh:
        return _service_type_names
    try:
        resp = wt_api.get("meta/service-types")
        if isinstance(resp, list):
            items = resp
        elif isinstance(resp, dict):
            items = resp.get("data") or resp.get("docs") or resp.get("response", {}).get("docs", [])
        else:
            items = []
        _service_type_names = {str(t["id"]): str(t["name"]) for t in items if isinstance(t, dict) and "id" in t and "name" in t}
    except Exception as e:
        logging.warning(f"Failed to fetch WT service types: {e}")
    return _service_type_names

@app.route("/api/churchtools/services", methods=["GET"])
@app.route("/api/churchtools/new-services", methods=["GET"])
def get_churchtools_services():
    """
    Fetches upcoming services from ChurchTools (ChurchTools API line).
    Returns:
      id, appointment_id, type (title), subtitle, name, date, has_subtitle, source='churchtools'.
    Query params:
      ?filter_subtitle=1: Return only services with a subtitle.
      ?all=1 / ?force=1: Bypass seen filter.
      ?days=60: Number of days ahead (default 60).
    """
    if not check_auth():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    try:
        force_all = request.args.get("all") == "1" or request.args.get("force") == "1"
        filter_sub = request.args.get("filter_subtitle") == "1"
        days_ahead = int(request.args.get("days", 60))

        now = datetime.now(timezone.utc)
        from_date = now.strftime("%Y-%m-%d")
        to_date = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

        events = ct_client.get_events(from_date=from_date, to_date=to_date)
        result = []
        apt_cache = {}

        for ev in events:
            ev_id = ev.get("id")
            app_id = ev.get("appointmentId")
            cal_id = ev.get("calendar", {}).get("domainIdentifier") or 2
            if not app_id:
                continue

            if app_id not in apt_cache:
                apt = ct_client.get_appointment(int(cal_id), int(app_id))
                apt_cache[app_id] = apt or {}
            apt = apt_cache[app_id]

            subtitle = apt.get("subtitle") or ""
            subtitle = str(subtitle).strip() if subtitle else ""
            has_sub = bool(subtitle)

            if filter_sub and not has_sub:
                continue

            event_type = apt.get("title") or ev.get("name") or "Gottesdienst"
            start_date = ev.get("startDate")

            seen_key = f"ct:{ev_id}:{subtitle}"
            legacy_key = f"ct:{ev_id}:{subtitle}:{start_date}"
            if not force_all and (state.is_seen("ct_new_services", seen_key) or state.is_seen("ct_new_services", legacy_key)):
                continue

            # Check if this service was seen before with an empty or different subtitle
            was_seen_empty = state.is_seen("ct_new_services", f"ct:{ev_id}:") or state.is_seen("ct_new_services", f"ct:{ev_id}::{start_date}")

            item = {
                "id": ev_id,
                "appointment_id": app_id,
                "type": event_type,
                "title": event_type,
                "subtitle": subtitle,
                "name": subtitle,
                "has_subtitle": has_sub,
                "is_subtitle_update": bool(has_sub and was_seen_empty),
                "date": [start_date],
                "source": "churchtools"
            }
            result.append(item)

            if not force_all:
                state.mark_seen("ct_new_services", seen_key)
                state.mark_seen("ct_new_services", legacy_key)

        return jsonify({
            "success": True,
            "source": "churchtools",
            "count": len(result),
            "services": result
        })
    except Exception as e:
        logging.error(f"Error fetching ChurchTools services: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/worshiptools/services", methods=["GET"])
@app.route("/api/worshiptools/new-services", methods=["GET"])
def get_worshiptools_services():
    """
    Fetches upcoming single services from WorshipTools (WorshipTools API line).
    Returns:
      id, type, subtitle, name, date, has_subtitle, source='worshiptools'.
    Query params:
      ?filter_subtitle=1: Return only services with a subtitle.
      ?all=1 / ?force=1: Bypass seen filter.
    """
    if not check_auth():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    try:
        force_all = request.args.get("all") == "1" or request.args.get("force") == "1"
        filter_sub = request.args.get("filter_subtitle") == "1"

        now = datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")

        type_names = get_wt_service_type_names()
        resp = wt_api.get("service", params={"past": "hidden", "today": today_str, "rows": 100})
        docs = resp.get("response", {}).get("docs", []) if isinstance(resp, dict) else []

        result = []
        for s in docs:
            wt_id = str(s.get("id"))
            times = s.get("times") or []
            if not times or len(times) > 3:
                # Skip invalid or multi-date service containers
                continue

            type_id = str(s.get("type", ""))
            type_name = type_names.get(type_id, type_id or "Gottesdienst")
            name = (s.get("name") or "").strip()
            has_sub = bool(name)

            if filter_sub and not has_sub:
                continue

            start_date = times[0]
            seen_key = f"wt:{wt_id}:{name}"
            legacy_key = f"wt:{wt_id}:{name}:{start_date}"
            if not force_all and (state.is_seen("wt_new_services", seen_key) or state.is_seen("wt_new_services", legacy_key)):
                continue

            was_seen_empty = state.is_seen("wt_new_services", f"wt:{wt_id}:") or state.is_seen("wt_new_services", f"wt:{wt_id}::{start_date}")

            item = {
                "id": wt_id,
                "type": type_name,
                "title": type_name,
                "subtitle": name,
                "name": name,
                "has_subtitle": has_sub,
                "is_subtitle_update": bool(has_sub and was_seen_empty),
                "date": times,
                "source": "worshiptools"
            }
            result.append(item)

            if not force_all:
                state.mark_seen("wt_new_services", seen_key)
                state.mark_seen("wt_new_services", legacy_key)

        return jsonify({
            "success": True,
            "source": "worshiptools",
            "count": len(result),
            "services": result
        })
    except Exception as e:
        logging.error(f"Error fetching WorshipTools services: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/new-services", methods=["GET"])
def get_new_services():
    """
    Combined services endpoint for n8n.
    Pass ?source=worshiptools to use WorshipTools line,
    default uses ChurchTools line.
    """
    source = request.args.get("source", "churchtools").lower()
    if source == "worshiptools":
        return get_worshiptools_services()
    return get_churchtools_services()

def _background_loop():
    while True:
        try:
            config = load_config()
            interval = int(config.get("sync", {}).get("interval_seconds", 900))
            run_full_sync(dry_run=False)
        except Exception as e:
            logging.error(f"Background sync error: {e}", exc_info=True)
            interval = 60
        time.sleep(interval)
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    # Start background sync thread only when running directly
    threading.Thread(target=_background_loop, daemon=True).start()

    port = int(os.environ.get("PORT", 5000))

    try:
        from waitress import serve
        logging.info(f"Starting production server on port {port}...")
        serve(app, host='0.0.0.0', port=port)
    except ImportError:
        logging.warning("waitress not installed, falling back to Flask dev server")
        app.run(host='0.0.0.0', port=port)
    except KeyboardInterrupt:
        logging.info("Server stopped by user")
    except Exception:
        logging.critical("Server error", exc_info=True)
