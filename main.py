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

# Fallbacks globales (solo si el bot no trae link en su JSON)
BOOKING_URL_FALLBACK = (os.environ.get("BOOKING_URL", "").strip()
                        or "https://calendar.app.google/2PAh6A4Lkxw3qxLC9")
APP_DOWNLOAD_URL_FALLBACK = (os.environ.get("APP_DOWNLOAD_URL", "").strip()
                             or "https://inhoustontexas.us/descargar-app/")

def _valid_url(u: str) -> bool:
    return isinstance(u, str) and (u.startswith("http://") or u.startswith("https://"))

if not _valid_url(BOOKING_URL_FALLBACK):
    print(f"⚠️ BOOKING_URL_FALLBACK inválido: '{BOOKING_URL_FALLBACK}'")
if not _valid_url(APP_DOWNLOAD_URL_FALLBACK):
    print(f"⚠️ APP_DOWNLOAD_URL_FALLBACK inválido: '{APP_DOWNLOAD_URL_FALLBACK}'")

client = OpenAI(api_key=OPENAI_API_KEY)
app = Flask(__name__)
app.secret_key = "supersecreto_sundin_panel_2025"

# =======================
#  Inicializar Firebase
# =======================
firebase_key_path = "/etc/secrets/firebase.json"
firebase_db_url = os.getenv("FIREBASE_DB_URL", "https://inhouston-209c0-default-rtdb.firebaseio.com/")
if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_key_path)
    firebase_admin.initialize_app(cred, {'databaseURL': firebase_db_url})

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
last_probe_used = {}       # clave_sesion -> índice de la última probe usada

# 👇 Nombre detectado y segundo saludo
contact_name = {}          # clave_sesion -> "Carlos"
second_greet_sent = {}     # clave_sesion -> bool

# =======================
#  Helpers generales
# =======================
GENERIC_FALLBACK_QUESTION = "¿Quieres que continúe con más detalles?"

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
    style = (bot_cfg or {}).get("style", {}) or {}
    short = bool(style.get("short_replies", True))
    max_sents = int(style.get("max_sentences", 2) or 2)
    if not text:
        return text
    if short:
        sents = _split_sentences(text)
        text = " ".join(sents[:max_sents]).strip()
    return text

def _next_probe(clave_sesion: str, bot_cfg: dict) -> str:
    style = (bot_cfg or {}).get("style", {}) or {}
    probes = style.get("probes") or []
    probes = [p.strip() for p in probes if isinstance(p, str) and p.strip()]
    if not probes:
        fb = style.get("fallback_question")
        if isinstance(fb, str) and fb.strip():
            return fb.strip()
        return GENERIC_FALLBACK_QUESTION
    last_idx = last_probe_used.get(clave_sesion, None)
    candidates = list(range(len(probes)))
    if last_idx is not None and last_idx in candidates and len(candidates) > 1:
        candidates.remove(last_idx)
    idx = random.choice(candidates)
    last_probe_used[clave_sesion] = idx
    return probes[idx]

def is_conversation_closed(clave: str) -> bool:
    st = agenda_state.get(clave) or {}
    return bool(st.get("closed", False))

def close_conversation(clave: str):
    st = agenda_state.get(clave) or {}
    st["closed"] = True
    st["last_update"] = int(time.time())
    agenda_state[clave] = st

def _ensure_question(bot_cfg: dict, text: str, clave_sesion: str = "", allow_question: bool = True) -> str:
    if not text:
        text = ""
    if is_conversation_closed(clave_sesion):
        return re.sub(r"\s+", " ", text).strip()
    if not allow_question:
        return re.sub(r"\s+", " ", text).strip()
    txt = re.sub(r"\s+", " ", text).strip()
    if "?" in txt:
        txt = re.sub(r"(\?\s*)(¿.+\?)", r"\1", txt)
        return txt
    if not txt.endswith((".", "!", "…")):
        txt += "."
    probe = _next_probe(clave_sesion, bot_cfg)
    return f"{txt} {probe}"

