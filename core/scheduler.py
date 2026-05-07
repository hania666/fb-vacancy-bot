#!/usr/bin/env python3
"""Scheduler for automatic warmup and posting"""

import logging
import random
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.combining import OrTrigger

from core.database import SessionLocal, Account
from core.ixbrowser_client import IXBrowserClient
from core.warmup import run_warmup_session

logger = logging.getLogger(__name__)


class BotScheduler:
    """Manages scheduled warmup and posting tasks"""

    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self._running = False
        self._jobs = {}  # name -> job info

    def start(self):
        """Start the scheduler"""
        if self._running:
            return
        self.scheduler.start()
        self._running = True
        logger.info("⏰ Scheduler started")

    def stop(self):
        """Stop the scheduler"""
        if not self._running:
            return
        self.scheduler.shutdown(wait=False)
        self._running = False
        logger.info("⏰ Scheduler stopped")

    # ---- Warmup Scheduling ----

    def add_warmup_schedule(self, account_id: int, hour: int, minute: int):
        """Schedule daily warmup for a specific account at HH:MM"""
        job_id = f"warmup_{account_id}_{hour}_{minute}"

        self.scheduler.add_job(
            func=self._run_warmup_job,
            trigger=CronTrigger(hour=hour, minute=minute, timezone="Europe/Warsaw"),
            args=[account_id],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=300,
        )
        self._jobs[job_id] = {
            "type": "warmup",
            "account_id": account_id,
            "schedule": f"{hour:02d}:{minute:02d}",
            "description": f"🔥 Прогрев аккаунта #{account_id} в {hour:02d}:{minute:02d}",
        }
        logger.info(f"📅 Scheduled warmup for account #{account_id} at {hour:02d}:{minute:02d}")

    def add_warmup_schedule_for_all(self, hour: int, minute: int):
        """Schedule daily warmup for all ready/warming accounts"""
        db = SessionLocal()
        accounts = db.query(Account).filter(
            Account.status.in_(["new", "warming", "ready"])
        ).all()
        db.close()

        for acc in accounts:
            self.add_warmup_schedule(acc.id, hour, minute)

    def add_daily_cycle(self, account_id: int,
                        warmup_hour: int, warmup_minute: int,
                        post_hour_start: int, post_minute_start: int,
                        post_hour_end: int, post_minute_end: int,
                        vacancy_id: int):
        """
        Add a daily warmup + posting cycle with human-like randomization.
        warmup runs AT warmup_hour:warmup_minute (exact start)
        posting runs in a random window between post_hour_start:post_minute_start 
        and post_hour_end:post_minute_end (15-30 min after warmup ends)
        """
        # Warmup - exact time
        warmup_job_id = f"warmup_{account_id}_{warmup_hour}_{warmup_minute}"
        self.scheduler.add_job(
            func=self._run_warmup_job,
            trigger=CronTrigger(hour=warmup_hour, minute=warmup_minute, timezone="Europe/Warsaw"),
            args=[account_id],
            id=warmup_job_id,
            replace_existing=True,
            misfire_grace_time=300,
        )
        self._jobs[warmup_job_id] = {
            "type": "warmup",
            "account_id": account_id,
            "schedule": f"{warmup_hour:02d}:{warmup_minute:02d}",
            "description": f"🔥 Прогрев акк #{account_id} в {warmup_hour:02d}:{warmup_minute:02d}",
        }

        # Posting - random time in window
        # We'll randomize at creation time and recreate daily
        post_job_id = f"post_{account_id}_{warmup_hour}_{warmup_minute}"
        self._add_randomized_posting(account_id, vacancy_id,
                                      post_hour_start, post_minute_start,
                                      post_hour_end, post_minute_end,
                                      post_job_id)
        logger.info(
            f"📅 Daily cycle for #{account_id}: warmup at {warmup_hour:02d}:{warmup_minute:02d}, "
            f"post between {post_hour_start:02d}:{post_minute_start:02d}-{post_hour_end:02d}:{post_minute_end:02d}"
        )

    def _add_randomized_posting(self, account_id: int, vacancy_id: int,
                                 hour_start: int, min_start: int,
                                 hour_end: int, min_end: int,
                                 job_id: str):
        """Add posting with time randomized within a window"""
        from core.posting_engine import PostingEngine

        # Randomize the exact minute for this schedule
        start_total = hour_start * 60 + min_start
        end_total = hour_end * 60 + min_end
        if end_total <= start_total:
            end_total = start_total + 30  # at least 30 min window
        
        random_minute = random.randint(start_total, end_total)
        rand_hour = random_minute // 60
        rand_min = random_minute % 60

        self.scheduler.add_job(
            func=self._run_posting_job,
            trigger=CronTrigger(hour=rand_hour, minute=rand_min, timezone="Europe/Warsaw"),
            args=[vacancy_id, [account_id]],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=600,
        )
        self._jobs[job_id] = {
            "type": "post",
            "account_id": account_id,
            "vacancy_id": vacancy_id,
            "schedule": f"{rand_hour:02d}:{rand_min:02d}",
            "description": f"📨 Рассылка акк #{account_id} ≈{rand_hour:02d}:{rand_min:02d} (окно {hour_start:02d}:{min_start:02d}-{hour_end:02d}:{min_end:02d})",
        }
        logger.info(f"  → Пост рандомизирован на {rand_hour:02d}:{rand_min:02d}")

    def add_three_cycles(self, account_id: int, vacancy_id: int):
        """Add 3 daily warmup+post cycles (human schedule)"""
        cycles = [
            (10, 0, 10, 30, 11, 0),    # Прогрев 10:00 → пост 10:30-11:00
            (14, 0, 14, 30, 15, 0),     # Прогрев 14:00 → пост 14:30-15:00
            (19, 30, 20, 0, 20, 30),    # Прогрев 19:30 → пост 20:00-20:30
        ]
        for warm_h, warm_m, post_hs, post_ms, post_he, post_me in cycles:
            self.add_daily_cycle(
                account_id=account_id,
                warmup_hour=warm_h, warmup_minute=warm_m,
                post_hour_start=post_hs, post_minute_start=post_ms,
                post_hour_end=post_he, post_minute_end=post_me,
                vacancy_id=vacancy_id,
            )

    def _run_warmup_job(self, account_id: int):
        """Internal: run warmup for one account"""
        logger.info(f"⏰ Scheduler: starting warmup for account #{account_id}")

        db = SessionLocal()
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account or not account.ix_profile_id:
            db.close()
            logger.warning(f"Account #{account_id} not found or no profile ID")
            return

        profile_id = account.ix_profile_id
        db.close()

        # Update status to warming
        db = SessionLocal()
        acc = db.query(Account).filter(Account.id == account_id).first()
        if acc and acc.status == "new":
            acc.status = "warming"
            acc.warmup_started_at = datetime.utcnow()
        db.commit()
        db.close()

        # Run warmup (non-blocking in thread)
        def warmup_thread():
            try:
                result = run_warmup_session(
                    account_id=account_id,
                    profile_id=profile_id,
                    duration_minutes=15,
                )
                logger.info(f"✅ Warmup for #{account_id}: {result['status']}")

                # Check if warmed enough (after 3 days of warming, mark as ready)
                db = SessionLocal()
                acc = db.query(Account).filter(Account.id == account_id).first()
                if acc and acc.warmup_started_at:
                    days_warming = (datetime.utcnow() - acc.warmup_started_at).days
                    if days_warming >= 3:
                        acc.status = "ready"
                        acc.warmed_at = datetime.utcnow()
                        logger.info(f"✅ Account #{account_id} marked as READY after {days_warming} days")
                db.commit()
                db.close()
            except Exception as e:
                logger.error(f"Warmup failed for #{account_id}: {e}")

        thread = threading.Thread(target=warmup_thread, daemon=True)
        thread.start()

    # ---- Posting Scheduling ----

    def add_posting_schedule(self, vacancy_id: int, hour: int, minute: int,
                             account_ids: list = None):
        """Schedule daily posting at HH:MM for given accounts (or all ready)"""
        job_id = f"post_{vacancy_id}_{hour}_{minute}"

        self.scheduler.add_job(
            func=self._run_posting_job,
            trigger=CronTrigger(hour=hour, minute=minute, timezone="Europe/Warsaw"),
            args=[vacancy_id, account_ids],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=300,
        )
        target = f"все ready акки" if account_ids is None else f"акки {account_ids}"
        self._jobs[job_id] = {
            "type": "post",
            "vacancy_id": vacancy_id,
            "schedule": f"{hour:02d}:{minute:02d}",
            "description": f"📨 Рассылка вакансии #{vacancy_id} в {hour:02d}:{minute:02d} ({target})",
        }
        logger.info(f"📅 Scheduled posting for vacancy #{vacancy_id} at {hour:02d}:{minute:02d}")

    def _run_posting_job(self, vacancy_id: int, account_ids: list = None):
        """Internal: run posting for accounts"""
        from core.posting_engine import PostingEngine

        logger.info(f"⏰ Scheduler: starting posting of vacancy #{vacancy_id}")

        engine = PostingEngine()

        def post_thread():
            try:
                if account_ids:
                    for acc_id in account_ids:
                        engine.run_posting_round(
                            account_id=acc_id,
                            vacancy_id=vacancy_id,
                            groups_per_batch=10,
                            batches=10,
                        )
                else:
                    engine.run_all_accounts(vacancy_id=vacancy_id)
            except Exception as e:
                logger.error(f"Posting failed: {e}")

        thread = threading.Thread(target=post_thread, daemon=True)
        thread.start()

    # ---- Manage Schedules ----

    def get_schedules(self) -> list:
        """Get all active schedules"""
        result = []
        for job_id, info in self._jobs.items():
            job = self.scheduler.get_job(job_id)
            result.append({
                "id": job_id,
                "description": info["description"],
                "type": info["type"],
                "schedule": info["schedule"],
                "next_run": str(job.next_run_time) if job else "unknown",
            })
        return result

    def remove_schedule(self, job_id: str) -> bool:
        """Remove a schedule by ID"""
        try:
            self.scheduler.remove_job(job_id)
            self._jobs.pop(job_id, None)
            logger.info(f"🗑 Removed schedule: {job_id}")
            return True
        except Exception as e:
            logger.warning(f"Failed to remove schedule {job_id}: {e}")
            return False

    def clear_all(self):
        """Remove all schedules"""
        for job_id in list(self._jobs.keys()):
            self.remove_schedule(job_id)


# Global scheduler instance
scheduler = BotScheduler()
