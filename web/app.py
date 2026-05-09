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
    return db


def close_db(db):
    """Close a database session safely"""
    try:
        db.close()
    except:
        pass


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


@app.post("/groups/import-text")
async def import_groups_text(urls: str = Form("")):
    """Import groups from textarea (one URL per line)"""
    lines = urls.strip().split("\n")
    
    db = get_db()
    added = 0
    for line in lines:
        url = line.strip().rstrip('/')
        # Clean URL: keep only facebook.com/groups/NUMBER
        if url and url.startswith("http"):
            # Make sure it ends with /
            if not url.endswith('/'):
                url += '/'
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
    """Run warmup for all warming/ready accounts (background)"""
    try:
        def run(**kwargs):
            warmup_all_ready_accounts(stop_flag=kwargs.get("stop_flag"))

        proc = pm.start(
            description="🔥 Прогрев всех акков",
            target=run,
        )
        return JSONResponse({
            "status": "started",
            "message": "Прогрев всех акков запущен в фоне",
            "process_id": proc.id,
        })
    except Exception as e:
        logger.error(f"Warmup error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/actions/warmup/{account_id}")
def action_warmup_one(account_id: int):
    """Run warmup for a single account in background"""
    try:
        for p in pm.list_processes():
            if f"Прогрев #{account_id}" in p.get("description", ""):
                return JSONResponse({
                    "status": "already_running",
                    "message": f"Прогрев акк #{account_id} уже идёт!",
                })

        db = get_db()
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            db.close()
            return JSONResponse({"status": "error", "message": "Аккаунт не найден"}, status_code=404)
        if not account.ix_profile_id:
            db.close()
            return JSONResponse({"status": "error", "message": "Нет ID профиля iXBrowser"}, status_code=400)

        profile_id = account.ix_profile_id
        if account.status == "new":
            account.status = "warming"
            account.warmup_started_at = datetime.utcnow()
            db.commit()
        db.close()

        def run(**kwargs):
            from core.warmup import run_warmup_session
            run_warmup_session(
                account_id=account_id,
                profile_id=profile_id,
                duration_minutes=15,
                stop_flag=kwargs.get("stop_flag"),
            )

        proc = pm.start(
            description=f"🔥 Прогрев #{account_id}",
            target=run,
        )
        return JSONResponse({
            "status": "started",
            "message": f"Прогрев акк #{account_id} запущен",
            "process_id": proc.id,
        })
    except Exception as e:
        logger.error(f"Warmup one error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/actions/warmup-single/{account_id}")
def action_warmup_single(account_id: int):
    """Run warmup for a single specific account"""
    try:
        db = get_db()
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            db.close()
            return JSONResponse({"status": "error", "message": "Account not found"})
        
        profile_id = account.ix_profile_id
        db.close()
        
        if not profile_id:
            return JSONResponse({"status": "error", "message": "No iXBrowser profile ID"})

        # Set status to warming
        db = get_db()
        acc = db.query(Account).filter(Account.id == account_id).first()
        if acc and acc.status == "new":
            acc.status = "warming"
            acc.warmup_started_at = datetime.utcnow()
            db.commit()
        db.close()

        from core.warmup import run_warmup_session

        def run(**kwargs):
            run_warmup_session(
                account_id=account_id,
                profile_id=profile_id,
                duration_minutes=15,
                stop_flag=kwargs.get("stop_flag"),
            )
            # Auto-set to ready after warmup
            db2 = get_db()
            acc2 = db2.query(Account).filter(Account.id == account_id).first()
            if acc2 and acc2.status == "warming":
                acc2.status = "ready"
                db2.commit()
            db2.close()

        proc = pm.start(
            description=f"🔥 Прогрев акк #{account_id}",
            target=run,
        )

        return JSONResponse({
            "status": "started",
            "message": f"Прогрев акк #{account_id} запущен в фоне",
            "process_id": proc.id,
        })
    except Exception as e:
        logger.error(f"Warmup single error: {e}")
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
    """Post a vacancy from one account (background)"""
    try:
        # Check if already running
        for p in pm.list_processes():
            if f"Рассылка акк #{account_id}" in p.get("description", ""):
                return JSONResponse({
                    "status": "already_running",
                    "message": f"Рассылка для акк #{account_id} уже запущена!",
                })
        
        def run(**kwargs):
            engine = PostingEngine()
            engine.run_posting_round(
                account_id=account_id,
                vacancy_id=vacancy_id,
                groups_per_batch=10,
                batches=10,
                stop_flag=kwargs.get('stop_flag'),
            )
        
        proc = pm.start(
            description=f"📨 Рассылка акк #{account_id}, вакансия #{vacancy_id}",
            target=run,
        )
        
        return JSONResponse({
            "status": "started",
            "message": f"Рассылка запущена в фоне (акк #{account_id})",
            "process_id": proc.id,
        })
    except Exception as e:
        logger.error(f"Post error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/actions/post-all/{vacancy_id}")
