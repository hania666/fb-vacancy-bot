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


# ════════════════════════════════════════════════════════════════════════════
# Collect groups from someone else's Facebook profile URL
# ════════════════════════════════════════════════════════════════════════════

def collect_groups_from_profile_url(
    profile_url: str,
    ix_profile_id: str,
    max_groups: int = 200,
) -> dict:
    """
    Visit a target Facebook profile via our iXBrowser session and scrape
    the list of groups they are a member of (the public ones at least).

    Args:
        profile_url: e.g. "https://www.facebook.com/zuck" or
                     "https://www.facebook.com/profile.php?id=100012345678"
        ix_profile_id: ID of OUR iXBrowser profile (logged-in account)
        max_groups: max groups to collect

    Returns:
        {"status": "success", "found": N, "new_added": M, "urls": [...]}
        or {"status": "error", "message": "..."}
    """
    profile_url = profile_url.strip().rstrip("/")
    if "facebook.com" not in profile_url:
        return {"status": "error", "message": "Invalid Facebook URL"}

    # Strip query string and fragment so we don't build broken URLs like
    # /username?locale=ru_RU/groups
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(profile_url)
    path = parts.path.rstrip("/")
    base_url = urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    # Build the groups tab URL for this profile
    if "/profile.php" in base_url:
        # /profile.php?id=123 — sk=groups goes in query
        # base_url here has no query (we stripped), so re-add id and sk
        from urllib.parse import parse_qs, urlencode
        qs = parse_qs(parts.query)
        user_id = qs.get("id", [""])[0]
        if not user_id:
            return {"status": "error", "message": "profile.php URL missing ?id="}
        groups_url = f"{base_url}?id={user_id}&sk=groups"
    else:
        # /username or /username/ → /username/groups
        groups_url = base_url + "/groups"

    logger.info(f"🌐 Opening target profile groups: {groups_url}")

    client = IXBrowserClient()
    driver = client.open_profile_and_get_driver(ix_profile_id)
    if not driver:
        return {"status": "error", "message": "Failed to open iXBrowser profile"}

    try:
        driver.get(groups_url)
        time.sleep(random.uniform(4.0, 6.0))

        cur = driver.current_url.lower()
        if "login" in cur or "checkpoint" in cur:
            return {"status": "error", "message": "Not logged in or checkpoint"}

        # If the user has no public group list, FB redirects to main profile
        if "/groups" not in cur and "sk=groups" not in cur:
            logger.warning("⚠️ Profile has no public groups list (redirected back)")
            return {"status": "error",
                    "message": "Этот профиль скрыл список групп (приватный)"}

        # Scroll to load all groups
        all_urls = set()
        no_new_streak = 0
        for scroll in range(60):
            # Collect group links currently visible
            links = driver.find_elements(By.TAG_NAME, "a")
            prev = len(all_urls)
            for link in links:
                try:
                    href = link.get_attribute("href") or ""
                    if "/groups/" not in href:
                        continue
                    # Strip query/fragment, take first path segment after /groups/
                    path = href.split("?")[0].split("#")[0].rstrip("/")
                    parts = path.split("/groups/")
                    if len(parts) < 2:
                        continue
                    gid = parts[1].split("/")[0]
                    if not gid or gid in (
                        "feed", "joins", "search", "discover", "manage",
                        "create", "saved", "invite", "requests", "pending",
                    ) or gid.startswith("_"):
                        continue
                    all_urls.add(f"https://www.facebook.com/groups/{gid}/")
                except Exception:
                    pass

            new = len(all_urls) - prev
            logger.info(f"   Scroll {scroll+1}: +{new} new | total {len(all_urls)}")

            if new == 0:
                no_new_streak += 1
                if no_new_streak >= 5:
                    logger.info("   ✅ No new groups for 5 scrolls — done")
                    break
            else:
                no_new_streak = 0

            if len(all_urls) >= max_groups:
                logger.info(f"   ✅ Reached max ({max_groups})")
                break

            # Scroll using multiple strategies
            try:
                from selenium.webdriver.common.keys import Keys as _Keys
                driver.find_element(By.TAG_NAME, "body").send_keys(_Keys.END)
            except Exception:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(random.uniform(2.5, 4.0))

        url_list = sorted(all_urls)
        logger.info(f"📋 Total found: {len(url_list)} groups")

        # Save to DB
        groups_to_save = [{"url": u, "name": "", "category": ""} for u in url_list]
        new_count = save_groups_to_db(groups_to_save)

        return {
            "status": "success",
            "found": len(url_list),
            "new_added": new_count,
            "urls": url_list,
        }

    except Exception as e:
        logger.error(f"Profile group scraping error: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        try:
            client.close_profile(ix_profile_id)
            driver.quit()
        except Exception:
            pass
