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
        Post to a single Facebook group using real FB selectors.
        
        Flow (based on user's manual test):
        1. Navigate to group
        2. Click "Переглянути групу" (View Group) if present
        3. Click "Напишіть щось..." (Write something) to open post composer
        4. Type text into the contenteditable <p> element
        5. Upload photo via photo button
        6. Click "Опублікувати" (Publish)
        7. Return to groups list, repeat
        
        Returns:
            (success: bool, message: str)
        """
        try:
            # STEP 1: Navigate to group
            driver.get(f"{group_url}")
            time.sleep(random.uniform(3.0, 5.0))
            
            # Wait for body to load
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            
            # STEP 2: Click "Переглянути групу" (View Group) if present
            try:
                view_group_btn = driver.find_element(By.XPATH,
                    "//span[contains(text(), 'Переглянути групу') or contains(text(), 'View Group') or contains(text(), 'Перейти до групи')]")
                view_group_btn.click()
                time.sleep(random.uniform(2.0, 3.0))
                logger.info("Clicked 'View Group' button")
            except:
                pass
            
            # STEP 3: Click "Напишіть щось..." to open the post composer
            post_composer_selectors = [
                # Ukrainian: Напишіть щось...
                "//span[contains(text(), 'Напишіть щось')]",
                # Russian: Напишите что-нибудь...
                "//span[contains(text(), 'Напишите что-нибудь')]",
                # English: Write something...
                "//span[contains(text(), 'Write something')]",
                # Generic
                "//span[contains(@class, 'x1lliihq') and contains(text(), 'Напиш')]",
                "//span[contains(@class, 'x1lliihq') and contains(text(), 'Write')]",
            ]
            
            composer_clicked = False
            for selector in post_composer_selectors:
                try:
                    span = driver.find_element(By.XPATH, selector)
                    # Click the parent clickable div (role="button")
                    parent_button = span.find_element(By.XPATH,
                        "./ancestor::div[@role='button']")
                    parent_button.click()
                    composer_clicked = True
                    logger.info(f"Opened post composer via: {selector}")
                    time.sleep(random.uniform(2.0, 3.0))
                    break
                except:
                    continue
            
            if not composer_clicked:
                return (False, "Could not open post composer (Напишіть щось... button)")
            
            # STEP 4: Find the text input area (contenteditable div inside dialog)
            text_input_selectors = [
                # The actual typing area after popup opens
                "//div[@role='dialog']//div[@contenteditable='true']",
                "//div[@aria-label='Напишіть щось...']",
                "//div[@aria-label='Напишите что-нибудь...']",
                "//div[@aria-label='Write something...']",
                # Fallback: any contenteditable div
                "//div[@contenteditable='true' and contains(@class, 'notranslate')]",
                "//div[@contenteditable='true']",
            ]
            
            text_input = None
            for selector in text_input_selectors:
                try:
                    text_input = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    if text_input:
                        logger.info(f"Found text input via: {selector}")
                        break
                except:
                    continue
            
            if not text_input:
                return (False, "Could not find text input area in dialog")
            
            # Click and type the text
            text_input.click()
            time.sleep(0.5)
            
            # Type text human-like (line by line)
            for line in text.split('\n'):
                text_input.send_keys(line)
                time.sleep(random.uniform(0.08, 0.2))
                text_input.send_keys(Keys.SHIFT + Keys.ENTER)
            
            time.sleep(random.uniform(1.0, 2.0))
            
            # STEP 5: Upload photo
            if photo_path and os.path.exists(photo_path):
                try:
                    # Click photo button in the post popup
                    photo_btn_selectors = [
                        # Ukrainian: Фото
                        "//div[@role='button']//span[contains(text(), 'Фото') and not(contains(text(), 'Відео'))]",
                        # Russian: Фото  
                        "//div[@role='button']//span[contains(text(), 'Фото')]",
                        # English: Photo
                        "//div[@role='button']//span[contains(text(), 'Photo')]",
                        # The actual webp icon button
                        "//img[contains(@src, 'photo')]/ancestor::div[@role='button']",
                    ]
                    
                    photo_clicked = False
                    for selector in photo_btn_selectors:
                        try:
                            photo_btn = driver.find_element(By.XPATH, selector)
                            parent_btn = photo_btn.find_element(By.XPATH,
                                "./ancestor::div[@role='button']")
                            parent_btn.click()
                            photo_clicked = True
                            logger.info("Clicked photo button")
                            time.sleep(random.uniform(2.0, 3.0))
                            break
                        except:
                            continue
                    
                    if photo_clicked:
                        # Find file input
                        file_input = driver.find_element(By.XPATH,
                            "//input[@type='file']")
                        abs_path = os.path.abspath(photo_path)
                        file_input.send_keys(abs_path)
                        logger.info(f"Uploaded photo: {abs_path}")
                        time.sleep(random.uniform(3.0, 5.0))
                    else:
                        # Direct file input
                        try:
                            file_input = driver.find_element(By.XPATH,
                                "//input[@type='file' and contains(@accept, 'image')]")
                            abs_path = os.path.abspath(photo_path)
                            file_input.send_keys(abs_path)
                            logger.info(f"Uploaded photo directly: {abs_path}")
                            time.sleep(random.uniform(3.0, 5.0))
                        except:
                            logger.warning("Could not find photo upload method")
                            
                except Exception as e:
                    logger.warning(f"Photo upload failed: {e}")
                    # Continue without photo
            
            # STEP 6: Click "Опублікувати" (Publish) button
            time.sleep(random.uniform(1.0, 2.0))
            
            submit_selectors = [
                # Ukrainian: Опублікувати
                "//span[contains(text(), 'Опублікувати')]",
                # Russian: Опубликовать
                "//span[contains(text(), 'Опубликовать')]",
                # English: Publish
                "//span[contains(text(), 'Publish') and not(contains(text(), 'Photo'))]",
                # English: Post
                "//span[contains(text(), 'Post') and not(contains(text(), 'Photo'))]",
                # Inside dialog
                "//div[@role='dialog']//span[contains(text(), 'Опублікувати')]",
                "//div[@role='dialog']//span[contains(text(), 'Publish')]",
            ]
            
            submitted = False
            for selector in submit_selectors:
                try:
                    publish_btn = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    publish_btn.click()
                    submitted = True
                    logger.info(f"Clicked publish via: {selector}")
                    time.sleep(random.uniform(2.0, 4.0))
                    break
                except:
                    continue
            
            if not submitted:
                # Last resort: Ctrl+Enter
                try:
                    text_input.send_keys(Keys.CONTROL + Keys.ENTER)
                    submitted = True
                    time.sleep(2)
                except:
                    pass
            
            if not submitted:
                return (False, "Could not find publish button (Опублікувати)")
            
            # STEP 7: Ban detection
            current_url = driver.current_url.lower()
            if "checkpoint" in current_url:
                return (False, "Account checkpoint - needs verification")
            if "blocked" in current_url or "restricted" in current_url:
                return (False, "Account restricted or blocked")
            
            return (True, "Posted successfully")
            
        except Exception as e:
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
        Collect group URLs from the user's "My Groups" page.
        
        Navigates to https://facebook.com/groups/joins/?...
        and scrapes all group links visible.
        
        Returns:
            list of group URLs (strings)
        """
        group_urls = []
        
        try:
            # Navigate to My Groups page
            driver.get("https://www.facebook.com/groups/joins/?nav_source=tab&ordering=viewer_added")
            time.sleep(random.uniform(4.0, 6.0))
            
            # Wait for page to load
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            
            # Scroll down gradually to load more groups
            last_height = 0
            scroll_attempts = 0
            max_scrolls = 20  # Prevent infinite scroll
            
            while scroll_attempts < max_scrolls:
                # Find all group links on current page
                group_links = driver.find_elements(By.XPATH,
                    "//a[contains(@href, '/groups/') and contains(@href, '/') and not(contains(@href, '/groups/feed')) and not(contains(@href, '/groups/joins')) and not(contains(@href, 'manage'))]"
                )
                
                for link in group_links:
                    url = link.get_attribute("href")
                    if url and url.startswith("https://www.facebook.com/groups/") and url not in group_urls:
                        # Filter out non-group pages
                        if not any(x in url for x in ['/feed/', '/joins/', '/discover/', '/create/', '/manage']):
                            group_urls.append(url)
                
                if len(group_urls) >= max_groups:
                    break
                
                # Scroll down
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(random.uniform(2.0, 3.0))
                
                new_height = driver.execute_script("return document.body.scrollHeight")
                if new_height == last_height:
                    scroll_attempts += 1
                else:
                    scroll_attempts = 0
                last_height = new_height
            
            logger.info(f"📋 Collected {len(group_urls)} groups from profile")
            
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
            # Step 1: Collect groups from profile
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
                
                # Return to "My Groups" page for next post
                if idx < len(group_urls) - 1:
                    # Random delay
                    delay = random.randint(self.MIN_DELAY_BETWEEN_POSTS, self.MAX_DELAY_BETWEEN_POSTS)
                    logger.info(f"⏳ Waiting {delay}s before next post...")
                    time.sleep(delay)
                    
                    # Navigate back to groups list for the next group
                    # (posting already returns us there)
                    if idx % 5 == 4:  # Every 5 posts, refresh the groups page
                        driver.get("https://www.facebook.com/groups/joins/?nav_source=tab&ordering=viewer_added")
                        time.sleep(random.uniform(3.0, 5.0))
                    else:
                        # Just go back
                        try:
                            driver.back()
                            time.sleep(random.uniform(2.0, 3.0))
                        except:
                            driver.get("https://www.facebook.com/groups/joins/?nav_source=tab&ordering=viewer_added")
                            time.sleep(random.uniform(3.0, 5.0))
            
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
