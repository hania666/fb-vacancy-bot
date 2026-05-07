#!/usr/bin/env python3
"""FastAPI web application"""

import json
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from config import UPLOADS_DIR, TEMPLATES_DIR
from core.database import init_db, SessionLocal, Account, Group, Vacancy, PostingLog

app = FastAPI(title="FB Vacancy Bot")

# Mount static files
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

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


# ---- Routes ----

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    db = get_db()
    total_accounts = db.query(Account).count()
    ready_accounts = db.query(Account).filter(Account.status == "ready").count()
    warming_accounts = db.query(Account).filter(Account.status == "warming").count()
    banned_accounts = db.query(Account).filter(Account.status == "banned").count()
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
        "total_groups": total_groups,
        "total_posts": total_posts,
        "today_posts": today_posts,
        "active_vacancies": active_vacancies,
    })


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
        file_path = UPLOADS_DIR / photo.filename
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


@app.get("/stats", response_class=HTMLResponse)
def stats(request: Request):
    db = get_db()
    logs = db.query(PostingLog).order_by(PostingLog.id.desc()).limit(100).all()
    success = db.query(PostingLog).filter(PostingLog.status == "success").count()
    failed = db.query(PostingLog).filter(PostingLog.status == "failed").count()
    banned = db.query(PostingLog).filter(PostingLog.status == "banned").count()
    db.close()
    return templates.TemplateResponse("stats.html", {
        "request": request,
        "logs": logs,
        "success": success,
        "failed": failed,
        "banned": banned,
    })