def action_post_all(vacancy_id: int):
    """Post from all ready accounts (background)"""
    try:
        def run(**kwargs):
            engine = PostingEngine()
            engine.run_all_accounts(
                vacancy_id=vacancy_id,
                stop_flag=kwargs.get('stop_flag'),
            )
        
        proc = pm.start(
            description=f"📨 Рассылка со всех акков, вакансия #{vacancy_id}",
            target=run,
        )
        
        return JSONResponse({
            "status": "started",
            "message": f"Рассылка со всех акков запущена в фоне!",
            "process_id": proc.id,
        })
    except Exception as e:
        logger.error(f"Post all error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/actions/post-multi")
async def action_post_multi(
    request: Request,
):
    """
    Post from multiple accounts, each with its own vacancy.

    Body: JSON with format:
    {"assignments": [{"account_id": 1, "vacancy_id": 1}, ...]}
    """
    try:
        body = await request.json()
        assignments = body.get("assignments", [])
        if not assignments:
            return JSONResponse({"status": "error", "message": "No assignments provided"})

        def run(**kwargs):
            engine = PostingEngine()
            engine.run_multiple_accounts_with_vacancies(
                assignments,
                stop_flag=kwargs.get("stop_flag"),
            )

        proc = pm.start(
            description=f"📨 Мульти-рассылка: {len(assignments)} акков",
            target=run,
        )
        return JSONResponse({
            "status": "started",
            "message": f"Рассылка для {len(assignments)} акков запущена в фоне",
            "process_id": proc.id,
        })
    except Exception as e:
        logger.error(f"Post multi error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/actions/post-from-profile/{account_id}/{vacancy_id}")
def action_post_from_profile(account_id: int, vacancy_id: int):
    """
    Post to all groups where the account is a member,
    WITHOUT needing groups in the database.
    
    Runs in background - check status at /actions/status
    """
    try:
        # Check if this account already has a posting process running
        for p in pm.list_processes():
            if "Постинг" in p.get("description", "") and str(account_id) in p.get("description", ""):
                return JSONResponse({
                    "status": "already_running",
                    "message": f"Рассылка для акк #{account_id} уже запущена!",
                })
        
        def run_in_background(**kwargs):
            engine = PostingEngine()
            result = engine.run_posting_from_profile(
                account_id=account_id,
                vacancy_id=vacancy_id,
                max_posts=30,
                stop_flag=kwargs.get('stop_flag'),
            )
            return result
        
        proc = pm.start(
            description=f"📤 Постинг из профиля: акк #{account_id}",
            target=run_in_background,
        )
        
        return JSONResponse({
            "status": "started",
            "message": f"Рассылка для акк #{account_id} запущена в фоне!",
            "process_id": proc.id,
        })
    except Exception as e:
        logger.error(f"Post from profile error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/actions/test-ix")
