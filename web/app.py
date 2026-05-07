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

# Mount uploads directory for serving photos
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")


@app.on_event("startup")
def startup():
    init_db()


# ---- Scheduler Actions ----


@app.get("/actions/scheduler/start")
def action_scheduler_start():
    """Start the background scheduler"""
    from core.scheduler import scheduler
    scheduler.start()
    return JSONResponse({"status": "ok", "message": "Scheduler started"})


@app.get("/actions/scheduler/stop")
def action_scheduler_stop():
    """Stop the background scheduler"""
    from core.scheduler import scheduler
    scheduler.stop()
    return JSONResponse({"status": "ok", "message": "Scheduler stopped"})


@app.get("/actions/scheduler/status")
def action_scheduler_status():
    """Get scheduler status and active schedules"""
    from core.scheduler import scheduler
    return JSONResponse({
        "running": scheduler._running,
        "schedules": scheduler.get_schedules(),
    })


@app.post("/actions/scheduler/add-warmup")
def action_add_warmup_schedule(account_id: int = Form(0),
                               hour: int = Form(10),
                               minute: int = Form(0)):
    """Add warmup schedule for an account (0 = all)"""
    from core.scheduler import scheduler
    scheduler.start()  # auto-start if not running
    if account_id == 0:
        scheduler.add_warmup_schedule_for_all(hour, minute)
        return JSONResponse({"status": "ok", "message": f"Warmup scheduled for all accounts at {hour:02d}:{minute:02d}"})
    else:
        scheduler.add_warmup_schedule(account_id, hour, minute)
        return JSONResponse({"status": "ok", "message": f"Warmup scheduled for account #{account_id} at {hour:02d}:{minute:02d}"})


@app.post("/actions/scheduler/add-posting")
def action_add_posting_schedule(vacancy_id: int = Form(0),
                                 hour: int = Form(12),
                                 minute: int = Form(0),
                                 all_accounts: bool = Form(True)):
    """Add posting schedule"""
    from core.scheduler import scheduler
    scheduler.start()
    scheduler.add_posting_schedule(vacancy_id, hour, minute,
                                    account_ids=None if all_accounts else [])
    return JSONResponse({"status": "ok", "message": f"Posting scheduled for vacancy #{vacancy_id} at {hour:02d}:{minute:02d}"})


@app.get("/actions/scheduler/remove/{job_id}")
def action_remove_schedule(job_id: str):
    """Remove a schedule"""
    from core.scheduler import scheduler
    success = scheduler.remove_schedule(job_id)
    return JSONResponse({"status": "ok" if success else "error", "message": "Removed" if success else "Not found"})


@app.get("/actions/scheduler/clear")
def action_clear_schedules():
    """Remove all schedules"""
    from core.scheduler import scheduler
    scheduler.clear_all()
    return JSONResponse({"status": "ok", "message": "All schedules cleared"})


@app.post("/actions/scheduler/setup-cycles")
def action_setup_cycles(account_id: int = Form(1), vacancy_id: int = Form(1)):
    """Setup 3 daily warmup+post cycles for an account"""
    from core.scheduler import scheduler
    scheduler.start()
    scheduler.add_three_cycles(account_id, vacancy_id)
    return JSONResponse({
        "status": "ok",
        "message": f"3 cycles configured for account #{account_id}, vacancy #{vacancy_id}",
    })


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
    url_path = ""
    if photo and photo.filename:
        # Save with timestamp to avoid conflicts
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        safe_filename = f"{ts}_{photo.filename}"
        file_path = UPLOADS_DIR / safe_filename
        content = await photo.read()
        with open(file_path, "wb") as f:
            f.write(content)
        photo_path = str(file_path)
        url_path = f"/uploads/{safe_filename}"

    db = get_db()
    vacancy = Vacancy(
        title=title,
        description=description,
        photo_path=url_path or photo_path,
    )
    db.add(vacancy)
    db.commit()
    db.close()
    return RedirectResponse("/vacancies", status_code=303)


@app.get("/vacancies/{vacancy_id}/edit", response_class=HTMLResponse)
def edit_vacancy_form(request: Request, vacancy_id: int):
    """Show edit form for a vacancy"""
    db = get_db()
    vacancy = db.query(Vacancy).filter(Vacancy.id == vacancy_id).first()
    db.close()
    if not vacancy:
        return RedirectResponse("/vacancies", status_code=303)
    return templates.TemplateResponse("vacancy_edit.html", {
        "request": request,
        "vacancy": vacancy,
    })


@app.post("/vacancies/{vacancy_id}/edit")
async def edit_vacancy(
    vacancy_id: int,
    title: str = Form(""),
    description: str = Form(""),
    photo: UploadFile = File(None),
):
    db = get_db()
    vacancy = db.query(Vacancy).filter(Vacancy.id == vacancy_id).first()
    if vacancy:
        vacancy.title = title
        vacancy.description = description
        vacancy.updated_at = datetime.utcnow()
        
        if photo and photo.filename:
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            safe_filename = f"{ts}_{photo.filename}"
            file_path = UPLOADS_DIR / safe_filename
            content = await photo.read()
            with open(file_path, "wb") as f:
                f.write(content)
            vacancy.photo_path = f"/uploads/{safe_filename}"
        
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


# ---- Routes: Scheduler Page ----

