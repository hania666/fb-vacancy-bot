#!/usr/bin/env python3
"""
🕸 Collect Facebook groups you're a member of → save to DB + file

Usage:
    python tools/collect_my_groups.py --profile 1
    python tools/collect_my_groups.py --profile 1 --max 1000 --output groups.txt
    python tools/collect_my_groups.py --import-file groups.txt
"""

import os
import sys
import time
import random
import logging
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains

from core.ixbrowser_client import IXBrowserClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

EXCLUDED = {
    "feed", "joins", "search", "discover", "manage", "create",
    "saved", "invite", "requests", "pending", "member", "members",
    "about", "photos", "videos", "files", "events", "topics",
    "buy_and_sell", "marketplace", "permalink", "discussion",
}


def _extract_group_id(href: str):
    """Extract clean group ID from FB URL."""
    if not href or "/groups/" not in href:
        return None
    try:
        url = href.split("?")[0].split("#")[0].rstrip("/")
        parts = url.split("/groups/")
        if len(parts) < 2:
            return None
        rest = parts[1].split("/")[0]
        if not rest or rest in EXCLUDED or rest.startswith("_"):
            return None
        return rest
    except Exception:
        return None


def _collect_links(driver) -> set:
    """Grab all group URLs currently visible on the page."""
    found = set()
    links = driver.find_elements(By.TAG_NAME, "a")
    for link in links:
        try:
            href = link.get_attribute("href")
            gid = _extract_group_id(href)
            if gid:
                found.add(f"https://www.facebook.com/groups/{gid}/")
        except Exception:
            pass
    return found


def _do_scroll(driver, attempt: int):
    """
    Try multiple scroll strategies. FB sometimes needs different approaches.
    """
    strategy = attempt % 4

    if strategy == 0:
        # Standard window scroll — most reliable trigger for lazy loading
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")

    elif strategy == 1:
        # Scroll by fixed amount — sometimes triggers when scrollTo doesn't
        driver.execute_script("window.scrollBy(0, 1200);")

    elif strategy == 2:
        # Send END key to body — simulates real keyboard scroll
        try:
            body = driver.find_element(By.TAG_NAME, "body")
            body.send_keys(Keys.END)
        except Exception:
            driver.execute_script("window.scrollBy(0, 1500);")

    elif strategy == 3:
        # Scroll main container if exists
        driver.execute_script("""
            const candidates = [
                document.querySelector('[role="main"]'),
                document.querySelector('[data-pagelet="GroupsJoinedByViewer"]'),
                document.querySelector('[data-pagelet="GroupsFeed"]'),
            ];
            for (const el of candidates) {
                if (el && el.scrollHeight > window.innerHeight) {
                    el.scrollTop = el.scrollHeight;
                    break;
                }
            }
            window.scrollTo(0, document.body.scrollHeight);
        """)


def _page_height(driver) -> int:
    return driver.execute_script("return document.body.scrollHeight")


def collect_groups(driver, max_groups: int = 500) -> list:
    """Collect all group URLs from FB groups page."""

    urls_to_try = [
        "https://www.facebook.com/groups/joins/?nav_source=tab&ordering=viewer_added",
        "https://www.facebook.com/groups/feed/",
    ]

    for url in urls_to_try:
        logger.info(f"🌐 Navigating to: {url}")
        driver.get(url)
        time.sleep(random.uniform(5.0, 7.0))  # longer initial wait

        current = driver.current_url.lower()
        if "login" in current or "checkpoint" in current:
            logger.error("❌ Not logged in! Login in iXBrowser first.")
            return []

        if "groups" in current:
            logger.info(f"✅ On page: {current[:80]}")
            break

    # Initial collection before any scrolling
    all_urls = _collect_links(driver)
    logger.info(f"   Initial: {len(all_urls)} groups")

    no_new_streak = 0
    scroll_num = 0
    MAX_SCROLLS = 80
    MAX_NO_NEW = 8  # wait longer before giving up
    last_height = _page_height(driver)

    while scroll_num < MAX_SCROLLS and len(all_urls) < max_groups:
        scroll_num += 1
        prev_count = len(all_urls)

        # Scroll
        _do_scroll(driver, scroll_num)

        # Wait for content to load — longer wait every 5 scrolls
        if scroll_num % 5 == 0:
            wait = random.uniform(4.0, 6.0)
        else:
            wait = random.uniform(2.5, 3.5)
        time.sleep(wait)

        # Collect
        new_links = _collect_links(driver)
        all_urls.update(new_links)

        new_found = len(all_urls) - prev_count
        new_height = _page_height(driver)
        height_grew = new_height > last_height
        last_height = new_height

        logger.info(
            f"   Scroll {scroll_num:2d}: +{new_found:3d} new  |  "
            f"total: {len(all_urls)}  |  "
            f"page grew: {'✅' if height_grew else '—'}"
        )

        if new_found == 0 and not height_grew:
            no_new_streak += 1
        else:
            no_new_streak = 0

        if no_new_streak >= MAX_NO_NEW:
            logger.info(f"   ✅ No new content for {MAX_NO_NEW} scrolls — reached end")
            break

        # Every 10 scrolls scroll back up a bit then down (helps FB re-render)
        if scroll_num % 10 == 0 and scroll_num > 0:
            logger.info("   🔄 Scroll up briefly to help FB load more...")
            driver.execute_script("window.scrollBy(0, -800);")
            time.sleep(1.5)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(random.uniform(3.0, 5.0))

    return sorted(all_urls)