def action_test_ix():
    """Test connection to ixBrowser"""
    result = test_connection()
    return JSONResponse(result)


@app.get("/actions/post-db/{account_id}/{vacancy_id}")
def action_post_from_db(account_id: int, vacancy_id: int,
                         delay_min: int = 30, delay_max: int = 120):
    """
    Post vacancy to ALL groups in DB, one by one.
    Stops automatically when all groups are done.
    Skips groups already posted to (per account+vacancy).
    """
    try:
        for p in pm.list_processes():
            if f"#{account_id}" in p.get("description", "") and "Рассылка" in p.get("description", ""):
                return JSONResponse({
                    "status": "already_running",
                    "message": f"Рассылка акк #{account_id} уже запущена!",
                })

        db = get_db()
        account = db.query(Account).filter(Account.id == account_id).first()
        vacancy = db.query(Vacancy).filter(Vacancy.id == vacancy_id, Vacancy.is_active == True).first()
        if not account:
            db.close()
            return JSONResponse({"status": "error", "message": "Аккаунт не найден"}, status_code=404)
        if not vacancy:
            db.close()
            return JSONResponse({"status": "error", "message": "Вакансия не найдена или неактивна"}, status_code=404)
        total_groups = db.query(Group).count()
        db.close()

        def run(**kwargs):
            engine = PostingEngine()
            engine.run_posting_from_db(
                account_id=account_id,
                vacancy_id=vacancy_id,
                delay_min=delay_min,
                delay_max=delay_max,
                stop_flag=kwargs.get("stop_flag"),
            )

        proc = pm.start(
            description=f"📨 Рассылка #{account_id} → {total_groups} групп",
            target=run,
        )
        return JSONResponse({
            "status": "started",
            "message": f"Рассылка запущена: акк #{account_id}, {total_groups} групп в очереди",
            "process_id": proc.id,
        })
    except Exception as e:
        logger.error(f"post-db error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/actions/post-db-all/{vacancy_id}")
def action_post_db_all(vacancy_id: int,
                        delay_min: int = 30, delay_max: int = 60,
                        between_accounts: int = 60):
    """
    Post vacancy to all DB groups from ALL ready accounts in sequence.
    Each account posts to all groups it hasn't posted to yet.
    """
    try:
        for p in pm.list_processes():
            if "Рассылка с всех" in p.get("description", ""):
                return JSONResponse({
                    "status": "already_running",
                    "message": "Массовая рассылка уже запущена!",
                })

        db = get_db()
        ready_count = db.query(Account).filter(Account.status == "ready").count()
        vacancy = db.query(Vacancy).filter(
            Vacancy.id == vacancy_id, Vacancy.is_active == True,
        ).first()
        db.close()

        if ready_count == 0:
            return JSONResponse({"status": "error", "message": "Нет аккаунтов в статусе ready"}, status_code=400)
        if not vacancy:
            return JSONResponse({"status": "error", "message": "Вакансия не найдена"}, status_code=404)

        def run(**kwargs):
            engine = PostingEngine()
            engine.run_posting_from_db_all_accounts(
                vacancy_id=vacancy_id,
                delay_min=delay_min,
                delay_max=delay_max,
                between_accounts_delay=between_accounts,
                stop_flag=kwargs.get("stop_flag"),
            )

        proc = pm.start(
            description=f"📨 Рассылка с всех ({ready_count} акк) → '{vacancy.title}'",
            target=run,
        )
        return JSONResponse({
            "status": "started",
            "message": f"Запущена рассылка с {ready_count} аккаунтов",
            "process_id": proc.id,
        })
    except Exception as e:
        logger.error(f"post-db-all error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/actions/join-groups/{account_id}")