@app.get("/scheduler", response_class=HTMLResponse)
def scheduler_page(request: Request):
    return templates.TemplateResponse("scheduler.html", {
        "request": request,
    })


# ---- API: Statistics JSON ----


@app.get("/api/stats")
def api_stats():
    """Return statistics data as JSON"""
    db = get_db()
    
    # Overall counts
    total_posts = db.query(PostingLog).count()
    success = db.query(PostingLog).filter(PostingLog.status == "success").count()
    failed = db.query(PostingLog).filter(PostingLog.status == "failed").count()
    banned_logs = db.query(PostingLog).filter(PostingLog.status == "banned").count()
    
    # Conversion rate
    conversion = round((success / (success + failed)) * 100, 1) if (success + failed) > 0 else 0
    
    # Accounts stats
    accounts_stats = []
    for acc in db.query(Account).all():
        acc_success = db.query(PostingLog).filter(
            PostingLog.account_id == acc.id, PostingLog.status == "success"
        ).count()
        acc_failed = db.query(PostingLog).filter(
            PostingLog.account_id == acc.id, PostingLog.status == "failed"
        ).count()
        acc_conversion = round((acc_success / (acc_success + acc_failed)) * 100, 1) if (acc_success + acc_failed) > 0 else 0
        accounts_stats.append({
            "id": acc.id,
            "login": acc.login or f"Акк #{acc.id}",
            "status": acc.status,
            "success": acc_success,
            "failed": acc_failed,
            "conversion": acc_conversion,
            "total_posts": acc.total_post_count or 0,
            "daily_posts": acc.daily_post_count or 0,
        })
    
    # Today stats
    today = datetime.utcnow().replace(hour=0, minute=0, second=0)
    today_success = db.query(PostingLog).filter(
        PostingLog.status == "success", PostingLog.posted_at >= today
    ).count()
    today_failed = db.query(PostingLog).filter(
        PostingLog.status == "failed", PostingLog.posted_at >= today
    ).count()
    
    # 7 days history
    from datetime import timedelta
    days_history = []
    for i in range(6, -1, -1):
        day = (datetime.utcnow() - timedelta(days=i)).replace(hour=0, minute=0, second=0)
        next_day = day + timedelta(days=1)
        day_count = db.query(PostingLog).filter(
            PostingLog.status == "success",
            PostingLog.posted_at >= day,
            PostingLog.posted_at < next_day,
        ).count()
        days_history.append({
            "date": day.strftime("%a"),
            "count": day_count,
        })
    
    # Active vacancies
    active_vacancies = []
    for vac in db.query(Vacancy).filter(Vacancy.is_active == True).all():
        vac_posts = db.query(PostingLog).filter(
            PostingLog.vacancy_id == vac.id, PostingLog.status == "success"
        ).count()
        vac_groups = db.query(PostingLog).filter(
            PostingLog.vacancy_id == vac.id, PostingLog.status == "success"
        ).distinct(PostingLog.group_id).count()
        active_vacancies.append({
            "id": vac.id,
            "title": vac.title or f"Вакансия #{vac.id}",
            "posts": vac_posts,
            "groups": vac_groups,
        })
    
    # Total groups count
    total_groups = db.query(Group).count()
    
    db.close()
    
    return JSONResponse({
        "total_posts": total_posts,
        "success": success,
        "failed": failed,
        "banned": banned_logs,
        "conversion": conversion,
        "today_success": today_success,
        "today_failed": today_failed,
        "accounts": accounts_stats,
        "days": days_history,
        "vacancies": active_vacancies,
        "total_groups": total_groups,
    })


@app.get("/api/stats/balance")
def api_balance():
    """Get group distribution across accounts for rebalancing"""
    db = get_db()
    
    accounts = db.query(Account).filter(Account.status == "ready").all()
    total_live = len(accounts)
    
    # Count how many groups each account has posted to
    balance_data = []
    for acc in accounts:
        posted_groups = db.query(PostingLog.group_id).filter(
            PostingLog.account_id == acc.id,
            PostingLog.status == "success",
        ).distinct().count()
        balance_data.append({
            "id": acc.id,
            "name": acc.login or f"Акк #{acc.id}",
            "groups_posted": posted_groups,
            "daily_posts": acc.daily_post_count or 0,
        })
    
    total_groups = db.query(Group).count()
    ideal_per_account = max(1, total_groups // total_live) if total_live > 0 else 0
    
    db.close()
    
    return JSONResponse({
        "total_groups": total_groups,
        "live_accounts": total_live,
        "ideal_per_account": ideal_per_account,
        "accounts": balance_data,
    })


@app.post("/api/stats/rebalance")
def api_do_rebalance():
    """Rebalance: distribute groups across live accounts"""
    # This just logs the intent for now, actual rebalancing
    # happens during posting (PostingEngine picks groups for each account)
    db = get_db()
    
    accounts = db.query(Account).filter(Account.status == "ready").all()
    total_groups = db.query(Group).count()
    per_account = max(1, total_groups // len(accounts)) if accounts else 0
    
    # Reset daily counters for a fresh start
    for acc in accounts:
        acc.daily_post_count = 0
    db.commit()
    db.close()
    
    return JSONResponse({
        "status": "ok",
        "message": f"Ребаланс: {len(accounts)} акков, ~{per_account} групп на акк. Дневные счётчики сброшены.",
        "accounts": len(accounts),
        "groups_per_account": per_account,
    })


# ---- Routes: Statistics Page ----


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
