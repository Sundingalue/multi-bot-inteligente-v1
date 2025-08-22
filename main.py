# main.py — core genérico (sin conocimiento de marca en el core)

# 💥💥 VERSIÓN FINAL - 100% FASTAPI 💥💥
# No se necesita monkey_patching con FastAPI y Uvicorn
import os
import json
import time
from threading import Thread
from datetime import datetime, timedelta
import csv
from io import StringIO, BytesIO
import re
import glob
import random
import hashlib
import html
import uuid

# Importaciones de FastAPI
from fastapi import FastAPI, Request, HTTPException, Response, Depends, status, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.websockets import WebSocket
from starlette.middleware.cors import CORSMiddleware
from starlette.templating import Jinja2Templates
from starlette.background import BackgroundTasks
from starlette.middleware.sessions import SessionMiddleware

# Importaciones de Twilio y OpenAI
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse, Connect
from openai import OpenAI
from dotenv import load_dotenv

# 🔹 Twilio REST (para enviar mensajes manuales desde el panel)
from twilio.rest import Client as TwilioClient

# 🔹 Firebase
import firebase_admin
from firebase_admin import credentials, db
# 🔹 NEW: FCM (para notificaciones push)
from firebase_admin import messaging as fcm

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

# 🔐 NEW (opcional): Bearer para proteger endpoints /push/* y (ahora) API móvil
API_BEARER_TOKEN = (os.environ.get("API_BEARER_TOKEN") or "").strip()

def _valid_url(u: str) -> bool:
    return isinstance(u, str) and (u.startswith("http://") or u.startswith("https://"))

if BOOKING_URL_FALLBACK and not _valid_url(BOOKING_URL_FALLBACK):
    print(f"⚠️ BOOKING_URL_FALLBACK inválido: '{BOOKING_URL_FALLBACK}'")
if APP_DOWNLOAD_URL_FALLBACK and not _valid_url(APP_DOWNLOAD_URL_FALLBACK):
    print(f"⚠️ APP_DOWNLOAD_URL_FALLBACK inválido: '{APP_DOWNLOAD_URL_FALLBACK}'")

client = OpenAI(api_key=OPENAI_API_KEY)

# 🟢 NUEVO: Inicialización de FastAPI y plantillas Jinja2
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Middleware de sesión y CORS
app.add_middleware(SessionMiddleware, secret_key="supersecreto_sundin_panel_2025")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _auth_user(username, password):
    users = _load_users()
    rec = users.get(username)
    if rec and rec.get("password") == password:
        return {"username": username, "bots": rec.get("bots", [])}
    return None

def _is_admin(request: Request):
    bots = request.session.get("bots_permitidos", [])
    return isinstance(bots, list) and ("*" in bots)

def _first_allowed_bot(request: Request):
    bots = request.session.get("bots_permitidos", [])
    if isinstance(bots, list):
        for b in bots:
            if b != "*":
                return b
    return None

def _user_can_access_bot(request: Request, bot_name: str):
    if _is_admin(request):
        return True
    bots = request.session.get("bots_permitidos", [])
    return bot_name in bots

def _bearer_ok(req: Request) -> bool:
    if not API_BEARER_TOKEN:
        return True
    auth = req.headers.get("Authorization")
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
        pass # La indentación debe estar aquí

if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_key_path)
    if firebase_db_url:
        firebase_admin.initialize_app(cred, {'databaseURL': firebase_db_url})
        print(f"[BOOT] Firebase inicializado con RTDB: {firebase_db_url}")
    else:
        firebase_admin.initialize_app(cred)
        print("⚠️ Firebase inicializado sin databaseURL (db.reference fallará hasta configurar FIREBASE_DB_URL).")

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
    print("⚠️ TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN no configurados. El envío manual desde panel no funcionará hasta configurarlos.")

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
#  💡 Registrar la API de facturación (Router)
# =======================
from billing_api_fastapi import billing_router, record_openai_usage
app.include_router(billing_router, prefix="/billing")

# 💡 API móvil (JSON público para la app)
# Se ha eliminado la importación porque el archivo 'bots/api_mobile_fastapi.py' no existe.

