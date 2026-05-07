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
        # Find groups where this account hasn't posted today
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
        
        Returns:
            (success: bool, message: str)
        """
        try:
            # Navigate to group
            driver.get(f"{group_url}")
            time.sleep(random.uniform(3.0, 5.0))
            
            # Wait for page to load
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            
            # Look for the post creation area
            # Facebook has different selectors depending on version
            post_box_selectors = [
                "//div[@aria-label=\"Создать публикацию\"]",
                "//div[@aria-label=\"Create a post\"]",
                "//div[@aria-label=\"Напишите что-нибудь...\"]",
                "//div[@aria-label=\"Write something...\"]",
                "//div[@role='textbox' and contains(@aria-label, 'публикац')]",
                "//div[@role='textbox' and contains(@aria-label, 'post')]",
                "//div[@class='notranslate' and @contenteditable='true']",
                "//div[@contenteditable='true' and @role='textbox']",
                "//div[@contenteditable='true']",
            ]
            
            post_box = None
            for selector in post_box_selectors:
                try:
                    post_box = driver.find_element(By.XPATH, selector)
                    if post_box:
                        break
                except:
                    continue
            
            if not post_box:
                # Try clicking the "Write post" button first
                try:
                    write_btn = driver.find_element(By.XPATH,
                        "//span[contains(text(), 'Напишите') or contains(text(), 'Write') or contains(text(), 'Створити')]")
                    write_btn.click()
                    time.sleep(2)
                    
                    # Try again
                    for selector in post_box_selectors:
                        try:
                            post_box = driver.find_element(By.XPATH, selector)
                            if post_box:
                                break
                        except:
                            continue
                except:
                    pass
            
            if not post_box:
                return (False, "Could not find post input box")
            
            # Click to activate
            post_box.click()
            time.sleep(1)
            
            # Type the text
            post_box.clear()
            
            # Send text in chunks with human-like typing
            for line in text.split('\n'):
                post_box.send_keys(line)
                time.sleep(random.uniform(0.05, 0.15))
                post_box.send_keys(Keys.SHIFT + Keys.ENTER)
            
            time.sleep(random.uniform(1.0, 2.0))
            
            # Upload photo if provided
            if photo_path and os.path.exists(photo_path):
                try:
                    # Find photo upload input
                    photo_input = driver.find_element(By.XPATH,
                        "//input[@type='file' and contains(@accept, 'image')]")
                    if photo_input:
                        photo_input.send_keys(os.path.abspath(photo_path))
                        time.sleep(random.uniform(3.0, 5.0))
                except:
                    # Try clicking the photo button
                    try:
                        photo_btn = driver.find_element(By.XPATH,
                            "//div[@aria-label='Фото' or @aria-label='Photo' or @aria-label='Додати фото']")
                        photo_btn.click()
                        time.sleep(2)
                        
                        # Now find file input (might be hidden)
                        file_input = driver.find_element(By.XPATH,
                            "//input[@type='file']")
                        file_input.send_keys(os.path.abspath(photo_path))
                        time.sleep(random.uniform(3.0, 5.0))
                    except:
                        pass
            
            # Submit the post
            submit_selectors = [
                "//div[@aria-label='Опубликовать' or @aria-label='Publish']",
                "//span[contains(text(), 'Опубликовать') or contains(text(), 'Publish') or contains(text(), 'Опублікувати')]",
            ]
            
            submitted = False
            for selector in submit_selectors:
                try:
                    publish_btn = driver.find_element(By.XPATH, selector)
                    publish_btn.click()
                    submitted = True
                    time.sleep(random.uniform(2.0, 4.0))
                    break
                except:
                    continue
            
            if not submitted:
                # Try pressing Ctrl+Enter
                post_box.send_keys(Keys.CONTROL + Keys.ENTER)
                time.sleep(2)
            
            # Check for errors
            body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
            if "checkpoint" in driver.current_url.lower():
                return (False, "Account banned or checkpoint")
            
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
            # Update group stats
            group = db.query(Group).filter(Group.id == group_id).first()
            if group:
                group.post_count = (group.post_count or 0) + 1
                group.last_posted_at = datetime.utcnow()
            
            # Update account stats
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
        
        Returns:
            dict with results
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
        
        # Use account's profile ID if not specified
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
            # Check if this account can post
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
                # Check daily limit again
                db = SessionLocal()
                if not self._check_daily_limit(db, account_id):
                    results["limit_reached"] = 1
                    db.close()
                    break
                
                # Get groups for this batch
                groups = self._get_session_groups(db, account_id, groups_per_batch)
                db.close()
                
                if not groups:
                    logger.info(f"No more groups available for account {account_id}")
                    break
                
                logger.info(f"📦 Batch {batch_idx + 1}/{batches}: {len(groups)} groups")
                
                # Post to each group
                for group in groups:
                    success, message = self._post_to_group(
                        driver=driver,
                        group_url=group.url,
                        text=vacancy.description,
                        photo_path=vacancy.photo_path,
                    )
                    
                    # Log result
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
                        
                        # Check if banned
                        if "banned" in message.lower() or "checkpoint" in message.lower():
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
                    
                    # Random delay between posts
                    delay = random.randint(self.MIN_DELAY_BETWEEN_POSTS, self.MAX_DELAY_BETWEEN_POSTS)
                    logger.info(f"⏳ Waiting {delay}s...")
                    time.sleep(delay)
                
                results["total_batches"] += 1
                
                # Longer break between batches
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
    
    def run_all_accounts(self, vacancy_id: int) -> dict:
        """Run posting for all 'ready' accounts"""
        db = SessionLocal()
        accounts = db.query(Account).filter(Account.status == "ready").all()
        db.close()
        
        all_results = []
        for acc in accounts:
            logger.info(f"🚀 Starting posting round for account #{acc.id}")
            result = self.run_posting_round(
                account_id=acc.id,
                vacancy_id=vacancy_id,
            )
            all_results.append({
                "account_id": acc.id,
                "login": acc.login,
                "result": result,
            })
            
            # Short delay between accounts
            time.sleep(random.randint(30, 60))
        
        return {"overall": all_results}
