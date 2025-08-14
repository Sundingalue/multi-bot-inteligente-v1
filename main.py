# main.py — core genérico (sin conocimiento de marca en el core)
from flask import Flask, request, session, redirect, url_for, send_file, jsonify, render_template
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from dotenv import load_dotenv
import os
import json
import time
from threading import Thread
from datetime import datetime
import csv
from io import StringIO
import re
import glob
import random
import hashlib

# 🔹 Firebase
import firebase_admin
from firebase_admin import credentials, db

# =======================
#  Cargar variables de entorno (Render -> Secret File)
# =======================
load_dotenv("/etc/secrets/.env")
load_dotenv()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") or ""

# Fallbacks globales (se usan SOLO si el bot no trae link en su JSON ni hay variable de entorno)
BOOKING_URL_FALLBACK = (os.environ.get("BOOKING_URL", "").strip())
APP_DOWNLOAD_URL_FALLBACK = (os.environ.get("APP_DOWNLOAD_URL", "").strip())

def _valid_url(u: str) -> bool:
    return isinstance(u, str) and (u.startswith("http://") or u.startswith("https://"))

if BOOKING_URL_FALLBACK and not _valid_url(BOOKING_URL_FALLBACK):
    print(f"⚠️ BOOKING_URL_FALLBACK inválido: '{BOOKING_URL_FALLBACK}'")
if APP_DOWNLOAD_URL_FALLBACK and not _valid_url(APP_DOWNLOAD_URL_FALLBACK):
    print(f"⚠️ APP_DOWNLOAD_URL_FALLBACK inválido: '{APP_DOWNLOAD_URL_FALLBACK}'")

client = OpenAI(api_key=OPENAI_API_KEY)
app = Flask(__name__)
app.secret_key = "supersecreto_sundin_panel_2025"

# =======================
#  Inicializar Firebase
# =======================
firebase_key_path = "/etc/secrets/firebase.json"

# 1) Intentar como variable de entorno
firebase_db_url = (os.getenv("FIREBASE_DB_URL") or "").strip()

# 2) Si no existe como env var, leerlo como Secret File en Render: /etc/secrets/FIREBASE_DB_URL
if not firebase_db_url:
    try:
        with open("/etc/secrets/FIREBASE_DB_URL", "r", encoding="utf-8") as f:
            firebase_db_url = f.read().strip().strip('"').strip("'")
            if firebase_db_url:
                print("[BOOT] FIREBASE_DB_URL leído desde Secret File.")
    except Exception:
        pass

if not firebase_db_url:
    print("❌ FIREBASE_DB_URL no configurado. Define la variable de entorno o crea el Secret File /etc/secrets/FIREBASE_DB_URL con la URL completa de tu RTDB.")

if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_key_path)
    if firebase_db_url:
        firebase_admin.initialize_app(cred, {'databaseURL': firebase_db_url})
        print(f"[BOOT] Firebase inicializado con RTDB: {firebase_db_url}")
    else:
        firebase_admin.initialize_app(cred)
        print("⚠️ Firebase inicializado sin databaseURL (db.reference fallará hasta configurar FIREBASE_DB_URL).")

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
#  Memorias por sesión (runtime)
# =======================
session_history = {}       # clave_sesion -> mensajes para OpenAI
last_message_time = {}     # clave_sesion -> timestamp último mensaje
follow_up_flags = {}       # clave_sesion -> {"5min": bool, "60min": bool}
agenda_state = {}          # clave_sesion -> {"awaiting_confirm": bool, "status": str, "last_update": ts, "last_link_time": ts, "last_bot_hash": str, "closed": bool}
greeted_state = {}         # clave_sesion -> bool (si ya se saludó)

# Datos por contacto (detectados en runtime, neutrales)
contact_name = {}          # clave_sesion -> "Carlos"

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

def _split_sentences(text: str):
    parts = re.split(r'(?<=[\.\!\?])\s+', text.strip())
    if len(parts) == 1 and len(text) > 280:
        parts = [text[:200].strip(), text[200:].strip()]
    return [p for p in parts if p]