def save_to_db(urls: list) -> tuple:
    """Save URLs to database. Returns (added, skipped)."""
    try:
        from core.database import SessionLocal, Group, init_db
        init_db()
        db = SessionLocal()
        added = skipped = 0
        for url in urls:
            exists = db.query(Group).filter(Group.url == url).first()
            if exists:
                skipped += 1
            else:
                db.add(Group(url=url))
                added += 1
        db.commit()
        db.close()
        return added, skipped
    except Exception as e:
        logger.error(f"DB error: {e}")
        return 0, len(urls)


def save_to_file(urls: list, path: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(urls) + "\n")
    logger.info(f"💾 Saved {len(urls)} URLs → {path}")


def import_from_file(path: str) -> list:
    if not os.path.exists(path):
        logger.error(f"File not found: {path}")
        return []
    urls = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip().rstrip("/")
            if line.startswith("https://www.facebook.com/groups/"):
                urls.append(line + "/")
    return urls


def main():
    parser = argparse.ArgumentParser(
        description="Collect Facebook groups from your profile",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/collect_my_groups.py --profile 1
  python tools/collect_my_groups.py --profile 1 --max 1000 --output groups.txt
  python tools/collect_my_groups.py --import-file groups.txt
        """,
    )
    parser.add_argument("--profile", default="1", help="iXBrowser profile ID (default: 1)")
    parser.add_argument("--max", type=int, default=500, help="Max groups (default: 500)")
    parser.add_argument("--output", default="", help="Save to text file (optional)")
    parser.add_argument("--no-db", action="store_true", help="Don't save to database")
    parser.add_argument("--import-file", default="", help="Import file → DB only, no browser")
    args = parser.parse_args()

    # ── Mode: import from file only ─────────────────────────────────────────
    if args.import_file:
        logger.info(f"📂 Importing: {args.import_file}")
        urls = import_from_file(args.import_file)
        if not urls:
            logger.error("No valid URLs found")
            sys.exit(1)
        logger.info(f"Found {len(urls)} URLs")
        added, skipped = save_to_db(urls)
        logger.info(f"✅ DB: {added} added, {skipped} already existed")
        return

    # ── Mode: collect via browser ───────────────────────────────────────────
    logger.info("🔌 Connecting to iXBrowser...")
    client = IXBrowserClient()
    driver = client.open_profile_and_get_driver(args.profile)

    if not driver:
        logger.error("❌ Failed to open profile. Is iXBrowser running?")
        sys.exit(1)

    try:
        urls = collect_groups(driver, max_groups=args.max)

        logger.info("")
        logger.info("=" * 55)
        logger.info(f"  📋 COLLECTED: {len(urls)} groups")
        logger.info("=" * 55)

        if not urls:
            logger.warning("No groups found.")
            return

        # Save to file
        out_path = args.output
        if not out_path:
            out_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "data", f"groups_profile_{args.profile}.txt"
            )
        save_to_file(urls, out_path)

        # Save to DB
        if not args.no_db:
            added, skipped = save_to_db(urls)
            logger.info(f"🗄  DB: {added} new, {skipped} already existed")
        else:
            logger.info("⏭️  Skipping DB (--no-db)")

        # Preview
        logger.info(f"\nFirst 10 groups:")
        for i, url in enumerate(urls[:10], 1):
            logger.info(f"  {i:3d}. {url}")
        if len(urls) > 10:
            logger.info(f"  ... and {len(urls) - 10} more")

    finally:
        try:
            driver.quit()
        except Exception:
            pass
        logger.info("\n✅ Done!")


if __name__ == "__main__":
    main()