def _make_system_message(bot_cfg: dict) -> str:
    base = (bot_cfg or {}).get("system_prompt", "") or ""
    style = (bot_cfg or {}).get("style", {}) or {}
    short = "Responde en mensajes cortos." if style.get("short_replies", True) else ""
    max_s = style.get("max_sentences")
    askq = "Termina cada respuesta con una sola pregunta clara para avanzar."
    extra = f" Usa como máximo {max_s} oraciones." if isinstance(max_s, int) else ""
    squeeze = f"\n\nDirectriz de estilo: {short}{extra} {askq}".strip()
    return (base + squeeze).strip()

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
        if _valid_url(str(val or "").strip()):
            return str(val).strip()
    return BOOKING_URL_FALLBACK

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
        if _valid_url(str(val or "").strip()):
            return str(val).strip()
    return APP_DOWNLOAD_URL_FALLBACK

# Reemplazo de placeholders viejos con el URL EFECTIVO del bot
PLACEHOLDER_PAT = re.compile(r"\{\{?\s*(?:GOOGLE_CALENDAR_BOOKING_URL|BOOKING_URL|CALENDAR_BOOKING_URL)\s*\}?\}", re.IGNORECASE)
def _sanitize_link_placeholders_for_bot(text: str, bot_cfg: dict) -> str:
    if not isinstance(text, str):
        return text
    return PLACEHOLDER_PAT.sub(_effective_booking_url(bot_cfg), text)

# ===== Detección de intenciones =====
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
    neg = {"no","nop","no gracias","ahora no","luego","después","despues","not now","no mas preguntas","no más preguntas","no necesito mas","no necesito más"}
    return any(t == n or t.startswith(n + " ") for n in neg)

