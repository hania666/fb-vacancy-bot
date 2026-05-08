#!/usr/bin/env python3
"""Account warmup engine - simulates human activity to build trust"""

import time
import random
import logging
from datetime import datetime
from typing import Optional

from selenium.webdriver import Chrome
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from core.database import SessionLocal, Account, Group
from core.ixbrowser_client import IXBrowserClient

logger = logging.getLogger(__name__)


# ---- Helpers ----

def random_sleep(min_sec: float = 1.0, max_sec: float = 5.0):
    """Human-like random delay"""
    time.sleep(random.uniform(min_sec, max_sec))


def scroll_feed(driver: Chrome, scrolls: int = 3):
    """Scroll Facebook feed like a human"""
    for i in range(scrolls):
        pixels = random.randint(300, 800)
        driver.execute_script(f"window.scrollBy(0, {pixels});")
        random_sleep(2.0, 6.0)

        # Sometimes scroll back up a bit
        if random.random() < 0.2:
            driver.execute_script(f"window.scrollBy(0, -{random.randint(100, 300)});")
            random_sleep(1.0, 3.0)


def watch_reels(driver: Chrome, max_reels: int = 3):
    """Watch Facebook Reels like a human"""
    try:
        reels_links = driver.find_elements(By.XPATH,
            "//a[contains(@href, '/reel/') or contains(@aria-label, 'Reels') or contains(@aria-label, 'Рилсы')]")

        if not reels_links:
            return

        reels_links[0].click()
        random_sleep(2.0, 4.0)

        for i in range(max_reels):
            watch_time = random.randint(5, 20)
            logger.info(f"🎬 Watching Reel #{i+1} for {watch_time}s")

            if random.random() < 0.3:
                driver.execute_script("window.scrollBy(0, 400);")
                random_sleep(2.0, 5.0)
                driver.execute_script("window.scrollBy(0, -400);")

            time.sleep(watch_time)

            # Swipe to next
            driver.execute_script("window.scrollBy(0, 600);")
            random_sleep(1.0, 2.0)

            # Like sometimes
            if random.random() < 0.4:
                try:
                    like_btn = driver.find_element(By.XPATH,
                        "//div[@aria-label='Нравится' or @aria-label='Like' or @aria-label='Подобається']")
                    like_btn.click()
                    logger.info("👍 Liked a reel")
                    random_sleep(1.0, 3.0)
                except Exception:
                    pass

    except Exception as e:
        logger.warning(f"Reels error: {e}")
        try:
            driver.get("https://www.facebook.com")
            random_sleep(2.0, 4.0)
        except Exception:
            pass


def browse_groups(driver: Chrome, max_groups: int = 3):
    """Visit and browse Facebook group feed"""
    visited = 0
    for i in range(max_groups):
        try:
            driver.get("https://www.facebook.com/groups/feed/")
            random_sleep(3.0, 5.0)

            scroll_feed(driver, random.randint(2, 4))
            like_random_posts(driver, random.randint(1, 3))
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

        scroll_feed(driver, random.randint(2, 4))

        if random.random() < 0.3:
            items = driver.find_elements(By.XPATH, "//a[contains(@href, '/marketplace/item/')]")
            if items:
                items[0].click()
                random_sleep(3.0, 6.0)
                driver.back()
                random_sleep(2.0, 4.0)

        logger.info("🛒 Browsed Marketplace")
    except Exception as e:
        logger.warning(f"Marketplace error: {e}")


def like_random_posts(driver: Chrome, max_likes: int = 3):
    """Like visible posts on the feed"""
    count = 0
    try:
        like_buttons = driver.find_elements(By.XPATH,
            "//div[@aria-label='Нравится' or @aria-label='Like' or @aria-label='Подобається']")

        for btn in like_buttons[:max_likes]:
            try:
                if random.random() < 0.4:
                    btn.click()
                    count += 1
                    logger.info(f"👍 Liked post #{count}")
                    random_sleep(2.0, 5.0)
            except Exception:
                pass

    except Exception as e:
        logger.warning(f"Like error: {e}")

    return count


