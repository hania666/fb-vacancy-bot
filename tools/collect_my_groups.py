#!/usr/bin/env python3
"""
🕸 Collect Facebook groups you're a member of → save to DB + file

Usage:
    python tools/collect_my_groups.py --profile 1
    python tools/collect_my_groups.py --profile 1 --max 500 --output groups.txt --no-db
    python tools/collect_my_groups.py --profile 1 --import-file groups.txt

What it does:
    1. Opens iXBrowser profile
    2. Goes to facebook.com/groups/joins/ (your groups)
    3. Scrolls smartly (inside FB container, not just body)
    4. Collects all group URLs
    5. Saves to DB and/or text file
"""

import os
import sys
import time
import random
import logging
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from selenium.webdriver.common.by import By

from core.ixbrowser_client import IXBrowserClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Group URL segments to exclude
EXCLUDED = {
    "feed", "joins", "search", "discover", "manage", "create",
    "saved", "invite", "requests", "pending", "member", "members",
    "about", "photos", "videos", "files", "events", "topics",
    "buy_and_sell", "marketplace", "permalink", "discussion",
}


def _extract_group_id(href: str):
    """Extract clean group ID/slug from a Facebook URL."""
    if not href or "/groups/" not in href:
        return None
    try:
        # Remove query params and fragments
        url = href.split("?")[0].split("#")[0].rstrip("/")
        parts = url.split("/groups/")
        if len(parts) < 2:
            return None
        rest = parts[1].split("/")[0]  # take only first segment after /groups/
        if not rest or rest in EXCLUDED or rest.startswith("_"):
            return None
        return rest
    except Exception:
        return None


def _scroll_fb_container(driver) -> bool:
    """
    Scroll Facebook's internal groups container.
    FB renders content in a virtual scrollable div, not document.body.
    Returns True if page height grew (new content loaded).
    """
    old_height = driver.execute_script("""
        const selectors = [
            '[role="main"]',
            '[data-pagelet="GroupsFeed"]',
            '[data-pagelet="GroupsJoinedByViewer"]',
            'div[class*="html-div"][style*="overflow"]',
        ];
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el && el.scrollHeight > 500) {
                const old = el.scrollTop;
                el.scrollTop += 1800;
                if (el.scrollTop !== old) return el.scrollHeight;
            }
        }
        // Fallback: scroll window
        const before = document.body.scrollHeight;
        window.scrollBy(0, 1800);
        return before;
    """)

    time.sleep(random.uniform(2.5, 3.5))

    new_height = driver.execute_script("""
        const selectors = [
            '[role="main"]',
            '[data-pagelet="GroupsFeed"]',
            '[data-pagelet="GroupsJoinedByViewer"]',
        ];
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el && el.scrollHeight > 500) return el.scrollHeight;
        }
        return document.body.scrollHeight;
    """)

    return new_height > old_height


def collect_groups(driver, max_groups: int = 500) -> list:
    """
    Navigate to FB groups page and collect all group URLs.
    Returns sorted list of clean group URLs.
    """

    # Try the most reliable URL first
    urls_to_try = [
        "https://www.facebook.com/groups/joins/?nav_source=tab&ordering=viewer_added",
        "https://www.facebook.com/groups/feed/",
    ]

    loaded = False
    for url in urls_to_try:
        logger.info(f"🌐 Navigating to: {url}")
        driver.get(url)
        time.sleep(random.uniform(4.0, 6.0))

        current = driver.current_url.lower()
        if "login" in current or "checkpoint" in current:
            logger.error("❌ Account not logged in or checkpoint! Login in iXBrowser first.")
            return []
        if "groups" in current:
            logger.info(f"✅ On groups page: {current[:80]}")
            loaded = True
            break

    if not loaded:
        logger.warning("⚠️  Could not land on groups page, trying anyway...")

    all_urls = set()
    no_new_streak = 0
    scroll_num = 0
    MAX_SCROLLS = 60
    MAX_NO_NEW = 5

    logger.info("🔍 Starting collection...")

    while scroll_num < MAX_SCROLLS and len(all_urls) < max_groups:
        scroll_num += 1

        # Collect links visible on page
        prev_count = len(all_urls)
        links = driver.find_elements(By.TAG_NAME, "a")

        for link in links:
            try:
                href = link.get_attribute("href")
                gid = _extract_group_id(href)
                if gid:
                    all_urls.add(f"https://www.facebook.com/groups/{gid}/")
            except Exception:
                pass

        new_found = len(all_urls) - prev_count
        logger.info(f"   Scroll {scroll_num:2d}: {new_found:3d} new  |  total: {len(all_urls)}")

        if new_found == 0:
            no_new_streak += 1
        else:
            no_new_streak = 0

        if no_new_streak >= MAX_NO_NEW:
            logger.info("   ✅ No new groups for 5 scrolls — reached end")
            break

        grew = _scroll_fb_container(driver)
        if not grew and no_new_streak >= 2:
            logger.info("   ✅ Page stopped growing")
            break

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
    """Save URLs to text file, one per line."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(urls) + "\n")
    logger.info(f"💾 Saved {len(urls)} URLs → {path}")


def import_from_file(path: str) -> list:
    """Load URLs from text file."""
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
  python tools/collect_my_groups.py --profile 1 --max 1000 --output my_groups.txt
  python tools/collect_my_groups.py --import-file my_groups.txt   # import file → DB only
        """,
    )
    parser.add_argument("--profile", default="1", help="iXBrowser profile ID (default: 1)")
    parser.add_argument("--max", type=int, default=500, help="Max groups to collect (default: 500)")
    parser.add_argument("--output", default="", help="Save URLs to this text file (optional)")
    parser.add_argument("--no-db", action="store_true", help="Don't save to database")
    parser.add_argument("--import-file", default="", help="Import URLs from file to DB (skips browser)")
    args = parser.parse_args()

    # ── Mode: import file only ──────────────────────────────────────────────
    if args.import_file:
        logger.info(f"📂 Importing from file: {args.import_file}")
        urls = import_from_file(args.import_file)
        if not urls:
            logger.error("No valid URLs found in file")
            sys.exit(1)
        logger.info(f"Found {len(urls)} URLs in file")
        added, skipped = save_to_db(urls)
        logger.info(f"✅ DB: {added} added, {skipped} already existed")
        return

    # ── Mode: collect from browser ──────────────────────────────────────────
    logger.info("🔌 Connecting to iXBrowser...")
    client = IXBrowserClient()
    driver = client.open_profile_and_get_driver(args.profile)

    if not driver:
        logger.error("❌ Failed to open iXBrowser profile")
        logger.error("   Make sure iXBrowser is running")
        sys.exit(1)

    try:
        urls = collect_groups(driver, max_groups=args.max)

        logger.info("")
        logger.info("=" * 55)
        logger.info(f"  📋 COLLECTED: {len(urls)} groups")
        logger.info("=" * 55)

        if not urls:
            logger.warning("No groups found. Check that the account is in groups.")
            return

        # Save to file
        if args.output:
            save_to_file(urls, args.output)
        else:
            # Always save to default file as backup
            default_out = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "data", f"groups_profile_{args.profile}.txt"
            )
            save_to_file(urls, default_out)

        # Save to DB
        if not args.no_db:
            added, skipped = save_to_db(urls)
            logger.info(f"🗄  DB: {added} new groups added, {skipped} already existed")
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