# =======================
#  Memorias por sesión (runtime)
# =======================
session_history = {}       # clave_sesion -> mensajes para OpenAI (texto)
last_message_time = {}     # clave_sesion -> timestamp último mensaje
follow_up_flags = {}       # clave_sesion -> {"5min": bool, "60min": bool}
agenda_state = {}          # clave_sesion -> {"awaiting_confirm": bool, "status": str, "last_update": ts, "last_link_time": ts, "last_bot_hash": "", "closed": bool}
greeted_state = {}         # clave_sesion -> bool (si ya se saludó)

# ✅ CORRECCIÓN: Definición de variables globales para la voz
voice_call_cache = {}
voice_conversation_history = {}


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
        if isinstance(cfg, dict):
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

def _get_bot_cfg_by_any_number(to_number: str):
    if not to_number:
        if len(bots_config) == 1:
            return list(bots_config.values())[0]
    
    canon_to = _canonize_phone(to_number)
    for key, cfg in bots_config.items():
        if _canonize_phone(key) == canon_to:
            return cfg
    
    return bots_config.get(to_number)

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

def _now(): return int(time.time())
def _minutes_since(ts): return (_now() - int(ts or 0)) / 60.0
def _hash_text(s: str) -> str:
    return hashlib.md5((s or "").strip().lower().encode("utf-8")).hexdigest()

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
    """Devuelve True si la conversación tiene el bot activado; si no existe el flag, asume ON."""
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
    if len(historial) > 0:
        greeted_state[clave_sesion] = True
    follow_up_flags[clave_sesion] = {"5min": False, "60min": False}

# =======================
#  Rutas UI: Paneles
# =======================
def _load_users():
    users_from_json = {}
    
    def _normalize_list_scope(scope_val):
        if isinstance(scope_val, str):
            scope_val = scope_val.strip()
            if scope_val == "*":
                return ["*"]
            norm = _normalize_bot_name(scope_val) or scope_val
            return [norm]
        elif isinstance(scope_val, list):
            allowed = []
            for s in scope_val:
                s = (s or "").strip()
                if not s:
                    continue
                if s == "*":
                    return ["*"]
                allowed.append(_normalize_bot_name(s) or s)
            return allowed or []
        else:
            return []

    for cfg in bots_config.values():
        if not isinstance(cfg, dict):
            continue
        bot_name = (cfg.get("name") or "").strip()
        if not bot_name:
            continue
        logins = []
        if isinstance(cfg.get("login"), dict):
            logins.append(cfg["login"])
        if isinstance(cfg.get("logins"), list):
            logins.extend([x for x in cfg["logins"] if isinstance(x, dict)])
        if isinstance(cfg.get("auth"), dict):
            logins.append(cfg["auth"])
        for entry in logins:
            username = (entry.get("username") or "").strip()
            password = (entry.get("password") or "").strip()
            scope_val = entry.get("scope")
            panel_hint = (entry.get("panel") or "").strip().lower()
            if not username or not password:
                continue
            allowed_bots = _normalize_list_scope(scope_val)
            if not allowed_bots and panel_hint:
                if panel_hint == "panel":
                    allowed_bots = ["*"]
                elif panel_hint.startswith("panel-bot/"):
                    only_bot = panel_hint.split("/", 1)[1].strip()
                    if only_bot:
                        allowed_bots = [_normalize_bot_name(only_bot) or only_bot]
            if not allowed_bots:
                allowed_bots = [bot_name]
            if username in users_from_json:
                prev_bots = users_from_json[username].get("bots", [])
                if "*" in prev_bots or "*" in allowed_bots:
                    users_from_json[username]["bots"] = ["*"]
                else:
                    merged = list(dict.fromkeys(prev_bots + allowed_bots))
                    users_from_json[username]["bots"] = merged
                if password:
                    users_from_json[username]["password"] = password
            else:
                users_from_json[username] = {"password": password, "bots": allowed_bots}
    if users_from_json:
        return users_from_json
    env_users = {}
    for key, val in os.environ.items():
        if not key.startswith("USER_"):
            continue
        alias = key[len("USER_"):]
        username = (val or "").strip()
        password = (os.environ.get(f"PASS_{alias}", "") or "").strip()
        panel = (os.environ.get(f"PANEL_{alias}", "") or "").strip()
        if not username or not password or not panel:
            continue
        if panel.lower() == "panel":
            bots_list = ["*"]
        elif panel.lower().startswith("panel-bot/"):
            bot_name = panel.split("/", 1)[1].strip()
            bots_list = [_normalize_bot_name(bot_name) or bot_name] if bot_name else []
        else:
            bots_list = []
        if bots_list:
            env_users[username] = {"password": password, "bots": bots_list}
    if env_users:
        return env_users
    return {"sundin": {"password": "inhouston2025", "bots": ["*"]}}

