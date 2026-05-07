#!/usr/bin/env python3
"""ixBrowser connection manager for FB Vacancy Bot"""

import time
import logging
from typing import Optional, List

from selenium.webdriver import Chrome
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from ixbrowser_local_api import IXBrowserClient as IXClient
from ixbrowser_local_api import Consts

from config import IXBROWSER_API

logger = logging.getLogger(__name__)


def _parse_port(api_url: str) -> int:
    """Extract port from API URL"""
    import re
    match = re.search(r':(\d+)', api_url)
    return int(match.group(1)) if match else 53200


class IXBrowserClient:
    """Client for ixBrowser Local API (using official library)"""

    def __init__(self, api_url: str = None):
        port = _parse_port(api_url or IXBROWSER_API)
        self._client = IXClient(port=port)
        self.code = 0
        self.message = ""

    # ---- Profile Management ----

    def get_profile_list(self) -> Optional[list]:
        """Get all profiles"""
        data = self._client.get_profile_list()
        self.code = self._client.code
        self.message = self._client.message
        return data

    def get_profile_info(self, profile_id) -> Optional[dict]:
        """Get profile info by ID"""
        # get_profile_list can be filtered by profile_id
        data = self._client.get_profile_list(profile_id=profile_id)
        self.code = self._client.code
        self.message = self._client.message
        if data and len(data) > 0:
            return data[0]
        return None

    def open_profile(self, profile_id, cookies_backup: bool = False,
                     load_profile_info_page: bool = False) -> Optional[dict]:
        """Open a profile. Returns dict with webdriver/debugging_address."""
        data = self._client.open_profile(
            profile_id=str(profile_id),
            cookies_backup=cookies_backup,
            load_profile_info_page=load_profile_info_page,
        )
        self.code = self._client.code
        self.message = self._client.message
        return data

    def close_profile(self, profile_id) -> bool:
        """Close a profile"""
        data = self._client.close_profile(str(profile_id))
        self.code = self._client.code
        self.message = self._client.message
        return data is not None

    def delete_profile(self, profile_id) -> bool:
        """Delete a profile"""
        data = self._client.delete_profile(str(profile_id))
        self.code = self._client.code
        self.message = self._client.message
        return data is not None

    # ---- Cookie Management ----

    def get_cookies(self, profile_id) -> Optional[list]:
        """Get cookies from an open profile"""
        data = self._client.get_profile_cookie(str(profile_id))
        self.code = self._client.code
        self.message = self._client.message
        return data

    def set_cookies(self, profile_id, cookies: list) -> bool:
        """Set cookies for a profile"""
        data = self._client.update_profile_cookie(str(profile_id), cookies)
        self.code = self._client.code
        self.message = self._client.message
        return data is not None

    # ---- Selenium Driver ----

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

    def open_profile_and_get_driver(self, profile_id) -> Optional[Chrome]:
        """Open profile and return Selenium driver"""
        open_result = self.open_profile(profile_id)
        if not open_result:
            logger.error(f"Failed to open profile {profile_id}: code={self.code}, msg={self.message}")
            return None

        time.sleep(1)
        return self.get_selenium_driver(open_result)

    # ---- Proxy Management ----

    def get_proxy_list(self) -> Optional[list]:
        """Get all saved proxies"""
        data = self._client.get_proxy_list()
        return data

    def create_proxy(self, proxy_type: str = "http", proxy_ip: str = "",
                     proxy_port: str = "", proxy_user: str = "",
                     proxy_password: str = "", note: str = "",
                     tag: str = "") -> bool:
        """Add a custom proxy"""
        data = self._client.create_proxy(
            proxy_type=proxy_type,
            proxy_ip=proxy_ip,
            proxy_port=proxy_port,
            proxy_user=proxy_user,
            proxy_password=proxy_password,
            note=note,
            tag=tag,
        )
        return data is not None

    def delete_proxy(self, proxy_id) -> bool:
        """Remove a proxy"""
        data = self._client.delete_proxy(proxy_id)
        return data is not None

    # ---- Utility ----

    def get_opened_profiles(self) -> Optional[list]:
        """Get currently opened profiles"""
        data = self._client.get_opened_profile_list()
        self.code = self._client.code
        self.message = self._client.message
        return data


def test_connection() -> dict:
    """Test connection to ixBrowser"""
    client = IXBrowserClient()
    profiles = client.get_profile_list()
    if profiles is None:
        return {"status": "error", "message": client.message, "code": client.code}
    return {
        "status": "ok",
        "profiles_count": len(profiles),
        "message": f"Connected. {len(profiles)} profiles found.",
    }
