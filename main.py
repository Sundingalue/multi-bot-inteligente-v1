# main.py — core genérico (sin conocimiento de marca en el core)
from gevent import monkey
monkey.patch_all()
from flask import Flask, request, session, redirect, url_for, send_file, jsonify, render_template, make_response
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse, Connect
from openai import OpenAI
from dotenv import load_dotenv
import os
import json
import time
from threading import Thread
from datetime import datetime, timedelta
import csv
from io import StringIO
import re
import glob
import random
import hashlib
from urllib.parse import parse_qs

# 🔹 Twilio REST (para enviar mensajes manuales desde el panel)
from twilio.rest import Client as TwilioClient

# 🔹 Firebase
import firebase_admin
from firebase_admin import credentials, db
# 🔹 NEW: FCM (para notificaciones push)
from firebase_admin import messaging as fcm

# 🔹 NEW (Realtime bridge) — dependencias WebSocket
import ssl
try:
    from flask_sock import Sock
    import websocket  # websocket-client
except Exception:
    print("⚠️ Falta dependencia para Realtime (instala): pip install flask-sock websocket-client")

# =======================
#  Cargar variables de entorno (Render -> Secret File)
# =======================
load_dotenv("/etc/secrets/.env")
load_dotenv()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") or ""

# Twilio REST creds (necesarias para enviar mensajes OUTBOUND)
TWILIO_ACCOUNT_SID = (os.environ.get("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN  = (os.environ.get("TWILIO_AUTH_TOKEN") or "").strip()

# Fallbacks globales (se usan SOLO si el bot no trae link en su JSON ni hay variable de entorno)
BOOKING_URL_FALLBACK = (os.environ.get("BOOKING_URL", "").strip())
APP_DOWNLOAD_URL_FALLBACK = (os.environ.get("APP_DOWNLOAD_URL", "").strip())

# 🔐 Bearer para proteger endpoints /push/* y API móvil
API_BEARER_TOKEN = (os.environ.get("API_BEARER_TOKEN") or "").strip()

# 🔹 NEW (Realtime): ajustes por defecto del modelo/voz
OPENAI_REALTIME_MODEL = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17").strip()
OPENAI_REALTIME_VOICE = os.environ.get("OPENAI_REALTIME_VOICE", "verse").strip()

def _valid_url(u: str) -> bool:
    return isinstance(u, str) and (u.startswith("http://") or u.startswith("https://"))

client = OpenAI(api_key=OPENAI_API_KEY)
app = Flask(__name__)
app.secret_key = "supersecreto_sundin_panel_2025"

# ✅ Sesión persistente (remember me)
app.permanent_session_lifetime = timedelta(days=60)
app.config.update({
    "SESSION_COOKIE_SAMESITE": "Lax",
    # Pon en True si sirves por HTTPS (recomendado en producción)
    "SESSION_COOKIE_SECURE": False if os.getenv("DEV_HTTP", "").lower() == "true" else True
})

# 🌐 CORS sencillo
@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

def _bearer_ok(req) -> bool:
    if not API_BEARER_TOKEN:
        return True
    auth = (req.headers.get("Authorization") or "").strip()
    return auth == f"Bearer {API_BEARER_TOKEN}"

# =======================
#  Inicializar Firebase
# =======================
firebase_key_path = "/etc/secrets/firebase.json"
firebase_db_url = (os.getenv("FIREBASE_DB_URL") or "").strip()

if not firebase_db_url:
    try:
        with open("/etc/secrets/FIREBASE_DB_URL", "r", encoding="utf-8") as f:
            firebase_db_url = f.read().strip().strip('"').strip("'")
            if firebase_db_url:
                print("[BOOT] FIREBASE_DB_URL leído desde Secret File.")
    except Exception:
        pass

if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_key_path)
    if firebase_db_url:
        firebase_admin.initialize_app(cred, {'databaseURL': firebase_db_url})
        print(f"[BOOT] Firebase inicializado con RTDB: {firebase_db_url}")
    else:
        firebase_admin.initialize_app(cred)
        print("⚠️ Firebase inicializado sin databaseURL (configura FIREBASE_DB_URL).")

# =======================
#  Twilio REST Client (para respuestas manuales)
# =======================
twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        print("[BOOT] Twilio REST client inicializado.")
    except Exception as e:
        print(f"⚠️ No se pudo inicializar Twilio REST client: {e}")
else:
    print("⚠️ TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN no configurados.")

# =======================
#  Cargar bots desde carpeta bots/
# =======================
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
if not bots_config:
    print("⚠️ No se encontraron bots en ./bots/*.json")

# =======================
#  💡 Registrar la API de facturación (Blueprint)
# =======================
from billing_api import billing_bp, record_openai_usage
app.register_blueprint(billing_bp, url_prefix="/billing")

# 💡 API móvil (JSON público para la app)
from bots.api_mobile import mobile_bp
app.register_blueprint(mobile_bp, url_prefix="/api/mobile")

# =======================
#  Memorias por sesión (runtime)
# =======================
session_history = {}
last_message_time = {}
follow_up_flags = {}
agenda_state = {}
greeted_state = {}

# =======================
#  Helpers generales (neutros)
# =======================
def _hora_to_epoch_ms(hora_str: str) -> int:
    try:
        dt = datetime.strptime(hora_str, "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0

def _normalize_bot_name(name: str):
    for cfg in bots_config.values():
        if cfg.get("name", "").lower() == str(name).lower():
            return cfg.get("name")
    return None

def _get_bot_cfg_by_name(name: str):
    if not name:
        return None
    for cfg in bots_config.values():
        if isinstance(cfg, dict) and cfg.get("name", "").lower() == name.lower():
            return cfg
    return None

def _get_bot_cfg_by_number(to_number: str):
    return bots_config.get(to_number)

# ✅ VOICE helper: canonizar número a E.164 (+1...)
def _canonize_phone(raw: str) -> str:
    s = str(raw or "").strip()
    for p in ("whatsapp:", "tel:", "sip:", "client:"):
        if s.startswith(p):
            s = s[len(p):]
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return ""
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits

# ✅ VOICE helper: encuentra bot por número (E.164 o whatsapp:+)
def _get_bot_cfg_by_any_number(to_number: str):
    if not to_number:
        return None
    target = _canonize_phone(to_number)
    if to_number in bots_config:
        return bots_config.get(to_number)
    cand_whatsapp = f"whatsapp:{target}"
    if cand_whatsapp in bots_config:
        return bots_config.get(cand_whatsapp)
    if target in bots_config:
        return bots_config.get(target)
    for key, cfg in bots_config.items():
        try:
            if _canonize_phone(key) == target:
                return cfg
        except Exception:
            continue
    try:
        if len(bots_config) == 1:
            return list(bots_config.values())[0]
    except Exception:
        pass
    return None

def _get_bot_number_by_name(bot_name: str) -> str:
    for number_key, cfg in bots_config.items():
        if isinstance(cfg, dict) and cfg.get("name", "").strip().lower() == (bot_name or "").strip().lower():
            return number_key
    return ""

def _split_sentences(text: str):
    parts = re.split(r'(?<=[\.\!\?])\s+', (text or "").strip())
    if len(parts) == 1 and len(text or "") > 280:
        parts = [text[:200].strip(), text[200:].strip()]
    return [p for p in parts if p]

def _apply_style(bot_cfg: dict, text: str) -> str:
    style = (bot_cfg or {}).get("style", {}) or {}
    short = bool(style.get("short_replies", True))
    max_sents = int(style.get("max_sentences", 2)) if style.get("max_sentences") is not None else 2
    if not text:
        return text
    if short:
        sents = _split_sentences(text)
        text = " ".join(sents[:max_sents]).strip()
    return text

def _next_probe_from_bot(bot_cfg: dict) -> str:
    style = (bot_cfg or {}).get("style", {}) or {}
    probes = style.get("probes") or []
    probes = [p.strip() for p in probes if isinstance(p, str) and p.strip()]
    if not probes:
        return ""
    return random.choice(probes)

def _ensure_question(bot_cfg: dict, text: str, force_question: bool) -> str:
    txt = re.sub(r"\s+", " ", (text or "")).strip()
    if not force_question:
        return txt
    if "?" in txt:
        return txt
    if not txt.endswith((".", "!", "…")):
        txt += "."
    probe = _next_probe_from_bot(bot_cfg)
    return f"{txt} {probe}".strip() if probe else txt

def _make_system_message(bot_cfg: dict) -> str:
    return (bot_cfg or {}).get("system_prompt", "") or ""

# =======================
#  Helpers de links por BOT
# =======================
def _drill_get(d: dict, path: str):
    cur = d
    for k in path.split("."):
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur

def _effective_booking_url(bot_cfg: dict) -> str:
    candidates = [
        "links.booking_url",
        "booking_url",
        "calendar_booking_url",
        "google_calendar_booking_url",
        "agenda.booking_url",
    ]
    for p in candidates:
        val = _drill_get(bot_cfg or {}, p)
        val = (val or "").strip() if isinstance(val, str) else ""
        if _valid_url(val):
            return val
    return BOOKING_URL_FALLBACK if _valid_url(BOOKING_URL_FALLBACK) else ""

def _effective_app_url(bot_cfg: dict) -> str:
    candidates = [
        "links.app_download_url",
        "links.app_url",
        "app_download_url",
        "app_url",
        "download_url",
        "link_app",
    ]
    for p in candidates:
        val = _drill_get(bot_cfg or {}, p)
        val = (val or "").strip() if isinstance(val, str) else ""
        if _valid_url(val):
            return val
    return APP_DOWNLOAD_URL_FALLBACK if _valid_url(APP_DOWNLOAD_URL_FALLBACK) else ""

# =======================
#  Intenciones
# =======================
SCHEDULE_OFFER_PAT = re.compile(
    r"\b(enlace|link|calendar|calendario|agendar|agenda|reservar|reserva|cita|schedule|book|appointment|meeting|call)\b",
    re.IGNORECASE
)

def _wants_link(text: str) -> bool:
    return bool(SCHEDULE_OFFER_PAT.search(text or ""))

def _wants_app_download(text: str) -> bool:
    t = (text or "").lower()
    has_app_word = any(w in t for w in ["app", "aplicación", "aplicacion", "ios", "android", "play store", "app store"])
    has_download_intent = any(w in t for w in ["descargar", "download", "bajar", "instalar", "link", "enlace"])
    return ("descargar app" in t) or ("download app" in t) or (has_app_word and has_download_intent)

def _is_affirmative(texto: str) -> bool:
    if not texto: return False
    t = texto.strip().lower()
    afirm = {"si","sí","ok","okay","dale","va","claro","por favor","hagamoslo","hagámoslo","perfecto","de una","yes","yep","yeah","sure","please"}
    return any(t == a or t.startswith(a + " ") for a in afirm)

def _is_negative(texto: str) -> bool:
    if not texto: return False
    t = re.sub(r'[.,;:!?]+$', '', texto.strip().lower())
    t = re.sub(r'\s+', ' ', t)
    negatives = {"no", "nop", "no gracias", "ahora no", "luego", "después", "despues", "not now"}
    return t in negatives

def _is_scheduled_confirmation(texto: str) -> bool:
    if not texto: return False
    t = texto.lower()
    kws = ["ya agende","ya agendé","agende","agendé","ya programe","ya programé","ya agendado","agendado","confirmé","confirmado","listo","done","booked","i booked","i scheduled","scheduled"]
    return any(k in t for k in kws)

def _is_polite_closure(texto: str) -> bool:
    if not texto: return False
    t = texto.strip().lower()
    cierres = {"gracias","muchas gracias","ok gracias","listo gracias","perfecto gracias","estamos en contacto","por ahora está bien","por ahora esta bien","luego te escribo","luego hablamos","hasta luego","buen día","buen dia","buenas noches","nos vemos","chao","bye","eso es todo","todo bien gracias"}
    return any(t == c or t.startswith(c + " ") for c in cierres)

_now = lambda: int(time.time())
_minutes_since = lambda ts: (_now() - int(ts or 0)) / 60.0
_hash_text = lambda s: hashlib.md5((s or "").strip().lower().encode("utf-8")).hexdigest()

def _get_agenda(clave):
    return agenda_state.get(clave) or {"awaiting_confirm": False, "status": "none", "last_update": 0, "last_link_time": 0, "last_bot_hash": "", "closed": False}

def _set_agenda(clave, **kw):
    st = _get_agenda(clave)
    st.update(kw)
    st["last_update"] = _now()
    agenda_state[clave] = st
    return st

def _can_send_link(clave, cooldown_min=10):
    st = _get_agenda(clave)
    if st.get("status") in ("link_sent", "confirmed") and _minutes_since(st.get("last_link_time")) < cooldown_min:
        return False
    return True

# =======================
#  Firebase: helpers de leads
# =======================

def _lead_ref(bot_nombre, numero):
    return db.reference(f"leads/{bot_nombre}/{numero}")

def fb_get_lead(bot_nombre, numero):
    ref = _lead_ref(bot_nombre, numero)
    data = ref.get()
    return data or {}

def fb_append_historial(bot_nombre, numero, entrada):
    ref = _lead_ref(bot_nombre, numero)
    lead = ref.get() or {}
    historial = lead.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]
    historial.append(entrada)
    lead["historial"] = historial
    lead["last_message"] = entrada.get("texto", "")
    lead["last_seen"] = entrada.get("hora", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lead["messages"] = int(lead.get("messages", 0)) + 1
    lead.setdefault("bot", bot_nombre)
    lead.setdefault("numero", numero)
    lead.setdefault("status", "nuevo")
    lead.setdefault("notes", "")
    ref.set(lead)

def fb_list_leads_all():
    root = db.reference("leads").get() or {}
    leads = {}
    if not isinstance(root, dict):
        return leads
    for bot_nombre, numeros in root.items():
        if not isinstance(numeros, dict):
            continue
        for numero, data in numeros.items():
            clave = f"{bot_nombre}|{numero}"
            leads[clave] = {
                "bot": bot_nombre,
                "numero": numero,
                "first_seen": data.get("first_seen", ""),
                "last_message": data.get("last_message", ""),
                "last_seen": data.get("last_seen", ""),
                "messages": int(data.get("messages", 0)),
                "status": data.get("status", "nuevo"),
                "notes": data.get("notes", "")
            }
    return leads

def fb_list_leads_by_bot(bot_nombre):
    numeros = db.reference(f"leads/{bot_nombre}").get() or {}
    leads = {}
    if not isinstance(numeros, dict):
        return leads
    for numero, data in numeros.items():
        clave = f"{bot_nombre}|{numero}"
        leads[clave] = {
                "bot": bot_nombre,
                "numero": numero,
                "first_seen": data.get("first_seen", ""),
                "last_message": data.get("last_message", ""),
                "last_seen": data.get("last_seen", ""),
                "messages": int(data.get("messages", 0)),
                "status": data.get("status", "nuevo"),
                "notes": data.get("notes", "")
        }
    return leads

# ✅ NUEVO: eliminar lead completo
def fb_delete_lead(bot_nombre, numero):
    try:
        _lead_ref(bot_nombre, numero).delete()
        return True
    except Exception as e:
        print(f"❌ Error eliminando lead {bot_nombre}/{numero}: {e}")
        return False

# ✅ NUEVO: vaciar solo el historial (mantener lead)
def fb_clear_historial(bot_nombre, numero):
    try:
        ref = _lead_ref(bot_nombre, numero)
        lead = ref.get() or {}
        lead["historial"] = []
        lead["messages"] = 0
        lead["last_message"] = ""
        lead["last_seen"] = ""
        lead.setdefault("status", "nuevo")
        lead.setdefault("notes", "")
        lead.setdefault("bot", bot_nombre)
        lead.setdefault("numero", numero)
        ref.set(lead)
        return True
    except Exception as e:
        print(f"❌ Error vaciando historial {bot_nombre}/{numero}: {e}")
        return False

# =======================
#  ✅ Kill-Switch GLOBAL por bot
# =======================

def fb_is_bot_on(bot_name: str) -> bool:
    try:
        val = db.reference(f"billing/status/{bot_name}").get()
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() == "on"
    except Exception as e:
        print(f"⚠️ Error leyendo status del bot '{bot_name}': {e}")
    return True  # si no hay dato, asumimos ON

# =======================
#  ✅ NUEVO: Kill-Switch por conversación (ON/OFF individual)
# =======================

def fb_is_conversation_on(bot_nombre: str, numero: str) -> bool:
    try:
        ref = _lead_ref(bot_nombre, numero)
        lead = ref.get() or {}
        val = lead.get("bot_enabled", None)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("on", "true", "1", "yes", "si", "sí")
    except Exception as e:
        print(f"⚠️ Error leyendo bot_enabled en {bot_nombre}/{numero}: {e}")
    return True

def fb_set_conversation_on(bot_nombre: str, numero: str, enabled: bool):
    try:
        ref = _lead_ref(bot_nombre, numero)
        cur = ref.get() or {}
        cur["bot_enabled"] = bool(enabled)
        ref.set(cur)
        return True
    except Exception as e:
        print(f"⚠️ Error guardando bot_enabled en {bot_nombre}/{numero}: {e}")
        return False

# =======================
#  🔄 Hidratar sesión desde Firebase (evita perder contexto tras reinicios)
# =======================

def _hydrate_session_from_firebase(clave_sesion: str, bot_cfg: dict, sender_number: str):
    if clave_sesion in session_history:
        return
    bot_name = (bot_cfg or {}).get("name", "")
    if not bot_name:
        return
    lead = fb_get_lead(bot_name, sender_number) or {}
    historial = lead.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]

    msgs = []
    sysmsg = _make_system_message(bot_cfg)
    if sysmsg:
        msgs.append({"role": "system", "content": sysmsg})

    for reg in historial:
        texto = reg.get("texto", "")
        if not texto:
            continue
        role = "assistant" if (reg.get("tipo", "user") != "user") else "user"
        msgs.append({"role": role, "content": texto})

    if msgs:
        session_history[clave_sesion] = msgs
        greeted_state[clave_sesion] = True
        follow_up_flags[clave_sesion] = {"5min": False, "60min": False}

# =======================
#  Rutas UI básicas
# =======================
@app.route("/", methods=["GET"]) 
def home():
    print(f"[BOOT] BOOKING_URL_FALLBACK={BOOKING_URL_FALLBACK}")
    print(f"[BOOT] APP_DOWNLOAD_URL_FALLBACK={APP_DOWNLOAD_URL_FALLBACK}")
    return "✅ Bot inteligente activo."

@app.route("/login")
def login_redirect():
    return redirect(url_for("panel"))

@app.route("/login.html")
def login_html_redirect():
    return redirect(url_for("panel"))

# ======= Panel reducido (se mantiene tu lógica de leads y auth) =======
# (por brevedad aquí sólo dejamos endpoints críticos; el resto de vistas del
#  panel sigue igual que tu versión anterior)

# =======================
#  Webhook WhatsApp (core de conversación)
# =======================
@app.route("/webhook", methods=["GET"]) 
def verify_whatsapp():
    VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN_WHATSAPP")
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    else:
        return "Token inválido", 403


def _compose_with_link(prefix: str, link: str) -> str:
    if _valid_url(link):
        return f"{prefix.strip()} {link}".strip()
    return prefix.strip()

@app.route("/webhook", methods=["POST"]) 
def whatsapp_bot():
    incoming_msg  = (request.values.get("Body", "") or "").strip()
    sender_number = request.values.get("From", "")
    bot_number    = request.values.get("To", "")

    clave_sesion = f"{bot_number}|{sender_number}"
    bot = _get_bot_cfg_by_number(bot_number)

    if not bot:
        resp = MessagingResponse()
        resp.message("Este número no está asignado a ningún bot.")
        return str(resp)

    # Reconstruir contexto si reiniciamos
    _hydrate_session_from_firebase(clave_sesion, bot, sender_number)

    # Guardar SIEMPRE el mensaje del usuario
    try:
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fb_append_historial(bot["name"], sender_number, {"tipo": "user", "texto": incoming_msg, "hora": ahora})
    except Exception as e:
        print(f"❌ Error guardando lead: {e}")

    bot_name = bot.get("name", "")
    if bot_name and not fb_is_bot_on(bot_name):
        return str(MessagingResponse())

    if not fb_is_conversation_on(bot_name, sender_number):
        return str(MessagingResponse())

    response = MessagingResponse()
    msg = response.message()

    # Atajos neutrales
    if _wants_app_download(incoming_msg):
        url_app = _effective_app_url(bot)
        if url_app:
            links_cfg = bot.get("links") or {}
            app_msg = (links_cfg.get("app_message") or "").strip() if isinstance(links_cfg, dict) else ""
            if app_msg:
                texto = app_msg if ("http://" in app_msg or "https://" in app_msg) else _compose_with_link(app_msg, url_app)
            else:
                texto = _compose_with_link("Aquí tienes:", url_app)
            msg.body(texto)
            _set_agenda(clave_sesion, status="app_link_sent")
            agenda_state[clave_sesion]["closed"] = True
        else:
            msg.body("No tengo enlace de app disponible.")
        last_message_time[clave_sesion] = time.time()
        return str(response)

    if _is_negative(incoming_msg):
        cierre = _compose_with_link("Entendido.", _effective_booking_url(bot))
        msg.body(cierre)
        agenda_state.setdefault(clave_sesion, {})["closed"] = True
        last_message_time[clave_sesion] = time.time()
        return str(response)

    if _is_polite_closure(incoming_msg):
        cierre = bot.get("policies", {}).get("polite_closure_message", "Gracias por contactarnos. ¡Hasta pronto!")
        msg.body(cierre)
        agenda_state.setdefault(clave_sesion, {})["closed"] = True
        last_message_time[clave_sesion] = time.time()
        return str(response)

    # ====== Sesión / saludo ======
    if clave_sesion not in session_history:
        sysmsg = _make_system_message(bot)
        session_history[clave_sesion] = [{"role": "system", "content": sysmsg}] if sysmsg else []
        follow_up_flags[clave_sesion] = {"5min": False, "60min": False}
        greeted_state[clave_sesion] = False

    greeting_text = (bot.get("greeting") or "").strip()
    intro_keywords = (bot.get("intro_keywords") or [])

    if (not greeted_state.get(clave_sesion)) and greeting_text and any(w in incoming_msg.lower() for w in intro_keywords):
        msg.body(greeting_text)
        greeted_state[clave_sesion] = True
        last_message_time[clave_sesion] = time.time()
        return str(response)

    # ====== Continuación normal (GPT) ======
    session_history.setdefault(clave_sesion, []).append({"role": "user", "content": incoming_msg})
    last_message_time[clave_sesion] = time.time()

    try:
        model_name = (bot.get("model") or "gpt-4o").strip()
        temperature = float(bot.get("temperature", 0.6)) if isinstance(bot.get("temperature", None), (int, float)) else 0.6

        completion = client.chat.completions.create(
            model=model_name,
            temperature=temperature,
            messages=session_history[clave_sesion]
        )

        respuesta = (completion.choices[0].message.content or "").strip()
        respuesta = _apply_style(bot, respuesta)
        style = (bot.get("style") or {})
        must_ask = bool(style.get("always_question", False))
        respuesta = _ensure_question(bot, respuesta, force_question=must_ask)

        st_prev = agenda_state.get(clave_sesion, {})
        if _hash_text(respuesta) == st_prev.get("last_bot_hash"):
            probe = _next_probe_from_bot(bot)
            if probe and probe not in respuesta:
                if not respuesta.endswith((".", "!", "…", "¿", "?")):
                    respuesta += "."
                respuesta = f"{respuesta} {probe}".strip()

        session_history[clave_sesion].append({"role": "assistant", "content": respuesta})
        msg.body(respuesta)
        agenda_state.setdefault(clave_sesion, {})
        agenda_state[clave_sesion]["last_bot_hash"] = _hash_text(respuesta)

        try:
            usage = getattr(completion, "usage", None)
            if usage:
                input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            else:
                usage_dict = getattr(completion, "to_dict", lambda: {})()
                input_tokens = int(((usage_dict or {}).get("usage") or {}).get("prompt_tokens", 0))
                output_tokens = int(((usage_dict or {}).get("usage") or {}).get("completion_tokens", 0))
            record_openai_usage(bot.get("name", ""), model_name, input_tokens, output_tokens)
        except Exception as e:
            print(f"⚠️ No se pudo registrar tokens en billing: {e}")

        try:
            ahora_bot = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fb_append_historial(bot["name"], sender_number, {"tipo": "bot", "texto": respuesta, "hora": ahora_bot})
        except Exception as e:
            print(f"⚠️ No se pudo guardar respuesta del bot: {e}")

    except Exception as e:
        print(f"❌ Error con GPT: {e}")
        msg.body("Error generando la respuesta.")

    return str(response)

# =======================
#  🔊 VOZ con OpenAI Realtime + Twilio Media Streams
# =======================

def _wss_base():
    base = (request.url_root or "").strip().rstrip("/")
    if base.startswith("http://"):
        base = "wss://" + base[len("http://"):]
    elif base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    else:
        base = "wss://" + base
    return base

@app.route("/voice", methods=["POST", "GET"]) 
def voice_entry():
    to_number = (request.values.get("To") or "").strip()
    bot_cfg = _get_bot_cfg_by_any_number(to_number) or {}
    bot_name = bot_cfg.get("name", "") or "default"

    vr = VoiceResponse()
    stream_url = f"{_wss_base()}/twilio-media-stream?bot={bot_name}"
    print(f"[VOICE] Respondiendo TwiML. bot={bot_name} stream_url={stream_url}")
    connect = Connect()
    connect.stream(url=stream_url)
    vr.append(connect)
    return str(vr), 200, {"Content-Type": "text/xml"}

# === WebSocket (Twilio -> OpenAI Realtime) ===
sock = None
try:
    sock = Sock(app)
except Exception:
    print("⚠️ Sock no inicializado (instala flask-sock). Realtime por WS no disponible.")

# Utilidad: crear sesión en OpenAI Realtime via WebSocket

def _openai_realtime_ws(model: str, voice: str, system_prompt: str):
    headers = [
        "Authorization: Bearer " + OPENAI_API_KEY,
        "OpenAI-Beta: realtime=v1",
    ]
    url = f"wss://api.openai.com/v1/realtime?model={model}"

    ws = websocket.WebSocket()
    ws.connect(url, header=headers, sslopt={"cert_reqs": ssl.CERT_REQUIRED})

    # Configurar sesión: MUY IMPORTANTE -> modalities incluye 'audio'
    session_update = {
        "type": "session.update",
        "session": {
            "voice": voice,
            "modalities": ["text", "audio"],
            "instructions": system_prompt or "Eres un asistente de voz amable, cercano y muy natural. Habla como humano.",
            "input_audio_format": {
                "mime_type": "audio/mulaw;rate=8000",
                "channels": 1
            },
            "output_audio_format": {
                "mime_type": "audio/mulaw;rate=8000",
                "channels": 1
            },
            # Transcripción para mejor contexto (opcional)
            "input_audio_transcription": {"model": "gpt-4o-mini-transcribe"},
            "turn_detection": {"type": "server_vad"},
        }
    }
    ws.send(json.dumps(session_update))
    return ws

# Helper para mandar audio de vuelta a Twilio

def _send_twi_media(ws_twi, stream_sid, chunk_base64):
    if not chunk_base64:
        return
    out = {
        "event": "media",
        "streamSid": stream_sid,
        "media": {"payload": chunk_base64}
    }
    try:
        ws_twi.send(json.dumps(out))
    except Exception as e:
        print("⚠️ Error enviando media a Twilio:", e)

# Forzar creación de respuesta (por si el VAD no dispara solo)

def _flush_openai_response(ws_ai):
    try:
        ws_ai.send(json.dumps({"type": "response.create", "response": {"conversation": True, "modalities": ["audio"]}}))
    except Exception:
        pass

if sock:
    @sock.route('/twilio-media-stream')
    def twilio_media_stream(ws_twi):
        # === Recuperar el 'bot' del querystring de forma robusta (flask_sock) ===
        try:
            qs = ws_twi.environ.get("QUERY_STRING", "")
            args = {k: v[0] for k, v in parse_qs(qs).items()}
        except Exception:
            args = {}
        bot_name = (args.get("bot") or "default").strip()
        bot_cfg = _get_bot_cfg_by_name(bot_name) or {}

        sysmsg = _make_system_message(bot_cfg)
        model = (bot_cfg.get("realtime_model") or OPENAI_REALTIME_MODEL).strip()
        voice = (bot_cfg.get("voice", {}).get("voice_name") or OPENAI_REALTIME_VOICE).strip()

        print(f"[WS] Twilio conectado. bot={bot_name} model={model.split(':')[0]} voice={voice}")

        # Conectar con OpenAI Realtime
        try:
            ws_ai = _openai_realtime_ws(model, voice, sysmsg)
        except Exception as e:
            print("❌ No se pudo conectar a OpenAI Realtime:", e)
            try:
                ws_twi.send(json.dumps({"event":"stop"}))
            except Exception:
                pass
            return

        stream_sid = None
        ai_reader_running = True

        # Hilo lector (AI -> Twilio)
        def _ai_reader():
            while ai_reader_running:
                try:
                    msg = ws_ai.recv()
                    if not msg:
                        continue
                    data = json.loads(msg)

                    if data.get("type") == "response.audio.delta":
                        payload = data.get("delta") or ""
                        if payload and stream_sid:
                            _send_twi_media(ws_twi, stream_sid, payload)
                    elif data.get("type") == "error":
                        print("[WS][AI] error:", data)
                    # (otros tipos ignorados)
                except Exception as e:
                    print("ℹ️ AI reader finalizado:", e)
                    break

        reader_thread = Thread(target=_ai_reader, daemon=True)
        reader_thread.start()

        # Acumulador para hacer commits periódicos si Twilio no manda 'mark'
        frames_since_commit = 0
        COMMIT_EVERY_FRAMES = 50  # ~1s
        last_media_ts = time.time()

        try:
            while True:
                raw = ws_twi.receive()
                if raw is None:
                    break
                try:
                    evt = json.loads(raw)
                except Exception:
                    continue

                etype = evt.get("event")

                if etype == "start":
                    stream_sid = (((evt.get("start") or {}).get("streamSid")) or stream_sid)
                    print(f"[WS] start streamSid={stream_sid}")
                elif etype == "media":
                    frames_since_commit += 1
                    last_media_ts = time.time()
                    chunk = ((evt.get("media") or {}).get("payload") or "")
                    if chunk:
                        # enviar buffer al AI
                        ws_ai.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": chunk,
                            "mime_type": "audio/mulaw;rate=8000"
                        }))
                    if frames_since_commit >= COMMIT_EVERY_FRAMES:
                        ws_ai.send(json.dumps({"type": "input_audio_buffer.commit"}))
                        _flush_openai_response(ws_ai)
                        frames_since_commit = 0
                        print("[WS] commit forzado por tamaño de buffer")
                elif etype == "mark":
                    ws_ai.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    _flush_openai_response(ws_ai)
                    frames_since_commit = 0
                    print("[WS] commit por mark")
                elif etype == "stop":
                    print("[WS] stop recibido de Twilio")
                    try:
                        ws_ai.send(json.dumps({"type": "input_audio_buffer.commit"}))
                        _flush_openai_response(ws_ai)
                        print("[WS] commit final en stop")
                    except Exception:
                        pass
                    break

                # commit por silencio (3s sin media)
                if time.time() - last_media_ts > 3 and frames_since_commit > 0:
                    try:
                        ws_ai.send(json.dumps({"type": "input_audio_buffer.commit"}))
                        _flush_openai_response(ws_ai)
                        frames_since_commit = 0
                        print("[WS] commit por silencio")
                    except Exception:
                        pass

        except Exception as e:
            print("⚠️ WS Twilio error:", e)
        finally:
            try:
                ai_reader_running = False
                try:
                    ws_ai.close()
                except Exception:
                    pass
            except Exception:
                pass
            print("[WS] conexión cerrada")