def _apply_style(bot_cfg: dict, text: str) -> str:
    """Aplica solo recortes de longitud si el bot lo indica. No impone tono/personalidad."""
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
    """Devuelve una 'probe' solo si el JSON del bot la provee; si no, no agrega nada."""
    style = (bot_cfg or {}).get("style", {}) or {}
    probes = style.get("probes") or []
    probes = [p.strip() for p in probes if isinstance(p, str) and p.strip()]
    if not probes:
        return ""
    return random.choice(probes)

def _ensure_question(bot_cfg: dict, text: str, force_question: bool) -> str:
    """
    Solo agrega pregunta si el JSON del bot lo pide (style.always_question) y no está cerrada la conversación.
    """
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
    """Deja el 'system_prompt' tal cual viene del JSON del bot, sin aditivos."""
    return (bot_cfg or {}).get("system_prompt", "") or ""

# =======================
#  Helpers de links por BOT (JSON primero, env fallback)
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

# Reemplazo de placeholders viejos con el URL EFECTIVO del bot
PLACEHOLDER_PAT = re.compile(r"\{\{?\s*GOOGLE_CALENDAR_BOOKING_URL\s*\}?\}", re.IGNORECASE)
def _sanitize_link_placeholders_for_bot(text: str, bot_cfg: dict) -> str:
    if not isinstance(text, str):
        return text
    link = _effective_booking_url(bot_cfg)
    return PLACEHOLDER_PAT.sub(link if link else "", text)

# Detecciones de intención (neutras)
SCHEDULE_OFFER_PAT = re.compile(
    r"\b(enlace|link|calendar|calendario|agendar|agenda|reservar|reserva|cita|schedule|book|appointment|meeting|call)\b",
    re.IGNORECASE
)
def _wants_link(text: str) -> bool:
    return bool(SCHEDULE_OFFER_PAT.search(text or ""))

def _assistant_recently_offered_link(clave: str, lookback: int = 3) -> bool:
    msgs = session_history.get(clave, [])
    cnt = 0
    for m in reversed(msgs):
        if m.get("role") != "assistant":
            continue
        cnt += 1
        content = (m.get("content") or "")
        if SCHEDULE_OFFER_PAT.search(content):
            return True
        if cnt >= lookback:
            break
    return False

def _wants_app_download(text: str) -> bool:
    t = (text or "").lower()
    has_app_word = any(w in t for w in ["app", "aplicación", "aplicacion", "ios", "android", "play store", "app store"])
    has_download_intent = any(w in t for w in ["descargar", "download", "bajar", "instalar", "link", "enlace"])
    return ("descargar app" in t) or ("download app" in t) or (has_app_word and has_download_intent)

# ===== Intención de agenda (keywords del JSON del bot) =====
def _bot_agenda_keywords(bot_cfg):
    agenda = (bot_cfg or {}).get("agenda", {}) or {}
    kws = agenda.get("keywords") or []
    return [k.lower() for k in kws if isinstance(k, str) and k.strip()]

def _wants_to_schedule(texto: str, bot_cfg: dict) -> bool:
    if not texto:
        return False
    t = texto.lower()
    kws = _bot_agenda_keywords(bot_cfg)
    return any(k in t for k in kws)

def _is_affirmative(texto: str) -> bool:
    if not texto:
        return False
    t = texto.strip().lower()
    afirm = {"si","sí","ok","okay","dale","va","claro","por favor","hagamoslo","hagámoslo","perfecto","de una","yes","yep","yeah","sure","please"}
    return any(t == a or t.startswith(a + " ") for a in afirm)

def _is_negative(texto: str) -> bool:
    if not texto:
        return False
    t = texto.strip().lower()
    t = re.sub(r'[.,;:!?]+$', '', t)
    t = re.sub(r'\s+', ' ', t)
    negatives = {"no", "nop", "no gracias", "ahora no", "luego", "después", "despues", "not now"}
    return t in negatives

def _is_scheduled_confirmation(texto: str) -> bool:
    if not texto:
        return False
    t = texto.lower()
    kws = ["ya agende","ya agendé","agende","agendé","ya programe","ya programé","ya agendado","agendado","confirmé","confirmado","listo","done","booked","i booked","i scheduled","scheduled"]
    return any(k in t for k in kws)