def random_comment(driver: Chrome, chance: float = 0.1):
    """Rarely comment on a post"""
    if random.random() > chance:
        return

    comments = ["👍", "🔥", "❤️", "nice", "good", "ok"]
    try:
        comment_buttons = driver.find_elements(By.XPATH,
            "//div[@aria-label='Комментировать' or @aria-label='Comment']")
        if not comment_buttons:
            return

        comment_buttons[0].click()
        random_sleep(1.0, 2.0)

        textarea = driver.find_element(By.XPATH,
            "//div[@aria-label='Напишите комментарий...' or @aria-label='Write a comment...']")
        textarea.send_keys(random.choice(comments))
        random_sleep(1.0, 2.0)
        textarea.submit()
        logger.info("💬 Commented on a post")
    except Exception:
        pass


def visit_random_group(driver: Chrome, groups_urls: list):
    """Visit a random group from the list"""
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
                "//div[@aria-label='Присоединиться к группе' or "
                "@aria-label='Join Group' or "
                "@aria-label='Приєднатися до групи']")
            join_btn.click()
            logger.info(f"📋 Joined group: {url}")
            random_sleep(2.0, 4.0)

            # FIX: правильный XPath с contains() на каждое условие отдельно
            try:
                confirm = driver.find_element(By.XPATH,
                    "//span["
                    "contains(text(), 'Ознакомлен') or "
                    "contains(text(), 'Accept') or "
                    "contains(text(), 'Підтвердити') or "
                    "contains(text(), 'Принять') or "
                    "contains(text(), 'Agree')"
                    "]")
                confirm.click()
                random_sleep(1.0, 2.0)
            except Exception:
                pass  # No confirm button, that's fine

        except Exception:
            pass  # Already a member

    except Exception as e:
        logger.warning(f"Group visit error: {e}")


# ---- Warmup Session ----

def run_warmup_session(
    account_id: int,
    profile_id: str,
    duration_minutes: int = 15,
    groups_urls: list = None,
    stop_flag=None,
) -> dict:
    """
    Run a full warmup session for one account.

    Returns:
        dict with results (likes, scrolls, groups_visited, etc.)
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
        driver.get("https://www.facebook.com")
        random_sleep(3.0, 5.0)

        start_time = time.time()
        end_time = start_time + (duration_minutes * 60)

        while time.time() < end_time:
            # Check stop flag
            if stop_flag and stop_flag.is_set():
                logger.info("⏹️ Warmup stop requested")
                break

            remaining = max(0, int(end_time - time.time()))
            if remaining < 30:
                break

            # Choose random activity
            activity = random.choices(
                ["scroll_feed", "watch_reels", "browse_groups", "browse_marketplace"],
                weights=[40, 25, 25, 10],
                k=1
            )[0]

            if activity == "scroll_feed":
                n_scrolls = random.randint(2, 5)
                scroll_feed(driver, n_scrolls)
                results["scrolls"] += n_scrolls

                liked = like_random_posts(driver, random.randint(1, 4))
                results["likes"] += liked

                random_comment(driver, chance=0.15)

            elif activity == "watch_reels":
                n_reels = random.randint(1, 3)
                watch_reels(driver, n_reels)
                results["scrolls"] += n_reels * 2

            elif activity == "browse_groups":
                n = random.randint(1, 2)
                visited = browse_groups(driver, n)
                results["groups_visited"] += visited

            elif activity == "browse_marketplace":
                browse_marketplace(driver, chance=1.0)

            # FIX: idle не превышает remaining
            remaining = max(0, int(end_time - time.time()))
            if remaining > 30:
                idle_time = random.randint(30, min(120, remaining - 10))
                logger.info(f"⏳ Chill for {idle_time}s... ({remaining}s left)")

                # Sleep in 1s chunks so stop_flag is checked
                for _ in range(idle_time):
                    if stop_flag and stop_flag.is_set():
                        break
                    time.sleep(1)

        # Save cookies
        try:
            cookies = client.get_cookies(profile_id)
            if cookies:
                db = SessionLocal()
                account = db.query(Account).filter(Account.id == account_id).first()
                if account:
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
        except Exception:
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

        db = SessionLocal()
        account = db.query(Account).filter(Account.id == acc.id).first()
        if account and account.status == "new":
            account.status = "warming"
            account.warmup_started_at = datetime.utcnow()
            db.commit()
        db.close()

        db = SessionLocal()
        groups = db.query(Group).filter(Group.is_open == True).limit(20).all()
        group_urls = [g.url for g in groups]
        db.close()

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
