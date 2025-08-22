# bots/api_mobile_fastapi.py
# Router de API móvil para el panel dentro de la app.
# - Login leyendo credenciales desde bots/*.json ("auth": {...})
# - Listado/actualización/borrado de leads en Firebase
# - Filtro por alcance (allowed bots) usando Authorization: Bearer <token>
# - ⬆️ Ahora también expone el "company" (business_name) de cada bot, tomado de bots/*.json

from __future__ import annotations

import os
import json
import glob
import secrets
from typing import Any, Dict, List

from fastapi import APIRouter, Request, HTTPException, status
from fastapi.responses import JSONResponse
from firebase_admin import db
from pydantic import BaseModel, Field

# Importamos las funciones auxiliares de main.py
# (Se asume que estas funciones no tienen dependencias de Flask)
from main import (
    _load_users,
    _get_bot_cfg_by_name,
    _normalize_bot_name,
    fb_list_leads_by_bot,
    fb_list_leads_all,
    fb_get_lead,
    fb_set_conversation_on,
    fb_delete_lead
)

# --------------------------------------------------------------------
# Router
# --------------------------------------------------------------------
mobile_router = APIRouter()

# --------------------------------------------------------------------
# Cache / Sesiones in-memory
# --------------------------------------------------------------------
_ACCOUNTS_CACHE: Dict[str, Dict[str, Any]] | None = None
_BOT_COMPANY_CACHE: Dict[str, str] | None = None
_SESSION_TOKENS: Dict[str, Dict[str, Any]] = {}

class LoginRequest(BaseModel):
    username: str
    password: str

class UpdateLeadRequest(BaseModel):
    bot: str
    numero: str
    estado: str | None = Field(None)
    nota: str | None = Field(None)

class DeleteLeadRequest(BaseModel):
    bot: str
    numero: str