def _is_polite_closure(texto: str) -> bool:
    if not texto:
        return False
    t = texto.strip().lower()
    cierres = {
        "gracias", "muchas gracias", "ok gracias", "listo gracias",
        "perfecto gracias", "estamos en contacto", "por ahora está bien",
        "por ahora esta bien", "luego te escribo", "luego hablamos",
        "hasta luego", "buen día", "buen dia", "buenas noches",
        "nos vemos", "chao", "bye", "eso es todo", "todo bien gracias"
    }
    return any(t == c or t.startswith(c + " ") for c in cierres)

# ===== Estado de agenda por sesión =====
def _now(): return int(time.time())
def _minutes_since(ts): return (_now() - int(ts or 0)) / 60.0
def _hash_text(s: str) -> str: return hashlib.md5((s or "").strip().lower().encode("utf-8")).hexdigest()

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

def _already_confirmed_recently(clave, window_days=14):
    st = _get_agenda(clave)
    return st.get("status") == "confirmed" and _minutes_since(st.get("last_update")) < (window_days*24*60)

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

# =======================
#  Leads y WhatsApp
# =======================
def guardar_lead(bot_nombre, numero, mensaje):
    try:
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lead = fb_get_lead(bot_nombre, numero)
        if not lead:
            lead = {
                "bot": bot_nombre,
                "numero": numero,
                "first_seen": ahora,
                "last_message": mensaje,
                "last_seen": ahora,
                "messages": 0,
                "status": "nuevo",
                "notes": "",
                "historial": []
            }
            _lead_ref(bot_nombre, numero).set(lead)
        fb_append_historial(bot_nombre, numero, {"tipo": "user", "texto": mensaje, "hora": ahora})
    except Exception as e:
        print(f"❌ Error guardando lead: {e}")

@app.after_request
def permitir_iframe(response):
    response.headers["X-Frame-Options"] = "ALLOWALL"
    return response

# =======================
#  Rutas UI: Paneles
# =======================
def _load_users():
    env_users = {}
    for key, val in os.environ.items():
        if not key.startswith("USER_"):
            continue
        alias = key[len("USER_"):]
        username = val.strip()
        password = os.environ.get(f"PASS_{alias}", "").strip()
        panel = os.environ.get(f"PANEL_{alias}", "").strip()
        if not username or not password or not panel:
            continue
        if panel.lower() == "panel":
            bots_list = ["*"]
        elif panel.lower().startswith("panel-bot/"):
            bot_name = panel.split("/", 1)[1].strip()
            bots_list = [bot_name] if bot_name else []
        else:
            bots_list = []
        if bots_list:
            env_users[username] = {"password": password, "bots": bots_list}
    if env_users:
        return env_users

    default_users = {"sundin": {"password": "inhouston2025", "bots": ["*"]}}
    raw = os.getenv("PANEL_USERS_JSON")
    if not raw:
        return default_users
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return default_users
        norm = {}
        for user, rec in data.items():
            pwd = rec.get("password") if isinstance(rec, dict) else None
            bots = rec.get("bots") if isinstance(rec, dict) else None
            if isinstance(pwd, str) and isinstance(bots, list) and bots:
                norm[user] = {"password": pwd, "bots": bots}
        return norm or default_users
    except Exception as e:
        print(f"⚠️ PANEL_USERS_JSON inválido: {e}")
        return default_users

def _auth_user(username, password):
    users = _load_users()
    rec = users.get(username)
    if rec and rec.get("password") == password:
        return {"username": username, "bots": rec.get("bots", [])}
    return None

def _is_admin():
    bots = session.get("bots_permitidos", [])
    return isinstance(bots, list) and ("*" in bots)

def _first_allowed_bot():
    bots = session.get("bots_permitidos", [])
    if isinstance(bots, list):
        for b in bots:
            if b != "*":
                return b
    return None

def _user_can_access_bot(bot_name):
    if _is_admin():
        return True
    bots = session.get("bots_permitidos", [])
    return bot_name in bots