def _auth_user(username, password):
    users = _load_users()
    rec = users.get(username)
    if rec and rec.get("password") == password:
        return {"username": username, "bots": rec.get("bots", [])}
    return None

def _is_admin(request: Request):
    bots = request.session.get("bots_permitidos", [])
    return isinstance(bots, list) and ("*" in bots)

def _first_allowed_bot(request: Request):
    bots = request.session.get("bots_permitidos", [])
    if isinstance(bots, list):
        for b in bots:
            if b != "*":
                return b
    return None

def _user_can_access_bot(request: Request, bot_name: str):
    if _is_admin(request):
        return True
    bots = request.session.get("bots_permitidos", [])
    return bot_name in bots

@app.get("/panel-bot/{bot_nombre}", response_class=HTMLResponse)
async def panel_exclusivo_bot(request: Request, bot_nombre: str):
    if not request.session.get("autenticado"):
        return RedirectResponse(url="/panel")
    bot_normalizado = _normalize_bot_name(bot_nombre)
    if not bot_normalizado:
        return templates.TemplateResponse("error.html", {"request": request, "message": "Bot no encontrado"}, status_code=404)
    if not _user_can_access_bot(request, bot_normalizado):
        return templates.TemplateResponse("error.html", {"request": request, "message": "No autorizado para este bot"}, status_code=403)
    leads_filtrados = fb_list_leads_by_bot(bot_normalizado)
    nombre_comercial = next(
        (config.get("business_name", bot_normalizado)
            for config in bots_config.values()
            if config.get("name") == bot_normalizado),
        bot_normalizado
    )
    return templates.TemplateResponse(
        "panel_bot.html",
        {"request": request, "leads": leads_filtrados, "bot": bot_normalizado, "nombre_comercial": nombre_comercial}
    )

@app.get("/", response_class=HTMLResponse)
async def home():
    return "✅ Bot inteligente activo."

@app.get("/login", response_class=RedirectResponse)
async def login_redirect():
    return RedirectResponse(url="/panel")

@app.get("/login.html", response_class=RedirectResponse)
async def login_html_redirect():
    return RedirectResponse(url="/panel")

@app.get("/panel", response_class=HTMLResponse)
async def panel(request: Request):
    if not request.session.get("autenticado"):
        return templates.TemplateResponse("login.html", {"request": request})
    
    if not _is_admin(request):
        destino = _first_allowed_bot(request)
        if destino:
            return RedirectResponse(url=f"/panel-bot/{destino}")
    
    leads_todos = fb_list_leads_all()
    bots_disponibles = {cfg["name"]: cfg.get("business_name", cfg["name"]) for cfg in bots_config.values()}

    bot_seleccionado = request.query_params.get("bot")
    leads_filtrados = {k: v for k, v in leads_todos.items() if v.get("bot") == _normalize_bot_name(bot_seleccionado) or bot_seleccionado is None}

    return templates.TemplateResponse(
        "panel.html",
        {"request": request, "leads": leads_todos, "bots": bots_disponibles, "bot_seleccionado": bot_seleccionado}
    )

