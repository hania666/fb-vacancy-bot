#!/usr/bin/env python3
"""Account warmup engine - simulates human activity to build trust"""

import time
import random
import logging
from datetime import datetime, timedelta
from typing import Optional

from selenium.webdriver import Chrome
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from core.database import SessionLocal, Account, Group
from core.ixbrowser_client import IXBrowserClient

logger = logging.getLogger(__name__)


# ---- Actions ----

def random_sleep(min_sec: float = 1.0, max_sec: float = 5.0):
    """Human-like random delay"""
    time.sleep(random.uniform(min_sec, max_sec))


def scroll_feed(driver: Chrome, scrolls: int = 3):
    """Scroll Facebook feed like a human"""
    for i in range(scrolls):
        # Scroll down a random amount
        pixels = random.randint(300, 800)
        driver.execute_script(f"window.scrollBy(0, {pixels});")
        random_sleep(2.0, 6.0)
        
        # Sometimes scroll back up
        if random.random() < 0.2:
            driver.execute_script(f"window.scrollBy(0, -{random.randint(100, 300)});")
            random_sleep(1.0, 3.0)


def watch_reels(driver: Chrome, max_reels: int = 3):
    """Watch Facebook Reels (short videos) like a human"""
    try:
        # Try to navigate to Reels
        reels_links = driver.find_elements(By.XPATH,
            "//a[contains(@href, '/reel/') or contains(@aria-label, 'Reels') or contains(@aria-label, 'Рилсы')]")
        
        if reels_links:
            # Click on a reel
            reels_links[0].click()
            random_sleep(2.0, 4.0)
            
            for i in range(max_reels):
                # Watch the reel for a bit
                watch_time = random.randint(5, 20)
                logger.info(f"🎬 Watching Reel #{i+1} for {watch_time}s")
                
                # Scroll through reel comments sometimes
                if random.random() < 0.3:
                    driver.execute_script("window.scrollBy(0, 400);")
                    random_sleep(2.0, 5.0)
                    driver.execute_script("window.scrollBy(0, -400);")
                
                for _ in range(watch_time):
                    time.sleep(1)
                    # Check if stop was requested
                    if hasattr(driver, '_stop_flag') and driver._stop_flag.is_set():
                        return
                
                # Swipe to next reel (scroll down)
                driver.execute_script("window.scrollBy(0, 600);")
                random_sleep(1.0, 2.0)
                
                # Like some reels
                if random.random() < 0.4:
                    try:
                        like_btn = driver.find_element(By.XPATH,
                            "//div[@aria-label='Нравится' or @aria-label='Like' or @aria-label='Подобається']")
                        like_btn.click()
                        logger.info("👍 Liked a reel")
                        random_sleep(1.0, 3.0)
                    except:
                        pass
    except Exception as e:
        logger.warning(f"Reels error: {e}")
        # If reels fail, just go back to feed
        try:
            driver.get("https://www.facebook.com")
            random_sleep(2.0, 4.0)
        except:
            pass


def browse_groups(driver: Chrome, max_groups: int = 3):
    """Visit and browse Facebook groups"""
    visited = 0
    for i in range(max_groups):
        try:
            # Navigate to Groups section
            driver.get("https://www.facebook.com/groups/feed/")
            random_sleep(3.0, 5.0)
            
            # Scroll through group feed
            scroll_feed(driver, random.randint(2, 4))
            
            # Like some posts in groups
            like_random_posts(driver, random.randint(1, 3))
            
            # Comment on a post sometimes
            random_comment(driver, chance=0.2)
            
            visited += 1
            logger.info(f"👥 Browsed group feed #{i+1}")
            
        except Exception as e:
            logger.warning(f"Group browse error: {e}")
    
    return visited