@app.route("/panel-bot/<bot_nombre>")
def panel_exclusivo_bot(bot_nombre):
    if not session.get("autenticado"):
        return redirect(url_for("panel"))
    bot_normalizado = _normalize_bot_name(bot_nombre)
    if not bot_normalizado:
        return f"Bot '{bot_nombre}' no encontrado", 404
    if not _user_can_access_bot(bot_normalizado):
        return "No autorizado para este bot", 403
    leads_filtrados = fb_list_leads_by_bot(bot_normalizado)
    nombre_comercial = next(
        (config.get("business_name", bot_normalizado)
         for config in bots_config.values()
         if config.get("name") == bot_normalizado),
        bot_normalizado
    )
    return render_template("panel_bot.html", leads=leads_filtrados, bot=bot_normalizado, nombre_comercial=nombre_comercial)

@app.route("/", methods=["GET"])
def home():
    print(f"[BOOT] BOOKING_URL_FALLBACK={BOOKING_URL_FALLBACK}")
    print(f"[BOOT] APP_DOWNLOAD_URL_FALLBACK={APP_DOWNLOAD_URL_FALLBACK}")
    return "✅ Bot inteligente activo."

@app.route("/login", methods=["GET"])
def login_redirect():
    return redirect(url_for("panel"))

@app.route("/login.html", methods=["GET"])
def login_html_redirect():
    return redirect(url_for("panel"))

@app.route("/panel", methods=["GET", "POST"])
def panel():
    if not session.get("autenticado"):
        if request.method == "POST":
            usuario = request.form.get("usuario", "").strip()
            clave = request.form.get("clave", "").strip()
            auth = _auth_user(usuario, clave)
            if auth:
                session["autenticado"] = True
                session["usuario"] = auth["username"]
                session["bots_permitidos"] = auth["bots"]
                if "*" in auth["bots"]:
                    return redirect(url_for("panel"))
                destino = _first_allowed_bot()
                if destino:
                    return redirect(url_for("panel_exclusivo_bot", bot_nombre=destino))
                return redirect(url_for("panel"))
            return render_template("login.html", error=True)
        return render_template("login.html")

    if not _is_admin():
        destino = _first_allowed_bot()
        if destino:
            return redirect(url_for("panel_exclusivo_bot", bot_nombre=destino))

    leads_todos = fb_list_leads_all()
    bots_disponibles = {}
    for cfg in bots_config.values():
        bots_disponibles[cfg["name"]] = cfg.get("business_name", cfg["name"])

    bot_seleccionado = request.args.get("bot")
    if bot_seleccionado:
        bot_norm = _normalize_bot_name(bot_seleccionado) or bot_seleccionado
        leads_filtrados = {k: v for k, v in leads_todos.items() if v.get("bot") == bot_norm}
    else:
        leads_filtrados = leads_todos

    return render_template("panel.html", leads=leads_filtrados, bots=bots_disponibles, bot_seleccionado=bot_seleccionado)

@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("panel"))

# =======================
#  Guardar/Exportar
# =======================
@app.route("/guardar-lead", methods=["POST"])
def guardar_edicion():
    data = request.json or {}
    numero_key = (data.get("numero") or "").strip()
    estado = (data.get("estado") or "").strip()
    nota = (data.get("nota") or "").strip()

    if "|" not in numero_key:
        return jsonify({"error": "Parámetro 'numero' inválido"}), 400

    bot_nombre, numero = numero_key.split("|", 1)
    bot_normalizado = _normalize_bot_name(bot_nombre) or bot_nombre

    try:
        ref = _lead_ref(bot_normalizado, numero)
        current = ref.get() or {}
        if estado:
            current["status"] = estado
        if nota != "":
            current["notes"] = nota
        current.setdefault("bot", bot_normalizado)
        current.setdefault("numero", numero)
        ref.set(current)
    except Exception as e:
        print(f"⚠️ No se pudo actualizar en Firebase: {e}")

    return jsonify({"mensaje": "Lead actualizado"})

@app.route("/exportar")
def exportar():
    if not session.get("autenticado"):
        return redirect(url_for("panel"))
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
    return send_file(output, mimetype="text/csv", download_name="leads.csv", as_attachment=True)