# --------------------------------------------------------------------
# Carga de bots desde ./bots/*.json
# --------------------------------------------------------------------
def _load_bots_folder() -> Dict[str, Any]:
    bots: Dict[str, Any] = {}
    for path in glob.glob(os.path.join("bots", "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    bots[k] = v
        except Exception as e:
            print(f"⚠️ No se pudo cargar {path}: {e}")
    return bots

def _build_bot_company_map() -> Dict[str, str]:
    company: Dict[str, str] = {}
    bots_cfg = _load_bots_folder()
    for _k, cfg in bots_cfg.items():
        if not isinstance(cfg, dict):
            continue
        name = (cfg.get("name") or "").strip()
        if not name:
            continue
        comp = (
            cfg.get("business_name")
            or cfg.get("company")
            or cfg.get("business")
            or ""
        )
        comp = str(comp).strip()
        if not comp:
            comp = name.upper()
        company[name] = comp
    return company

def _get_bot_company_map() -> Dict[str, str]:
    global _BOT_COMPANY_CACHE
    if _BOT_COMPANY_CACHE is None:
        _BOT_COMPANY_CACHE = _build_bot_company_map()
        print(f"[api_mobile] Company map: {_BOT_COMPANY_CACHE}")
    return _BOT_COMPANY_CACHE

def _build_accounts_from_bots() -> Dict[str, Dict[str, Any]]:
    accounts: Dict[str, Dict[str, Any]] = {}
    bots_cfg = _load_bots_folder()
    for _num_key, cfg in bots_cfg.items():
        if not isinstance(cfg, dict):
            continue
        auth = (cfg.get("auth") or {}) if isinstance(cfg.get("auth"), dict) else {}
        username = (auth.get("username") or "").strip()
        password = (auth.get("password") or "").strip()
        if not username or not password:
            continue
        bot_name = (cfg.get("name") or "").strip()
        if not bot_name:
            continue
        acc = accounts.setdefault(username, {"password": password, "bots": set(), "admin": False})
        acc["bots"].add(bot_name)
        if str(auth.get("panel", "")).strip().lower() == "panel":
            acc["admin"] = True
    for u, a in accounts.items():
        if a.get("admin"):
            a["bots"] = "*"
        else:
            a["bots"] = sorted(list(a["bots"]))
    return accounts

def _get_accounts() -> Dict[str, Dict[str, Any]]:
    global _ACCOUNTS_CACHE
    if _ACCOUNTS_CACHE is None:
        _ACCOUNTS_CACHE = _build_accounts_from_bots()
        print(f"[api_mobile] Cuentas cargadas desde /bots: {list(_ACCOUNTS_CACHE.keys())}")
    return _ACCOUNTS_CACHE

def _issue_token(allowed):
    tok = secrets.token_urlsafe(32)
    _SESSION_TOKENS[tok] = {"allowed": allowed}
    return tok

def _allowed_from_request(request: Request) -> Any:
    auth = request.headers.get("Authorization") or ""
    if auth.startswith("Bearer "):
        tok = auth[7:].strip()
        entry = _SESSION_TOKENS.get(tok)
        if entry:
            return entry.get("allowed", "*")
    return "*"

def _is_allowed(bot_name: str, allowed) -> bool:
    if allowed == "*":
        return True
    if isinstance(allowed, list):
        return bot_name in allowed
    return True

# --------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------
@mobile_router.get("/health")
async def mobile_health():
    return JSONResponse(content={"ok": True, "service": "mobile"})

@mobile_router.post("/login")
async def mobile_login(login_data: LoginRequest):
    username = login_data.username.strip()
    password = login_data.password.strip()
    accounts = _get_accounts()
    acc = accounts.get(username)
    if not acc or password != acc.get("password"):
        raise HTTPException(status_code=401, detail="bad_credentials")
    allowed = acc.get("bots", "*")
    token = _issue_token(allowed)
    bots_payload = allowed if allowed == "*" else list(allowed)
    return JSONResponse(content={"ok": True, "token": token, "bots": bots_payload})

@mobile_router.get("/leads")
async def mobile_leads(request: Request):
    allowed = _allowed_from_request(request)
    bot_q = request.query_params.get("bot", "").strip()

    try:
        if bot_q:
            if not _is_allowed(bot_q, allowed):
                return JSONResponse(content={"leads": []})
            leads = _list_leads_by_bot(bot_q)
        else:
            leads = _list_leads_all()
            if allowed != "*":
                allowed_set = set(allowed) if isinstance(allowed, list) else set()
                leads = [l for l in leads if l.get("bot") in allowed_set]
        return JSONResponse(content={"leads": leads})
    except Exception as e:
        print(f"❌ Error leyendo leads: {e}")
        raise HTTPException(status_code=500, detail="Error al leer los leads")

@mobile_router.get("/bots_meta")
async def mobile_bots_meta():
    comp = _get_bot_company_map()
    arr = [{"name": k, "company": v} for k, v in sorted(comp.items())]
    return JSONResponse(content={"bots": arr})

@mobile_router.post("/lead")
async def mobile_update_lead(lead_data: UpdateLeadRequest, request: Request):
    bot_name = lead_data.bot.strip()
    numero = lead_data.numero.strip()

    if not bot_name or not numero:
        raise HTTPException(status_code=400, detail="params")

    allowed = _allowed_from_request(request)
    if not _is_allowed(bot_name, allowed):
        raise HTTPException(status_code=403, detail="forbidden")

    ok = _update_lead(bot_name, numero, estado=lead_data.estado, nota=lead_data.nota)
    return JSONResponse(content={"ok": bool(ok)})

@mobile_router.post("/delete")
async def mobile_delete_lead(delete_data: DeleteLeadRequest, request: Request):
    bot_name = delete_data.bot.strip()
    numero = delete_data.numero.strip()

    if not bot_name or not numero:
        raise HTTPException(status_code=400, detail="params")

    allowed = _allowed_from_request(request)
    if not _is_allowed(bot_name, allowed):
        raise HTTPException(status_code=403, detail="forbidden")

    ok = _delete_lead(bot_name, numero)
    return JSONResponse(content={"ok": bool(ok)})