import argparse
import logging
import os
import sys

import yaml
from dotenv import load_dotenv
from cache import Cacher, YamlDatabase
from custom_types import Config
from worshiptools_api import Worshiptools_API
import io
from flask import Flask, jsonify, request

log_stream = io.StringIO()

load_dotenv()

app = Flask(__name__)

# Initialize global objects
cacher = Cacher(YamlDatabase("db.yaml"))

wt_api = Worshiptools_API(
    os.environ.get("WORSHIPTOOLS_EMAIL"),
    os.environ.get("WORSHIPTOOLS_PASSWORD"),
    os.environ.get("WORSHIPTOOLS_ACCOUNT_ID"),
)


@app.route('/api/new-services', methods=['GET'])
def get_new_services():
    try:
        from datetime import datetime
        today = datetime.now().strftime('%Y-%m-%d')
        
        params = {
            'past': 'hidden',
            'today': today,
            'sort': 'times_ss asc',
            'rows': 50,
            'start': 0
        }
        
        response = wt_api.get("service", params=params)
        
        # Handle different response structures
        if isinstance(response, dict):
            wt_services = response.get("docs") or response.get("data") or response.get("services") or []
            if not wt_services and "response" in response:
                wt_services = response["response"].get("docs", [])
        elif isinstance(response, list):
            wt_services = response
        else:
            wt_services = []
        
        filtered_services = [service for service in wt_services if service.get('type') == 'a7c123fb-bfa6-4824-8722-ade0ee562c2e']
        
        # Get previously seen service IDs
        seen_service_ids = set(cacher.get("seen_services") or [])
        
        # Find new services
        new_services = [service for service in filtered_services if service['id'] not in seen_service_ids]
        
        # Format response
        result = []
        for service in new_services:
            result.append({
                "type": service.get('type'),
                "name": service.get('name'),
                "date": service.get('times')
            })
        
        # Update seen services
        if new_services:
            all_service_ids = [service['id'] for service in filtered_services]
            cacher.set("seen_services", all_service_ids)
        
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
        from datetime import datetime
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


@app.route('/api/upload-files', methods=['POST'])
def upload_files():
    try:
        # Check if files are in the request
        if 'file' not in request.files and 'files[]' not in request.files:
            return jsonify({
                "success": False,
                "error": "No files provided"
            }), 400
        
        # Prepare form data for WorshipTools API
        files_to_upload = []
        form_data = {}
        
        # Handle single file upload
        if 'file' in request.files:
            file = request.files['file']
            files_to_upload.append(('file', (file.filename, file.stream, file.content_type)))
            
            # Add optional parameters
            if request.form.get('stream'):
                form_data['stream'] = request.form.get('stream')
            if request.form.get('description'):
                form_data['description'] = request.form.get('description')
            if request.form.get('category1'):
                form_data['category1'] = request.form.get('category1')
            if request.form.get('category2'):
                form_data['category2'] = request.form.get('category2')
        
        # Handle multiple files upload
        elif 'files[]' in request.files:
            files = request.files.getlist('files[]')
            for idx, file in enumerate(files):
                files_to_upload.append(('files[]', (file.filename, file.stream, file.content_type)))
            
            # Add optional array parameters
            if request.form.getlist('stream[]'):
                form_data['stream[]'] = request.form.getlist('stream[]')
            if request.form.getlist('description[]'):
                form_data['description[]'] = request.form.getlist('description[]')
            if request.form.getlist('category1[]'):
                form_data['category1[]'] = request.form.getlist('category1[]')
            if request.form.getlist('category2[]'):
                form_data['category2[]'] = request.form.getlist('category2[]')
        
        # Upload to WorshipTools API
        response = wt_api.post("files", files=files_to_upload, data=form_data)
        
        return jsonify({
            "success": True,
            "response": response
        })
    
    except Exception as e:
        logging.error(f"Error uploading files: {e}", exc_info=True)
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
    except KeyboardInterrupt:
        logging.info("Server stopped by user")
    except Exception as e:
        logging.critical("Server error", exc_info=True)