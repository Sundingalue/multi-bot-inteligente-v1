# api_mobile.py — Endpoints JSON para la APP (sin login de panel)
from flask import Blueprint, request, jsonify
from datetime import datetime
import os
from firebase_admin import db

mobile_bp = Blueprint("mobile_bp", __name__)

API_BEARER_TOKEN = (os.environ.get("API_BEARER_TOKEN") or "").strip()

def _authorized(req) -> bool:
    if not API_BEARER_TOKEN:
        return True  # sin token => público (útil para tus pruebas)
    auth = (req.headers.get("Authorization") or "").strip()
    return auth == f"Bearer {API_BEARER_TOKEN}"

def _hora_to_epoch_ms(hora_str: str) -> int:
    try:
        dt = datetime.strptime(hora_str, "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0

def _lead_ref(bot_nombre, numero):
    return db.reference(f"leads/{bot_nombre}/{numero}")

def _list_leads_all():
    root = db.reference("leads").get() or {}
    out = []
    if not isinstance(root, dict):
        return out
    for bot_nombre, numeros in root.items():
        if not isinstance(numeros, dict):
            continue
        for numero, data in numeros.items():
            out.append({
                "bot": bot_nombre,
                "numero": numero,
                "first_seen": data.get("first_seen", ""),
                "last_message": data.get("last_message", ""),
                "last_seen": data.get("last_seen", ""),
                "messages": int(data.get("messages", 0) or 0),
                "status": data.get("status", "nuevo"),
                "notes": data.get("notes", ""),
            })
    out.sort(key=lambda x: (_hora_to_epoch_ms(x.get("last_seen","")), x.get("messages",0)), reverse=True)
    return out

def _list_leads_by_bot(bot_nombre):
    numeros = db.reference(f"leads/{bot_nombre}").get() or {}
    out = []
    if not isinstance(numeros, dict):
        return out
    for numero, data in numeros.items():
        out.append({
            "bot": bot_nombre,
            "numero": numero,
            "first_seen": data.get("first_seen", ""),
            "last_message": data.get("last_message", ""),
            "last_seen": data.get("last_seen", ""),
            "messages": int(data.get("messages", 0) or 0),
            "status": data.get("status", "nuevo"),
            "notes": data.get("notes", ""),
        })
    out.sort(key=lambda x: (_hora_to_epoch_ms(x.get("last_seen","")), x.get("messages",0)), reverse=True)
    return out

def _is_conversation_on(bot_nombre: str, numero: str) -> bool:
    lead = (_lead_ref(bot_nombre, numero).get() or {})
    val = lead.get("bot_enabled", None)
    if isinstance(val, bool): return val
    if isinstance(val, str):  return val.lower() in ("on","true","1","yes","si","sí")
    return True

def _set_conversation_on(bot_nombre: str, numero: str, enabled: bool) -> bool:
    ref = _lead_ref(bot_nombre, numero)
    cur = ref.get() or {}
    cur["bot_enabled"] = bool(enabled)
    ref.set(cur)
    return True

# ---- GET /api/mobile/leads?bot=Sara  -> lista de leads (por bot o todos)
@mobile_bp.route("/leads", methods=["GET"])
def mobile_leads():
    if not _authorized(request):
        return jsonify({"error":"Unauthorized"}), 401
    bot = (request.args.get("bot") or "").strip()
    leads = _list_leads_by_bot(bot) if bot else _list_leads_all()
    return jsonify({"leads": leads})

# ---- GET /api/mobile/chat?bot=Sara&numero=whatsapp:+1...&since=0 -> mensajes
@mobile_bp.route("/chat", methods=["GET"])
def mobile_chat():
    if not _authorized(request):
        return jsonify({"error":"Unauthorized"}), 401
    bot = (request.args.get("bot") or "").strip()
    numero = (request.args.get("numero") or "").strip()
    since_param = (request.args.get("since") or "").strip()
    if not bot or not numero:
        return jsonify({"error":"faltan parámetros bot/numero"}), 400
    try:
        since_ms = int(since_param) if since_param else 0
    except ValueError:
        since_ms = 0

    lead = _lead_ref(bot, numero).get() or {}
    historial = lead.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]

    nuevos, last_ts = [], since_ms
    for reg in historial:
        ts = _hora_to_epoch_ms(reg.get("hora",""))
        if ts > since_ms:
            nuevos.append({"texto": reg.get("texto",""),
                           "hora": reg.get("hora",""),
                           "tipo": reg.get("tipo","user"),
                           "ts": ts})
        if ts > last_ts: last_ts = ts

    if since_ms == 0 and not nuevos and historial:
        nuevos = [{"texto": r.get("texto",""),
                   "hora": r.get("hora",""),
                   "tipo": r.get("tipo","user"),
                   "ts": _hora_to_epoch_ms(r.get("hora",""))} for r in historial]
        for n in nuevos:
            if n["ts"] > last_ts: last_ts = n["ts"]

    return jsonify({"mensajes": nuevos, "last_ts": last_ts, "bot_enabled": _is_conversation_on(bot, numero)})

# ---- POST /api/mobile/conversation_bot  {bot,numero,enabled} -> ON/OFF
@mobile_bp.route("/conversation_bot", methods=["POST"])
def mobile_conv_toggle():
    if not _authorized(request):
        return jsonify({"error":"Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    bot = (data.get("bot") or "").strip()
    numero = (data.get("numero") or "").strip()
    enabled = data.get("enabled", None)
    if enabled is None or not bot or not numero:
        return jsonify({"error":"Parámetros inválidos (bot, numero, enabled)"}), 400
    _set_conversation_on(bot, numero, bool(enabled))
    return jsonify({"ok": True, "enabled": bool(enabled)})
