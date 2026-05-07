#!/usr/bin/env python3
"""ixBrowser connection manager for FB Vacancy Bot"""

import json
import time
import logging
from typing import Optional, Dict, Any

import httpx
from selenium.webdriver import Chrome
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from config import IXBROWSER_API

logger = logging.getLogger(__name__)


class IXBrowserClient:
    """Client for ixBrowser Local API V2"""

    def __init__(self, api_url: str = None, api_key: str = ""):
        if api_url:
            base = api_url.rstrip("/")
        else:
            base = IXBROWSER_API.rstrip("/")
        self.base_url = base + "/api/v2/"
        self.api_key = api_key
        self.code = 0
        self.message = ""
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = api_key

    def _request(self, method: str, endpoint: str, data: dict = None) -> Optional[dict]:
        """Make HTTP request to ixBrowser API"""
        url = f"{self.base_url}{endpoint.lstrip('/')}"
        try:
            if method == "GET":
                resp = httpx.get(url, headers=self._headers, timeout=30)
            else:
                resp = httpx.post(url, headers=self._headers, json=data or {}, timeout=30)
            
            result = resp.json()
            
            # ixBrowser error format: {"error": {"code": N, "message": "..."}, "data": null}
            # Success format: {"error": null, "data": {...}}
            if result.get("error") and isinstance(result["error"], dict):
                self.code = result["error"].get("code", -1)
                self.message = result["error"].get("message", "Unknown error")
                return None
            
            self.code = 0
            self.message = "ok"
            return result.get("data")
        except Exception as e:
            logger.error(f"ixBrowser API error: {e}")
            self.code = -1
            self.message = str(e)
            return None

    # ---- Profile Management ----

    def get_profile_list(self) -> Optional[list]:
        """Get all profiles"""
        return self._request("POST", "/profile/list")

    def get_profile_info(self, profile_id: str) -> Optional[dict]:
        """Get profile info by ID"""
        return self._request("POST", "/profile/info", {"profile_id": profile_id})

    def create_profile(self, name: str, proxy: str = "", proxy_type: str = "http",
                       user_agent: str = "", note: str = "",
                       group_id: str = "", cookie: str = "") -> Optional[dict]:
        """Create a new profile"""
        data = {
            "name": name,
            "group_id": group_id,
            "user_agent": user_agent,
            "note": note,
            "proxy": proxy,
            "proxy_type": proxy_type,
            "cookie": cookie,
        }
        return self._request("POST", "/profile/create", data)

    def update_profile(self, profile_id: str, name: str = None, proxy: str = None,
                       proxy_type: str = "http", user_agent: str = None,
                       note: str = None, group_id: str = None) -> bool:
        """Update profile settings"""
        data = {"profile_id": profile_id}
        if name is not None: data["name"] = name
        if proxy is not None: data["proxy"] = proxy
        if proxy_type is not None: data["proxy_type"] = proxy_type
        if user_agent is not None: data["user_agent"] = user_agent
        if note is not None: data["note"] = note
        if group_id is not None: data["group_id"] = group_id
        result = self._request("POST", "/profile/update", data)
        return result is not None

    def open_profile(self, profile_id: str, cookies_backup: bool = False,
                     load_profile_info_page: bool = False) -> Optional[dict]:
        """Open a profile. Returns {webdriver, debugging_address, ...}"""
        data = {
            "profile_id": profile_id,
            "cookies_backup": cookies_backup,
            "load_profile_info_page": load_profile_info_page,
        }
        return self._request("POST", "/browser/open", data)

    def close_profile(self, profile_id: str) -> bool:
        """Close a profile"""
        result = self._request("POST", "/browser/close", {"profile_id": profile_id})
        return result is not None

    def delete_profile(self, profile_id: str) -> bool:
        """Delete a profile"""
        result = self._request("POST", "/profile/delete", {"profile_id": profile_id})
        return result is not None

    # ---- Cookie Management ----

    def get_cookies(self, profile_id: str) -> Optional[list]:
        """Get cookies from an open profile"""
        return self._request("POST", "/cookies/get", {"profile_id": profile_id})

    def set_cookies(self, profile_id: str, cookies: list) -> bool:
        """Set cookies for a profile"""
        result = self._request("POST", "/cookies/set", {
            "id": profile_id,
            "cookies": cookies,
        })
        return result is not None

    # ---- Browser Actions via Selenium ----

    def get_selenium_driver(self, open_result: dict) -> Optional[Chrome]:
        """Create Selenium driver connected to an open ixBrowser profile"""
        try:
            web_driver_path = open_result.get("webdriver")
            debugging_address = open_result.get("debugging_address")
            
            chrome_options = Options()
            chrome_options.add_experimental_option("debuggerAddress", debugging_address)
            
            service = Service(web_driver_path)
            driver = Chrome(service=service, options=chrome_options)
            return driver
        except Exception as e:
            logger.error(f"Failed to create Selenium driver: {e}")
            return None

    def open_profile_and_get_driver(self, profile_id: str) -> Optional[Chrome]:
        """Open profile and return Selenium driver"""
        open_result = self.open_profile(profile_id)
        if not open_result:
            logger.error(f"Failed to open profile {profile_id}: code={self.code}, msg={self.message}")
            return None
        
        time.sleep(1)
        return self.get_selenium_driver(open_result)

    # ---- Profile Search / Filter ----

    def search_profiles(self, keyword: str = "") -> Optional[list]:
        """Search profiles by keyword"""
        return self._request("POST", "/profile/search", {"keyword": keyword})


def test_connection() -> dict:
    """Test ixBrowser API connection"""
    client = IXBrowserClient()
    profiles = client.get_profile_list()
    if profiles is None:
        return {"status": "error", "message": client.message, "code": client.code}
    return {"status": "ok", "profiles_count": len(profiles), "profiles": profiles}


def open_fb_session(profile_id: str) -> Optional[Chrome]:
    """Open Facebook in an ixBrowser profile and return Selenium driver"""
    client = IXBrowserClient()
    driver = client.open_profile_and_get_driver(profile_id)
    if not driver:
        return None
    
    # Navigate to Facebook
    driver.get("https://www.facebook.com")
    time.sleep(3)
    return driver
