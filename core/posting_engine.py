#!/usr/bin/env python3
"""Posting engine - send vacancy posts to Facebook groups"""

import os
import time
import random
import logging
from datetime import datetime, timedelta
from typing import Optional, List
from pathlib import Path

from selenium.webdriver import Chrome
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from core.database import SessionLocal, Account, Group, Vacancy, PostingLog, DailyLimit
from core.ixbrowser_client import IXBrowserClient

logger = logging.getLogger(__name__)


class PostingEngine:
    """Engine for posting vacancies to Facebook groups"""
    
    # Posting limits per account
    MAX_POSTS_PER_DAY = 30          # Max posts per account per day
    MIN_DELAY_BETWEEN_POSTS = 30    # Seconds
    MAX_DELAY_BETWEEN_POSTS = 120   # Seconds
    POSTS_PER_BATCH = 10            # Posts before a longer break
    BATCH_BREAK_MIN = 300           # 5 min break between batches
    BATCH_BREAK_MAX = 600           # 10 min
    
    def __init__(self):
        self.client = IXBrowserClient()
    
    def _get_session_groups(self, db, account_id: int, limit: int = 10) -> List[Group]:
        """Get groups that haven't been posted to recently by this account"""
        today = datetime.utcnow().date()
        posted_group_ids = db.query(PostingLog.group_id).filter(
            PostingLog.account_id == account_id,
            PostingLog.posted_at >= today,
        ).all()
        posted_ids = {p[0] for p in posted_group_ids}
        
        groups = db.query(Group).filter(
            ~Group.id.in_(posted_ids) if posted_ids else True
        ).limit(limit).all()
        
        return groups
    
    def _check_daily_limit(self, db, account_id: int) -> bool:
        """Check if account has reached daily posting limit"""
        today = datetime.utcnow().date().isoformat()
        limit = db.query(DailyLimit).filter(
            DailyLimit.account_id == account_id,
            DailyLimit.date == today,
        ).first()
        
        if limit and limit.posts_made >= self.MAX_POSTS_PER_DAY:
            return False  # Limit reached
        return True
    
    def _increment_daily_count(self, db, account_id: int):
        """Increment daily post counter for an account"""
        today = datetime.utcnow().date().isoformat()
        limit = db.query(DailyLimit).filter(
            DailyLimit.account_id == account_id,
            DailyLimit.date == today,
        ).first()
        
        if limit:
            limit.posts_made += 1
        else:
            limit = DailyLimit(account_id=account_id, date=today, posts_made=1)
            db.add(limit)
        db.commit()
    
    def _post_to_group(self, driver: Chrome, group_url: str,
                       text: str, photo_path: str = None) -> tuple:
        """
        Post to a single Facebook group.
        
        Uses WebDriverWait for reliable element detection.
        Uses JS clipboard paste for fast text input.
        Takes screenshot on error for diagnostics.
        
        Returns:
            (success: bool, message: str)
        """
        group_id_str = group_url.rstrip('/').split('/')[-1]
        try:
            # STEP 1: Ensure we're in groups context
            driver.get("https://www.facebook.com/groups/feed/")
            time.sleep(2)
            
            # STEP 2: Navigate to target group
            logger.info(f"Navigating to group: {group_url}")
            driver.get(group_url)
            time.sleep(random.uniform(4.0, 6.0))
            
            current_url = driver.current_url.lower()
            if "checkpoint" in current_url:
                return (False, "Account checkpoint - needs verification")
            if "login" in current_url:
                return (False, "Logout detected")
            
            logger.info(f"📍 Group page loaded")
            
            # STEP 3: Find post composer with WebDriverWait
            post_box = None
            
            # Strategy A: Find visible contenteditable inside form (already open)
            try:
                # Wait up to 8s for a contenteditable inside the group's post form
                post_box = WebDriverWait(driver, 8).until(
                    EC.presence_of_element_located(
                        (By.XPATH, "//form//div[@contenteditable='true' and contains(@class, 'notranslate')]")
                    )
                )
                logger.info("Found textbox inside form (already open)")
            except:
                pass
            
            if not post_box:
                # Strategy B: Find "Write something" button and click it
                try:
                    write_btn = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable(
                            (By.XPATH, "//span[contains(text(), 'Напишіть') or contains(text(), 'Напишите') or contains(text(), 'Write something')]/ancestor::div[@role='button']")
                        )
                    )
                    write_btn.click()
                    logger.info("Clicked 'Write something' button")
                    time.sleep(random.uniform(2.0, 3.0))
                    
                    # Now find the textbox in the popup
                    post_box = WebDriverWait(driver, 8).until(
                        EC.presence_of_element_located(
                            (By.XPATH, "//div[@role='dialog']//div[@contenteditable='true' and contains(@class, 'notranslate')]")
                        )
                    )
                    logger.info("Found textbox in dialog")
                except:
                    pass
            
            if not post_box:
                # Strategy C: generic contenteditable anywhere
                try:
                    post_box = WebDriverWait(driver, 5).until(
                        EC.presence_of_element_located(
                            (By.XPATH, "//div[@contenteditable='true']")
                        )
                    )
                    logger.info("Found fallback contenteditable")
                except:
                    pass
            
            if not post_box:
                return (False, "Could not find post composer (tried 3 strategies)")
            
            # STEP 4: Type/insert text
            try:
                post_box.click()
                time.sleep(0.5)
            except:
                pass
            
            # Use JS to paste text via clipboard (much faster than send_keys line-by-line)
            try:
                # Place text in clipboard via JS
                import pyperclip
                pyperclip.copy(text)
                post_box.send_keys(Keys.CONTROL, 'v')
                time.sleep(1)
            except ImportError:
                # Fallback: send_keys line by line
                logger.info("pyperclip not installed, using send_keys")
                for line in text.split('\n'):
                    post_box.send_keys(line)
                    time.sleep(random.uniform(0.05, 0.1))
                    post_box.send_keys(Keys.SHIFT + Keys.ENTER)
            except Exception:
                # Fallback: send_keys
                for line in text.split('\n'):
                    post_box.send_keys(line)
                    time.sleep(random.uniform(0.05, 0.1))
                    post_box.send_keys(Keys.SHIFT + Keys.ENTER)
            
            time.sleep(random.uniform(1.0, 2.0))
            
            # STEP 5: Upload photo
            if photo_path and os.path.exists(photo_path):
                try:
                    file_inputs = driver.find_elements(By.XPATH, "//input[@type='file']")
                    uploaded = False
                    for fi in file_inputs:
                        try:
                            fi.send_keys(os.path.abspath(photo_path))
                            logger.info(f"Uploaded photo: {photo_path}")
                            time.sleep(random.uniform(3.0, 5.0))
                            uploaded = True
                            break
                        except:
                            continue
                    if not uploaded:
                        logger.warning("No file input found for photo")
                except Exception as e:
                    logger.warning(f"Photo upload error: {e}")
            
            # STEP 6: Click Publish
            time.sleep(1)
            
            publish_clicked = False
            for selector in [
                "//div[@aria-label='Опублікувати']",
                "//div[@aria-label='Опубликовать']",
                "//div[@aria-label='Post']",
                "//span[text()='Опублікувати']/ancestor::div[@role='button']",
                "//span[text()='Опубликовать']/ancestor::div[@role='button']",
                "//span[text()='Publish']/ancestor::div[@role='button']",
                "//span[text()='Post']/ancestor::div[@role='button']",
            ]:
                try:
                    btn = WebDriverWait(driver, 3).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    driver.execute_script("arguments[0].click();", btn)
                    publish_clicked = True
                    logger.info(f"Clicked publish")
                    time.sleep(2)
                    break
                except:
                    continue
            
            if not publish_clicked:
                try:
                    post_box.send_keys(Keys.CONTROL + Keys.ENTER)
                    time.sleep(2)
                except:
                    pass
            
            # Check for ban
            if "checkpoint" in driver.current_url.lower():
                return (False, "Account checkpoint")
            
            return (True, "Posted successfully")
            
        except Exception as e:
            # Take screenshot for diagnostics
            try:
                screenshot_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs')
                os.makedirs(screenshot_dir, exist_ok=True)
                screenshot_path = os.path.join(screenshot_dir, f"error_{group_id_str}_{int(time.time())}.png")
                driver.save_screenshot(screenshot_path)
                logger.info(f"📸 Screenshot saved: {screenshot_path}")
            except:
                pass
            logger.error(f"Post error for {group_url}: {e}")
            return (False, str(e))
    
    def _log_posting_result(self, db, account_id: int, group_id: int,
                            vacancy_id: int, group_url: str,
                            success: bool, message: str):
        """Save posting result to database"""
        log = PostingLog(
            account_id=account_id,
            group_id=group_id,
            vacancy_id=vacancy_id,
            group_url=group_url,
            status="success" if success else "failed",
            error_message=message if not success else "",
        )
        db.add(log)
        
        if success:
            group = db.query(Group).filter(Group.id == group_id).first()
            if group:
                group.post_count = (group.post_count or 0) + 1
                group.last_posted_at = datetime.utcnow()
            
            account = db.query(Account).filter(Account.id == account_id).first()
            if account:
                account.total_post_count = (account.total_post_count or 0) + 1
                account.daily_post_count = (account.daily_post_count or 0) + 1
                account.last_active_at = datetime.utcnow()
        
        db.commit()
    
    def run_posting_round(self, account_id: int, vacancy_id: int,
                          groups_per_batch: int = 10,
                          batches: int = 10,
                          profile_id: str = None) -> dict:
        """
        Run a full posting round for one account.
        
        1 batch = post to N groups. Between batches - longer break.
        10 batches x 10 groups = 100 groups total.
        """
        db = SessionLocal()
        account = db.query(Account).filter(Account.id == account_id).first()
        vacancy = db.query(Vacancy).filter(Vacancy.id == vacancy_id, Vacancy.is_active == True).first()
        
        if not account:
            db.close()
            return {"status": "error", "message": "Account not found"}
        if not vacancy:
            db.close()
            return {"status": "error", "message": "Vacancy not found or inactive"}
        
        if not profile_id:
            profile_id = account.ix_profile_id
        
        db.close()
        
        if not profile_id:
            return {"status": "error", "message": "No iXBrowser profile ID"}
        
        # Open profile
        driver = self.client.open_profile_and_get_driver(profile_id)
        if not driver:
            return {"status": "error", "message": "Failed to open ixBrowser profile"}
        
        results = {
            "status": "success",
            "total_batches": 0,
            "total_posts": 0,
            "successful": 0,
            "failed": 0,
            "limit_reached": 0,
            "overall": [],
        }
        
        try:
            db = SessionLocal()
            if account.status != "ready":
                logger.warning(f"Account {account_id} status is '{account.status}', not 'ready'")
            
            if not self._check_daily_limit(db, account_id):
                results["status"] = "limit_reached"
                results["message"] = "Daily posting limit reached"
                db.close()
                return results
            db.close()
            
            for batch_idx in range(batches):
                db = SessionLocal()
                if not self._check_daily_limit(db, account_id):
                    results["limit_reached"] = 1
                    db.close()
                    break
                
                groups = self._get_session_groups(db, account_id, groups_per_batch)
                db.close()
                
                if not groups:
                    logger.info(f"No more groups available for account {account_id}")
                    break
                
                logger.info(f"📦 Batch {batch_idx + 1}/{batches}: {len(groups)} groups")
                
                for group in groups:
                    batch_result = {
                        "group_url": group.url,
                        "group_id": group.id,
                    }
                    
                    success, message = self._post_to_group(
                        driver=driver,
                        group_url=group.url,
                        text=vacancy.description,
                        photo_path=vacancy.photo_path,
                    )
                    
                    batch_result["success"] = success
                    batch_result["message"] = message
                    results["overall"].append(batch_result)
                    
                    db = SessionLocal()
                    self._log_posting_result(
                        db=db,
                        account_id=account_id,
                        group_id=group.id,
                        vacancy_id=vacancy_id,
                        group_url=group.url,
                        success=success,
                        message=message,
                    )
                    
                    if success:
                        self._increment_daily_count(db, account_id)
                        results["successful"] += 1
                    else:
                        results["failed"] += 1
                        
                        if "banned" in message.lower() or "checkpoint" in message.lower() or "restricted" in message.lower():
                            account = db.query(Account).filter(Account.id == account_id).first()
                            if account:
                                account.status = "banned"
                                account.banned_at = datetime.utcnow()
                                db.commit()
                            db.close()
                            results["status"] = "banned"
                            results["message"] = message
                            return results
                    
                    db.close()
                    results["total_posts"] += 1
                    
                    delay = random.randint(self.MIN_DELAY_BETWEEN_POSTS, self.MAX_DELAY_BETWEEN_POSTS)
                    logger.info(f"⏳ Waiting {delay}s before next post...")
                    time.sleep(delay)
                
                results["total_batches"] += 1
                
                if batch_idx < batches - 1:
                    break_time = random.randint(self.BATCH_BREAK_MIN, self.BATCH_BREAK_MAX)
                    logger.info(f"☕ Batch break: {break_time // 60} min")
                    time.sleep(break_time)
            
        except Exception as e:
            logger.error(f"Posting round error for account {account_id}: {e}")
            results["status"] = "error"
            results["message"] = str(e)
        finally:
            try:
                self.client.close_profile(profile_id)
                driver.quit()
            except:
                pass
        
        return results
    
    def run_multiple_accounts_with_vacancies(self, assignments: list) -> dict:
        """
        Run multiple accounts, each with its own vacancy.
        
        Args:
            assignments: List of dicts:
                [{"account_id": 1, "vacancy_id": 1, "groups_per_batch": 10, "batches": 10}, ...]
        """
        all_results = []
        for assignment in assignments:
            account_id = assignment["account_id"]
            vacancy_id = assignment["vacancy_id"]
            groups_per_batch = assignment.get("groups_per_batch", 10)
            batches = assignment.get("batches", 10)
            
            db = SessionLocal()
            account = db.query(Account).filter(Account.id == account_id).first()
            vacancy = db.query(Vacancy).filter(Vacancy.id == vacancy_id).first()
            db.close()
            
            acc_name = account.name if account else f"ID:{account_id}"
            vac_title = vacancy.title if vacancy else f"ID:{vacancy_id}"
            
            logger.info(f"🚀 Account '{acc_name}' posting vacancy '{vac_title}'")
            
            result = self.run_posting_round(
                account_id=account_id,
                vacancy_id=vacancy_id,
                groups_per_batch=groups_per_batch,
                batches=batches,
            )
            
            all_results.append({
                "account_id": account_id,
                "account_name": acc_name,
                "vacancy_title": vac_title,
                "result": result,
            })
            
            delay = random.randint(30, 60)
            logger.info(f"⏳ Waiting {delay}s before next account...")
            time.sleep(delay)
        
        return {"all_accounts": all_results}
    
    def run_all_accounts(self, vacancy_id: int) -> dict:
        """Run posting for all 'ready' accounts with the same vacancy"""
        db = SessionLocal()
        accounts = db.query(Account).filter(Account.status == "ready").all()
        db.close()
        
        assignments = [
            {"account_id": acc.id, "vacancy_id": vacancy_id}
            for acc in accounts
        ]
        
        return self.run_multiple_accounts_with_vacancies(assignments)
    
    def _collect_groups_from_profile(self, driver: Chrome, max_groups: int = 100) -> list:
        """
        Collect group URLs from the user's "Your Groups" page.
        Only collects direct group URLs like:
        https://www.facebook.com/groups/123456789/
        NOT group/feed/, group/joins/ etc.
        """
        group_urls = []
        
        try:
            # Scroll a few times to load more groups
            for scroll in range(10):
                # Collect all links on page
                links = driver.find_elements(By.TAG_NAME, "a")
                
                for link in links:
                    try:
                        href = link.get_attribute("href")
                        if not href:
                            continue
                        
                        # Must be a group URL
                        if not href.startswith("https://www.facebook.com/groups/"):
                            continue
                        
                        # Extract the part after /groups/
                        rest = href.replace("https://www.facebook.com/groups/", "")
                        
                        # Direct group URL is facebook.com/groups/NUMBER/ or facebook.com/groups/NUMBER
                        # Exclude feed, joins, search, discover etc.
                        excluded = ['feed', 'joins', 'search', 'discover', 'manage', 
                                    'create', 'saved', 'invite', 'requests', 'pending']
                        
                        # Get first path segment
                        group_id = rest.split('/')[0]
                        
                        if not group_id or group_id in excluded:
                            continue
                        
                        # Clean URL - remove query params, keep just the group
                        clean_url = f"https://www.facebook.com/groups/{group_id}/"
                        
                        if clean_url not in group_urls:
                            group_urls.append(clean_url)
                    except:
                        pass
                
                if len(group_urls) >= max_groups:
                    break
                
                # Scroll inside the main FB content area (not just body)
                driver.execute_script("""
                    const main = document.querySelector('[role="main"]');
                    if (main) main.scrollTo(0, main.scrollHeight);
                    else window.scrollTo(0, document.body.scrollHeight);
                """)
                time.sleep(random.uniform(1.5, 2.5))
            
            logger.info(f"📋 Collected {len(group_urls)} clean group URLs")
            
        except Exception as e:
            logger.error(f"Error collecting groups from profile: {e}")
        
        return group_urls[:max_groups]
    
    def run_posting_from_profile(self, account_id: int, vacancy_id: int,
                                  max_posts: int = 30,
                                  profile_id: str = None) -> dict:
        """
        Post to groups directly from the account's existing groups on Facebook,
        WITHOUT needing groups in the database.
        
        1. Opens iXBrowser profile
        2. Goes to facebook.com/groups/joins/ (Your Groups)
        3. Collects all group URLs from the page
        4. Posts vacancy to each group one by one
        5. Returns to groups page after each post
        """
        db = SessionLocal()
        account = db.query(Account).filter(Account.id == account_id).first()
        vacancy = db.query(Vacancy).filter(Vacancy.id == vacancy_id, Vacancy.is_active == True).first()
        
        if not account:
            db.close()
            return {"status": "error", "message": "Account not found"}
        if not vacancy:
            db.close()
            return {"status": "error", "message": "Vacancy not found or inactive"}
        
        if not profile_id:
            profile_id = account.ix_profile_id
        db.close()
        
        if not profile_id:
            return {"status": "error", "message": "No iXBrowser profile ID"}
        
        # Open profile
        driver = self.client.open_profile_and_get_driver(profile_id)
        if not driver:
            return {"status": "error", "message": "Failed to open ixBrowser profile"}
        
        results = {
            "status": "success",
            "total_posts": 0,
            "successful": 0,
            "failed": 0,
            "overall": [],
        }
        
        try:
            # Step 1: Navigate to Groups feed (Facebook might redirect from joins URL)
            logger.info("📋 Navigating to groups page...")
            driver.get("https://www.facebook.com/groups/feed/")
            
            # Wait for page to settle
            time.sleep(random.uniform(3.0, 5.0))
            
            # Check if we're on groups feed or got redirected to login
            current_url = driver.current_url.lower()
            if "login" in current_url:
                return {"status": "error", "message": "Not logged in - Facebook login page detected"}
            
            # Try to click "Your Groups" or "Мои группы" in the left sidebar
            try:
                your_groups_btn = driver.find_element(By.XPATH,
                    "//span[contains(text(), 'Мои группы') or contains(text(), 'Your Groups') or contains(text(), 'Мої групи') or contains(text(), 'Ваши группы') or contains(text(), 'Всі групи')]"
                )
                your_groups_btn.click()
                time.sleep(random.uniform(2.0, 3.0))
                logger.info("Clicked 'Your Groups' link in sidebar")
            except:
                # Fallback: try direct URL
                logger.info("Could not find 'Your Groups' button, trying direct URL")
                driver.get("https://www.facebook.com/groups/joins/?nav_source=tab&ordering=viewer_added")
                time.sleep(random.uniform(3.0, 5.0))
            
            # Check if we're on the groups list
            if "joins" in driver.current_url.lower() or "feed" in driver.current_url.lower():
                logger.info(f"✅ On groups page: {driver.current_url}")
            else:
                logger.warning(f"Unexpected URL after navigation: {driver.current_url}")
            
            # Step 2: Collect groups from the My Groups page
            logger.info("🔍 Collecting groups from profile...")
            group_urls = self._collect_groups_from_profile(driver, max_groups=max_posts)
            
            if not group_urls:
                return {"status": "error", "message": "No groups found in profile"}
            
            logger.info(f"📋 Found {len(group_urls)} groups. Starting posting...")
            
            # Step 2: Post to each group
            for idx, group_url in enumerate(group_urls):
                if results["successful"] >= max_posts:
                    logger.info(f"✅ Reached max_posts limit ({max_posts})")
                    break
                
                logger.info(f"📦 ({idx + 1}/{len(group_urls)}) Posting to: {group_url}")
                
                batch_result = {
                    "group_url": group_url,
                    "index": idx + 1,
                }
                
                success, message = self._post_to_group(
                    driver=driver,
                    group_url=group_url,
                    text=vacancy.description,
                    photo_path=vacancy.photo_path,
                )
                
                batch_result["success"] = success
                batch_result["message"] = message
                results["overall"].append(batch_result)
                
                if success:
                    results["successful"] += 1
                else:
                    results["failed"] += 1
                    
                    if "checkpoint" in message.lower() or "restricted" in message.lower() or "blocked" in message.lower():
                        db = SessionLocal()
                        account = db.query(Account).filter(Account.id == account_id).first()
                        if account:
                            account.status = "banned"
                            account.banned_at = datetime.utcnow()
                            db.commit()
                        db.close()
                        results["status"] = "banned"
                        results["message"] = message
                        return results
                
                results["total_posts"] += 1
                
                # Just delay between posts - no navigation back
                if idx < len(group_urls) - 1:
                    delay = random.randint(self.MIN_DELAY_BETWEEN_POSTS, self.MAX_DELAY_BETWEEN_POSTS)
                    logger.info(f"⏳ Waiting {delay}s before next post...")
                    time.sleep(delay)
            
            logger.info(f"✅ Done! {results['successful']} successful, {results['failed']} failed")
            
        except Exception as e:
            logger.error(f"Posting from profile error: {e}")
            results["status"] = "error"
            results["message"] = str(e)
        finally:
            try:
                self.client.close_profile(profile_id)
                driver.quit()
            except:
                pass
        
        return results