def _is_soft_ack(texto: str) -> bool:
    """Ok/Gracias/Listo/etc. (se usa para cerrar cortésmente tras enviar/ofrecer link)."""
    if not texto:
        return False
    t = texto.strip().lower()
    pats = {
        "ok","okay","ok gracias","gracias","muchas gracias","gracias!","gracias.","listo",
        "perfecto","entendido","todo claro","está bien","esta bien","bien","ya","vale",
        "thank you","thanks"
    }
    return any(t == p or t.startswith(p + " ") for p in pats) or any(p in t for p in ["gracias", "thank"])

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
#  Utilidad: detectar nombre
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
        msg.body("Lo siento, este número no está asignado a ningún bot.")
        return str(response)

    # Guarda el mensaje del usuario
    guardar_lead(bot["name"], sender_number, incoming_msg)

    # ================== Atajos ANTES de todo ==================

    # APP: si piden descarga, responde mensaje + link del BOT (sin pregunta) y no disparamos follow-ups
    if _wants_app_download(incoming_msg):
        app_url = _effective_app_url(bot)
        app_msg = f"Puedes descargar nuestra app aquí:\n{app_url}"
        msg.body(_ensure_question(bot, app_msg, clave_sesion, allow_question=False))
        last_message_time[clave_sesion] = time.time()
        return str(response)

    # ❌ STOP conversacional si el usuario dice "no" o declina
    if _is_negative(incoming_msg):
        nombre = contact_name.get(clave_sesion, "")
        cierre = f"Entendido{f', {nombre}' if nombre else ''}. Quedo a la orden. Aquí tienes el enlace por si luego lo quieres usar: {_effective_booking_url(bot)}"
        msg.body(_ensure_question(bot, cierre, clave_sesion, allow_question=False))
        close_conversation(clave_sesion)
        last_message_time[clave_sesion] = time.time()
        return str(response)

    # Estado actual de agenda
    st = _get_agenda(clave_sesion)

    # Auto-cierre si el usuario responde "ok/gracias/listo…" DESPUÉS de que ofrecimos/enviamos link
    if _is_soft_ack(incoming_msg) and (st.get("status") in ("link_sent","confirmed") or _assistant_recently_offered_link(clave_sesion)):
        msg.body(_ensure_question(bot, "¡Gracias! Quedo a la orden por este medio.", clave_sesion, allow_question=False))
        close_conversation(clave_sesion)
        last_message_time[clave_sesion] = time.time()
        return str(response)

    # ====== FLUJO AGENDA con confirmación, cooldown y antidupe ======
    agenda_cfg = (bot.get("agenda") or {}) if isinstance(bot, dict) else {}

    confirm_q = agenda_cfg.get("confirm_question") or "¿Quieres que te comparta el enlace para agendar?"
    decline_msg = agenda_cfg.get("decline_message") or "Sin problema. Cuando quieras, escribe *cita* y te envío el enlace."
    closing_default = agenda_cfg.get("closing_message") or (
        "¡Perfecto! Me alegra que agendaste. El Sr. Sundin Galue estará encantado de hablar contigo en la hora elegida. "
        "Si surge algo, escríbeme aquí."
    )
    # Sanitiza placeholders de enlaces obsoletos
    confirm_q = _sanitize_link_placeholders_for_bot(confirm_q, bot)
    decline_msg = _sanitize_link_placeholders_for_bot(decline_msg, bot)
    closing_default = _sanitize_link_placeholders_for_bot(closing_default, bot)

    # Confirmación explícita "ya agendé/booked"
    if _is_scheduled_confirmation(incoming_msg):
        if not _already_confirmed_recently(clave_sesion, window_days=14):
            closing = closing_default
            if _hash_text(closing) != st.get("last_bot_hash"):
                msg.body(_ensure_question(bot, closing, clave_sesion, allow_question=False))
                _set_agenda(clave_sesion, status="confirmed", last_bot_hash=_hash_text(closing))
            else:
                alt = "¡Súper! Cualquier cosa, escríbeme por aquí."
                msg.body(_ensure_question(bot, alt, clave_sesion, allow_question=False))
                _set_agenda(clave_sesion, status="confirmed")
            close_conversation(clave_sesion)
        else:
            msg.body(_ensure_question(bot, "¡Perfecto! Quedo atento por aquí.", clave_sesion, allow_question=False))
            close_conversation(clave_sesion)
        last_message_time[clave_sesion] = time.time()
        return str(response)

    # Si está esperando confirmación del usuario para enviar link
    if st.get("awaiting_confirm"):
        if _is_affirmative(incoming_msg):
            if _can_send_link(clave_sesion, cooldown_min=10):
                nombre = contact_name.get(clave_sesion, "")
                personal_link = f"¡Perfecto{f', {nombre}' if nombre else ''}! Aquí está el enlace para agendar: {_effective_booking_url(bot)}"
                msg.body(personal_link)  # sin pregunta adicional
                _set_agenda(clave_sesion, awaiting_confirm=False, status="link_sent",
                            last_link_time=_now(), last_bot_hash=_hash_text(personal_link))
                try:
                    ahora_bot = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    fb_append_historial(bot["name"], sender_number, {"tipo": "bot", "texto": personal_link, "hora": ahora_bot})
                except Exception as e:
                    print(f"⚠️ No se pudo guardar respuesta AGENDA: {e}")
            else:
                msg.body("Te envié el enlace hace un momento. ¿Quieres que te lo reenvíe o prefieres que te explique cómo funciona?")
                _set_agenda(clave_sesion, awaiting_confirm=False)
            last_message_time[clave_sesion] = time.time()
            # 👇 Importante: NO disparamos follow-ups tras enviar link
            return str(response)
        elif _is_negative(incoming_msg):
            msg.body(_ensure_question(bot, decline_msg, clave_sesion, allow_question=False))
            _set_agenda(clave_sesion, awaiting_confirm=False)
            close_conversation(clave_sesion)
            last_message_time[clave_sesion] = time.time()
            return str(response)
        else:
            msg.body(_ensure_question(bot, confirm_q, clave_sesion))
            last_message_time[clave_sesion] = time.time()
            # Seguimos con follow-ups solo si seguimos esperando confirmación
            if clave_sesion not in follow_up_flags:
                follow_up_flags[clave_sesion] = {"5min": False, "60min": False}
            Thread(target=follow_up_task, args=(clave_sesion, bot_number)).start()
            return str(response)

    # Usuario pide agendar (por keywords del bot)
    if _wants_to_schedule(incoming_msg, bot):
        msg.body(_ensure_question(bot, confirm_q, clave_sesion))
        _set_agenda(clave_sesion, awaiting_confirm=True)
        last_message_time[clave_sesion] = time.time()
        if clave_sesion not in follow_up_flags:
            follow_up_flags[clave_sesion] = {"5min": False, "60min": False}
        Thread(target=follow_up_task, args=(clave_sesion, bot_number)).start()
        return str(response)

    # ====== Sesión / saludo y segundo mensaje fijo ======
    if clave_sesion not in session_history:
        sysmsg = _make_system_message(bot)
        session_history[clave_sesion] = [{"role": "system", "content": sysmsg}]
        follow_up_flags[clave_sesion] = {"5min": False, "60min": False}
        greeted_state[clave_sesion] = False
        second_greet_sent[clave_sesion] = False
        last_probe_used[clave_sesion] = None

    greeting_text = bot.get("greeting")
    intro_keywords = (bot.get("intro_keywords") or [
        "hola","hello","buenas","hey","buenos días","buenas tardes","buenas noches","quién eres","quien eres"
    ])

    if (not greeted_state.get(clave_sesion)) and any(w in incoming_msg.lower() for w in intro_keywords):
        if greeting_text:
            txt = _ensure_question(bot, greeting_text, clave_sesion)
            _set_agenda(clave_sesion, last_bot_hash=_hash_text(txt))
            msg.body(txt)
            greeted_state[clave_sesion] = True
            last_message_time[clave_sesion] = time.time()
            if clave_sesion not in follow_up_flags:
                follow_up_flags[clave_sesion] = {"5min": False, "60min": False}
            Thread(target=follow_up_task, args=(clave_sesion, bot_number)).start()
            return str(response)

    if greeted_state.get(clave_sesion) and not second_greet_sent.get(clave_sesion, False):
        nombre_detectado = _extract_name(incoming_msg)
        if nombre_detectado:
            contact_name[clave_sesion] = nombre_detectado
        nombre = contact_name.get(clave_sesion, "")
        saludo2 = (
            f"¡Hola, {nombre}! Gracias por escribirnos. Somos la revista IN Houston Texas, "
            f"el único directorio en español, ¿te gustaría saber cómo funciona?"
        ) if nombre else (
            "¡Hola! Gracias por escribirnos. Somos la revista IN Houston Texas, "
            "el único directorio en español, ¿te gustaría saber cómo funciona?"
        )
        msg.body(_ensure_question(bot, _sanitize_link_placeholders_for_bot(saludo2, bot), clave_sesion, allow_question=True))
        second_greet_sent[clave_sesion] = True
        last_message_time[clave_sesion] = time.time()
        if clave_sesion not in follow_up_flags:
            follow_up_flags[clave_sesion] = {"5min": False, "60min": False}
        Thread(target=follow_up_task, args=(clave_sesion, bot_number)).start()
        return str(response)

    # ====== Continuación normal (GPT) ======
    session_history[clave_sesion].append({"role": "user", "content": incoming_msg})
    last_message_time[clave_sesion] = time.time()
    if clave_sesion not in follow_up_flags:
        follow_up_flags[clave_sesion] = {"5min": False, "60min": False}
    Thread(target=follow_up_task, args=(clave_sesion, bot_number)).start()

    try:
        completion = client.chat.completions.create(
            model="gpt-4o",
            messages=session_history[clave_sesion]
        )
        respuesta = (completion.choices[0].message.content or "").strip()

        respuesta = _apply_style(bot, respuesta)
        respuesta = _ensure_question(bot, respuesta, clave_sesion)

        st = _get_agenda(clave_sesion)
        if _hash_text(respuesta) == st.get("last_bot_hash"):
            respuesta = _ensure_question(bot, "Te leo. ¿Prefieres ejemplos reales o ver opciones de tamaños?", clave_sesion)

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
        msg.body("Lo siento, hubo un error generando la respuesta. ¿Quieres que lo intentemos de nuevo?")

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
# =======================
def follow_up_task(clave_sesion, bot_number):
    if is_conversation_closed(clave_sesion):
        return
    # 5 minutos
    time.sleep(300)
    if (not is_conversation_closed(clave_sesion) and
        clave_sesion in last_message_time and
        time.time() - last_message_time[clave_sesion] >= 300 and
        not follow_up_flags[clave_sesion]["5min"]):
        send_whatsapp_message(clave_sesion.split("|")[1], "¿Sigues por aquí? Si quieres, te cuento opciones o agendamos una llamada 😊", bot_number)
        follow_up_flags[clave_sesion]["5min"] = True
    # +55 minutos (total ~60)
    time.sleep(3300)
    if (not is_conversation_closed(clave_sesion) and
        clave_sesion in last_message_time and
        time.time() - last_message_time[clave_sesion] >= 3600 and
        not follow_up_flags[clave_sesion]["60min"]):
        send_whatsapp_message(clave_sesion.split("|")[1], "Puedo ayudarte a agendar tu cita cuando gustes. ¿Te comparto el enlace?", bot_number)
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
