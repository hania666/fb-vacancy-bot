#!/usr/bin/env python3
"""Posting engine - send vacancy posts to Facebook groups"""

import os
import time
import random
import threading
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
        Post vacancy to a single Facebook group.

        Flow:
          1. Navigate to group
          2. Click "Напишіть щось..." placeholder → opens composer
          3. Wait for contenteditable to appear in dialog
          4. Paste text via clipboard (or send_keys fallback)
          5. Upload photo if provided
          6. Click Publish button
          7. Verify no checkpoint/ban
        """
        group_id_str = group_url.rstrip('/').split('/')[-1]

        def _screenshot(tag="error"):
            try:
                logs_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
                os.makedirs(logs_dir, exist_ok=True)
                path = os.path.join(logs_dir, f"{tag}_{group_id_str}_{int(time.time())}.png")
                driver.save_screenshot(path)
                logger.info(f"📸 Screenshot: {path}")
            except Exception:
                pass

        try:
            # ── STEP 1: Navigate to group ────────────────────────────────
            logger.info(f"🌐 Navigating to: {group_url}")
            driver.get(group_url)
            time.sleep(random.uniform(4.0, 6.0))

            cur = driver.current_url.lower()
            if "checkpoint" in cur:
                return (False, "Account checkpoint - needs verification")
            if "login" in cur:
                return (False, "Logout detected")

            # ── Detect blockers BEFORE trying to post ──────────────────────
            # Check for "Запит на участь" / "Membership request" popup
            # — means we are NOT a member of this group
            blocker_xpaths = [
                "//div[@role='dialog']//*[contains(text(),'Запит на участь')]",
                "//div[@role='dialog']//*[contains(text(),'Запрос на участие')]",
                "//div[@role='dialog']//*[contains(text(),'Request to join')]",
                "//div[@role='dialog']//*[contains(text(),'Membership request')]",
                "//div[@role='dialog']//*[contains(text(),'Незабаром ви зможете')]",
                "//div[@role='dialog']//*[contains(text(),'Скоро вы сможете')]",
            ]
            for bx in blocker_xpaths:
                try:
                    blocker = driver.find_element(By.XPATH, bx)
                    if blocker:
                        # Close popup so we don't leave it hanging in the next group
                        try:
                            close_btn = driver.find_element(By.XPATH,
                                "//div[@role='dialog']//*[@aria-label='Закрити' or @aria-label='Close']")
                            driver.execute_script("arguments[0].click();", close_btn)
                            time.sleep(0.5)
                        except Exception:
                            pass
                        logger.warning("⛔ Membership pending — not a full member yet")
                        return (False, "Not a member: membership request pending")
                except Exception:
                    continue

            # Dismiss any popups (rules, cookie banners, etc.) — multi-step
            self._dismiss_popups(driver)
            time.sleep(0.5)
            self._dismiss_popups(driver)  # second pass for multi-step popups

            # Re-check for blocker after popups (rules popup may hide membership question)
            for bx in blocker_xpaths:
                try:
                    if driver.find_elements(By.XPATH, bx):
                        logger.warning("⛔ Membership pending after dismiss")
                        return (False, "Not a member: membership request pending")
                except Exception:
                    pass

            # ── STEP 2: Click the main "Напишіть щось..." composer ─────────
            # CRITICAL: must click the GROUP composer at the top, NOT a comment box
            # under some user's post. Strategy: scroll to top, then prefer aria-label
            # selectors that only match the main composer.
            placeholder_clicked = False

            # Scroll to the very top of the group page first
            driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(1.0)

            # Selectors ordered from MOST specific (only matches main composer)
            # to LEAST specific. The first 3 (aria-label) are unique to the post
            # composer and never match comment fields.
            placeholder_xpaths = [
                # Aria-label selectors — these ONLY exist on the main composer
                "//div[@aria-label='Створіть публічний допис…']",
                "//div[@aria-label='Створіть публічний допис...']",
                "//div[@aria-label='Створіть публічний допис']",
                "//div[@aria-label='Создайте общедоступную публикацию...']",
                "//div[@aria-label='Создайте общедоступную публикацию…']",
                "//div[@aria-label='Create a public post...']",
                "//div[@aria-label='Create a public post…']",
                # Generic post composer aria-labels
                "//div[@aria-label='Створити допис']",
                "//div[@aria-label='Create post']",
                "//div[@aria-label='Создать публикацию']",
                # Placeholder text — but only FIRST occurrence (top of page = main composer)
                "(//span[contains(text(),'Напишіть щось')])[1]",
                "(//div[contains(text(),'Напишіть щось')])[1]",
                "(//span[contains(text(),'Напишите что-нибудь')])[1]",
                "(//span[contains(text(),'Write something')])[1]",
            ]

            for xpath in placeholder_xpaths:
                try:
                    elements = driver.find_elements(By.XPATH, xpath)
                    if not elements:
                        continue

                    # Check each match — pick the FIRST one that is NOT inside a post/article
                    # (which would mean it's a comment field on someone else's post)
                    for el in elements:
                        try:
                            # Verify element is not inside an existing post (article)
                            # The main composer is NOT inside <div role='article'>
                            is_in_article = driver.execute_script("""
                                let node = arguments[0];
                                while (node && node !== document.body) {
                                    if (node.getAttribute && node.getAttribute('role') === 'article') {
                                        return true;
                                    }
                                    node = node.parentElement;
                                }
                                return false;
                            """, el)

                            if is_in_article:
                                logger.info(f"⚠️ Skipping match inside article (comment box)")
                                continue

                            # Verify element is visible
                            is_visible = driver.execute_script("""
                                const rect = arguments[0].getBoundingClientRect();
                                return rect.width > 0 && rect.height > 0;
                            """, el)

                            if not is_visible:
                                continue

                            # Scroll element into view (it's safe now — we know it's NOT a comment)
                            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                            time.sleep(0.5)
                            driver.execute_script("arguments[0].click();", el)
                            logger.info(f"✅ Clicked main composer via: {xpath[:65]}")
                            placeholder_clicked = True
                            break
                        except Exception:
                            continue

                    if placeholder_clicked:
                        break
                except Exception:
                    continue

            if not placeholder_clicked:
                _screenshot("no_placeholder")
                return (False, "Could not find main composer (only comment boxes found)")

            # Wait for composer to open (dialog or expanded form)
            time.sleep(random.uniform(2.0, 3.0))

            # ── STEP 3: Find the real contenteditable textbox ────────────
            # Real composer has aria-placeholder "Створіть публічний допис..." 
            # and data-lexical-editor="true"
            post_box = None
            textbox_xpaths = [
                # Most specific: lexical editor with role=textbox
                "//div[@role='textbox' and @contenteditable='true' and @data-lexical-editor='true']",
                # By aria-placeholder text (multilingual)
                "//div[@contenteditable='true' and contains(@aria-placeholder,'Створіть')]",
                "//div[@contenteditable='true' and contains(@aria-placeholder,'Создайте')]",
                "//div[@contenteditable='true' and contains(@aria-placeholder,'Create')]",
                "//div[@contenteditable='true' and @aria-placeholder]",
                # Inside dialog
                "//div[@role='dialog']//div[@contenteditable='true' and @role='textbox']",
                "//div[@role='dialog']//div[@contenteditable='true']",
                # Inside form
                "//form//div[@contenteditable='true' and @role='textbox']",
                "//form//div[@contenteditable='true']",
                # Generic fallback
                "//div[@contenteditable='true' and contains(@class,'notranslate')]",
                "//div[@contenteditable='true']",
            ]

            # First wait briefly for composer to render
            time.sleep(1.0)

            # Find textbox NOT inside an article (= not a comment field)
            for xpath in textbox_xpaths:
                try:
                    candidates = driver.find_elements(By.XPATH, xpath)
                    if not candidates:
                        continue

                    for el in candidates:
                        # Skip if inside an article (= comment box on someone's post)
                        is_in_article = driver.execute_script("""
                            let node = arguments[0];
                            while (node && node !== document.body) {
                                if (node.getAttribute && node.getAttribute('role') === 'article') {
                                    return true;
                                }
                                node = node.parentElement;
                            }
                            return false;
                        """, el)

                        if is_in_article:
                            continue

                        # Verify visible
                        is_visible = driver.execute_script("""
                            const r = arguments[0].getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        """, el)
                        if not is_visible:
                            continue

                        post_box = el
                        logger.info(f"✅ Found composer textbox via: {xpath[:60]}")
                        break

                    if post_box:
                        break
                except Exception:
                    continue

            if not post_box:
                _screenshot("no_textbox")
                return (False, "Composer textbox not found (only comment fields available)")

            # Click to focus
            try:
                driver.execute_script("arguments[0].click();", post_box)
                time.sleep(0.5)
            except Exception:
                pass

            # ── STEP 4: Insert text ───────────────────────────────────────
            # Lexical (FB editor) is tricky:
            # - send_keys fails on non-BMP chars (emojis)
            # - CDP Input.insertText needs the element to be ACTUALLY focused
            # - textContent verification gives false positives (placeholder counts)
            # We click into the textbox to set focus, then use the most reliable strategy

            def _real_text_in_lexical() -> str:
                """Get real user-entered text from Lexical editor (excludes placeholder)"""
                try:
                    return driver.execute_script("""
                        const el = arguments[0];
                        // Lexical wraps content in <p> tags — get text from those only
                        const ps = el.querySelectorAll('p');
                        if (ps.length === 0) return '';
                        let text = '';
                        ps.forEach(p => {
                            // Skip empty placeholder paragraphs (only contain <br>)
                            const inner = p.innerHTML.trim();
                            if (inner === '<br>' || inner === '') return;
                            text += p.textContent + '\\n';
                        });
                        return text.trim();
                    """, post_box) or ""
                except Exception:
                    return ""

            def _verify_text_present(min_chars: int = 10) -> bool:
                actual = _real_text_in_lexical()
                logger.info(f"🔍 Verify: textbox now contains {len(actual)} chars: {actual[:60]!r}")
                return len(actual) >= min_chars

            # ─── Focus the textbox properly ─────────────────────────────────
            # Multiple click attempts because Lexical needs real focus
            try:
                from selenium.webdriver.common.action_chains import ActionChains
                # Move + click via ActionChains (more like a real user)
                ActionChains(driver).move_to_element(post_box).click().perform()
                time.sleep(0.4)
            except Exception as e:
                logger.warning(f"ActionChains click failed: {e}")
                try:
                    post_box.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", post_box)

            time.sleep(0.6)

            pasted = False

            # ─── Strategy A: synthetic paste event (BEST for Lexical) ─────────
            # Lexical has a paste handler that processes ClipboardEvent natively
            try:
                driver.execute_script("""
                    const el = arguments[0];
                    const text = arguments[1];
                    el.focus();
                    // Click to make sure cursor is in
                    const dt = new DataTransfer();
                    dt.setData('text/plain', text);
                    dt.setData('text', text);
                    const evt = new ClipboardEvent('paste', {
                        clipboardData: dt,
                        bubbles: true,
                        cancelable: true,
                    });
                    el.dispatchEvent(evt);
                """, post_box, text)
                time.sleep(1.5)
                if _verify_text_present():
                    pasted = True
                    logger.info("📋 Text inserted via synthetic paste event")
                else:
                    logger.warning("⚠️ paste event did not insert text")
            except Exception as e:
                logger.warning(f"paste event failed: {e}")

            # ─── Strategy B: pyperclip + real Ctrl+V via CDP ─────────────────
            if not pasted:
                try:
                    import pyperclip
                    pyperclip.copy(text)
                    time.sleep(0.4)
                    # Click on the textbox via ActionChains (real focus)
                    try:
                        ActionChains(driver).move_to_element(post_box).click().perform()
                    except Exception:
                        driver.execute_script("arguments[0].click();", post_box)
                    time.sleep(0.4)
                    # Send Ctrl+V via CDP (no BMP limit, real keystroke)
                    driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
                        "type": "keyDown", "modifiers": 2,  # 2 = Ctrl
                        "key": "v", "code": "KeyV",
                        "windowsVirtualKeyCode": 86, "nativeVirtualKeyCode": 86,
                    })
                    time.sleep(0.1)
                    driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
                        "type": "keyUp", "modifiers": 2,
                        "key": "v", "code": "KeyV",
                        "windowsVirtualKeyCode": 86, "nativeVirtualKeyCode": 86,
                    })
                    time.sleep(1.5)
                    if _verify_text_present():
                        pasted = True
                        logger.info("📋 Text pasted via clipboard + CDP Ctrl+V")
                except Exception as e:
                    logger.warning(f"CDP Ctrl+V failed: {e}")

            # ─── Strategy C: CDP Input.insertText (last resort) ────────────────
            if not pasted:
                try:
                    # Refocus before typing
                    try:
                        ActionChains(driver).move_to_element(post_box).click().perform()
                    except Exception:
                        pass
                    time.sleep(0.3)
                    driver.execute_cdp_cmd("Input.insertText", {"text": text})
                    time.sleep(2.0)
                    if _verify_text_present():
                        pasted = True
                        logger.info("⌨️ Text inserted via CDP Input.insertText")
                except Exception as e:
                    logger.warning(f"CDP insertText failed: {e}")

            if not pasted:
                _screenshot("text_not_inserted")
                logger.error("❌ All text insertion strategies failed")
                return (False, "Could not insert text into composer")

            time.sleep(random.uniform(1.0, 2.0))

            time.sleep(random.uniform(1.0, 2.0))

            # ── STEP 5: Upload photo ──────────────────────────────────────
            if photo_path:
                # Resolve path — handle both /uploads/... URL and filesystem path
                abs_path = photo_path
                if photo_path.startswith("/uploads/"):
                    base = os.path.dirname(os.path.dirname(__file__))
                    abs_path = os.path.join(base, "data", "uploads", os.path.basename(photo_path))
                elif not os.path.isabs(photo_path):
                    abs_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), photo_path)

                if os.path.exists(abs_path):
                    uploaded = False
                    # First try to click "Photo/Video" button to reveal file input
                    for photo_btn_xpath in [
                        # Direct file input (best — bypasses button click)
                        "//div[@role='dialog']//input[@type='file' and contains(@accept,'image')]",
                        "//input[@type='file' and contains(@accept,'image')]",
                        "//input[@type='file' and contains(@accept,'photo')]",
                        # Photo/Video button by aria-label
                        "//div[@aria-label='Фото/відео']",
                        "//div[@aria-label='Photo/video']",
                        "//div[@aria-label='Фото/видео']",
                        "//div[@aria-label='Додати фото/відео']",
                        "//div[@aria-label='Add photo/video']",
                        # By image icon inside dialog (the webp icon you mentioned)
                        "//div[@role='dialog']//div[@role='button' and .//img[contains(@src,'8_VnccIZfRa') or contains(@src,'.webp')]]",
                        # Span text fallback
                        "//span[contains(text(),'Фото') and contains(text(),'відео')]/ancestor::div[@role='button'][1]",
                        "//span[contains(text(),'Photo') and contains(text(),'video')]/ancestor::div[@role='button'][1]",
                    ]:
                        try:
                            el = driver.find_element(By.XPATH, photo_btn_xpath)
                            if el.tag_name == "input":
                                el.send_keys(os.path.abspath(abs_path))
                                uploaded = True
                                logger.info(f"🖼 Photo uploaded directly: {abs_path}")
                                break
                            else:
                                el.click()
                                time.sleep(1.5)
                                # Now find file input
                                inputs = driver.find_elements(By.XPATH, "//input[@type='file']")
                                for inp in inputs:
                                    try:
                                        inp.send_keys(os.path.abspath(abs_path))
                                        uploaded = True
                                        logger.info(f"🖼 Photo uploaded after button click: {abs_path}")
                                        break
                                    except Exception:
                                        continue
                                if uploaded:
                                    break
                        except Exception:
                            continue

                    if not uploaded:
                        # Last resort: try any file input
                        inputs = driver.find_elements(By.XPATH, "//input[@type='file']")
                        for inp in inputs:
                            try:
                                inp.send_keys(os.path.abspath(abs_path))
                                uploaded = True
                                logger.info(f"🖼 Photo uploaded via fallback input")
                                break
                            except Exception:
                                continue

                    if uploaded:
                        time.sleep(random.uniform(3.0, 5.0))  # wait for upload
                    else:
                        logger.warning(f"⚠️ Could not upload photo: {abs_path}")
                else:
                    logger.warning(f"⚠️ Photo file not found: {abs_path}")

            # ── STEP 6: Click Publish ─────────────────────────────────────
            time.sleep(1.0)
            publish_clicked = False

            # Search publish button INSIDE the dialog ONLY — never use Ctrl+Enter
            # because that submits a comment if focus is wrong.
            publish_xpaths = [
                # Inside dialog with aria-label
                "//div[@role='dialog']//div[@aria-label='Опублікувати']",
                "//div[@role='dialog']//div[@aria-label='Опубликовать']",
                "//div[@role='dialog']//div[@aria-label='Post']",
                "//div[@role='dialog']//div[@aria-label='Publish']",
                # Inside dialog with text
                "//div[@role='dialog']//span[normalize-space(text())='Опублікувати']/ancestor::div[@role='button'][1]",
                "//div[@role='dialog']//span[normalize-space(text())='Опубликовать']/ancestor::div[@role='button'][1]",
                "//div[@role='dialog']//span[normalize-space(text())='Publish']/ancestor::div[@role='button'][1]",
                "//div[@role='dialog']//span[normalize-space(text())='Post']/ancestor::div[@role='button'][1]",
                # Fallback: aria-label outside dialog (but still verify it's not a post action)
                "//div[@aria-label='Опублікувати']",
                "//div[@aria-label='Опубликовать']",
                "//div[@aria-label='Publish']",
            ]

            for xpath in publish_xpaths:
                try:
                    candidates = driver.find_elements(By.XPATH, xpath)
                    for btn in candidates:
                        # Skip if inside an article (would be a comment Submit)
                        is_in_article = driver.execute_script("""
                            let node = arguments[0];
                            while (node && node !== document.body) {
                                if (node.getAttribute && node.getAttribute('role') === 'article') return true;
                                node = node.parentElement;
                            }
                            return false;
                        """, btn)
                        if is_in_article:
                            continue

                        # Make sure visible and clickable
                        is_visible = driver.execute_script("""
                            const r = arguments[0].getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        """, btn)
                        if not is_visible:
                            continue

                        driver.execute_script("arguments[0].click();", btn)
                        publish_clicked = True
                        logger.info(f"✅ Publish clicked via: {xpath[:55]}")
                        time.sleep(random.uniform(2.5, 3.5))
                        break

                    if publish_clicked:
                        break
                except Exception:
                    continue

            if not publish_clicked:
                # No Ctrl+Enter fallback — too risky, posts as comment
                _screenshot("no_publish_btn")
                logger.error("❌ Could not find Publish button — aborting to prevent comment-spam")
                return (False, "Publish button not found in composer dialog")

            # ── STEP 7: Verify ────────────────────────────────────────────
            time.sleep(1.0)
            cur = driver.current_url.lower()
            if "checkpoint" in cur:
                return (False, "Account checkpoint after posting")

            return (True, "Posted successfully")

        except Exception as e:
            _screenshot("exception")
            logger.error(f"❌ Post error [{group_id_str}]: {e}")
            return (False, str(e))

    def _dismiss_popups(self, driver: Chrome, max_steps: int = 5) -> None:
        """
        Dismiss any popups/modals that appear after navigating to a group.
        Handles multi-step rules popups (progress bar + Далі x N times),
        cookie banners, and any other blocking dialogs.
        """
        from selenium.webdriver.common.keys import Keys as _Keys

        # Selectors ordered by priority
        dismiss_xpaths = [
            # X close button
            "//div[@role='dialog']//*[@aria-label='Закрити']",
            "//div[@role='dialog']//*[@aria-label='Close']",
            # Далі / Next / Продовжити (multi-step rules)
            "//div[@role='dialog']//div[@role='button']//span[normalize-space(text())='Далі']",
            "//div[@role='dialog']//div[@role='button']//span[normalize-space(text())='Next']",
            "//div[@role='dialog']//div[@role='button']//span[normalize-space(text())='Продовжити']",
            "//div[@role='dialog']//div[@role='button']//span[normalize-space(text())='Continue']",
            # Будь-яка кнопка в діалозі (last resort)
            "//div[@role='dialog']//div[@role='button'][last()]",
        ]

        for step in range(max_steps):
            dismissed = False
            for xpath in dismiss_xpaths:
                try:
                    el = WebDriverWait(driver, 2).until(
                        EC.element_to_be_clickable((By.XPATH, xpath))
                    )
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                    driver.execute_script("arguments[0].click();", el)
                    logger.info(f"🚪 Popup step {step+1}: clicked {xpath[:55]}")
                    time.sleep(1.2)
                    dismissed = True
                    break
                except Exception:
                    continue

            if not dismissed:
                # Try Escape as last resort
                try:
                    from selenium.webdriver.common.action_chains import ActionChains
                    ActionChains(driver).send_keys(_Keys.ESCAPE).perform()
                    time.sleep(0.8)
                except Exception:
                    pass
                break  # No more popups found


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
                          profile_id: str = None,
                          stop_flag: threading.Event = None) -> dict:
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
                if stop_flag and stop_flag.is_set():
                    logger.info("⏹ Stop requested, ending posting round")
                    break
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
                    for s_ in range(delay):
                        if stop_flag and stop_flag.is_set():
                            logger.info("⏹ Stop during delay")
                            break
                        time.sleep(1)
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
    
    def run_multiple_accounts_with_vacancies(self, assignments: list,
                                              stop_flag: threading.Event = None) -> dict:
        """
        Run multiple accounts, each with its own vacancy.
        
        Args:
            assignments: List of dicts:
                [{"account_id": 1, "vacancy_id": 1, "groups_per_batch": 10, "batches": 10}, ...]
        """
        all_results = []
        for assignment in assignments:
            if stop_flag and stop_flag.is_set():
                logger.info("⏹ Stop requested between accounts")
                break
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
                stop_flag=stop_flag,
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
    
    def run_all_accounts(self, vacancy_id: int,
                         stop_flag: threading.Event = None) -> dict:
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
        Collect group URLs from the user's Facebook groups.
        Uses specific URLs:
        1. https://www.facebook.com/groups/?category=membership
        2. https://www.facebook.com/groups/joins/
        
        Collects only direct group URLs: facebook.com/groups/NUMBER/
        NOT feed, joins, search etc.
        """
        group_urls = []
        
        try:
            # Try multiple URLs to find the groups list
            urls_to_try = [
                "https://www.facebook.com/groups/?category=membership",
                "https://www.facebook.com/groups/joins/?nav_source=tab&ordering=viewer_added",
            ]
            
            for target_url in urls_to_try:
                if len(group_urls) >= max_groups:
                    break
                    
                logger.info(f"Navigating to: {target_url}")
                try:
                    driver.get(target_url)
                    time.sleep(random.uniform(4.0, 6.0))
                except Exception as e:
                    logger.warning(f"Navigation failed: {e}")
                    continue
                
                current = driver.current_url.lower()
                if "checkpoint" in current:
                    return []
                if "login" in current:
                    return []
                
                logger.info(f"Current URL: {current}")
                
                # Take screenshot to help debug
                try:
                    from datetime import datetime
                    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                    driver.save_screenshot(f"logs/collect_groups_{ts}.png")
                except:
                    pass
                
                # Scroll and collect
                seen_ids = set()
                for scroll in range(15):
                    # Find ALL links on the page
                    links = driver.find_elements(By.TAG_NAME, "a")
                    
                    for link in links:
                        try:
                            href = link.get_attribute("href")
                            if not href:
                                continue
                            if not href.startswith("https://www.facebook.com/groups/"):
                                continue
                            
                            rest = href.replace("https://www.facebook.com/groups/", "").split('?')[0].split('/')[0]
                            
                            excluded = ['feed', 'joins', 'search', 'discover', 'manage',
                                       'create', 'saved', 'invite', 'requests', 'pending',
                                       'member', 'members', 'about', 'photos', 'videos',
                                       'files', 'events', 'topics']
                            
                            if rest and rest not in excluded and not rest.startswith('?') and not rest.startswith('#'):
                                if rest not in seen_ids:
                                    seen_ids.add(rest)
                                    clean_url = f"https://www.facebook.com/groups/{rest}/"
                                    group_urls.append(clean_url)
                        except:
                            pass
                    
                    if len(group_urls) >= max_groups:
                        break
                    
                    # Scroll
                    driver.execute_script("""
                        const main = document.querySelector('[role="main"]');
                        if (main) main.scrollTo(0, main.scrollHeight);
                        else window.scrollTo(0, document.body.scrollHeight);
                    """)
                    time.sleep(random.uniform(2.0, 3.0))
            
            logger.info(f"📋 Collected {len(group_urls)} group URLs from profile")
            
        except Exception as e:
            logger.error(f"Error collecting groups: {e}")
        
        # Deduplicate and limit
        unique = list(dict.fromkeys(group_urls))
        return unique[:max_groups]
    
    def run_posting_from_profile(self, account_id: int, vacancy_id: int,
                                  max_posts: int = 30,
                                  profile_id: str = None,
                                  stop_flag: threading.Event = None) -> dict:
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
            # Step 1: Check driver is alive
            try:
                _ = driver.current_url
            except Exception:
                logger.warning("Driver is stale, re-opening profile...")
                try:
                    self.client.close_profile(profile_id)
                except:
                    pass
                time.sleep(2)
                driver = self.client.open_profile_and_get_driver(profile_id)
                if not driver:
                    return {"status": "error", "message": "Failed to re-open ixBrowser profile"}
            
            # Step 2: Collect groups from profile
            logger.info("🔍 Collecting groups from profile...")
            group_urls = self._collect_groups_from_profile(driver, max_groups=max_posts)
            
            if not group_urls:
                return {"status": "error", "message": "No groups found in profile"}
            
            logger.info(f"📋 Found {len(group_urls)} groups. Starting posting...")
            
            # Step 2: Post to each group
            for idx, group_url in enumerate(group_urls):
                if stop_flag and stop_flag.is_set():
                    logger.info("⏹ Stop requested, ending profile posting")
                    break
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
                    for s_ in range(delay):
                        if stop_flag and stop_flag.is_set():
                            logger.info("⏹ Stop during delay")
                            break
                        time.sleep(1)
            
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

    def _get_unposted_groups(self, db, account_id: int, vacancy_id: int) -> list:
        """
        Get all groups from DB that this account hasn't posted to for this vacancy yet.
        No limit — returns ALL remaining groups so we can post to all of them.
        """
        already_posted_ids = db.query(PostingLog.group_id).filter(
            PostingLog.account_id == account_id,
            PostingLog.vacancy_id == vacancy_id,
            PostingLog.status == "success",
        ).all()
        posted_ids = {p[0] for p in already_posted_ids}

        if posted_ids:
            groups = db.query(Group).filter(~Group.id.in_(posted_ids)).all()
        else:
            groups = db.query(Group).all()

        return groups

    def run_posting_from_db(
        self,
        account_id: int,
        vacancy_id: int,
        profile_id: str = None,
        delay_min: int = None,
        delay_max: int = None,
        stop_flag: threading.Event = None,
    ) -> dict:
        """
        Post vacancy to ALL groups in the database, one by one.

        - Takes groups from DB (not from FB page)
        - Skips groups already posted to (for this account + vacancy)
        - Stops automatically when all groups are done
        - Logs every result to PostingLog
        - Marks account as banned if checkpoint detected

        Returns summary dict.
        """
        delay_min = delay_min or self.MIN_DELAY_BETWEEN_POSTS
        delay_max = delay_max or self.MAX_DELAY_BETWEEN_POSTS

        # Load account + vacancy
        db = SessionLocal()
        account = db.query(Account).filter(Account.id == account_id).first()
        vacancy = db.query(Vacancy).filter(
            Vacancy.id == vacancy_id,
            Vacancy.is_active == True,
        ).first()

        if not account:
            db.close()
            return {"status": "error", "message": "Account not found"}
        if not vacancy:
            db.close()
            return {"status": "error", "message": "Vacancy not found or inactive"}

        pid = profile_id or account.ix_profile_id
        db.close()

        if not pid:
            return {"status": "error", "message": "No iXBrowser profile ID on this account"}

        # Open browser
        driver = self.client.open_profile_and_get_driver(pid)
        if not driver:
            return {"status": "error", "message": "Failed to open iXBrowser profile"}

        results = {
            "status": "success",
            "total": 0,
            "successful": 0,
            "failed": 0,
            "skipped": 0,
            "details": [],
        }

        try:
            # Get groups not yet posted to
            db = SessionLocal()
            groups = self._get_unposted_groups(db, account_id, vacancy_id)
            db.close()

            if not groups:
                logger.info(f"✅ Account #{account_id}: all groups already posted to for vacancy #{vacancy_id}")
                results["status"] = "done"
                results["message"] = "All groups already posted"
                return results

            logger.info(f"📋 Account #{account_id}: {len(groups)} groups to post to")

            for idx, group in enumerate(groups):
                if stop_flag and stop_flag.is_set():
                    logger.info("⏹️ Stop requested")
                    results["status"] = "stopped"
                    break

                # Check daily limit
                db = SessionLocal()
                limit_ok = self._check_daily_limit(db, account_id)
                db.close()
                if not limit_ok:
                    logger.info(f"⛔️ Daily limit reached for account #{account_id}")
                    results["status"] = "limit_reached"
                    break

                logger.info(f"📨 ({idx+1}/{len(groups)}) → {group.url}")

                # Check driver still alive
                if not self.client.is_driver_alive(driver):
                    logger.warning("Driver died, reopening profile...")
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = self.client.open_profile_and_get_driver(pid)
                    if not driver:
                        results["status"] = "error"
                        results["message"] = "Driver lost and could not reopen profile"
                        break

                success, message = self._post_to_group(
                    driver=driver,
                    group_url=group.url,
                    text=vacancy.description,
                    photo_path=vacancy.photo_path,
                )

                results["total"] += 1
                results["details"].append({
                    "group_id": group.id,
                    "group_url": group.url,
                    "success": success,
                    "message": message,
                })

                # Save to DB
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

                    # Check for ban/checkpoint
                    if any(w in message.lower() for w in ["checkpoint", "restricted", "blocked", "banned"]):
                        acc = db.query(Account).filter(Account.id == account_id).first()
                        if acc:
                            acc.status = "banned"
                            acc.banned_at = datetime.utcnow()
                            db.commit()
                        db.close()
                        results["status"] = "banned"
                        results["message"] = message
                        logger.error(f"🚫 Account #{account_id} banned/restricted: {message}")
                        return results
                db.close()

                # Last group — no delay needed
                if idx == len(groups) - 1:
                    break

                # Delay between posts
                delay = random.randint(delay_min, delay_max)
                logger.info(f"⏳ {delay}s before next group...")
                for _ in range(delay):
                    if stop_flag and stop_flag.is_set():
                        break
                    time.sleep(1)

            # If we exited the loop normally (not via break), all done
            if not (stop_flag and stop_flag.is_set()):
                logger.info(
                    f"✅ Account #{account_id}: finished! "
                    f"{results['successful']} posted, {results['failed']} failed"
                )

        except Exception as e:
            logger.error(f"run_posting_from_db error: {e}")
            results["status"] = "error"
            results["message"] = str(e)
        finally:
            try:
                self.client.close_profile(pid)
                driver.quit()
            except Exception:
                pass

        return results

    # ---- Group deduplication ----

    @staticmethod
    def dedup_groups_in_db() -> dict:
        """
        Remove duplicate groups from DB (same URL).
        Keeps the oldest record, deletes newer duplicates.
        Also normalises URLs (trailing slash, no query params).
        Returns {"removed": N, "normalised": M}
        """
        db = SessionLocal()
        removed = 0
        normalised = 0

        try:
            all_groups = db.query(Group).all()

            # Step 1: normalise URLs
            for g in all_groups:
                clean = g.url.split("?")[0].rstrip("/") + "/"
                if clean != g.url:
                    g.url = clean
                    normalised += 1
            db.commit()

            # Step 2: find duplicates
            from collections import defaultdict
            url_map = defaultdict(list)
            for g in all_groups:
                url_map[g.url].append(g)

            for url, dupes in url_map.items():
                if len(dupes) <= 1:
                    continue
                # Keep oldest (smallest id), delete rest
                dupes.sort(key=lambda x: x.id)
                for dup in dupes[1:]:
                    db.delete(dup)
                    removed += 1

            db.commit()
            logger.info(f"🧹 Dedup: removed {removed} duplicates, normalised {normalised} URLs")

        except Exception as e:
            logger.error(f"Dedup error: {e}")
            db.rollback()
        finally:
            db.close()

        return {"removed": removed, "normalised": normalised}
