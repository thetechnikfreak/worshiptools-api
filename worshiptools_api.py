import os
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse
import requests


class Worshiptools_API:
    def __init__(self, email, password, account_id):
        self.email = email
        self.password = password
        self.account_id = account_id
        self.base_url = "https://api.worship.tools/v1"
        self.auth_url = "https://auth.worshiptools.com"
        self.site_url = "https://www.worshiptools.com"
        self.firestore_base_url = (
            "https://firestore.googleapis.com/v1/projects/worship-extreme-datastore/"
            "databases/(default)/documents"
        )
        self.refresh_token = None
        self.firebase_token = None
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        })

        self.token = os.environ.get("WORSHIPTOOLS_TOKEN") or None
        if not self.token:
            self.token = self._authenticate()

        if not self.token:
            raise ValueError("Failed to authenticate. Check your email/password.")

    def _authenticate(self):
        """Authenticate via OAuth2 Authorization Code flow."""
        try:
            self.session.cookies.clear()

            # Step 1: Start OAuth from www.worshiptools.com to get proper server-side state
            login_page_resp = self.session.get(f"{self.site_url}/auth/login", allow_redirects=False)

            if login_page_resp.status_code not in (301, 302, 303, 307, 308):
                print(f"Failed to initiate OAuth flow: status {login_page_resp.status_code}")
                return None

            auth_redirect = login_page_resp.headers.get('Location', '')

            # Step 2: Follow redirect to auth.worshiptools.com
            auth_resp = self.session.get(auth_redirect, allow_redirects=True)

            # Step 3: POST login credentials
            login_resp = self.session.post(
                f"{self.auth_url}/login",
                data={
                    'email': self.email,
                    'password': self.password
                },
                headers={
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Origin': 'https://auth.worshiptools.com',
                    'Referer': auth_resp.url,
                },
                allow_redirects=False
            )

            # Step 4: Follow redirects to get the authorization code
            max_redirects = 10
            code = None
            callback_url = None

            while max_redirects > 0 and login_resp.status_code in (301, 302, 303, 307, 308):
                max_redirects -= 1
                location = login_resp.headers.get('Location', '')

                if location.startswith('/'):
                    parsed = urlparse(login_resp.url)
                    location = f"{parsed.scheme}://{parsed.netloc}{location}"

                if 'code=' in location:
                    parsed_url = urlparse(location)
                    query_params = parse_qs(parsed_url.query)
                    code = query_params.get('code', [None])[0]
                    callback_url = location
                    break

                login_resp = self.session.get(location, allow_redirects=False)

            if not code:
                print("Failed to get authorization code")
                return None

            # Step 5: Call the callback URL with full session to exchange code for token
            callback_resp = self.session.get(callback_url, allow_redirects=True)

            token = self.session.cookies.get('weAuthToken')
            if token:
                self.refresh_token = self.session.cookies.get('weAuthRefresh')
                print(f"Authentication successful, token length: {len(token)}")
                return token

            jwt_pattern = r'eyJ[a-zA-Z0-9_-]+\.eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+'
            jwt_matches = re.findall(jwt_pattern, callback_resp.text)
            if jwt_matches:
                print(f"Authentication successful (from body), token length: {len(jwt_matches[0])}")
                return jwt_matches[0]

            print("Failed to extract token from callback response")
            return None

        except Exception as e:
            print(f"Authentication error: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _request(self, method, endpoint, **kwargs):
        """Make a request to the account-scoped API (/v1/account/{account_id}/...)"""
        url = f"{self.base_url}/account/{self.account_id}/{endpoint}"

        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token}"
        headers["Accept"] = "application/json"

        response = self.session.request(method, url, headers=headers, **kwargs)

        if response.status_code == 401:
            print("Token expired, re-authenticating...")
            self.token = self._authenticate()
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
                response = self.session.request(method, url, headers=headers, **kwargs)
            else:
                raise ValueError("Re-authentication failed")

        response.raise_for_status()
        return response.json()

    def _request_root(self, method, endpoint, **kwargs):
        """Make a request to root /v1/ endpoints (without /account/{account_id}/)"""
        url = f"{self.base_url}/{endpoint}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token}"
        headers["Accept"] = "application/json"

        response = self.session.request(method, url, headers=headers, **kwargs)
        if response.status_code == 401:
            self.token = self._authenticate()
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
                response = self.session.request(method, url, headers=headers, **kwargs)
            else:
                raise ValueError("Re-authentication failed")

        response.raise_for_status()
        return response.json()

    def get(self, endpoint, params=None):
        return self._request("GET", endpoint, params=params)

    def post(self, endpoint, files=None, data=None, json_data=None):
        if files:
            return self._request("POST", endpoint, files=files, data=data)
        elif json_data:
            return self._request("POST", endpoint, json=json_data, headers={"Content-Type": "application/json"})
        else:
            return self._request("POST", endpoint, data=data)

    def get_firebase_id_token(self, force_refresh=False):
        """
        1. Fetch custom Firebase token from WorshipTools (/v1/firebase/token)
        2. Exchange it at Google Identity Toolkit for a Firestore Bearer idToken
        """
        if self.firebase_token and not force_refresh:
            return self.firebase_token

        fb_config = self._request_root("GET", "config/firebase")
        api_key = (
            fb_config.get("apiKey")
            if isinstance(fb_config, dict) and fb_config.get("apiKey")
            else "AIzaSyBUIc_jmMk50WUlix6SlAlByAFBshPGiho"
        )

        token_resp = self._request_root("GET", "firebase/token")
        custom_token = (
            token_resp.get("token") or token_resp.get("customToken") or token_resp.get("data")
            if isinstance(token_resp, dict) else token_resp
        )

        google_url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={api_key}"
        resp = requests.post(
            google_url,
            json={"token": custom_token, "returnSecureToken": True}
        )
        resp.raise_for_status()
        self.firebase_token = resp.json()["idToken"]
        return self.firebase_token

    def get_firestore_document(self, collection_path):
        """Read any document or collection from WorshipTools Firestore"""
        fb_token = self.get_firebase_id_token()
        url = f"{self.firestore_base_url}/accounts/{self.account_id}/{collection_path}"
        resp = requests.get(url, headers={"Authorization": f"Bearer {fb_token}"})
        if resp.status_code == 401:
            fb_token = self.get_firebase_id_token(force_refresh=True)
            resp = requests.get(url, headers={"Authorization": f"Bearer {fb_token}"})
        resp.raise_for_status()
        return resp.json()
    @staticmethod
    def _to_firestore_value(val):
        """Convert a Python value to Firestore REST API value format."""
        if val is None:
            return {"nullValue": "NULL_VALUE"}
        if isinstance(val, bool):
            return {"booleanValue": val}
        if isinstance(val, int):
            return {"integerValue": str(val)}
        if isinstance(val, float):
            return {"doubleValue": val}
        if isinstance(val, str):
            return {"stringValue": val}
        if isinstance(val, list):
            return {"arrayValue": {"values": [Worshiptools_API._to_firestore_value(v) for v in val]}}
        if isinstance(val, dict):
            return {"mapValue": {"fields": {k: Worshiptools_API._to_firestore_value(v) for k, v in val.items()}}}
        return {"stringValue": str(val)}

    @staticmethod
    def _normalize_time(t):
        """The app stores local time without timezone: 2026-10-24T08:00:00"""
        if isinstance(t, dict):
            t = t.get("time", "")
        t = str(t).strip()
        if len(t) == 10:            # date only
            t += "T00:00:00"
        return t.rstrip("Z")[:19]

    def create_service(self, service_type_id, times, name=None, extra_fields=None, notes=""):
        """Create a service the same way the web app does (cuelists collection)."""
        service_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        now_ts = now.strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"

        if isinstance(times, str):
            times = [times]
        time_entries = [{"time": self._normalize_time(t), "type": None} for t in times]

        fb_token = self.get_firebase_id_token()

        user_id = getattr(self, "firebase_uid", None)
        user_name = os.environ.get("WORSHIPTOOLS_USER_NAME", "API")

        fields = {
            "client": self._to_firestore_value({"id": None, "name": "Planning"}),
            "data": self._to_firestore_value({
                "cuelist_created": int(now.timestamp()),
                "cuelist_options": {"date": ""},
                "cuelist_status": 0,
                "cuelist_tags": [],
                "cuelist_title": "",
                "cues": [],
                "songMeta": {},
            }),
            "mod": {"timestampValue": now_ts},
            "post": {"timestampValue": now_ts},
            "name": self._to_firestore_value(name or ""),
            "service": self._to_firestore_value({
                "notes": notes,
                "times": time_entries,
                "type": service_type_id,
            }),
            "user": self._to_firestore_value({"id": user_id, "name": user_name}),
        }

        if extra_fields:
            for k, v in extra_fields.items():
                fields[k] = self._to_firestore_value(v)

        url = f"{self.firestore_base_url}/accounts/{self.account_id}/cuelists?documentId={service_id}"
        headers = {"Authorization": f"Bearer {fb_token}", "Content-Type": "application/json"}
        resp = requests.post(url, headers=headers, json={"fields": fields})
        if resp.status_code == 401:
            headers["Authorization"] = f"Bearer {self.get_firebase_id_token(force_refresh=True)}"
            resp = requests.post(url, headers=headers, json={"fields": fields})
        resp.raise_for_status()

        return {
            "id": service_id,
            "type": service_type_id,
            "name": name,
            "times": [e["time"] for e in time_entries],
            "firestore_response": resp.json(),
        }