# =======================
#  Utilidad: detectar nombre (neutro)
# =======================
def _extract_name(text: str) -> str:
    if not text:
        return ""
    t = text.strip()
    m = re.search(r"(?:me llamo|mi nombre es)\s+([A-Za-zÁÉÍÓÚÑáéíóúñ]+)", t, re.IGNORECASE)
    if m:
        return m.group(1).strip().capitalize()
    m = re.search(r"^soy\s+([A-Za-zÁÉÍÓÚÑáéíóúñ]+)", t, re.IGNORECASE)
    if m:
        return m.group(1).strip().capitalize()
    m = re.search(r"^(?:hola[,!\s]+)?([A-Za-zÁÉÍÓÚÑáéíóúñ]{3,})\b", t, re.IGNORECASE)
    if m:
        posible = m.group(1).strip().capitalize()
        if posible.lower() not in {"hola","buenas","buenos","dias","días","tardes","noches"}:
            return posible
    return ""

# =======================
#  Webhook WhatsApp
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
    """Concatena un texto con un link solo si el link es válido (sin tono/actitud)."""
    if _valid_url(link):
        return f"{prefix.strip()} {link}".strip()
    return prefix.strip()

@app.route("/webhook", methods=["POST"])
def whatsapp_bot():
    incoming_msg = (request.values.get("Body", "") or "").strip()
    sender_number = request.values.get("From", "")
    bot_number = request.values.get("To", "")

    clave_sesion = f"{bot_number}|{sender_number}"
    bot = _get_bot_cfg_by_number(bot_number)

    response = MessagingResponse()
    msg = response.message()

    if not bot:
        msg.body("Este número no está asignado a ningún bot.")
        return str(response)

    # Guarda el mensaje del usuario
    guardar_lead(bot["name"], sender_number, incoming_msg)

    # ================== Atajos neutrales ==================
    # APP: si piden descarga, responde con link si existe y cierra
    if _wants_app_download(incoming_msg):
        url_app = _effective_app_url(bot)
        if url_app:
            texto = _compose_with_link("Aquí tienes:", url_app)
            msg.body(texto)
            _set_agenda(clave_sesion, status="app_link_sent")
            agenda_state[clave_sesion]["closed"] = True
        else:
            msg.body("No tengo enlace de app disponible.")
        last_message_time[clave_sesion] = time.time()
        return str(response)

    # STOP conversacional si el usuario dice "no" o declina
    if _is_negative(incoming_msg):
        cierre = _compose_with_link("Entendido.", _effective_booking_url(bot))
        msg.body(cierre)
        agenda_state.setdefault(clave_sesion, {})["closed"] = True
        last_message_time[clave_sesion] = time.time()
        return str(response)

    # Cierre cortés (gracias/bye/etc.) sin insistir
    if _is_polite_closure(incoming_msg):
        cierre = _compose_with_link("Gracias.", _effective_booking_url(bot))
        msg.body(cierre)
        agenda_state.setdefault(clave_sesion, {})["closed"] = True
        last_message_time[clave_sesion] = time.time()
        return str(response)

    # ====== FLUJO AGENDA controlado por JSON del bot ======
    st = _get_agenda(clave_sesion)
    agenda_cfg = (bot.get("agenda") or {}) if isinstance(bot, dict) else {}

    confirm_q = _sanitize_link_placeholders_for_bot(agenda_cfg.get("confirm_question") or "", bot)
    decline_msg = _sanitize_link_placeholders_for_bot(agenda_cfg.get("decline_message") or "", bot)
    closing_default = _sanitize_link_placeholders_for_bot(agenda_cfg.get("closing_message") or "", bot)

    # Confirmación explícita "ya agendé/booked"
    if _is_scheduled_confirmation(incoming_msg):
        texto = closing_default or "Agendado."
        msg.body(texto)
        _set_agenda(clave_sesion, status="confirmed")
        agenda_state[clave_sesion]["closed"] = True
        last_message_time[clave_sesion] = time.time()
        return str(response)

    # Si está esperando confirmación del usuario para enviar link
    if st.get("awaiting_confirm"):
        if _is_affirmative(incoming_msg):
            if _can_send_link(clave_sesion, cooldown_min=10):
                link = _effective_booking_url(bot)

                # 🔸 NUEVO: mensaje personalizable desde JSON -> agenda.link_message
                link_message = (agenda_cfg.get("link_message") or "").strip()
                link_message = _sanitize_link_placeholders_for_bot(link_message, bot)
                if link_message:
                    # Si el template ya trae el URL (por placeholder) lo mandamos tal cual.
                    # Si no trae URL, adjuntamos el link válido al final.
                    if ("http://" in link_message) or ("https://" in link_message):
                        texto = link_message
                    else:
                        texto = _compose_with_link(link_message, link)
                else:
                    # Fallback histórico
                    texto = _compose_with_link("Enlace:", link) if link else "Sin enlace disponible."

                msg.body(texto)
                _set_agenda(clave_sesion, awaiting_confirm=False, status="link_sent", last_link_time=_now(), last_bot_hash=_hash_text(texto))
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
            return str(response)
        elif _is_negative(incoming_msg):
            if decline_msg:
                msg.body(decline_msg)
            _set_agenda(clave_sesion, awaiting_confirm=False)
            agenda_state[clave_sesion]["closed"] = True
            last_message_time[clave_sesion] = time.time()
            return str(response)
        else:
            if confirm_q:
                msg.body(confirm_q)
            last_message_time[clave_sesion] = time.time()
            return str(response)

    # Usuario pide agendar (por keywords del bot)
    if _wants_to_schedule(incoming_msg, bot):
        if confirm_q:
            msg.body(confirm_q)
        _set_agenda(clave_sesion, awaiting_confirm=True)
        last_message_time[clave_sesion] = time.time()
        return str(response)

    # ====== Sesión / saludo (solo si JSON lo define) ======
    if clave_sesion not in session_history:
        sysmsg = _make_system_message(bot)
        session_history[clave_sesion] = [{"role": "system", "content": sysmsg}] if sysmsg else []
        follow_up_flags[clave_sesion] = {"5min": False, "60min": False}
        greeted_state[clave_sesion] = False

    greeting_text = (bot.get("greeting") or "").strip()
    intro_keywords = (bot.get("intro_keywords") or [])

    if (not greeted_state.get(clave_sesion)) and greeting_text and any(w in incoming_msg.lower() for w in intro_keywords):
        txt = greeting_text
        msg.body(txt)
        greeted_state[clave_sesion] = True
        last_message_time[clave_sesion] = time.time()
        return str(response)

    # ====== Continuación normal (GPT) ======
    if "system" not in [m.get("role") for m in session_history.get(clave_sesion, [])]:
        pass

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

        st_prev = _get_agenda(clave_sesion)
        if _hash_text(respuesta) == st_prev.get("last_bot_hash"):
            probe = _next_probe_from_bot(bot)
            if probe and probe not in respuesta:
                if not respuesta.endswith((".", "!", "…", "¿", "?")):
                    respuesta += "."
                respuesta = f"{respuesta} {probe}".strip()

        session_history[clave_sesion].append({"role": "assistant", "content": respuesta})
        msg.body(respuesta)
        _set_agenda(clave_sesion, last_bot_hash=_hash_text(respuesta))

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
#  Vistas de conversación (leen Firebase)
# =======================
@app.route("/conversacion_general/<bot>/<numero>")
def chat_general(bot, numero):
    if not session.get("autenticado"):
        return redirect(url_for("panel"))
    bot_normalizado = _normalize_bot_name(bot)
    if not bot_normalizado:
        return "Bot no encontrado", 404
    if not _user_can_access_bot(bot_normalizado):
        return "No autorizado para este bot", 403

    bot_cfg = _get_bot_cfg_by_name(bot_normalizado) or {}
    company_name = bot_cfg.get("business_name", bot_normalizado)

    data = fb_get_lead(bot_normalizado, numero)
    historial = data.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]
    mensajes = [{"texto": r.get("texto", ""), "hora": r.get("hora", ""), "tipo": r.get("tipo", "user")} for r in historial]

    return render_template("chat.html", numero=numero, mensajes=mensajes, bot=bot_normalizado, bot_data=bot_cfg, company_name=company_name)