# =======================
#  API de polling (lee Firebase) — versión reducida
# =======================
@app.route("/api/chat/<bot>/<numero>", methods=["GET", "OPTIONS"]) 
def api_chat(bot, numero):
    if request.method == "OPTIONS":
        return ("", 204)
    if not session.get("autenticado") and not _bearer_ok(request):
        return jsonify({"error": "No autenticado"}), 401

    bot_normalizado = _normalize_bot_name(bot)
    if not bot_normalizado:
        return jsonify({"error": "Bot no encontrado"}), 404

    since_param = request.args.get("since", "").strip()
    try:
        since_ms = int(since_param) if since_param else 0
    except ValueError:
        since_ms = 0

    data = fb_get_lead(bot_normalizado, numero)
    historial = data.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]

    nuevos = []
    last_ts = since_ms
    for reg in historial:
        ts = _hora_to_epoch_ms(reg.get("hora", ""))
        if ts > since_ms:
            nuevos.append({"texto": reg.get("texto", ""), "hora": reg.get("hora", ""), "tipo": reg.get("tipo", "user"), "ts": ts})
        if ts > last_ts:
            last_ts = ts

    if since_ms == 0 and not nuevos and historial:
        for reg in historial:
            ts = _hora_to_epoch_ms(reg.get("hora", ""))
            if ts > last_ts:
                last_ts = ts
        nuevos = [{"texto": reg.get("texto", ""), "hora": reg.get("hora", ""), "tipo": reg.get("tipo", "user"), "ts": _hora_to_epoch_ms(reg.get("hora", ""))} for reg in historial]

    bot_enabled = fb_is_conversation_on(bot_normalizado, numero)
    return jsonify({"mensajes": nuevos, "last_ts": last_ts, "bot_enabled": bool(bot_enabled)})

# =======================
#  Run
# =======================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[BOOT] BOOKING_URL_FALLBACK={BOOKING_URL_FALLBACK}")
    print(f"[BOOT] APP_DOWNLOAD_URL_FALLBACK={APP_DOWNLOAD_URL_FALLBACK}")
    app.run(host="0.0.0.0", port=port)
