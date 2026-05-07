#!/usr/bin/env python3
"""🕸 Collect all Facebook group URLs from your profile and save to file

Usage:
    python tools/collect_my_groups.py --profile 1 --output groups.txt

This script:
1. Opens iXBrowser profile
2. Goes to your Facebook groups page
3. Scrolls down to load all groups
4. Collects every group URL
5. Saves to a text file (one URL per line)
6. Prints count
"""

import os
import sys
import time
import logging
import argparse

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from core.ixbrowser_client import IXBrowserClient

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)


def collect_groups(driver, max_groups: int = 200) -> list:
    """Collect all group URLs from the user's Facebook groups page."""
    
    # Navigate to My Groups page
    driver.get("https://www.facebook.com/groups/joins/?nav_source=tab&ordering=viewer_added")
    time.sleep(5)
    
    # Check login status
    current = driver.current_url.lower()
    if "login" in current:
        logger.error("❌ Not logged in! Open iXBrowser profile and login to FB first.")
        return []
    
    logger.info(f"✅ Logged in. Current URL: {current}")
    
    all_urls = set()
    no_new_count = 0
    
    for scroll in range(30):  # Max 30 scrolls
        # Collect all links
        links = driver.find_elements(By.TAG_NAME, "a")
        for link in links:
            try:
                href = link.get_attribute("href")
                if not href:
                    continue
                if not href.startswith("https://www.facebook.com/groups/"):
                    continue
                
                rest = href.replace("https://www.facebook.com/groups/", "").split('/')[0]
                
                # Only keep direct group URLs
                excluded = ['feed', 'joins', 'search', 'discover', 'manage', 
                           'create', 'saved', 'invite', 'requests', 'pending',
                           'member', 'members', 'about', 'photos', 'videos',
                           'files', 'events', 'topics', 'buy_and_sell']
                
                if rest and rest not in excluded and not rest.startswith('?'):
                    clean_url = f"https://www.facebook.com/groups/{rest}/"
                    all_urls.add(clean_url)
            except:
                pass
        
        logger.info(f"   Scroll {scroll+1}: {len(all_urls)} groups so far")
        
        if len(all_urls) >= max_groups:
            break
        
        # Scroll down
        old_height = driver.execute_script("return document.body.scrollHeight")
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(3)
        new_height = driver.execute_script("return document.body.scrollHeight")
        
        if new_height == old_height:
            no_new_count += 1
            if no_new_count >= 3:  # 3 scrolls without new content = end of page
                logger.info("   ✅ Reached end of page")
                break
        else:
            no_new_count = 0
    
    return sorted(all_urls)


def main():
    parser = argparse.ArgumentParser(description="Collect Facebook group URLs from your profile")
    parser.add_argument("--profile", default="1", help="iXBrowser profile ID (default: 1)")
    parser.add_argument("--output", default="groups.txt", help="Output file path")
    parser.add_argument("--max", type=int, default=200, help="Max groups to collect")
    
    args = parser.parse_args()
    
    logger.info("🔌 Connecting to iXBrowser...")
    client = IXBrowserClient()
    
    # Open or connect to profile
    logger.info(f"📱 Opening profile {args.profile}...")
    driver = client.open_profile_and_get_driver(args.profile)
    
    if not driver:
        logger.error("❌ Failed to open iXBrowser profile")
        logger.error("   Make sure iXBrowser is running on your PC")
        sys.exit(1)
    
    try:
        urls = collect_groups(driver, max_groups=args.max)
        
        logger.info(f"\n{'='*50}")
        logger.info(f"📋 TOTAL: {len(urls)} groups collected")
        logger.info(f"{'='*50}")
        
        # Save to file
        output_path = args.output
        if not os.path.isabs(output_path):
            output_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), output_path)
        
        with open(output_path, "w", encoding="utf-8") as f:
            for url in urls:
                f.write(url + "\n")
        
        logger.info(f"\n💾 Saved to: {output_path}")
        logger.info(f"📊 Total groups: {len(urls)}")
        
        # Print first 20
        logger.info("\nFirst 20 groups:")
        for i, url in enumerate(urls[:20], 1):
            logger.info(f"  {i:3d}. {url}")
        
    finally:
        try:
            driver.quit()
        except:
            pass
        logger.info("\n✅ Done! Close iXBrowser profile manually if needed.")


if __name__ == "__main__":
    main()