@app.route("/conversacion_bot/<bot>/<numero>")
def chat_bot(bot, numero):
    if not session.get("autenticado"):
        return redirect(url_for("panel"))
    bot_normalizado = _normalize_bot_name(bot)
    if not bot_normalizado:
        return "Bot no encontrado", 404
    if not _user_can_access_bot(bot_normalizado):
        return "No autorizado para este bot", 403

    bot_cfg = _get_bot_cfg_by_name(bot_normalizado) or {}
    company_name = bot_cfg.get("business_name", bot_normalizado)

    data = fb_get_lead(bot_normalizado, numero)
    historial = data.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]
    mensajes = [{"texto": r.get("texto", ""), "hora": r.get("hora", ""), "tipo": r.get("tipo", "user")} for r in historial]

    return render_template("chat_bot.html", numero=numero, mensajes=mensajes, bot=bot_normalizado, bot_data=bot_cfg, company_name=company_name)

# =======================
#  API de polling (lee Firebase)
# =======================
@app.route("/api/chat/<bot>/<numero>", methods=["GET"])
def api_chat(bot, numero):
    if not session.get("autenticado"):
        return jsonify({"error": "No autenticado"}), 401
    bot_normalizado = _normalize_bot_name(bot)
    if not bot_normalizado:
        return jsonify({"error": "Bot no encontrado"}), 404
    if not _user_can_access_bot(bot_normalizado):
        return jsonify({"error": "No autorizado"}), 403

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

    return jsonify({"mensajes": nuevos, "last_ts": last_ts})