def browse_marketplace(driver: Chrome, chance: float = 0.3):
    """Occasionally browse Facebook Marketplace"""
    if random.random() > chance:
        return
    
    try:
        driver.get("https://www.facebook.com/marketplace")
        random_sleep(3.0, 6.0)
        
        # Scroll through listings
        scroll_feed(driver, random.randint(2, 4))
        
        # Click on a random item sometimes
        if random.random() < 0.3:
            items = driver.find_elements(By.XPATH, "//a[contains(@href, '/marketplace/item/')]")
            if items:
                items[0].click()
                random_sleep(3.0, 6.0)
                # Go back
                driver.back()
                random_sleep(2.0, 4.0)
        
        logger.info("🛒 Browsed Marketplace")
    except Exception as e:
        logger.warning(f"Marketplace error: {e}")


def like_random_posts(driver: Chrome, max_likes: int = 3):
    """Like visible posts on the feed"""
    try:
        like_buttons = driver.find_elements(By.XPATH, 
            "//div[@aria-label='Нравится' or @aria-label='Like' or @aria-label='Подобається']")
        
        count = 0
        for btn in like_buttons[:max_likes]:
            try:
                if random.random() < 0.4:  # 40% chance to like
                    btn.click()
                    count += 1
                    logger.info(f"👍 Liked post #{count}")
                    random_sleep(2.0, 5.0)
            except:
                pass
        return count
    except Exception as e:
        logger.warning(f"Like error: {e}")
        return 0


def random_comment(driver: Chrome, chance: float = 0.1):
    """Rarely comment on a post"""
    if random.random() > chance:
        return
    
    comments = ["👍", "🔥", "❤️", "nice", "good", "ok"]
    try:
        comment_buttons = driver.find_elements(By.XPATH,
            "//div[@aria-label='Комментировать' or @aria-label='Comment']")
        if comment_buttons:
            comment_buttons[0].click()
            random_sleep(1.0, 2.0)
            
            textarea = driver.find_element(By.XPATH,
                "//div[@aria-label='Напишите комментарий...' or @aria-label='Write a comment...']")
            textarea.send_keys(random.choice(comments))
            random_sleep(1.0, 2.0)
            textarea.submit()
            logger.info("💬 Commented on a post")
    except:
        pass


def visit_random_group(driver: Chrome, groups_urls: list):
    """Visit a random group from our list to show activity"""
    if not groups_urls:
        return
    
    url = random.choice(groups_urls)
    try:
        driver.get(url)
        random_sleep(3.0, 6.0)
        scroll_feed(driver, 2)
        
        # Try to join group if not a member
        try:
            join_btn = driver.find_element(By.XPATH,
                "//div[@aria-label='Присоединиться к группе' or @aria-label='Join Group' or @aria-label='Приєднатися до групи']")
            join_btn.click()
            logger.info(f"📋 Joined group: {url}")
            random_sleep(2.0, 4.0)
            
            # Any of these in body means rules page
            try:
                confirm = driver.find_element(By.XPATH,
                    "//span[contains(text(), 'Ознакомлен') or contains(text(), 'Accept') or contains(text(), 'Підтвердити')]")
                confirm.click()
                random_sleep(1.0, 2.0)
            except:
                pass
        except:
            pass  # Already a member or can't join
    except Exception as e:
        logger.warning(f"Group visit error: {e}")


# ---- Warmup Session ----

