# bots/api_mobile.py
# Blueprint de API móvil para el panel dentro de la app.
# - Login leyendo credenciales desde bots/*.json ("auth": {...})
# - Listado/actualización/borrado de leads en Firebase
# - Filtro por alcance (allowed bots) usando Authorization: Bearer <token>
# - NUEVO: /bot_info para resolver business_name por bot (global)

from __future__ import annotations

import os
import json
import glob
import secrets
from typing import Any, Dict, List

from flask import Blueprint, request, jsonify
from firebase_admin import db

# --------------------------------------------------------------------
# Blueprint  (SIN url_prefix: lo aporta main.py al registrar)
# --------------------------------------------------------------------
mobile_bp = Blueprint("mobile_bp", __name__)

# --------------------------------------------------------------------
# Cache / Sesiones in-memory
# --------------------------------------------------------------------
_ACCOUNTS_CACHE: Dict[str, Dict[str, Any]] | None = None
_SESSION_TOKENS: Dict[str, Dict[str, Any]] = {}  # token -> {"allowed": "*"/[bot_name,...]}

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

def _build_accounts_from_bots() -> Dict[str, Dict[str, Any]]:
    """
    Construye usuarios a partir de los JSON de /bots:
    {
      "username": {
        "password": "...",
        "bots": ["Sara","Camila"],  # lista de cfg["name"] (NO business_name)
        "admin": bool               # si alguno trae "panel":"panel" => admin
      }
    }
    """
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

        bot_name = (cfg.get("name") or "").strip()  # usamos "name" para empatar Firebase
        if not bot_name:
            continue

        acc = accounts.setdefault(username, {"password": password, "bots": set(), "admin": False})
        acc["bots"].add(bot_name)

        # Si en alguno de los bots del mismo usuario aparece "panel":"panel" => admin (ve todo)
        if str(auth.get("panel", "")).strip().lower() == "panel":
            acc["admin"] = True

    # Normaliza a listas / '*' si admin
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
    """Genera token y guarda alcance ('*' o lista de bot names)."""
    tok = secrets.token_urlsafe(32)
    _SESSION_TOKENS[tok] = {"allowed": allowed}
    return tok

def _allowed_from_request(req) -> Any:
    """Devuelve '*' o lista de bot names a partir del header Authorization."""
    auth = (req.headers.get("Authorization") or "").strip()
    if auth.startswith("Bearer "):
        tok = auth[7:].strip()
        entry = _SESSION_TOKENS.get(tok)
        if entry:
            return entry.get("allowed", "*")
    # si no hay token devolver '*' (compatibilidad), pero lo normal es exigir token
    return "*"

def _is_allowed(bot_name: str, allowed) -> bool:
    if allowed == "*":
        return True
    if isinstance(allowed, list):
        return bot_name in allowed
    return True

# --------------------------------------------------------------------
# Firebase helpers (leads)
# Estructura en RTDB: leads/<bot_name>/<numero> => {...}
# donde <bot_name> coincide con cfg["name"] del bot (p.ej. "Sara")
# --------------------------------------------------------------------
def _lead_ref(bot_name: str, numero: str):
    return db.reference(f"leads/{bot_name}/{numero}")

def _list_leads_all() -> List[Dict[str, Any]]:
    root = db.reference("leads").get() or {}
    leads: List[Dict[str, Any]] = []
    if not isinstance(root, dict):
        return leads
    for bot_name, numeros in root.items():
        if not isinstance(numeros, dict):
            continue
        for numero, data in numeros.items():
            leads.append({
                "bot": bot_name,
                "numero": numero,
                "first_seen": (data or {}).get("first_seen", ""),
                "last_message": (data or {}).get("last_message", ""),
                "last_seen": (data or {}).get("last_seen", ""),
                "messages": int((data or {}).get("messages", 0) or 0),
                "status": (data or {}).get("status", "nuevo") or "nuevo",
                "notes": (data or {}).get("notes", "") or "",
            })
    # Ordena por last_seen descendente si hay string comparable
    leads.sort(key=lambda x: x.get("last_seen", ""), reverse=True)
    return leads

def _list_leads_by_bot(bot_name: str) -> List[Dict[str, Any]]:
    numeros = db.reference(f"leads/{bot_name}").get() or {}
    leads: List[Dict[str, Any]] = []
    if not isinstance(numeros, dict):
        return leads
    for numero, data in numeros.items():
        leads.append({
            "bot": bot_name,
            "numero": numero,
            "first_seen": (data or {}).get("first_seen", ""),
            "last_message": (data or {}).get("last_message", ""),
            "last_seen": (data or {}).get("last_seen", ""),
            "messages": int((data or {}).get("messages", 0) or 0),
            "status": (data or {}).get("status", "nuevo") or "nuevo",
            "notes": (data or {}).get("notes", "") or "",
        })
    leads.sort(key=lambda x: x.get("last_seen", ""), reverse=True)
    return leads

def _update_lead(bot_name: str, numero: str, estado: str | None = None, nota: str | None = None) -> bool:
    try:
        ref = _lead_ref(bot_name, numero)
        cur = ref.get() or {}
        if estado is not None and estado != "":
            cur["status"] = estado
        if nota is not None:
            cur["notes"] = nota
        cur.setdefault("bot", bot_name)
        cur.setdefault("numero", numero)
        ref.set(cur)
        return True
    except Exception as e:
        print(f"❌ Error actualizando lead {bot_name}/{numero}: {e}")
        return False

def _delete_lead(bot_name: str, numero: str) -> bool:
    try:
        _lead_ref(bot_name, numero).delete()
        return True
    except Exception as e:
        print(f"❌ Error eliminando lead {bot_name}/{numero}: {e}")
        return False

