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
