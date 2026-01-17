import requests
import os

class Worshiptools_API:
    def __init__(self, email, password, account_id):
        self.email = email
        self.password = password
        self.account_id = account_id
        self.base_url = "https://api.worship.tools/v1"
        self.token = os.environ.get("WORSHIPTOOLS_TOKEN")
        if not self.token:
            self.token = self._authenticate()

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
        return response.json().get("token")

    def get(self, endpoint, params=None):
        """
        Make a GET request to the WorshipTools API

        Args:
            endpoint: API endpoint path
            params: Query parameters dictionary
        """
        url = f"{self.base_url}/account/{self.account_id}/{endpoint}"

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json;charset=utf-8"
        }

        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()

    def post(self, endpoint, files=None, data=None, json_data=None):
        """
        Make a POST request to the WorshipTools API

        Args:
            endpoint: API endpoint path
            files: List of file tuples for multipart upload
            data: Form data dictionary
            json_data: JSON data dictionary
        """
        url = f"{self.base_url}/account/{self.account_id}/{endpoint}"

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json"
        }

        # Don't set Content-Type for multipart/form-data, requests will set it automatically
        if files:
            response = requests.post(url, headers=headers, files=files, data=data)
        elif json_data:
            headers["Content-Type"] = "application/json"
            response = requests.post(url, headers=headers, json=json_data)
        else:
            response = requests.post(url, headers=headers, data=data)

        response.raise_for_status()
        return response.json()