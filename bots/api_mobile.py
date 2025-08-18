# bots/api_mobile.py
from flask import Blueprint, request, jsonify
from firebase_admin import db
from twilio.rest import Client as TwilioClient
import os, glob, json
from datetime import datetime

mobile_bp = Blueprint("mobile_bp", __name__)

# ===== Cargar bots (igual que main, pero local para evitar import circular) =====
def load_bots_folder():
    bots = {}
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

bots_config = load_bots_folder()

def _normalize_bot_name(name: str):
    for cfg in bots_config.values():
        if cfg.get("name", "").lower() == (name or "").lower():
            return cfg.get("name")
    return None

def _get_bot_number_by_name(bot_name: str) -> str:
    for number_key, cfg in bots_config.items():
        if isinstance(cfg, dict) and cfg.get("name", "").strip().lower() == (bot_name or "").strip().lower():
            return number_key
    return ""

def _lead_ref(bot_nombre, numero):
    return db.reference(f"leads/{bot_nombre}/{numero}")

def _hora_to_epoch_ms(hora_str: str) -> int:
    try:
        dt = datetime.strptime(hora_str, "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0

def _append_historial(bot_nombre, numero, entrada):
    ref = _lead_ref(bot_nombre, numero)
    lead = ref.get() or {}
    historial = lead.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]
    historial.append(entrada)
    lead["historial"]   = historial
    lead["last_message"] = entrada.get("texto", "")
    lead["last_seen"]    = entrada.get("hora", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lead["messages"]     = int(lead.get("messages", 0)) + 1
    lead.setdefault("bot", bot_nombre)
    lead.setdefault("numero", numero)
    lead.setdefault("status", "nuevo")
    lead.setdefault("notes", "")
    ref.set(lead)

# ===== Twilio REST (para enviar manual desde la app) =====
TWILIO_ACCOUNT_SID = (os.environ.get("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN  = (os.environ.get("TWILIO_AUTH_TOKEN")  or "").strip()
_twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        _twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        print("[MOBILE] Twilio REST listo.")
    except Exception as e:
        print(f"⚠️ Twilio REST no disponible: {e}")

# =========================
#         ENDPOINTS
# =========================

@mobile_bp.route("/leads", methods=["GET"])
def mobile_leads():
    bot = (request.args.get("bot") or "").strip()
    if bot:
        bot_norm = _normalize_bot_name(bot) or bot
        data = db.reference(f"leads/{bot_norm}").get() or {}
        items = []
        if isinstance(data, dict):
            for numero, d in data.items():
                items.append({
                    "bot": bot_norm,
                    "numero": numero,
                    "first_seen": d.get("first_seen", ""),
                    "last_message": d.get("last_message", ""),
                    "last_seen": d.get("last_seen", ""),
                    "messages": int(d.get("messages", 0)),
                    "status": d.get("status", "nuevo"),
                    "notes": d.get("notes", "")
                })
        return jsonify({"leads": items})
    else:
        root = db.reference("leads").get() or {}
        items = []
        if isinstance(root, dict):
            for bot_nombre, numeros in root.items():
                if not isinstance(numeros, dict):
                    continue
                for numero, d in numeros.items():
                    items.append({
                        "bot": bot_nombre,
                        "numero": numero,
                        "first_seen": d.get("first_seen", ""),
                        "last_message": d.get("last_message", ""),
                        "last_seen": d.get("last_seen", ""),
                        "messages": int(d.get("messages", 0)),
                        "status": d.get("status", "nuevo"),
                        "notes": d.get("notes", "")
                    })
        return jsonify({"leads": items})

@mobile_bp.route("/chat", methods=["GET"])
def mobile_chat():
    bot = (request.args.get("bot") or "").strip()
    numero = (request.args.get("numero") or "").strip()
    since = int((request.args.get("since") or "0").strip() or 0)

    bot_norm = _normalize_bot_name(bot) or bot
    lead = _lead_ref(bot_norm, numero).get() or {}
    historial = lead.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]

    nuevos, last_ts = [], since
    for reg in historial:
        ts = _hora_to_epoch_ms(reg.get("hora", ""))
        if ts > since:
            nuevos.append({
                "texto": reg.get("texto", ""),
                "hora": reg.get("hora", ""),
                "tipo": reg.get("tipo", "user"),
                "ts": ts
            })
        if ts > last_ts:
            last_ts = ts

    # Si since=0, devolvemos TODO el historial
    if since == 0 and historial:
        nuevos = [{
            "texto": reg.get("texto", ""),
            "hora": reg.get("hora", ""),
            "tipo": reg.get("tipo", "user"),
            "ts": _hora_to_epoch_ms(reg.get("hora", "")),
        } for reg in historial]
        last_ts = max((m["ts"] for m in nuevos), default=0)

    bot_enabled = lead.get("bot_enabled", True)
    return jsonify({"mensajes": nuevos, "last_ts": last_ts, "bot_enabled": bool(bot_enabled)})

@mobile_bp.route("/send_manual", methods=["POST"])
def mobile_send_manual():
    body = request.get_json(silent=True) or {}
    bot_nombre = (body.get("bot") or "").strip()
    numero = (body.get("numero") or "").strip()
    texto  = (body.get("texto") or "").strip()
    if not bot_nombre or not numero or not texto:
        return jsonify({"error": "Parámetros inválidos"}), 400

    bot_norm = _normalize_bot_name(bot_nombre) or bot_nombre
    from_number = _get_bot_number_by_name(bot_norm)

    if not _twilio_client or not from_number:
        return jsonify({"error": "Twilio no configurado o bot sin número"}), 500

    try:
        _twilio_client.messages.create(from_=from_number, to=numero, body=texto)
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _append_historial(bot_norm, numero, {"tipo": "admin", "texto": texto, "hora": ahora})
        return jsonify({"ok": True})
    except Exception as e:
        print(f"❌ Error Twilio (mobile): {e}")
        return jsonify({"error": "Fallo enviando el mensaje"}), 500

@mobile_bp.route("/conversation_bot", methods=["POST"])
def mobile_conv_switch():
    body = request.get_json(silent=True) or {}
    bot_nombre = (body.get("bot") or "").strip()
    numero = (body.get("numero") or "").strip()
    enabled = body.get("enabled", None)
    if enabled is None or not bot_nombre or not numero:
        return jsonify({"error": "Parámetros inválidos"}), 400

    bot_norm = _normalize_bot_name(bot_nombre) or bot_nombre
    ref = _lead_ref(bot_norm, numero)
    cur = ref.get() or {}
    cur["bot_enabled"] = bool(enabled)
    ref.set(cur)
    return jsonify({"ok": True, "enabled": bool(enabled)})

# ✅ NEW: actualizar estado y/o alias (notes)
@mobile_bp.route("/lead", methods=["POST"])
def mobile_update_lead():
    body = request.get_json(silent=True) or {}
    bot_nombre = (body.get("bot") or "").strip()
    numero = (body.get("numero") or "").strip()
    estado = (body.get("estado") or "").strip()
    nota   = body.get("nota", None)

    if not bot_nombre or not numero:
        return jsonify({"error": "Parámetros inválidos"}), 400

    bot_norm = _normalize_bot_name(bot_nombre) or bot_nombre
    ref = _lead_ref(bot_norm, numero)
    cur = ref.get() or {}
    if estado:
        cur["status"] = estado
    if nota is not None:
        cur["notes"] = (nota or "").strip()
    cur.setdefault("bot", bot_norm)
    cur.setdefault("numero", numero)
    ref.set(cur)
    return jsonify({"ok": True})

# ✅ NEW: borrar conversación completa
@mobile_bp.route("/delete", methods=["POST"])
def mobile_delete_lead():
    body = request.get_json(silent=True) or {}
    bot_nombre = (body.get("bot") or "").strip()
    numero     = (body.get("numero") or "").strip()
    if not bot_nombre or not numero:
        return jsonify({"error": "Parámetros inválidos"}), 400

    bot_norm = _normalize_bot_name(bot_nombre) or bot_nombre
    try:
        _lead_ref(bot_norm, numero).delete()
        return jsonify({"ok": True})
    except Exception as e:
        print(f"❌ Error eliminando lead {bot_norm}/{numero}: {e}")
        return jsonify({"ok": False}), 500
