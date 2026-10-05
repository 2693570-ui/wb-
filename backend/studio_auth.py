"""Вход в Vi-Smart Studio (Владислав + менеджеры WB/Ozon)."""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/studio", tags=["studio"])

ACCOUNTS = (
    {"login": "vladislav", "name": "Владислав", "role": "admin", "env": "STUDIO_VLAD_PASSWORD"},
    {"login": "wb", "name": "Менеджер WB", "role": "wb", "env": "STUDIO_WB_PASSWORD"},
    {"login": "ozon", "name": "Менеджер Ozon", "role": "ozon", "env": "STUDIO_OZON_PASSWORD"},
)


def _sb():
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_KEY", "")
    return url, {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _accounts_ready():
    out = []
    for a in ACCOUNTS:
        pwd = (os.getenv(a["env"]) or "").strip()
        if pwd:
            out.append({**a, "password": pwd})
    return out


def studio_user_from_request(request: Request) -> dict | None:
    token = (request.headers.get("x-studio-token") or "").strip()
    if not token:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        return None
    url, headers = _sb()
    if not url:
        return None
    try:
        now = datetime.now(timezone.utc).isoformat()
        r = httpx.get(
            f"{url}/rest/v1/studio_sessions?token=eq.{token}&expires_at=gte.{now}&select=login,name,role,expires_at",
            headers=headers,
            timeout=10,
        )
        if r.is_success and r.json():
            row = r.json()[0]
            return {"login": row["login"], "name": row["name"], "role": row["role"]}
    except Exception as e:
        logger.warning(f"studio session: {e}")
    return None


@router.post("/login")
def studio_login(body: dict):
    accounts = _accounts_ready()
    if not accounts:
        raise HTTPException(
            status_code=503,
            detail="Задайте пароли STUDIO_VLAD_PASSWORD, STUDIO_WB_PASSWORD, STUDIO_OZON_PASSWORD в Railway",
        )
    login = str((body or {}).get("login") or "").strip().lower()
    password = str((body or {}).get("password") or "")
    acc = next((a for a in accounts if a["login"] == login), None)
    if not acc or acc["password"] != password:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    url, headers = _sb()
    if not url:
        raise HTTPException(status_code=500, detail="Нет SUPABASE_URL")
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=21)
    row = {
        "token": token,
        "login": acc["login"],
        "name": acc["name"],
        "role": acc["role"],
        "expires_at": expires.isoformat(),
    }
    r = httpx.post(f"{url}/rest/v1/studio_sessions", json=row, headers=headers, timeout=15)
    if not r.is_success:
        logger.error(f"studio_sessions insert {r.status_code} {r.text[:200]}")
        raise HTTPException(status_code=500, detail="Не удалось создать сессию. Накати supabase/studio.sql")
    return {"token": token, "user": {"login": acc["login"], "name": acc["name"], "role": acc["role"]}}


@router.get("/me")
def studio_me(request: Request):
    user = studio_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Нужен вход")
    return {"user": user}


@router.get("/references")
def list_references(request: Request):
    if not studio_user_from_request(request):
        raise HTTPException(status_code=401, detail="Нужен вход")
    url, headers = _sb()
    r = httpx.get(
        f"{url}/rest/v1/studio_references?select=*&order=created_at.desc",
        headers=headers,
        timeout=15,
    )
    if not r.is_success:
        return []
    return r.json() or []


@router.post("/references")
def add_reference(request: Request, body: dict):
    user = studio_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Нужен вход")
    url, headers = _sb()
    nm = body.get("nm_id")
    try:
        nm = int(nm) if nm not in (None, "") else None
    except (TypeError, ValueError):
        nm = None
    row = {
        "nm_id": nm,
        "marketplace": (body.get("marketplace") or "wb")[:8],
        "title": (body.get("title") or "")[:200],
        "url": (body.get("url") or "")[:500],
        "ctr": body.get("ctr"),
        "cr": body.get("cr"),
        "why_works": body.get("why_works") or "",
        "notes": body.get("notes") or "",
        "created_by": user["login"],
    }
    r = httpx.post(f"{url}/rest/v1/studio_references", json=row, headers=headers, timeout=15)
    if not r.is_success:
        raise HTTPException(status_code=500, detail=r.text[:200])
    data = r.json()
    return data[0] if isinstance(data, list) and data else row


@router.post("/projects")
def add_project(request: Request, body: dict):
    user = studio_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Нужен вход")
    title = str((body or {}).get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Название модели нужно")
    url, headers = _sb()
    row = {
        "title": title[:200],
        "keyword": (body.get("keyword") or "")[:200],
        "strengths": body.get("strengths") or "",
        "status": "draft",
        "winner_json": body.get("winner_json"),
        "created_by": user["login"],
    }
    r = httpx.post(f"{url}/rest/v1/studio_projects", json=row, headers=headers, timeout=15)
    if not r.is_success:
        raise HTTPException(status_code=500, detail=r.text[:200])
    data = r.json()
    return data[0] if isinstance(data, list) and data else row


@router.get("/projects")
def list_projects(request: Request):
    if not studio_user_from_request(request):
        raise HTTPException(status_code=401, detail="Нужен вход")
    url, headers = _sb()
    r = httpx.get(
        f"{url}/rest/v1/studio_projects?select=*&order=created_at.desc&limit=50",
        headers=headers,
        timeout=15,
    )
    return r.json() if r.is_success else []