# --------------------------------------------------------------------
# Helpers de metadatos de bots (business_name, etc.)
# --------------------------------------------------------------------
def _bot_display_map() -> Dict[str, str]:
    """
    Devuelve { cfg['name'] : cfg['business_name'] || cfg['name'] } para todos los bots.
    """
    mapping: Dict[str, str] = {}
    bots_cfg = _load_bots_folder()
    for _num_key, cfg in bots_cfg.items():
        if not isinstance(cfg, dict):
            continue
        name = (cfg.get("name") or "").strip()
        if not name:
            continue
        business = (cfg.get("business_name") or "").strip() or name
        mapping[name] = business
    return mapping

# --------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------
@mobile_bp.route("/health", methods=["GET"])
def mobile_health():
    return jsonify({"ok": True, "service": "mobile"})

@mobile_bp.route("/login", methods=["POST"])
def mobile_login():
    """
    Valida usuario/clave contra los JSON de /bots.
    Respuesta:
      { ok: true, token: "...", bots: ["Sara","Camila"] }  # o bots: "*" si admin
    """
    try:
        body = request.get_json(force=True) or {}
    except Exception:
        body = {}
    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip()

    accounts = _get_accounts()
    acc = accounts.get(username)
    if not acc or password != acc.get("password"):
        return jsonify({"ok": False, "error": "bad_credentials"}), 401

    allowed = acc.get("bots", "*")  # '*' o lista de bot "name"
    token = _issue_token(allowed)

    # Asegura que "bots" sea serializable (lista o '*')
    bots_payload = allowed if allowed == "*" else list(allowed)
    return jsonify({"ok": True, "token": token, "bots": bots_payload})

@mobile_bp.route("/leads", methods=["GET"])
def mobile_leads():
    """
    Lista leads visibles para el usuario según su token.
    Query opcional: ?bot=<bot_name>
    """
    allowed = _allowed_from_request(request)  # "*" o lista de bot names
    bot_q = (request.args.get("bot") or "").strip()

    try:
        if bot_q:
            # filtra por bot + permisos
            if not _is_allowed(bot_q, allowed):
                return jsonify({"leads": []})
            leads = _list_leads_by_bot(bot_q)
        else:
            # todos (luego aplicamos filtro de permisos)
            leads = _list_leads_all()
            if allowed != "*":
                allowed_set = set(allowed) if isinstance(allowed, list) else set()
                leads = [l for l in leads if l.get("bot") in allowed_set]

        return jsonify({"leads": leads})
    except Exception as e:
        print(f"❌ Error leyendo leads: {e}")
        return jsonify({"leads": []}), 500

@mobile_bp.route("/lead", methods=["POST"])
def mobile_update_lead():
    """
    Actualiza estado y/o nota (alias visible) de un lead.
    Body JSON: { "bot": "Sara", "numero": "whatsapp:+1...", "estado": "en espera", "nota": "Carlos" }
    Requiere permiso sobre ese bot vía token.
    """
    try:
        body = request.get_json(force=True) or {}
    except Exception:
        body = {}
    bot_name = (body.get("bot") or "").strip()
    numero = (body.get("numero") or "").strip()
    estado = body.get("estado", None)
    nota = body.get("nota", None)

    if not bot_name or not numero:
        return jsonify({"ok": False, "error": "params"}), 400

    allowed = _allowed_from_request(request)
    if not _is_allowed(bot_name, allowed):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    ok = _update_lead(bot_name, numero, estado=estado, nota=nota)
    return jsonify({"ok": bool(ok)})

@mobile_bp.route("/delete", methods=["POST"])
def mobile_delete_lead():
    """
    Elimina completamente una conversación.
    Body JSON: { "bot": "Sara", "numero": "whatsapp:+1..." }
    Requiere permiso sobre ese bot vía token.
    """
    try:
        body = request.get_json(force=True) or {}
    except Exception:
        body = {}
    bot_name = (body.get("bot") or "").strip()
    numero = (body.get("numero") or "").strip()

    if not bot_name or not numero:
        return jsonify({"ok": False, "error": "params"}), 400

    allowed = _allowed_from_request(request)
    if not _is_allowed(bot_name, allowed):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    ok = _delete_lead(bot_name, numero)
    return jsonify({"ok": bool(ok)})

# --------------------------------------------------------------------
# NUEVO: Info de bots (business_name por bot)
# --------------------------------------------------------------------
@mobile_bp.route("/bot_info", methods=["GET"])
def mobile_bot_info():
    """
    Devuelve el 'business_name' real para cada bot.
    - GET /api/mobile/bot_info?bot=Sara  -> { ok:true, bot:"Sara", business_name:"IN HOUSTON TEXAS" }
    - GET /api/mobile/bot_info           -> { ok:true, bots:[ {bot:"Sara", business_name:"..."}, ... ] } (filtrado por permisos)
    Respeta los permisos del token Bearer: si el token tiene lista de bots, se filtra.
    """
    allowed = _allowed_from_request(request)  # "*" o lista de bot-names
    requested = (request.args.get("bot") or "").strip()

    mapping = _bot_display_map()

    def _allowed_filter(name: str) -> bool:
        return _is_allowed(name, allowed)

    if requested:
        # Responder solo ese bot (si existe y está permitido)
        if requested in mapping and _allowed_filter(requested):
            return jsonify({"ok": True, "bot": requested, "business_name": mapping[requested]})
        # Bot inexistente o no permitido
        return jsonify({"ok": False, "error": "not_found_or_forbidden"}), 404

    # Responder todos los bots visibles por permisos
    items = [
        {"bot": name, "business_name": mapping[name]}
        for name in sorted(mapping.keys())
        if _allowed_filter(name)
    ]
    return jsonify({"ok": True, "bots": items})
