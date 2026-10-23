import logging
import os
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from cache import Cacher, YamlDatabase
from worshiptools_api import Worshiptools_API

load_dotenv()

app = Flask(__name__)

# Initialize global objects
cacher = Cacher(YamlDatabase("db.yaml"))

wt_api = Worshiptools_API(
    os.environ.get("WORSHIPTOOLS_EMAIL"),
    os.environ.get("WORSHIPTOOLS_PASSWORD"),
    os.environ.get("WORSHIPTOOLS_ACCOUNT_ID"),
)

_service_type_names = {}


def get_service_type_names(force_refresh=False):
    """Return a {type_id: name} dict, fetched once and cached in memory."""
    global _service_type_names
    if _service_type_names and not force_refresh:
        return _service_type_names

    response = wt_api.get("meta/service-types")
    if isinstance(response, dict):
        response = response.get("data") or response.get("docs") or []

    _service_type_names = {t["id"]: t["name"] for t in response if "id" in t}
    return _service_type_names


def extract_services(response):
    """Handle the different response structures of the WorshipTools API."""
    if isinstance(response, dict):
        services = response.get("docs") or response.get("data") or response.get("services") or []
        if not services and "response" in response:
            services = response["response"].get("docs", [])
        return services
    if isinstance(response, list):
        return response
    return []


@app.route('/api/new-services', methods=['GET'])
def get_new_services():
    try:
        today = datetime.now().strftime('%Y-%m-%d')

        params = {
            'past': 'hidden',
            'today': today,
            'sort': 'times_ss asc',
            'rows': 50,
            'start': 0
        }

        response = wt_api.get("service", params=params)
        wt_services = extract_services(response)

        # Resolve type IDs to names; refresh once if an unknown type shows up
        type_names = get_service_type_names()
        if any(s.get('type') not in type_names for s in wt_services):
            type_names = get_service_type_names(force_refresh=True)

        # Find new services (no type filter)
        seen_service_ids = set(cacher.get("seen_services") or [])
        new_services = [s for s in wt_services if s['id'] not in seen_service_ids]

        result = [
            {
                "type": type_names.get(s.get('type'), s.get('type')),  # falls back to the ID if unknown
                "name": s.get('name'),
                "date": s.get('times')
            }
            for s in new_services
        ]

        # Update seen services
        if new_services:
            cacher.set("seen_services", [s['id'] for s in wt_services])

        return jsonify({
            "success": True,
            "count": len(result),
            "services": result
        })

    except Exception as e:
        logging.error(f"Error fetching new services: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/api/debug-services', methods=['GET'])
def debug_services():
    """Debug endpoint to see raw API response structure"""
    try:
        today = datetime.now().strftime('%Y-%m-%d')

        params = {
            'past': 'hidden',
            'today': today,
            'sort': 'times_ss asc',
            'rows': 10,
            'start': 0
        }

        response = wt_api.get("service", params=params)
        return jsonify({
            "success": True,
            "raw_response": response,
            "response_type": str(type(response)),
            "keys": list(response.keys()) if isinstance(response, dict) else None
        })
    except Exception as e:
        logging.error(f"Debug error: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500




if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

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
