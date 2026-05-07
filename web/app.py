#!/usr/bin/env python3
"""FastAPI web application"""

import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from config import UPLOADS_DIR, TEMPLATES_DIR
from core.database import init_db, SessionLocal, Account, Group, Vacancy, PostingLog
from core.ixbrowser_client import test_connection
from core.warmup import warmup_all_ready_accounts
from core.posting_engine import PostingEngine
from core.group_collector import collect_groups
from core.process_manager import ProcessManager

logger = logging.getLogger(__name__)

app = FastAPI(title="FB Vacancy Bot")

# Global process manager
pm = ProcessManager()

# Mount static files
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
try:
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
except Exception:
    pass

# Templates
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# Uploads dir
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)


@app.on_event("startup")
def startup():
    init_db()


def get_db():
    db = SessionLocal()
    try:
        return db
    finally:
        db.close()


# ---- Routes: Dashboard ----

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    db = get_db()
    total_accounts = db.query(Account).count()
    ready_accounts = db.query(Account).filter(Account.status == "ready").count()
    warming_accounts = db.query(Account).filter(Account.status == "warming").count()
    banned_accounts = db.query(Account).filter(Account.status == "banned").count()
    new_accounts = db.query(Account).filter(Account.status == "new").count()
    total_groups = db.query(Group).count()
    total_posts = db.query(PostingLog).count()
    today_posts = db.query(PostingLog).filter(
        PostingLog.posted_at >= datetime.utcnow().replace(hour=0, minute=0, second=0)
    ).count()
    active_vacancies = db.query(Vacancy).filter(Vacancy.is_active == True).count()
    db.close()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "total_accounts": total_accounts,
        "ready_accounts": ready_accounts,
        "warming_accounts": warming_accounts,
        "banned_accounts": banned_accounts,
        "new_accounts": new_accounts,
        "total_groups": total_groups,
        "total_posts": total_posts,
        "today_posts": today_posts,
        "active_vacancies": active_vacancies,
    })


# ---- Routes: Accounts ----

@app.get("/accounts", response_class=HTMLResponse)
def accounts_list(request: Request):
    db = get_db()
    accounts = db.query(Account).order_by(Account.id.desc()).all()
    db.close()
    return templates.TemplateResponse("accounts.html", {
        "request": request,
        "accounts": accounts
    })


@app.post("/accounts/add")
async def add_account(
    login: str = Form(""),
    password: str = Form(""),
    proxy: str = Form(""),
    proxy_login: str = Form(""),
    proxy_pass: str = Form(""),
    ix_profile_id: str = Form(""),
):
    db = get_db()
    account = Account(
        login=login,
        password=password,
        proxy=proxy,
        proxy_login=proxy_login,
        proxy_pass=proxy_pass,
        ix_profile_id=ix_profile_id,
    )
    db.add(account)
    db.commit()
    db.close()
    return RedirectResponse("/accounts", status_code=303)


@app.get("/accounts/{account_id}/delete")
def delete_account(account_id: int):
    db = get_db()
    account = db.query(Account).filter(Account.id == account_id).first()
    if account:
        db.delete(account)
        db.commit()
    db.close()
    return RedirectResponse("/accounts", status_code=303)


@app.get("/accounts/{account_id}/status/{new_status}")
def set_account_status(account_id: int, new_status: str):
    db = get_db()
    account = db.query(Account).filter(Account.id == account_id).first()
    if account and new_status in ("new", "warming", "ready", "banned", "paused"):
        account.status = new_status
        db.commit()
    db.close()
    return RedirectResponse("/accounts", status_code=303)


# ---- Routes: Vacancies ----

@app.get("/vacancies", response_class=HTMLResponse)
def vacancies_list(request: Request):
    db = get_db()
    vacancies = db.query(Vacancy).order_by(Vacancy.id.desc()).all()
    db.close()
    return templates.TemplateResponse("vacancies.html", {
        "request": request,
        "vacancies": vacancies
    })


@app.post("/vacancies/add")
async def add_vacancy(
    title: str = Form(""),
    description: str = Form(""),
    photo: UploadFile = File(None),
):
    photo_path = ""
    if photo and photo.filename:
        # Save with timestamp to avoid conflicts
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        safe_filename = f"{ts}_{photo.filename}"
        file_path = UPLOADS_DIR / safe_filename
        content = await photo.read()
        with open(file_path, "wb") as f:
            f.write(content)
        photo_path = str(file_path)

    db = get_db()
    vacancy = Vacancy(
        title=title,
        description=description,
        photo_path=photo_path,
    )
    db.add(vacancy)
    db.commit()
    db.close()
    return RedirectResponse("/vacancies", status_code=303)