# =======================
#  🔺 Borrar conversación (Firebase)
# =======================
@app.route("/api/delete_chat", methods=["POST"])
def api_delete_chat():
    if not session.get("autenticado"):
        return jsonify({"error": "No autenticado"}), 401
    data = request.get_json(silent=True) or {}
    bot_nombre = (data.get("bot") or "").strip()
    numero = (data.get("numero") or "").strip()
    if not bot_nombre or not numero:
        return jsonify({"error": "Faltan parámetros 'bot' y/o 'numero'"}), 400

    bot_normalizado = _normalize_bot_name(bot_nombre)
    if not bot_normalizado:
        return jsonify({"error": "Bot no encontrado"}), 404
    if not _user_can_access_bot(bot_normalizado):
        return jsonify({"error": "No autorizado"}), 403

    try:
        ref = _lead_ref(bot_normalizado, numero)
        ref.delete()
    except Exception as e:
        print(f"⚠️ No se pudo eliminar en Firebase: {e}")

    return jsonify({"ok": True})

# =======================
#  Follow-up (WhatsApp vía Twilio)
#  Ahora solo envía follow-ups si el JSON del bot define los mensajes.
# =======================
def follow_up_task(clave_sesion, bot_number):
    if agenda_state.get(clave_sesion, {}).get("closed"):
        return

    to_num = bot_number
    bot_cfg = bots_config.get(to_num) or {}
    fu = bot_cfg.get("follow_up", {}) if isinstance(bot_cfg, dict) else {}

    after_5 = fu.get("after_5min", "").strip() if isinstance(fu, dict) else ""
    after_60 = fu.get("after_60min", "").strip() if isinstance(fu, dict) else ""

    if not after_5 and not after_60:
        return

    time.sleep(300)
    if (not agenda_state.get(clave_sesion, {}).get("closed") and
        clave_sesion in last_message_time and
        time.time() - last_message_time[clave_sesion] >= 300 and
        not follow_up_flags[clave_sesion]["5min"] and
        after_5):
        send_whatsapp_message(clave_sesion.split("|")[1], after_5, bot_number)
        follow_up_flags[clave_sesion]["5min"] = True

    time.sleep(3300)
    if (not agenda_state.get(clave_sesion, {}).get("closed") and
        clave_sesion in last_message_time and
        time.time() - last_message_time[clave_sesion] >= 3600 and
        not follow_up_flags[clave_sesion]["60min"] and
        after_60):
        send_whatsapp_message(clave_sesion.split("|")[1], after_60, bot_number)
        follow_up_flags[clave_sesion]["60min"] = True

def send_whatsapp_message(to_number, message, bot_number=None):
    from twilio.rest import Client
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = bot_number if bot_number else os.environ.get("TWILIO_WHATSAPP_NUMBER")
    client_twilio = Client(account_sid, auth_token)
    client_twilio.messages.create(body=message, from_=from_number, to=to_number)

# =======================
#  Run
# =======================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[BOOT] BOOKING_URL_FALLBACK={BOOKING_URL_FALLBACK}")
    print(f"[BOOT] APP_DOWNLOAD_URL_FALLBACK={APP_DOWNLOAD_URL_FALLBACK}")
    app.run(host="0.0.0.0", port=port)
