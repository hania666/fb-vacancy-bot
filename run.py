#!/usr/bin/env python3
"""FB Vacancy Bot - Main Entry Point"""

import logging
import sys
from pathlib import Path

import uvicorn

# ── Logging setup ──────────────────────────────────────────────────────────
LOGS_DIR = Path(__file__).parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)

# File log (everything)
file_handler = logging.FileHandler(LOGS_DIR / "bot.log", encoding="utf-8")
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
))

# Console log (info+)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
))

# Root logger captures everything
root = logging.getLogger()
root.setLevel(logging.DEBUG)
root.handlers.clear()
root.addHandler(file_handler)
root.addHandler(console_handler)

# Quiet noisy libraries
for noisy in ("urllib3", "selenium.webdriver.remote", "selenium.webdriver.common"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# Quiet uvicorn HTTP access logs (the GET /actions/status spam)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

# Make sure our modules log at INFO+
for own in ("core", "core.posting_engine", "core.warmup",
            "core.ixbrowser_client", "core.process_manager",
            "core.scheduler", "web", "web.app"):
    logging.getLogger(own).setLevel(logging.INFO)

logger = logging.getLogger(__name__)
logger.info("=" * 60)
logger.info("🚀 FB Vacancy Bot starting...")
logger.info(f"📝 Logs: {LOGS_DIR / 'bot.log'}")
logger.info("=" * 60)

# Import app AFTER logging is configured
from web.app import app

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_config=None,  # use our root logger
        access_log=False,  # we manage our own access logs
    )