@app.get("/vacancies/{vacancy_id}/delete")
def delete_vacancy(vacancy_id: int):
    db = get_db()
    vacancy = db.query(Vacancy).filter(Vacancy.id == vacancy_id).first()
    if vacancy:
        db.delete(vacancy)
        db.commit()
    db.close()
    return RedirectResponse("/vacancies", status_code=303)


@app.get("/vacancies/{vacancy_id}/activate")
def toggle_vacancy(vacancy_id: int):
    db = get_db()
    vacancy = db.query(Vacancy).filter(Vacancy.id == vacancy_id).first()
    if vacancy:
        vacancy.is_active = not vacancy.is_active
        db.commit()
    db.close()
    return RedirectResponse("/vacancies", status_code=303)


# ---- Routes: Groups ----

@app.get("/groups", response_class=HTMLResponse)
def groups_list(request: Request):
    db = get_db()
    groups = db.query(Group).order_by(Group.id.desc()).limit(200).all()
    total = db.query(Group).count()
    db.close()
    return templates.TemplateResponse("groups.html", {
        "request": request,
        "groups": groups,
        "total": total
    })


@app.post("/groups/add")
async def add_group(
    url: str = Form(""),
    name: str = Form(""),
    category: str = Form(""),
):
    db = get_db()
    group = Group(url=url, name=name, category=category)
    db.add(group)
    db.commit()
    db.close()
    return RedirectResponse("/groups", status_code=303)


@app.post("/groups/import")
async def import_groups(file: UploadFile = File(...)):
    """Import groups from a text file (one URL per line)"""
    content = await file.read()
    lines = content.decode("utf-8").strip().split("\n")
    
    db = get_db()
    added = 0
    for line in lines:
        url = line.strip()
        if url and url.startswith("http"):
            existing = db.query(Group).filter(Group.url == url).first()
            if not existing:
                group = Group(url=url)
                db.add(group)
                added += 1
    db.commit()
    db.close()
    return RedirectResponse(f"/groups?imported={added}", status_code=303)


# ---- Routes: Actions (Warmup / Post / Collect) ----

@app.get("/actions/warmup")
def action_warmup():
    """Run warmup for all warming/ready accounts"""
    try:
        results = warmup_all_ready_accounts()
        return JSONResponse({
            "status": "ok",
            "message": f"Warmup run: {len(results)} accounts processed",
            "details": results,
        })
    except Exception as e:
        logger.error(f"Warmup error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/actions/status")
def action_status():
    """Get status of all running processes"""
    return JSONResponse({
        "processes": pm.list_processes(),
    })


@app.get("/actions/stop-all")
def action_stop_all():
    """Stop all running processes"""
    count = pm.stop_all()
    return JSONResponse({
        "status": "ok",
        "message": f"Stopped {count} processes",
    })


@app.get("/actions/collect-groups/{profile_id}")
def action_collect_groups(profile_id: str):
    """Collect groups by keyword search"""
    try:
        results = collect_groups(profile_id=profile_id)
        return JSONResponse(results)
    except Exception as e:
        logger.error(f"Collect groups error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/actions/post/{account_id}/{vacancy_id}")
def action_post(account_id: int, vacancy_id: int):
    """Post a vacancy from one account"""
    try:
        engine = PostingEngine()
        result = engine.run_posting_round(
            account_id=account_id,
            vacancy_id=vacancy_id,
            groups_per_batch=10,
            batches=10,
        )
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Post error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/actions/post-all/{vacancy_id}")
def action_post_all(vacancy_id: int):
    """Post from all ready accounts"""
    try:
        engine = PostingEngine()
        result = engine.run_all_accounts(vacancy_id=vacancy_id)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Post all error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/actions/test-ix")
def action_test_ix():
    """Test connection to ixBrowser"""
    result = test_connection()
    return JSONResponse(result)


# ---- Routes: Statistics ----

@app.get("/stats", response_class=HTMLResponse)
def stats(request: Request):
    db = get_db()
    logs = db.query(PostingLog).order_by(PostingLog.id.desc()).limit(100).all()
    success = db.query(PostingLog).filter(PostingLog.status == "success").count()
    failed = db.query(PostingLog).filter(PostingLog.status == "failed").count()
    banned_count = db.query(PostingLog).filter(PostingLog.status == "banned").count()
    db.close()
    return templates.TemplateResponse("stats.html", {
        "request": request,
        "logs": logs,
        "success": success,
        "failed": failed,
        "banned": banned_count,
    })
