#!/usr/bin/env python3
"""Facebook group search and collection"""

import re
import time
import random
import logging
from typing import Optional

from selenium.webdriver import Chrome
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from core.database import SessionLocal, Group
from core.ixbrowser_client import IXBrowserClient

logger = logging.getLogger(__name__)

# Key search terms for job groups in Poland
DEFAULT_SEARCH_TERMS = [
    "работа в Польше",
    "работа Варшава",
    "работа Вроцлав",
    "работа Краков",
    "работа Лодзь",
    "работа Гданьск",
    "работа Познань",
    "работа Катовице",
    "работа Щецин",
    "работа Люблин",
    "работа польща",
    "праця в Польщі",
    "праця Варшава",
    "праця Вроцлав",
    "праця Лодзь",
    "вакансії Польща",
    "вакансии Польша",
    "робота за кордоном",
    "praca w Polsce",
    "praca Warszawa",
    "praca Wrocław",
    "praca Kraków",
    "praca Łódź",
    "praca Gdańsk",
    "praca Poznań",
    "oferty pracy Polska",
]


def search_groups(driver: Chrome, query: str, max_groups: int = 30) -> list:
    """
    Search Facebook groups by keyword and collect group links.
    Assumes driver is already on Facebook homepage.
    
    Returns:
        list of dicts: [{url, name, is_open}]
    """
    found_groups = []
    
    try:
        # Go to Facebook search
        driver.get(f"https://www.facebook.com/search/groups/?q={query}")
        time.sleep(random.uniform(3.0, 5.0))
        
        # Scroll to load more results
        for _ in range(3):
            driver.execute_script("window.scrollBy(0, 800);")
            time.sleep(random.uniform(2.0, 4.0))
        
        # Collect group links
        group_elements = driver.find_elements(By.XPATH,
            "//a[contains(@href, '/groups/')]")
        
        seen_urls = set()
        for elem in group_elements:
            try:
                href = elem.get_attribute("href")
                if not href or '/groups/' not in href:
                    continue
                
                # Clean up URL properly
                # facebook.com/groups/123456/... -> facebook.com/groups/123456/
                url = href.split('?')[0].rstrip('/')
                parts = url.split('/')
                # Take exactly facebook.com/groups/GROUP_ID
                if len(parts) >= 5:
                    url = '/'.join(parts[:5]) + '/'
                
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                
                name = elem.text.strip() or url.split('/')[-1]
                
                found_groups.append({
                    "url": url,
                    "name": name,
                    "is_open": True,  # Default, will verify later
                })
                
                if len(found_groups) >= max_groups:
                    break
            except:
                continue
        
        logger.info(f"🔍 Search '{query}': found {len(found_groups)} groups")
        
    except Exception as e:
        logger.error(f"Search error for '{query}': {e}")
    
    return found_groups


def is_group_open(driver: Chrome, group_url: str) -> bool:
    """Check if a group is open (can post without joining)"""
    try:
        driver.get(group_url)
        time.sleep(random.uniform(2.0, 4.0))
        
        body = driver.find_element(By.TAG_NAME, "body").text.lower()
        
        # Check for indicators
        if "присоединиться" in body or "join group" in body or "приєднатися" in body:
            return False
        if "пишіть" in body and "повідомлення" in body:
            return False
        
        return True
    except:
        return False


def save_groups_to_db(groups: list) -> int:
    """Save collected groups to database, return count of new groups"""
    db = SessionLocal()
    added = 0
    for g in groups:
        existing = db.query(Group).filter(Group.url == g["url"]).first()
        if not existing:
            group = Group(
                url=g["url"],
                name=g["name"],
                is_open=g.get("is_open", True),
                category=g.get("category", ""),
            )
            db.add(group)
            added += 1
    db.commit()
    db.close()
    return added


def collect_groups(
    profile_id: str,
    search_terms: list = None,
    max_per_term: int = 20,
    max_total: int = 200,
) -> dict:
    """
    Full group collection pipeline:
    1. Open ixBrowser profile
    2. For each search term, search FB groups
    3. Collect and save to DB
    
    Returns:
        dict with results (total_found, total_new)
    """
    if search_terms is None:
        search_terms = DEFAULT_SEARCH_TERMS
    
    client = IXBrowserClient()
    driver = client.open_profile_and_get_driver(profile_id)
    
    if not driver:
        return {"status": "error", "message": "Failed to open profile"}
    
    try:
        all_found = []
        
        # Login to Facebook
        driver.get("https://www.facebook.com")
        time.sleep(3)
        
        # Check if we're logged in
        if "login" in driver.current_url.lower() or "checkpoint" in driver.current_url.lower():
            return {"status": "error", "message": "Account not logged in. Please login first in ixBrowser."}
        
        for i, term in enumerate(search_terms):
            if len(all_found) >= max_total:
                break
            
            logger.info(f"🔍 Searching: '{term}' ({i+1}/{len(search_terms)})")
            groups = search_groups(driver, term, max_per_term)
            all_found.extend(groups)
            
            # Random delay between searches
            time.sleep(random.uniform(3.0, 8.0))
        
        # Deduplicate by URL
        seen = set()
        unique_groups = []
        for g in all_found:
            if g["url"] not in seen:
                seen.add(g["url"])
                unique_groups.append(g)
        
        # Save to DB
        new_count = save_groups_to_db(unique_groups)
        
        return {
            "status": "success",
            "total_found": len(unique_groups),
            "new_added": new_count,
        }
        
    except Exception as e:
        logger.error(f"Group collection error: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        try:
            client.close_profile(profile_id)
            driver.quit()
        except:
            pass
