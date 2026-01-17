import requests
import os
import re
from urllib.parse import parse_qs, urlparse

class Worshiptools_API:
    def __init__(self, email, password, account_id):
        self.email = email
        self.password = password
        self.account_id = account_id
        self.base_url = "https://api.worship.tools/v1"
        self.auth_url = "https://auth.worshiptools.com"
        self.site_url = "https://www.worshiptools.com"
        self.refresh_token = None
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
        """
        Authenticate via OAuth2 Authorization Code flow.
        
        The flow is:
        1. Visit www.worshiptools.com/auth/login to initiate OAuth (gets server-side state)
        2. Follow redirect to auth.worshiptools.com/authorize
        3. POST credentials to auth.worshiptools.com/login
        4. Follow redirects back to www.worshiptools.com/auth/callback
        5. The callback sets weAuthToken cookie containing the JWT
        """
        try:
            # Clear any existing session cookies to start fresh
            self.session.cookies.clear()
            
            # Step 1: Start OAuth from www.worshiptools.com to get proper server-side state
            login_page_resp = self.session.get(f"{self.site_url}/auth/login", allow_redirects=False)
            
            if login_page_resp.status_code not in (301, 302, 303, 307, 308):
                print(f"Failed to initiate OAuth flow: status {login_page_resp.status_code}")
                return None
            
            # Get the redirect URL which contains the server-generated state
            auth_redirect = login_page_resp.headers.get('Location', '')
            state = None
            if 'state=' in auth_redirect:
                parsed = urlparse(auth_redirect)
                params = parse_qs(parsed.query)
                state = params.get('state', [None])[0]
            
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
            callback_state = None
            callback_url = None
            
            while max_redirects > 0 and login_resp.status_code in (301, 302, 303, 307, 308):
                max_redirects -= 1
                location = login_resp.headers.get('Location', '')
                
                # Make absolute URL if needed
                if location.startswith('/'):
                    parsed = urlparse(login_resp.url)
                    location = f"{parsed.scheme}://{parsed.netloc}{location}"
                
                # Check if we got the callback with code
                if 'code=' in location:
                    parsed_url = urlparse(location)
                    query_params = parse_qs(parsed_url.query)
                    code = query_params.get('code', [None])[0]
                    callback_state = query_params.get('state', [None])[0]
                    callback_url = location
                    break
                
                login_resp = self.session.get(location, allow_redirects=False)
            
            if not code:
                print("Failed to get authorization code")
                return None
            
            # Step 5: Call the callback URL with full session to exchange code for token
            # The www.worshiptools.com backend exchanges the code and sets weAuthToken cookie
            callback_resp = self.session.get(callback_url, allow_redirects=True)
            
            # Extract token from weAuthToken cookie
            token = self.session.cookies.get('weAuthToken')
            if token:
                # Also save refresh token if available
                self.refresh_token = self.session.cookies.get('weAuthRefresh')
                print(f"Authentication successful, token length: {len(token)}")
                return token
            
            # Fallback: try to find JWT in response body
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
        """Make a request to the API"""
        url = f"{self.base_url}/account/{self.account_id}/{endpoint}"
        
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token}"
        headers["Accept"] = "application/json"
        
        response = self.session.request(method, url, headers=headers, **kwargs)
        
        # If unauthorized, try to re-authenticate
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

    def get(self, endpoint, params=None):
        """
        Make a GET request to the WorshipTools API

        Args:
            endpoint: API endpoint path
            params: Query parameters dictionary
        """
        return self._request("GET", endpoint, params=params)

    def post(self, endpoint, files=None, data=None, json_data=None):
        """
        Make a POST request to the WorshipTools API

        Args:
            endpoint: API endpoint path
            files: List of file tuples for multipart upload
            data: Form data dictionary
            json_data: JSON data dictionary
        """
        if files:
            return self._request("POST", endpoint, files=files, data=data)
        elif json_data:
            return self._request("POST", endpoint, json=json_data, headers={"Content-Type": "application/json"})
        else:
            return self._request("POST", endpoint, data=data)