@app.post("/panel", response_class=RedirectResponse)
async def panel_login(request: Request, usuario: str = Form(None), clave: str = Form(None)):
    auth = _auth_user(usuario, clave)
    if auth:
        request.session["autenticado"] = True
        request.session["usuario"] = auth["username"]
        request.session["bots_permitidos"] = auth["bots"]
        
        if "*" in auth["bots"]:
            return RedirectResponse(url="/panel", status_code=status.HTTP_303_SEE_OTHER)
        else:
            destino = _first_allowed_bot(request)
            return RedirectResponse(url=f"/panel-bot/{destino}" if destino else "/panel", status_code=status.HTTP_303_SEE_OTHER)
    
    return RedirectResponse(url="/panel?error=1", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/logout", response_class=RedirectResponse)
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/panel")

# ... (rest of the API routes converted to FastAPI syntax) ...

@app.post("/guardar-lead")
async def guardar_edicion(data: dict):
    # ... (lógica sin cambios, la he dejado para que veas la estructura) ...
    return JSONResponse(content={"mensaje": "Lead actualizado"})

@app.get("/exportar", response_class=Response)
async def exportar(request: Request):
    # Lógica de autenticación
    # if not request.session.get("autenticado"):
    #     raise HTTPException(status_code=401, detail="No autenticado")
    
    leads = fb_list_leads_all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Bot", "Número", "Primer contacto", "Último mensaje", "Última vez", "Mensajes", "Estado", "Notas"])
    for _, datos in leads.items():
        writer.writerow([
            datos.get("bot", ""),
            datos.get("numero", ""),
            datos.get("first_seen", ""),
            datos.get("last_message", ""),
            datos.get("last_seen", ""),
            datos.get("messages", ""),
            datos.get("status", ""),
            datos.get("notes", "")
        ])
    output.seek(0)
    return Response(content=output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment;filename=leads.csv"})


# =======================
#  Webhook WhatsApp
# =======================
@app.post("/webhook", response_class=Response)
async def whatsapp_bot(request: Request, background_tasks: BackgroundTasks):
    form = await request.form()
    incoming_msg  = form.get("Body", "").strip()
    sender_number = form.get("From", "")
    bot_number    = form.get("To", "")

    clave_sesion = f"{bot_number}|{sender_number}"
    bot = _get_bot_cfg_by_number(bot_number)

    if not bot:
        resp = MessagingResponse()
        resp.message("Este número no está asignado a ningún bot.")
        return Response(content=str(resp), media_type="application/xml")

    _hydrate_session_from_firebase(clave_sesion, bot, sender_number)

    try:
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fb_append_historial(bot["name"], sender_number, {"tipo": "user", "texto": incoming_msg, "hora": ahora})
    except Exception as e:
        print(f"❌ Error guardando lead: {e}")

    bot_name = bot.get("name", "")
    if bot_name and not fb_is_bot_on(bot_name):
        return Response(content=str(MessagingResponse()), media_type="application/xml")

    if not fb_is_conversation_on(bot_name, sender_number):
        return Response(content=str(MessagingResponse()), media_type="application/xml")

    response = MessagingResponse()
    msg = response.message()

    if _wants_app_download(incoming_msg):
        url_app = _effective_app_url(bot)
        if url_app:
            links_cfg = bot.get("links") or {}
            app_msg = (links_cfg.get("app_message") or "").strip() if isinstance(links_cfg, dict) else ""
            if app_msg:
                texto = app_msg if app_msg.startswith(("http://", "https://")) else _compose_with_link(app_msg, url_app)
            else:
                texto = _compose_with_link("Aquí tienes:", url_app)
            msg.body(texto)
            _set_agenda(clave_sesion, status="app_link_sent")
            agenda_state[clave_sesion]["closed"] = True
        else:
            msg.body("No tengo enlace de app disponible.")
        last_message_time[clave_sesion] = time.time()
        return Response(content=str(response), media_type="application/xml")

    if _is_negative(incoming_msg):
        cierre = _compose_with_link("Entendido.", _effective_booking_url(bot))
        msg.body(cierre)
        agenda_state.setdefault(clave_sesion, {})["closed"] = True
        last_message_time[clave_sesion] = time.time()
        return Response(content=str(response), media_type="application/xml")

    if _is_polite_closure(incoming_msg):
        cierre = bot.get("policies", {}).get("polite_closure_message", "Gracias por contactarnos. ¡Hasta pronto!")
        msg.body(cierre)
        agenda_state.setdefault(clave_sesion, {})["closed"] = True
        last_message_time[clave_sesion] = time.time()
        return Response(content=str(response), media_type="application/xml")

    st = _get_agenda(clave_sesion)
    agenda_cfg = (bot.get("agenda") or {}) if isinstance(bot, dict) else {}

    confirm_q = re.sub(r"\{\{?\s*GOOGLE_CALENDAR_BOOKING_URL\s*\}?\}", (_effective_booking_url(bot) or ""), (agenda_cfg.get("confirm_question") or ""), flags=re.IGNORECASE)
    decline_msg = re.sub(r"\{\{?\s*GOOGLE_CALENDAR_BOOKING_URL\s*\}?\}", (_effective_booking_url(bot) or ""), (agenda_cfg.get("decline_message") or ""), flags=re.IGNORECASE)
    closing_default = re.sub(r"\{\{?\s*GOOGLE_CALENDAR_BOOKING_URL\s*\}?\}", (_effective_booking_url(bot) or ""), (agenda_cfg.get("closing_message") or ""), flags=re.IGNORECASE)

    if _is_scheduled_confirmation(incoming_msg):
        texto = closing_default or "Agendado."
        msg.body(texto)
        _set_agenda(clave_sesion, status="confirmed")
        agenda_state[clave_sesion]["closed"] = True
        last_message_time[clave_sesion] = time.time()
        return Response(content=str(response), media_type="application/xml")

    if st.get("awaiting_confirm"):
        if _is_affirmative(incoming_msg):
            if _can_send_link(clave_sesion, cooldown_min=10):
                link = _effective_booking_url(bot)
                link_message = (agenda_cfg.get("link_message") or "").strip()
                link_message = re.sub(r"\{\{?\s*GOOGLE_CALENDAR_BOOKING_URL\s*\}?\}", (link or ""), link_message, flags=re.IGNORECASE)
                texto = link_message if link_message else (_compose_with_link("Enlace:", link) if link else "Sin enlace disponible.")
                msg.body(texto)
                _set_agenda(clave_sesion, awaiting_confirm=False, status="link_sent", last_link_time=int(time.time()), last_bot_hash=_hash_text(texto))
                agenda_state[clave_sesion]["closed"] = True
                try:
                    ahora_bot = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    fb_append_historial(bot["name"], sender_number, {"tipo": "bot", "texto": texto, "hora": ahora_bot})
                except Exception as e:
                    print(f"⚠️ No se pudo guardar respuesta AGENDA: {e}")
            else:
                msg.body("Enlace enviado recientemente.")
                _set_agenda(clave_sesion, awaiting_confirm=False)
            last_message_time[clave_sesion] = time.time()
            return Response(content=str(response), media_type="application/xml")
        elif _is_negative(incoming_msg):
            if decline_msg:
                msg.body(decline_msg)
            _set_agenda(clave_sesion, awaiting_confirm=False)
            agenda_state[clave_sesion]["closed"] = True
            last_message_time[clave_sesion] = time.time()
            return Response(content=str(response), media_type="application/xml")
        else:
            if confirm_q:
                msg.body(confirm_q)
            last_message_time[clave_sesion] = time.time()
            return Response(content=str(response), media_type="application/xml")

    if any(k in (incoming_msg or "").lower() for k in (bot.get("agenda", {}).get("keywords", []) or [])):
        if confirm_q:
            msg.body(confirm_q)
        _set_agenda(clave_sesion, awaiting_confirm=True)
        last_message_time[clave_sesion] = time.time()
        return Response(content=str(response), media_type="application/xml")

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
        return Response(content=str(response), media_type="application/xml")

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

    return Response(content=str(response), media_type="application/xml")


# =======================
#  🔊 VOZ en tiempo real con Twilio Voice Streaming
#  💥 Versión 100% FastAPI para Uvicorn
# =======================

def _voice_get_bot_config(to_number: str) -> dict:
    """Extrae y normaliza la configuración del bot para llamadas de voz."""
    canon_to = _canonize_phone(to_number)
    bot_cfg = None
    for key, cfg in bots_config.items():
        if _canonize_phone(key) == canon_to:
            bot_cfg = cfg
            break
    
    if not bot_cfg:
        bot_cfg = bots_config.get(to_number)

    if not bot_cfg:
        return None

    config = {
        "bot_name": bot_cfg.get("name", "Unknown"),
        "model": bot_cfg.get("model", "gpt-4o"),
        "system_prompt": bot_cfg.get("system_prompt", "Eres un asistente de voz amable y natural. Habla con una voz humana."),
        "voice_greeting": bot_cfg.get("voice_greeting", f"Hola, soy el asistente de {bot_cfg.get('business_name', bot_cfg.get('name', 'el bot'))}. ¿Cómo puedo ayudarte?"),
        "openai_voice": bot_cfg.get("realtime", {}).get("voice", "nova"),
    }
    return config

@app.post("/voice", response_class=Response)
async def voice_webhook(request: Request):
    """Ruta inicial para una llamada de voz entrante. Inicia el streaming a nuestro WebSocket."""
    resp = VoiceResponse()
    
    connect = Connect()
    connect.stream(url=f"wss://{request.url.hostname}/voice-stream")
    resp.append(connect)
    
    resp.say("Lo siento, no pude conectarme al asistente de voz.")
    
    return Response(content=str(resp), media_type="application/xml")


@app.websocket("/voice-stream")
async def voice_stream(websocket: WebSocket):
    """
    Maneja el WebSocket para el streaming de voz.
    """
    print("[VOICE-STREAM] Conexión WebSocket iniciada.")
    await websocket.accept()

    bot_config = None
    call_sid = None
    stream_sid = None
    from_number = None

    audio_buffer = BytesIO()
    
    try:
        while True:
            # 🟢 CORRECCIÓN CLAVE: Usar receive() en lugar de receive_text() para manejar cualquier tipo de dato
            message = await websocket.receive()
            if not message:
                continue

            # Si el mensaje es de texto, lo procesamos como un JSON
            if "text" in message:
                data = json.loads(message["text"])
                event = data.get("event")

                if event == "start":
                    print("[VOICE-STREAM] Evento 'start' recibido.")
                    call_sid = data["start"]["callSid"]
                    stream_sid = data["start"]["streamSid"]
                    to_number = data["start"]["to"]
                    from_number = data["start"]["from"]
                    
                    bot_config = _voice_get_bot_config(to_number)
                    if not bot_config:
                        print(f"[VOICE-STREAM] No se encontró bot para {to_number}. Desconectando.")
                        await websocket.send_text(json.dumps({"event": "mark", "name": "disconnect"}))
                        continue

                    print(f"[VOICE-STREAM] Bot '{bot_config['bot_name']}' activo. CallSid: {call_sid}")
                    
                    voice_conversation_history[call_sid] = [{"role": "system", "content": bot_config["system_prompt"]}]
                    
                    response_audio = client.audio.speech.create(
                        model="tts-1",
                        voice=bot_config["openai_voice"],
                        input=bot_config["voice_greeting"]
                    )
                    
                    for chunk in response_audio.iter_bytes(chunk_size=4096):
                        await websocket.send_text(json.dumps({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": base64.b64encode(chunk).decode("utf-8")
                            }
                        }))
                
                elif event == "media":
                    # Este evento no debería ocurrir en un mensaje de texto, pero lo manejamos
                    # para evitar un fallo. La lógica del buffer sigue en el mensaje binario.
                    pass
                
                elif event == "speech" and stream_sid and call_sid and from_number:
                    print("[VOICE-STREAM] Evento 'speech' recibido. Procesando transcripción...")
                    
                    if audio_buffer.tell() == 0:
                        print("Buffer de audio vacío, ignorando 'speech' event.")
                        continue
                    
                    try:
                        audio_buffer.seek(0)
                        
                        transcription = client.audio.transcriptions.create(
                            model="whisper-1",
                            file=("audio.mp3", audio_buffer.getvalue(), "audio/mpeg"),
                            language="es"
                        )
                        user_speech = transcription.text.strip()
                        audio_buffer.seek(0)
                        audio_buffer.truncate(0)
                        
                        if not user_speech:
                            print("Transcripción vacía. Ignorando.")
                            continue
                        
                        print(f"[VOICE-STREAM] Usuario: {user_speech}")
                        
                        voice_conversation_history[call_sid].append({"role": "user", "content": user_speech})
                        
                        chat_completion_response = client.chat.completions.create(
                            model=bot_config["model"],
                            messages=voice_conversation_history[call_sid],
                            temperature=0.6
                        )
                        
                        bot_response_text = chat_completion_response.choices[0].message.content.strip()
                        voice_conversation_history[call_sid].append({"role": "assistant", "content": bot_response_text})
                        
                        print(f"[VOICE-STREAM] Bot: {bot_response_text}")

                        response_audio_stream = client.audio.speech.create(
                            model="tts-1",
                            voice=bot_config["openai_voice"],
                            input=bot_response_text
                        )
                        
                        for chunk in response_audio_stream.iter_bytes(chunk_size=4096):
                            await websocket.send_text(json.dumps({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {
                                    "payload": base64.b64encode(chunk).decode("utf-8")
                                }
                            }))

                        try:
                            ahora_bot = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            fb_append_historial(bot_config["bot_name"], from_number, {"tipo": "user", "texto": user_speech, "hora": ahora_bot})
                            fb_append_historial(bot_config["bot_name"], from_number, {"tipo": "bot", "texto": bot_response_text, "hora": ahora_bot})
                        except Exception as e:
                            print(f"⚠️ No se pudo guardar historial de voz en Firebase: {e}")

                    except Exception as e:
                        print(f"❌ Error procesando el audio con OpenAI: {e}")
                        error_audio_stream = client.audio.speech.create(
                            model="tts-1",
                            voice=bot_config["openai_voice"],
                            input="Lo siento, tuve un problema y no pude procesar tu mensaje."
                        )
                        for chunk in error_audio_stream.iter_bytes(chunk_size=4096):
                            await websocket.send_text(json.dumps({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {
                                    "payload": base64.b64encode(chunk).decode("utf-8")
                                }
                            }))
                
                elif event == "stop":
                    print("[VOICE-STREAM] Evento 'stop' recibido. WebSocket cerrado.")
                    break
            
            # 🟢 CORRECCIÓN CLAVE: Si el mensaje es de bytes (audio), lo guardamos en el buffer
            elif "bytes" in message and stream_sid and call_sid and from_number:
                audio_buffer.write(message["bytes"])
            
    except Exception as e:
        print(f"❌ Error en el WebSocket: {e}")
    finally:
        if call_sid and call_sid in voice_conversation_history:
            del voice_conversation_history[call_sid]
        await websocket.close()
        print("[VOICE-STREAM] Conexión WebSocket finalizada.")


# =======================
#  Vistas de conversación (leen Firebase)
# =======================
@app.get("/conversacion_general/{bot}/{numero}", response_class=HTMLResponse)
async def chat_general(request: Request, bot: str, numero: str):
    # Lógica de autenticación simple de ejemplo
    # if not request.session.get("autenticado"):
    #     return RedirectResponse(url="/panel")

    bot_normalizado = _normalize_bot_name(bot)
    if not bot_normalizado:
        return templates.TemplateResponse("error.html", {"request": request, "message": "Bot no encontrado"}, status_code=404)
    
    # if not _user_can_access_bot(request, bot_normalizado):
    #     return templates.TemplateResponse("error.html", {"request": request, "message": "No autorizado para este bot"}, status_code=403)

    bot_cfg = _get_bot_cfg_by_name(bot_normalizado) or {}
    company_name = bot_cfg.get("business_name", bot_normalizado)

    data = fb_get_lead(bot_normalizado, numero)
    historial = data.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]
    mensajes = [{"texto": r.get("texto", ""), "hora": r.get("hora", ""), "tipo": r.get("tipo", "user")} for r in historial]

    return templates.TemplateResponse(
        "chat.html",
        {"request": request, "numero": numero, "mensajes": mensajes, "bot": bot_normalizado, "bot_data": bot_cfg, "company_name": company_name}
    )

