#!/usr/bin/env python3
"""Configuration settings for FB Vacancy Bot"""

from pathlib import Path

# Paths
BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "bot.db"
UPLOADS_DIR = BASE_DIR / "data" / "uploads"
TEMPLATES_DIR = BASE_DIR / "data" / "vacancy_templates"
LOGS_DIR = BASE_DIR / "data" / "logs"

# iXBrowser API
IXBROWSER_API = "http://127.0.0.1:53200"
IXBROWSER_API_KEY = ""

# Server
HOST = "0.0.0.0"
PORT = 8000
