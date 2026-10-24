import os
import re
import uuid
from typing import Dict, Any, Optional, List
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

        if response.status_code == 204:
            return None
        response.raise_for_status()
        if not response.content:
            return None
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

        if response.status_code == 204:
            return None
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()

    def get(self, endpoint, params=None):
        return self._request("GET", endpoint, params=params)

    def post(self, endpoint, files=None, data=None, json=None, json_data=None):
        payload = json if json is not None else json_data
        if files:
            return self._request("POST", endpoint, files=files, data=data)
        elif payload is not None:
            return self._request("POST", endpoint, json=payload, headers={"Content-Type": "application/json"})
        elif data:
            return self._request("POST", endpoint, data=data)
        return self._request("POST", endpoint)

    def delete(self, endpoint):
        return self._request("DELETE", endpoint)

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

    def _build_firestore_url(self, collection_path):
        path = collection_path.strip("/")
        if not path.startswith("accounts/"):
            path = f"accounts/{self.account_id}/{path}"
        return f"{self.firestore_base_url}/{path}"

    def get_firestore_document(self, collection_path):
        """Read any document or collection from WorshipTools Firestore"""
        fb_token = self.get_firebase_id_token()
        url = self._build_firestore_url(collection_path)
        resp = requests.get(url, headers={"Authorization": f"Bearer {fb_token}"})
        if resp.status_code == 401:
            fb_token = self.get_firebase_id_token(force_refresh=True)
            resp = requests.get(url, headers={"Authorization": f"Bearer {fb_token}"})
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _from_firestore_value(val):
        """Convert a Firestore REST API value dictionary back to Python native value."""
        if not isinstance(val, dict):
            return val
        if "nullValue" in val:
            return None
        if "booleanValue" in val:
            return val["booleanValue"]
        if "integerValue" in val:
            return int(val["integerValue"])
        if "doubleValue" in val:
            return float(val["doubleValue"])
        if "stringValue" in val:
            return val["stringValue"]
        if "timestampValue" in val:
            return val["timestampValue"]
        if "arrayValue" in val:
            values = val["arrayValue"].get("values", [])
            return [Worshiptools_API._from_firestore_value(v) for v in values]
        if "mapValue" in val:
            fields = val["mapValue"].get("fields", {})
            return {k: Worshiptools_API._from_firestore_value(v) for k, v in fields.items()}
        return val

    def firestore_patch(self, collection_path, fields, update_mask_fields=None):
        """Update a document in Firestore (uses full document replace to comply with Firestore security rules)."""
        fb_token = self.get_firebase_id_token()
        url = self._build_firestore_url(collection_path)
        headers = {"Authorization": f"Bearer {fb_token}", "Content-Type": "application/json"}
        
        # Merge with existing document fields
        existing = self.get_firestore_document(collection_path)
        existing_fields = existing.get("fields", {})
        for k, v in fields.items():
            existing_fields[k] = self._to_firestore_value(v)
            
        payload = {"fields": existing_fields}
        resp = requests.patch(url, headers=headers, json=payload)
        if resp.status_code == 401:
            headers["Authorization"] = f"Bearer {self.get_firebase_id_token(force_refresh=True)}"
            resp = requests.patch(url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()

    def firestore_delete(self, collection_path):
        """Delete a document in Firestore."""
        fb_token = self.get_firebase_id_token()
        url = self._build_firestore_url(collection_path)
        headers = {"Authorization": f"Bearer {fb_token}"}
        resp = requests.delete(url, headers=headers)
        if resp.status_code == 401:
            headers["Authorization"] = f"Bearer {self.get_firebase_id_token(force_refresh=True)}"
            resp = requests.delete(url, headers=headers)
        if resp.status_code == 404:
            return True
        resp.raise_for_status()
        return True

    def update_service(self, service_id, name=None, times=None, service_type_id=None, notes=None):
        """Updates service name, times, type, or notes in Firestore preserving raw document format."""
        fb_token = self.get_firebase_id_token()
        url = f"{self.firestore_base_url}/accounts/{self.account_id}/cuelists/{service_id}"
        headers = {"Authorization": f"Bearer {fb_token}", "Content-Type": "application/json"}
        
        # 1. Fetch raw existing doc
        resp = requests.get(url, headers=headers)
        if resp.status_code == 401:
            fb_token = self.get_firebase_id_token(force_refresh=True)
            headers["Authorization"] = f"Bearer {fb_token}"
            resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        doc = resp.json()
        fields = doc.setdefault("fields", {})

        # 2. Update mod timestamp
        now = datetime.now(timezone.utc)
        now_ts = now.strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"
        fields["mod"] = {"timestampValue": now_ts}

        # 3. Update top-level fields
        if name is not None:
            fields["name"] = self._to_firestore_value(name)

        # 4. Update service map
        if "service" in fields and "mapValue" in fields["service"]:
            srv_fields = fields["service"]["mapValue"].setdefault("fields", {})
        else:
            srv_fields = {}
            fields["service"] = {"mapValue": {"fields": srv_fields}}

        if times is not None:
            if isinstance(times, str):
                times = [times]
            srv_fields["times"] = self._to_firestore_value([{"time": self._normalize_time(t), "type": None} for t in times])

        if service_type_id is not None:
            srv_fields["type"] = self._to_firestore_value(service_type_id)

        if notes is not None:
            srv_fields["notes"] = self._to_firestore_value(notes)

        # 5. Patch back full document
        patch_resp = requests.patch(url, headers=headers, json=doc)
        if patch_resp.status_code == 401:
            headers["Authorization"] = f"Bearer {self.get_firebase_id_token(force_refresh=True)}"
            patch_resp = requests.patch(url, headers=headers, json=doc)
        patch_resp.raise_for_status()
        return patch_resp.json()

    def delete_service(self, service_id):
        """Deletes a service from cuelists collection."""
        return self.firestore_delete(f"cuelists/{service_id}")

    def upload_media_image(self, name: str, image_bytes: bytes) -> Dict[str, Any]:
        """
        Uploads an image to WorshipTools Presenter media library:
        1. Generates thumbnail PNG (320x180) and saves original full image.
        2. Uploads both directly to Firebase Storage bucket 'we-data' under accounts/{account_id}/
        3. Registers document in Firestore 'accounts/{account_id}/media/{media_id}'
        This makes it instantly visible inside the Presenter Desktop App Media library.
        """
        import io, urllib.parse
        from PIL import Image

        media_id = str(uuid.uuid4())
        cloud_key = f"{media_id}.jpg"
        thumb_key = f"{media_id}_thumb_s.png"

        # Read dimensions & generate thumbnail
        im = Image.open(io.BytesIO(image_bytes))
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGB")
        width, height = im.size

        im_thumb = im.copy()
        im_thumb.thumbnail((320, 180))
        thumb_buf = io.BytesIO()
        im_thumb.save(thumb_buf, format="PNG")
        thumb_bytes = thumb_buf.getvalue()

        fb_token = self.get_firebase_id_token()
        headers = {"Authorization": f"Firebase {fb_token}"}

        # 1. Full image to we-data
        p1 = f"accounts/{self.account_id}/{cloud_key}"
        url1 = f"https://firebasestorage.googleapis.com/v0/b/we-data/o?name={urllib.parse.quote(p1, safe='')}"
        r1 = requests.post(url1, headers={**headers, "Content-Type": "image/jpeg"}, data=image_bytes)
        if r1.status_code == 401:
            fb_token = self.get_firebase_id_token(force_refresh=True)
            headers = {"Authorization": f"Firebase {fb_token}"}
            r1 = requests.post(url1, headers={**headers, "Content-Type": "image/jpeg"}, data=image_bytes)
        r1.raise_for_status()

        # 2. Thumbnail to we-data
        p2 = f"accounts/{self.account_id}/{thumb_key}"
        url2 = f"https://firebasestorage.googleapis.com/v0/b/we-data/o?name={urllib.parse.quote(p2, safe='')}"
        r2 = requests.post(url2, headers={**headers, "Content-Type": "image/png"}, data=thumb_bytes)
        r2.raise_for_status()

        # 3. Register document in Firestore accounts/{account_id}/media
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"
        fields = {
            "name": {"stringValue": name},
            "mod": {"timestampValue": now},
            "post": {"timestampValue": now},
            "client": {
                "mapValue": {
                    "fields": {
                        "id": {"stringValue": "1042a211-c45c-4ff3-a2e7-58a3f5fc8508"},
                        "name": {"stringValue": "Presenter 2026.1.8"}
                    }
                }
            },
            "user": {
                "mapValue": {
                    "fields": {
                        "id": {"stringValue": "8b157fb1-05e0-4110-81ba-5d0327b84a13"},
                        "name": {"stringValue": "Paul"}
                    }
                }
            },
            "data": {
                "mapValue": {
                    "fields": {
                        "cloudKey": {"stringValue": cloud_key},
                        "duration": {"integerValue": "0"},
                        "width": {"integerValue": str(width)},
                        "height": {"integerValue": str(height)},
                        "size": {"integerValue": str(len(image_bytes))},
                        "type": {"stringValue": "i"},
                        "store": {"booleanValue": False},
                        "tags": {"arrayValue": {}}
                    }
                }
            }
        }
        fs_url = f"{self.firestore_base_url}/accounts/{self.account_id}/media?documentId={media_id}"
        fs_r = requests.post(fs_url, headers={"Authorization": f"Bearer {fb_token}", "Content-Type": "application/json"}, json={"fields": fields})
        fs_r.raise_for_status()

        return {
            "id": media_id,
            "name": name,
            "cloudKey": cloud_key,
            "thumbKey": thumb_key,
            "width": width,
            "height": height
        }

    def upload_media_presentation(self, name: str, pptx_bytes: bytes, media_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Uploads a PowerPoint presentation (.pptx) or PDF presentation (.pdf) to WorshipTools Presenter:
        1. If PDF: Uses pdftoppm directly for 100% pixel-perfect vector and font rendering (Canva PDF).
        2. If PPTX: Uses PowerPoint COM (Windows) or LibreOffice headless + pdftoppm (Linux).
        3. Uploads presentation file and slide images to Firebase Storage under accounts/{account_id}/slides/{presentation_id}/
        4. Registers in Firestore 'accounts/{account_id}/slides/{presentation_id}'.
        """
        import io, zipfile, tempfile, shutil, urllib.parse, subprocess, glob, logging
        from PIL import Image

        presentation_id = media_id or str(uuid.uuid4())
        slide_images = []  # list of (bytes, width, height)
        is_pdf = name.lower().endswith(".pdf") or pptx_bytes.startswith(b"%PDF-")

        # 1. Native PDF rendering (Pixel-perfect Canva rendering without font substitution)
        if is_pdf:
            pdftoppm = shutil.which("pdftoppm")
            if pdftoppm:
                temp_dir = tempfile.mkdtemp()
                try:
                    temp_pdf = os.path.join(temp_dir, "input.pdf")
                    with open(temp_pdf, "wb") as f:
                        f.write(pptx_bytes)

                    slide_prefix = os.path.join(temp_dir, "slide")
                    cmd_img = [pdftoppm, "-jpeg", "-r", "150", temp_pdf, slide_prefix]
                    subprocess.run(cmd_img, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)

                    img_files = sorted(
                        glob.glob(os.path.join(temp_dir, "slide-*.jpg")),
                        key=lambda x: int("".join(c for c in os.path.basename(x) if c.isdigit()) or "0")
                    )
                    for f in img_files:
                        with open(f, "rb") as fp:
                            b = fp.read()
                        with Image.open(io.BytesIO(b)) as im:
                            w, h = im.size
                        slide_images.append((b, w, h))
                except Exception as e:
                    logging.warning(f"pdftoppm PDF export error: {e}")
                finally:
                    try: shutil.rmtree(temp_dir)
                    except: pass

        # 2. Try PowerPoint COM on Windows (for PPTX)
        if not slide_images and os.name == "nt" and not is_pdf:
            try:
                import win32com.client
                temp_pptx = tempfile.NamedTemporaryFile(suffix=".pptx", delete=False)
                temp_pptx.write(pptx_bytes)
                temp_pptx.close()
                temp_dir = tempfile.mkdtemp()
                ppt = win32com.client.Dispatch("PowerPoint.Application")
                pres = ppt.Presentations.Open(os.path.abspath(temp_pptx.name), 1, 0, 0)
                export_base = os.path.join(temp_dir, "slide.jpg")
                pres.SaveAs(export_base, 17)
                pres.Close()
                ppt.Quit()

                slide_folder = os.path.join(temp_dir, "slide")
                if not os.path.exists(slide_folder) and os.path.exists(export_base):
                    slide_folder = export_base

                files = sorted(os.listdir(slide_folder), key=lambda x: int("".join(c for c in x if c.isdigit()) or "0"))
                for f in files:
                    with open(os.path.join(slide_folder, f), "rb") as fp:
                        b = fp.read()
                    with Image.open(io.BytesIO(b)) as im:
                        w, h = im.size
                    slide_images.append((b, w, h))

                try: os.unlink(temp_pptx.name)
                except: pass
                try: shutil.rmtree(temp_dir)
                except: pass
            except Exception:
                pass

        # 3. Try LibreOffice headless + pdftoppm (Linux / Docker / macOS / Windows with LibreOffice)
        if not slide_images:
            soffice = shutil.which("soffice") or shutil.which("libreoffice")
            pdftoppm = shutil.which("pdftoppm")
            if soffice and pdftoppm:
                try:
                    temp_dir = tempfile.mkdtemp()
                    temp_in = os.path.join(temp_dir, "input.pdf" if is_pdf else "input.pptx")
                    with open(temp_in, "wb") as f:
                        f.write(pptx_bytes)

                    temp_pdf = temp_in if is_pdf else os.path.join(temp_dir, "input.pdf")
                    if not is_pdf:
                        cmd_pdf = [soffice, "--headless", "--convert-to", "pdf", "--outdir", temp_dir, temp_in]
                        subprocess.run(cmd_pdf, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)

                    if os.path.exists(temp_pdf):
                        slide_prefix = os.path.join(temp_dir, "slide")
                        cmd_img = [pdftoppm, "-jpeg", "-r", "150", temp_pdf, slide_prefix]
                        subprocess.run(cmd_img, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)

                        img_files = sorted(
                            glob.glob(os.path.join(temp_dir, "slide-*.jpg")),
                            key=lambda x: int("".join(c for c in os.path.basename(x) if c.isdigit()) or "0")
                        )
                        for f in img_files:
                            with open(f, "rb") as fp:
                                b = fp.read()
                            with Image.open(io.BytesIO(b)) as im:
                                w, h = im.size
                            slide_images.append((b, w, h))
                except Exception as e:
                    logging.warning(f"Slide export error: {e}")
                finally:
                    try: shutil.rmtree(temp_dir)
                    except: pass

        # 3. Fallback: extract embedded slide images or render slides with Pillow
        if not slide_images:
            slide_count = 1
            try:
                with zipfile.ZipFile(io.BytesIO(pptx_bytes), "r") as z:
                    slides = [f for f in z.namelist() if f.startswith("ppt/slides/slide") and f.endswith(".xml")]
                    if slides:
                        slide_count = len(slides)
            except Exception:
                pass
            for _ in range(slide_count):
                buf = io.BytesIO()
                im = Image.new("RGB", (1920, 1080), color=(20, 20, 20))
                im.save(buf, format="JPEG", quality=85)
                slide_images.append((buf.getvalue(), 1920, 1080))

        fb_token = self.get_firebase_id_token()
        headers = {"Authorization": f"Firebase {fb_token}"}
        encoded_filename = urllib.parse.quote(name, safe="")

        # 3. Upload presentation file (.pptx or .pdf) to Firebase Storage
        file_mime = "application/pdf" if is_pdf else "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        pptx_storage_url = f"https://firebasestorage.googleapis.com/v0/b/we-data/o?name=accounts/{self.account_id}/slides/{presentation_id}/{encoded_filename}"
        r_ppt = requests.post(
            pptx_storage_url,
            headers={**headers, "Content-Type": file_mime},
            data=pptx_bytes
        )
        if r_ppt.status_code == 401:
            fb_token = self.get_firebase_id_token(force_refresh=True)
            headers = {"Authorization": f"Firebase {fb_token}"}
            r_ppt = requests.post(
                pptx_storage_url,
                headers={**headers, "Content-Type": file_mime},
                data=pptx_bytes
            )
        r_ppt.raise_for_status()

        # 4. Upload each slide image and thumbnail
        slide_values = []
        max_w, max_h = 1920, 1080

        for img_bytes, w, h in slide_images:
            slide_id = str(uuid.uuid4())
            max_w, max_h = w, h

            # Thumbnail
            ratio = h / w if w else 9/16
            thumb_w = 600
            thumb_h = int(round(thumb_w * ratio))
            with Image.open(io.BytesIO(img_bytes)) as im:
                thumb_im = im.resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
                thumb_buf = io.BytesIO()
                thumb_im.save(thumb_buf, format="JPEG", quality=85)
                thumb_bytes = thumb_buf.getvalue()

            # Upload slide image
            s_url = f"https://firebasestorage.googleapis.com/v0/b/we-data/o?name=accounts/{self.account_id}/slides/{presentation_id}/{slide_id}.jpg"
            r_s = requests.post(s_url, headers={**headers, "Content-Type": "image/jpeg"}, data=img_bytes)
            r_s.raise_for_status()

            # Upload thumbnail
            t_url = f"https://firebasestorage.googleapis.com/v0/b/we-data/o?name=accounts/{self.account_id}/slides/{presentation_id}/{slide_id}_thumbnail.jpg"
            r_t = requests.post(t_url, headers={**headers, "Content-Type": "image/jpeg"}, data=thumb_bytes)
            r_t.raise_for_status()

            layer_id = str(uuid.uuid4())
            slide_values.append({
                "mapValue": {
                    "fields": {
                        "id": {"stringValue": slide_id},
                        "slide_notes": {"stringValue": ""},
                        "blocks": {"arrayValue": {}},
                        "layers": {"arrayValue": {}},
                        "slide_layers": {
                            "arrayValue": {
                                "values": [
                                    {
                                        "mapValue": {
                                            "fields": {
                                                "id": {"stringValue": layer_id},
                                                "type": {"stringValue": "embeddedImage"},
                                                "name": {"stringValue": "Imported Image"},
                                                "media": {
                                                  "mapValue": {
                                                    "fields": {
                                                      "mediaData": {
                                                        "mapValue": {
                                                          "fields": {
                                                            "media_type": {"stringValue": "i"},
                                                            "media_width": {"integerValue": str(w)},
                                                            "media_height": {"integerValue": str(h)},
                                                            "media_url": {"stringValue": f"presenter://accounts/{self.account_id}/slides/{presentation_id}/{slide_id}.jpg"}
                                                          }
                                                        }
                                                      },
                                                      "mediaScale": {"stringValue": "contain"}
                                                    }
                                                  }
                                                },
                                                "x": {"integerValue": "0"},
                                                "y": {"integerValue": "0"},
                                                "width": {"integerValue": str(w)},
                                                "height": {"integerValue": str(h)},
                                                "zIndex": {"integerValue": "0"}
                                            }
                                        }
                                    }
                                ]
                            }
                        }
                    }
                }
            })

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"

        # 5. Register in Firestore accounts/{account_id}/slides/{presentation_id}
        slides_doc_payload = {
            "fields": {
                "name": {"stringValue": name},
                "type": {"stringValue": "slides"},
                "mod": {"timestampValue": now_iso},
                "post": {"timestampValue": now_iso},
                "client": {
                    "mapValue": {
                        "fields": {
                            "name": {"stringValue": "Presenter 2026.1.9"},
                            "id": {"stringValue": "1042a211-c45c-4ff3-a2e7-58a3f5fc8508"}
                        }
                    }
                },
                "user": {
                    "mapValue": {
                        "fields": {
                            "name": {"stringValue": "Paul"},
                            "id": {"stringValue": "8b157fb1-05e0-4110-81ba-5d0327b84a13"}
                        }
                    }
                },
                "data": {
                    "mapValue": {
                        "fields": {
                            "presentationSize": {"stringValue": f"{max_w}x{max_h}"},
                            "external": {
                                "mapValue": {
                                    "fields": {
                                        "id": {"stringValue": encoded_filename},
                                        "source": {"stringValue": "powerpoint"},
                                        "fileURL": {"stringValue": f"presenter://accounts/{self.account_id}/slides/{presentation_id}/{encoded_filename}"}
                                    }
                                }
                            },
                            "slides": {
                                "arrayValue": {
                                    "values": slide_values
                                }
                            }
                        }
                    }
                }
            }
        }
        slides_fs_url = f"{self.firestore_base_url}/accounts/{self.account_id}/slides/{presentation_id}"
        fs_r = requests.patch(
            slides_fs_url,
            headers={"Authorization": f"Bearer {fb_token}", "Content-Type": "application/json"},
            json=slides_doc_payload
        )
        fs_r.raise_for_status()

        # 6. Optional local cache sync
        appdata = os.environ.get("APPDATA")
        if appdata:
            local_dir = os.path.join(appdata, "WorshipTools Library", self.account_id)
            if os.path.exists(local_dir):
                try:
                    with open(os.path.join(local_dir, f"{presentation_id}.pptx"), "wb") as f:
                        f.write(pptx_bytes)
                except Exception:
                    pass

        return {
            "id": presentation_id,
            "name": name,
            "slides": len(slide_images),
            "width": max_w,
            "height": max_h,
            "size": len(pptx_bytes)
        }

    def upload_file(self, filename: str, file_bytes: bytes, destination_path: str = None, content_type: str = "application/octet-stream") -> str:
        """
        Uploads a file to WorshipTools Storage:
        1. Uploads to temporary user bucket: gs://we-upload/{user_id}/{filename}
        2. Calls Cloud Function accountLibraryUserCopy to move to permanent account storage.
        Returns the permanent storage path.
        """
        import urllib.parse
        fb_token = self.get_firebase_id_token()
        
        # Determine user ID (fallback to API person ID if not in self)
        user_id = getattr(self, "firebase_uid", None) or "dc805f47-3134-4e6e-8c9e-2e65ab2bbc70"
        
        # Step 1: Upload to Firebase Storage we-upload bucket
        object_name = f"{user_id}/{filename}"
        encoded_name = urllib.parse.quote(object_name, safe='')
        upload_url = f"https://firebasestorage.googleapis.com/v0/b/we-upload/o?name={encoded_name}"
        headers = {
            "Authorization": f"Bearer {fb_token}",
            "Content-Type": content_type
        }
        resp = requests.post(upload_url, headers=headers, data=file_bytes)
        if resp.status_code == 401:
            fb_token = self.get_firebase_id_token(force_refresh=True)
            headers["Authorization"] = f"Bearer {fb_token}"
            resp = requests.post(upload_url, headers=headers, data=file_bytes)
        resp.raise_for_status()

        # Step 2: Copy to permanent account storage
        if not destination_path:
            destination_path = f"files/{filename}"
            
        fn_url = "https://us-east1-worship-extreme-datastore.cloudfunctions.net/accountLibraryUserCopy"
        fn_payload = {
            "data": {
                "account": self.account_id,
                "objects": [
                    {
                        "name": filename,
                        "destination": destination_path
                    }
                ]
            }
        }
        fn_resp = requests.post(fn_url, headers={"Authorization": f"Bearer {fb_token}", "Content-Type": "application/json"}, json=fn_payload)
        fn_resp.raise_for_status()
        result_paths = fn_resp.json().get("result", [])
        return result_paths[0] if result_paths else f"accounts/{self.account_id}/{destination_path}"

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