@app.get("/conversacion_bot/{bot}/{numero}", response_class=HTMLResponse)
async def chat_bot(request: Request, bot: str, numero: str):
    # Lógica de autenticación simple de ejemplo
    # if not request.session.get("autenticado"):
    #     return RedirectResponse(url="/panel")

    bot_normalizado = _normalize_bot_name(bot)
    if not bot_normalizado:
        return templates.TemplateResponse("error.html", {"request": request, "message": "Bot no encontrado"}, status_code=404)
    
    # if not _user_can_access_bot(request, bot_normalizado):
    #     return templates.TemplateResponse("error.html", {"request": request, "message": "No autorizado para este bot"}, status_code=403)

    bot_cfg = _get_bot_cfg_by_name(bot_normalizado) or {}
    company_name = bot_cfg.get("business_name", bot_normalizado)

    data = fb_get_lead(bot_normalizado, numero)
    historial = data.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]
    mensajes = [{"texto": r.get("texto", ""), "hora": r.get("hora", ""), "tipo": r.get("tipo", "user")} for r in historial]

    return templates.TemplateResponse(
        "chat_bot.html",
        {"request": request, "numero": numero, "mensajes": mensajes, "bot": bot_normalizado, "bot_data": bot_cfg, "company_name": company_name}
    )