def action_join_groups(account_id: int, max_joins: int = 30,
                        delay_min: int = 30, delay_max: int = 90):
    """
    Subscribe single account to all DB groups it isn't already in.
    """
    try:
        for p in pm.list_processes():
            if f"Подписка #{account_id}" in p.get("description", ""):
                return JSONResponse({
                    "status": "already_running",
                    "message": f"Подписка акк #{account_id} уже идёт!",
                })

        db = get_db()
        account = db.query(Account).filter(Account.id == account_id).first()
        db.close()
        if not account:
            return JSONResponse({"status": "error", "message": "Аккаунт не найден"}, status_code=404)
        if not account.ix_profile_id:
            return JSONResponse({"status": "error", "message": "Нет iXBrowser ID"}, status_code=400)

        def run(**kwargs):
            engine = PostingEngine()
            engine.join_all_db_groups_with_account(
                account_id=account_id,
                max_joins=max_joins,
                delay_min=delay_min,
                delay_max=delay_max,
                stop_flag=kwargs.get("stop_flag"),
            )

        proc = pm.start(
            description=f"📌 Подписка #{account_id} (макс {max_joins})",
            target=run,
        )
        return JSONResponse({
            "status": "started",
            "message": f"Подписка запущена для акк #{account_id}, макс {max_joins} групп",
            "process_id": proc.id,
        })
    except Exception as e:
        logger.error(f"join-groups error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/actions/join-groups-all")
def action_join_groups_all(max_joins: int = 30,
                            delay_min: int = 30, delay_max: int = 90,
                            between_accounts: int = 120):
    """
    Subscribe ALL ready accounts to all DB groups (sequentially).
    """
    try:
        for p in pm.list_processes():
            if "Массовая подписка" in p.get("description", ""):
                return JSONResponse({
                    "status": "already_running",
                    "message": "Массовая подписка уже запущена!",
                })

        db = get_db()
        ready = db.query(Account).filter(Account.status == "ready").all()
        ready_ids = [(a.id, a.login) for a in ready]
        db.close()

        if not ready_ids:
            return JSONResponse({"status": "error", "message": "Нет аккаунтов в статусе ready"}, status_code=400)

        import time
        def run(**kwargs):
            stop = kwargs.get("stop_flag")
            engine = PostingEngine()
            for idx, (acc_id, login) in enumerate(ready_ids):
                if stop and stop.is_set():
                    logger.info("⏹ Stop requested in mass-join")
                    break
                logger.info(f"━━━ Подписка ({idx+1}/{len(ready_ids)}) акк #{acc_id} '{login}' ━━━")
                try:
                    engine.join_all_db_groups_with_account(
                        account_id=acc_id,
                        max_joins=max_joins,
                        delay_min=delay_min,
                        delay_max=delay_max,
                        stop_flag=stop,
                    )
                except Exception as e:
                    logger.error(f"❌ Подписка акк #{acc_id} упала: {e}")
                if idx < len(ready_ids) - 1 and not (stop and stop.is_set()):
                    logger.info(f"⏳ Пауза {between_accounts}с перед следующим аккаунтом...")
                    for _ in range(between_accounts):
                        if stop and stop.is_set():
                            break
                        time.sleep(1)

        proc = pm.start(
            description=f"📌 Массовая подписка ({len(ready_ids)} акк × макс {max_joins})",
            target=run,
        )
        return JSONResponse({
            "status": "started",
            "message": f"Запущена массовая подписка для {len(ready_ids)} аккаунтов",
            "process_id": proc.id,
        })
    except Exception as e:
        logger.error(f"join-groups-all error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/actions/dedup-groups")
def action_dedup_groups():
    """Remove duplicate groups from DB and normalise URLs"""
    try:
        result = PostingEngine.dedup_groups_in_db()
        return JSONResponse({
            "status": "ok",
            "message": f"Удалено дублей: {result['removed']}, нормализовано URL: {result['normalised']}",
            **result,
        })
    except Exception as e:
        logger.error(f"Dedup error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/actions/reset-daily-limit/{account_id}")
