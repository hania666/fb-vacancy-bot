#!/usr/bin/env python3
"""ixBrowser connection manager for FB Vacancy Bot"""

import time
import logging
from typing import Optional

from selenium.webdriver import Chrome
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import InvalidSessionIdException, WebDriverException

from ixbrowser_local_api import IXBrowserClient as IXClient

from config import IXBROWSER_API

logger = logging.getLogger(__name__)


def _parse_port(api_url: str) -> int:
    import re
    match = re.search(r':(\d+)', api_url)
    return int(match.group(1)) if match else 53200


class IXBrowserClient:
    """Client for ixBrowser Local API"""

    def __init__(self, api_url: str = None):
        port = _parse_port(api_url or IXBROWSER_API)
        self._client = IXClient(port=port)
        self.code = 0
        self.message = ""

    # ---- Profile Management ----

    def get_profile_list(self) -> Optional[list]:
        data = self._client.get_profile_list()
        self.code = self._client.code
        self.message = self._client.message
        return data

    def get_profile_info(self, profile_id) -> Optional[dict]:
        data = self._client.get_profile_list(profile_id=profile_id)
        self.code = self._client.code
        self.message = self._client.message
        if data and len(data) > 0:
            return data[0]
        return None

    def open_profile(self, profile_id, cookies_backup: bool = False,
                     load_profile_info_page: bool = False) -> Optional[dict]:
        data = self._client.open_profile(
            profile_id=int(profile_id),
            cookies_backup=cookies_backup,
            load_profile_info_page=load_profile_info_page,
        )
        self.code = self._client.code
        self.message = self._client.message
        return data

    def close_profile(self, profile_id) -> bool:
        data = self._client.close_profile(int(profile_id))
        self.code = self._client.code
        self.message = self._client.message
        return data is not None

    def delete_profile(self, profile_id) -> bool:
        data = self._client.delete_profile(int(profile_id))
        self.code = self._client.code
        self.message = self._client.message
        return data is not None

    # ---- Cookie Management ----

    def get_cookies(self, profile_id) -> Optional[list]:
        data = self._client.get_profile_cookie(int(profile_id))
        self.code = self._client.code
        self.message = self._client.message
        return data

    def set_cookies(self, profile_id, cookies: list) -> bool:
        data = self._client.update_profile_cookie(int(profile_id), cookies)
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

            if web_driver_path:
                service = Service(web_driver_path)
                driver = Chrome(service=service, options=chrome_options)
            else:
                driver = Chrome(options=chrome_options)

            return driver
        except Exception as e:
            logger.error(f"Failed to create Selenium driver: {e}")
            return None

    def is_driver_alive(self, driver: Chrome) -> bool:
        """Check if Selenium driver session is still alive"""
        try:
            _ = driver.current_url
            return True
        except (InvalidSessionIdException, WebDriverException):
            return False

    def open_profile_and_get_driver(self, profile_id, max_retries: int = 2) -> Optional[Chrome]:
        """
        Open ixBrowser profile and return Selenium driver.

        Strategy:
        1. Check if profile already open → try to connect to existing session
        2. If session is dead or not open → close stale, open fresh
        3. Retry once on 1004 error (profile busy) with longer wait
        """
        pid = int(profile_id)

        # Step 1: Check if already open → try reconnecting
        try:
            opened = self._client.get_opened_profile_list()
            logger.info(f"Opened profiles: {opened}")

            if opened:
                for p in opened:
                    p_id = p.get("profile_id")
                    if str(p_id) == str(pid) or str(p.get("id", "")) == str(pid):
                        debug_addr = p.get("debugging_address")
                        if debug_addr:
                            logger.info(f"Profile {pid} already open at {debug_addr}, reconnecting...")
                            chrome_options = Options()
                            chrome_options.add_experimental_option("debuggerAddress", debug_addr)
                            try:
                                driver = Chrome(options=chrome_options)
                                # Verify session is alive
                                if self.is_driver_alive(driver):
                                    logger.info(f"✅ Reconnected to existing profile {pid}")
                                    return driver
                                else:
                                    logger.warning(f"Session dead, will reopen profile {pid}")
                                    try:
                                        driver.quit()
                                    except Exception:
                                        pass
                            except Exception as e:
                                logger.warning(f"Could not reconnect: {e}")
                        break
        except Exception as e:
            logger.warning(f"Error checking opened profiles: {e}")

        # Step 2: Close any stale instance
        try:
            self.close_profile(pid)
            logger.info(f"Closed stale profile {pid}")
        except Exception:
            pass

        # Step 3: Open fresh with retries
        for attempt in range(max_retries):
            wait = 3 + attempt * 4  # 3s, then 7s
            logger.info(f"Waiting {wait}s before opening profile {pid} (attempt {attempt + 1})...")
            time.sleep(wait)

            open_result = self.open_profile(pid)

            if open_result:
                time.sleep(3)
                driver = self.get_selenium_driver(open_result)
                if driver:
                    if self.is_driver_alive(driver):
                        logger.info(f"✅ Opened and connected to profile {pid}")
                        return driver
                    else:
                        logger.warning(f"Driver created but session dead for profile {pid}")
                        try:
                            driver.quit()
                        except Exception:
                            pass
                else:
                    logger.error(f"Failed to create driver for profile {pid}")
            else:
                # code=1004 means profile is still closing, wait longer
                if self.code == 1004:
                    logger.warning(f"Profile {pid} busy (1004), waiting longer before retry...")
                    time.sleep(8)
                else:
                    logger.error(f"Failed to open profile {pid}: code={self.code}, msg={self.message}")

        logger.error(f"❌ Could not open profile {pid} after {max_retries} attempts")
        return None

    # ---- Proxy Management ----

    def get_proxy_list(self) -> Optional[list]:
        return self._client.get_proxy_list()

    def create_proxy(self, proxy_type: str = "http", proxy_ip: str = "",
                     proxy_port: str = "", proxy_user: str = "",
                     proxy_password: str = "", note: str = "",
                     tag: str = "") -> bool:
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
        data = self._client.delete_proxy(proxy_id)
        return data is not None

    # ---- Utility ----

    def get_opened_profiles(self) -> Optional[list]:
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
