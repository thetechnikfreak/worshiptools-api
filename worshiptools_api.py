import requests
import os

class Worshiptools_API:
    def __init__(self, email, password, account_id):
        self.email = email
        self.password = password
        self.account_id = account_id
        self.base_url = "https://api.worship.tools/v1"
        self.token = None
        self._authenticate()

    def _authenticate(self):
        """
        Authenticate with the WorshipTools API and get an access token.
        """
        url = f"{self.base_url}/auth/token"
        
        payload = {
            "email": self.email,
            "password": self.password
        }
        
        headers = {
            "Content-Type": "application/json"
        }
        
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        self.token = response.json().get("access_token") or response.json().get("token")
        return self.token

    def _request(self, method, endpoint, **kwargs):
        """Make a request with automatic token refresh on 401"""
        url = f"{self.base_url}/account/{self.account_id}/{endpoint}"
        
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token}"
        headers["Accept"] = "application/json"
        
        response = requests.request(method, url, headers=headers, **kwargs)
        
        # If unauthorized, try to re-authenticate and retry once
        if response.status_code == 401:
            self._authenticate()
            headers["Authorization"] = f"Bearer {self.token}"
            response = requests.request(method, url, headers=headers, **kwargs)
        
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