def run_warmup_session(
    account_id: int,
    profile_id: str,
    duration_minutes: int = 15,
    groups_urls: list = None,
) -> dict:
    """
    Run a full warmup session for one account
    
    Returns:
        dict with results (likes, scrolls, joined_groups)
    """
    logger.info(f"🔥 Starting warmup for account #{account_id} (profile: {profile_id})")
    
    client = IXBrowserClient()
    driver = client.open_profile_and_get_driver(profile_id)
    
    if not driver:
        return {"status": "error", "message": "Failed to open profile"}
    
    results = {
        "status": "running",
        "scrolls": 0,
        "likes": 0,
        "reels_watched": 0,
        "groups_visited": 0,
        "groups_joined": 0,
        "session_duration": 0,
    }
    
    try:
        # 1. Go to Facebook homepage
        driver.get("https://www.facebook.com")
        random_sleep(3.0, 5.0)
        
        start_time = time.time()
        end_time = start_time + (duration_minutes * 60)
        
        # Force close after duration (even if stuck)
        deadline = end_time + 60  # 1 minute grace period
        
        while time.time() < end_time:
            # Choose a random activity
            activity = random.choices(
                ["scroll_feed", "watch_reels", "browse_groups", "browse_marketplace"],
                weights=[40, 25, 25, 10],  # вероятности
                k=1
            )[0]
            
            if activity == "scroll_feed":
                # Scroll the feed
                n_scrolls = random.randint(2, 5)
                scroll_feed(driver, n_scrolls)
                results["scrolls"] += n_scrolls
                
                # Like some posts
                liked = like_random_posts(driver, random.randint(1, 4))
                results["likes"] += liked
                
                # Sometimes comment
                random_comment(driver, chance=0.15)
            
            elif activity == "watch_reels":
                # Watch reels for a bit
                n_reels = random.randint(1, 3)
                watch_reels(driver, n_reels)
                results["scrolls"] += n_reels * 2  # each reel swipe counts
            
            elif activity == "browse_groups":
                # Browse groups
                n_groups = random.randint(1, 2)
                result = browse_groups(driver, n_groups)
                results["groups_visited"] += result
            
            elif activity == "browse_marketplace":
                        browse_marketplace(driver, chance=1.0)  # always, since we already chose it
            
            # Stay on page for a bit
            remaining = max(0, int(end_time - time.time()))
            if remaining > 30:
                idle_time = min(remaining, random.randint(30, 120))
                max_idle = min(idle_time + 10, remaining)
                logger.info(f"⏳ Chill for {idle_time}s... ({remaining}s left)")
                random_sleep(idle_time, max_idle)
            
            # Break if 30 seconds left
            if remaining < 30:
                break
        
        # 2. Save updated cookies to database
        try:
            cookies = client.get_cookies(profile_id)
            if cookies:
                db = SessionLocal()
                account = db.query(Account).filter(Account.id == account_id).first()
                if account:
                    # Convert cookies to serializable format
                    serializable = []
                    for c in cookies:
                        if isinstance(c, dict):
                            serializable.append({
                                "name": c.get("name"),
                                "value": c.get("value"),
                                "domain": c.get("domain"),
                                "path": c.get("path"),
                            })
                    account.cookies = serializable
                    account.last_active_at = datetime.utcnow()
                    db.commit()
                db.close()
        except Exception as e:
            logger.warning(f"Failed to save cookies: {e}")
        
        results["status"] = "success"
        results["session_duration"] = int(time.time() - start_time)
        
    except Exception as e:
        logger.error(f"Warmup error for account {account_id}: {e}")
        results["status"] = "error"
        results["message"] = str(e)
    finally:
        try:
            client.close_profile(profile_id)
            driver.quit()
        except:
            pass
    
    return results


def warmup_all_ready_accounts():
    """Run warmup for all accounts in 'warming' or 'ready' status"""
    db = SessionLocal()
    accounts = db.query(Account).filter(
        Account.status.in_(["warming", "ready"])
    ).all()
    db.close()
    
    results = []
    for acc in accounts:
        if not acc.ix_profile_id:
            logger.warning(f"Account {acc.id} has no iXBrowser profile ID, skipping")
            continue
        
        # Set to warming status
        db = SessionLocal()
        account = db.query(Account).filter(Account.id == acc.id).first()
        if account.status == "new":
            account.status = "warming"
            account.warmup_started_at = datetime.utcnow()
            db.commit()
        db.close()
        
        # Get some group URLs for visiting
        db = SessionLocal()
        groups = db.query(Group).filter(Group.is_open == True).limit(20).all()
        group_urls = [g.url for g in groups]
        db.close()
        
        # Run warmup
        session_result = run_warmup_session(
            account_id=acc.id,
            profile_id=acc.ix_profile_id,
            duration_minutes=15,
            groups_urls=group_urls,
        )
        results.append({
            "account_id": acc.id,
            "profile_id": acc.ix_profile_id,
            "result": session_result,
        })
    
    return results
