#!/usr/bin/env python3
"""Database models for FB Vacancy Bot"""

from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, Float, JSON
from sqlalchemy.orm import declarative_base, sessionmaker

from config import DB_PATH

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class Account(Base):
    """Facebook account"""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), default="")
    login = Column(String(255), default="")
    password = Column(String(255), default="")
    proxy = Column(String(255), default="")
    proxy_login = Column(String(100), default="")
    proxy_pass = Column(String(100), default="")
    user_agent = Column(String(500), default="")
    cookies = Column(JSON, default=dict)
    ix_profile_id = Column(String(100), default="")
    status = Column(String(20), default="new")  # new | warming | ready | banned | paused
    daily_post_count = Column(Integer, default=0)
    total_post_count = Column(Integer, default=0)
    last_active_at = Column(DateTime, nullable=True)
    warmup_started_at = Column(DateTime, nullable=True)
    warmed_at = Column(DateTime, nullable=True)
    banned_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Group(Base):
    """Facebook group for posting"""
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True)
    url = Column(String(500), unique=True)
    name = Column(String(300), default="")
    category = Column(String(100), default="")  # город
    is_open = Column(Boolean, default=True)
    is_member = Column(Boolean, default=False)
    last_posted_at = Column(DateTime, nullable=True)
    post_count = Column(Integer, default=0)
    parsed_at = Column(DateTime, default=datetime.utcnow)


class Vacancy(Base):
    """Vacancy template"""
    __tablename__ = "vacancies"

    id = Column(Integer, primary_key=True)
    title = Column(String(255), default="")
    description = Column(Text, default="")
    photo_path = Column(String(500), default="")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PostingLog(Base):
    """Posting history"""
    __tablename__ = "posting_logs"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer)
    group_id = Column(Integer)
    vacancy_id = Column(Integer)
    group_url = Column(String(500))
    status = Column(String(20))  # success | failed | banned
    error_message = Column(Text, default="")
    posted_at = Column(DateTime, default=datetime.utcnow)


class DailyLimit(Base):
    """Daily posting limits per account"""
    __tablename__ = "daily_limits"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer)
    date = Column(String(10))  # YYYY-MM-DD
    posts_made = Column(Integer, default=0)


def init_db():
    """Create all tables"""
    Base.metadata.create_all(engine)