def action_reset_daily_limit(account_id: int):
    """Reset today's posting count for an account (use carefully)."""
    try:
        from core.database import DailyLimit
        from datetime import datetime
        today = datetime.utcnow().date().isoformat()

        db = get_db()
        if account_id == 0:
            # Reset for ALL accounts
            limits = db.query(DailyLimit).filter(DailyLimit.date == today).all()
            count = len(limits)
            for lim in limits:
                lim.posts_made = 0
            db.commit()
            db.close()
            return JSONResponse({"status": "ok", "message": f"Reset daily limit for {count} accounts"})
        else:
            limit = db.query(DailyLimit).filter(
                DailyLimit.account_id == account_id,
                DailyLimit.date == today,
            ).first()
            if limit:
                limit.posts_made = 0
                db.commit()
            db.close()
            return JSONResponse({"status": "ok", "message": f"Daily limit reset for account #{account_id}"})
    except Exception as e:
        logger.error(f"Reset limit error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/actions/scrape-profile-groups")
async def action_scrape_profile_groups(request: Request):
    """
    Scrape groups from another user's profile.
    Body: {"profile_url": "https://...", "account_id": 1, "max_groups": 200}
    Runs in background.
    """
    try:
        body = await request.json()
        profile_url = (body.get("profile_url") or "").strip()
        account_id = int(body.get("account_id", 1))
        max_groups = int(body.get("max_groups", 200))

        if not profile_url or "facebook.com" not in profile_url:
            return JSONResponse({"status": "error", "message": "Нужен валидный FB URL"}, status_code=400)

        db = get_db()
        account = db.query(Account).filter(Account.id == account_id).first()
        db.close()
        if not account or not account.ix_profile_id:
            return JSONResponse({"status": "error", "message": "Аккаунт не найден или нет iXBrowser ID"}, status_code=400)

        ix_profile = account.ix_profile_id

        def run(**kwargs):
            from core.group_collector import collect_groups_from_profile_url
            result = collect_groups_from_profile_url(
                profile_url=profile_url,
                ix_profile_id=ix_profile,
                max_groups=max_groups,
            )
            logger.info(f"📋 Scrape result: {result.get('status')} found={result.get('found',0)} new={result.get('new_added',0)}")

        proc = pm.start(
            description=f"🔍 Парсинг профиля → {profile_url[:40]}...",
            target=run,
        )
        return JSONResponse({
            "status": "started",
            "message": f"Парсинг запущен в фоне. Жди в логах.",
            "process_id": proc.id,
        })
    except Exception as e:
        logger.error(f"scrape-profile-groups error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/actions/clean-broken-groups")
def action_clean_broken_groups():
    """Remove groups with malformed URLs from DB."""
    try:
        import re
        db = get_db()
        all_groups = db.query(Group).all()
        removed = 0
        # Valid URL pattern: facebook.com/groups/{numeric_id_or_slug}/
        valid_pattern = re.compile(
            r'^https://(?:www\.)?facebook\.com/groups/[a-zA-Z0-9_.\-]+/?$'
        )
        for g in all_groups:
            if not valid_pattern.match(g.url) or 'httpst' in g.url or g.url.count('/') > 5:
                logger.info(f"🗑 Removing broken URL: {g.url}")
                db.delete(g)
                removed += 1
        db.commit()
        db.close()
        return JSONResponse({"status": "ok", "removed": removed,
                             "message": f"Removed {removed} broken group URLs"})
    except Exception as e:
        logger.error(f"Clean broken groups error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


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


@app.get("/api/vacancies")
def api_vacancies():
    """Return active vacancies for UI dropdowns"""
    db = get_db()
    vacancies = db.query(Vacancy).filter(Vacancy.is_active == True).all()
    db.close()
    return JSONResponse({
        "vacancies": [{"id": v.id, "title": v.title} for v in vacancies]
    })
