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

# 🔹 Firebase
import firebase_admin
from firebase_admin import credentials, db

# =======================
#  Cargar variables de entorno (Render -> Secret File)
# =======================
load_dotenv("/etc/secrets/.env")
load_dotenv()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") or ""
GOOGLE_CALENDAR_BOOKING_URL = os.environ.get("GOOGLE_CALENDAR_BOOKING_URL", "").strip()

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
#  Cada archivo JSON puede tener uno o varios bots.
#  Clave esperada: "whatsapp:+1XXXXXXXXXX": { ... }
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

# Memorias por sesión
session_history = {}       # clave_sesion -> mensajes para OpenAI
last_message_time = {}     # clave_sesion -> timestamp último mensaje
follow_up_flags = {}       # clave_sesion -> {"5min": bool, "60min": bool}
agenda_state = {}          # clave_sesion -> {"awaiting_confirm": bool}
greeted_state = {}         # clave_sesion -> bool (si ya se saludó)
last_probe_used = {}       # clave_sesion -> índice de la última probe usada

# =======================
#  Helpers generales
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
    """Aplica reglas de estilo corto/max_sentences."""
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
    """
    Elige una 'probe' (pregunta breve) desde style.probes sin repetir la última.
    Si no hay probes, usa fallback_question o una por defecto.
    """
    style = (bot_cfg or {}).get("style", {}) or {}
    probes = style.get("probes") or []
    probes = [p.strip() for p in probes if isinstance(p, str) and p.strip()]

    fallback = style.get("fallback_question") or "¿Te cuento cómo funciona o prefieres ver opciones?"

    if not probes:
        return fallback

    last_idx = last_probe_used.get(clave_sesion, None)

    # Construir lista de índices candidatos evitando repetir el último
    candidates = list(range(len(probes)))
    if last_idx is not None and last_idx in candidates and len(candidates) > 1:
        candidates.remove(last_idx)

    idx = random.choice(candidates)
    last_probe_used[clave_sesion] = idx
    return probes[idx]

def _ensure_question(bot_cfg: dict, text: str, clave_sesion: str = "") -> str:
    """Si la respuesta no termina en ?, agrega una pregunta breve para avanzar (rotando probes)."""
    if not text:
        return text
    trimmed = text.strip()
    if trimmed.endswith("?"):
        return trimmed

    # Si el texto ya llega al límite de oraciones, cerramos con punto.
    if not trimmed.endswith((".", "!", "…")):
        trimmed += "."

    probe = _next_probe(clave_sesion, bot_cfg) if clave_sesion else (
        (bot_cfg.get("style", {}) or {}).get("fallback_question") or "¿Te cuento cómo funciona o prefieres ver opciones?"
    )
    return f"{trimmed} {probe}"

def _make_system_message(bot_cfg: dict) -> str:
    """Combina el system_prompt con un recordatorio de estilo breve y pregunta final."""
    base = (bot_cfg or {}).get("system_prompt", "") or ""
    style = (bot_cfg or {}).get("style", {}) or {}
    short = "Responde en mensajes cortos." if style.get("short_replies", True) else ""
    max_s = style.get("max_sentences")
    extra = f" Usa como máximo {max_s} oraciones." if isinstance(max_s, int) else ""
    askq = "Termina cada respuesta con una sola pregunta clara para avanzar."  # forzado global
    squeeze = f"\n\nDirectriz de estilo: {short}{extra} {askq}".strip()
    return (base + squeeze).strip()

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
    afirm = {
        "si", "sí", "ok", "okay", "dale", "va", "claro", "por favor",
        "hagamoslo", "hagámoslo", "perfecto", "de una", "yes", "yep", "yeah", "sure", "please"
    }
    return any(t == a or t.startswith(a + " ") for a in afirm)

def _is_negative(texto: str) -> bool:
    if not texto:
        return False
    t = texto.strip().lower()
    neg = {"no", "nop", "no gracias", "ahora no", "luego", "después", "despues", "not now"}
    return any(t == n or t.startswith(n + " ") for n in neg)

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
    return "✅ Bot inteligente activo en Render."

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

    # ====== FLUJO AGENDA con confirmación basada en JSON ======
    st = agenda_state.get(clave_sesion, {"awaiting_confirm": False})
    agenda_cfg = (bot.get("agenda") or {}) if isinstance(bot, dict) else {}
    # Prepara textos con reemplazo del link de calendario (ENV)
    confirm_q = agenda_cfg.get("confirm_question") or "¿Quieres que te comparta el enlace para agendar?"
    link_tmpl = agenda_cfg.get("link_message") or "Agenda aquí:\n{GOOGLE_CALENDAR_BOOKING_URL}"
    decline_msg = agenda_cfg.get("decline_message") or "Sin problema. Cuando quieras, escribe *cita* y te envío el enlace."
    link_msg = link_tmpl.replace("{GOOGLE_CALENDAR_BOOKING_URL}", GOOGLE_CALENDAR_BOOKING_URL)

    if st.get("awaiting_confirm"):
        if _is_affirmative(incoming_msg):
            msg.body(link_msg)
            try:
                ahora_bot = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                fb_append_historial(bot["name"], sender_number, {"tipo": "bot", "texto": link_msg, "hora": ahora_bot})
            except Exception as e:
                print(f"⚠️ No se pudo guardar respuesta AGENDA: {e}")
            agenda_state[clave_sesion] = {"awaiting_confirm": False}
            last_message_time[clave_sesion] = time.time()
            if clave_sesion not in follow_up_flags:
                follow_up_flags[clave_sesion] = {"5min": False, "60min": False}
            Thread(target=follow_up_task, args=(clave_sesion, bot_number)).start()
            return str(response)
        elif _is_negative(incoming_msg):
            msg.body(_ensure_question(bot, decline_msg, clave_sesion))
            agenda_state[clave_sesion] = {"awaiting_confirm": False}
            last_message_time[clave_sesion] = time.time()
            Thread(target=follow_up_task, args=(clave_sesion, bot_number)).start()
            return str(response)
        else:
            msg.body(_ensure_question(bot, confirm_q, clave_sesion))
            last_message_time[clave_sesion] = time.time()
            Thread(target=follow_up_task, args=(clave_sesion, bot_number)).start()
            return str(response)

    if _wants_to_schedule(incoming_msg, bot):
        msg.body(_ensure_question(bot, confirm_q, clave_sesion))
        agenda_state[clave_sesion] = {"awaiting_confirm": True}
        last_message_time[clave_sesion] = time.time()
        if clave_sesion not in follow_up_flags:
            follow_up_flags[clave_sesion] = {"5min": False, "60min": False}
        Thread(target=follow_up_task, args=(clave_sesion, bot_number)).start()
        return str(response)
    # ====== FIN FLUJO AGENDA ======

    # ====== Sesión / saludo ======
    if clave_sesion not in session_history:
        sysmsg = _make_system_message(bot)
        session_history[clave_sesion] = [{"role": "system", "content": sysmsg}]
        follow_up_flags[clave_sesion] = {"5min": False, "60min": False}
        greeted_state[clave_sesion] = False
        last_probe_used[clave_sesion] = None

    # Saludo inicial: solo una vez por conversación
    greeting_text = bot.get("greeting")
    intro_keywords = (bot.get("intro_keywords") or [
        "hola","hello","buenas","hey","buenos días","buenas tardes","buenas noches","quién eres","quien eres"
    ])
    if (not greeted_state.get(clave_sesion)) and any(w in incoming_msg.lower() for w in intro_keywords):
        if greeting_text:
            msg.body(_ensure_question(bot, greeting_text, clave_sesion))
            greeted_state[clave_sesion] = True
            last_message_time[clave_sesion] = time.time()
            Thread(target=follow_up_task, args=(clave_sesion, bot_number)).start()
            return str(response)

    # ====== Continuación normal (GPT) ======
    session_history[clave_sesion].append({"role": "user", "content": incoming_msg})
    last_message_time[clave_sesion] = time.time()
    Thread(target=follow_up_task, args=(clave_sesion, bot_number)).start()

    try:
        completion = client.chat.completions.create(
            model="gpt-4o",
            messages=session_history[clave_sesion]
        )
        respuesta = (completion.choices[0].message.content or "").strip()

        # Estilo corto
        respuesta = _apply_style(bot, respuesta)
        # Forzar pregunta final (rotando probes)
        respuesta = _ensure_question(bot, respuesta, clave_sesion)

        # Si por error el modelo repitió el saludo completo, lo evitamos tras el primer saludo
        if greeted_state.get(clave_sesion) and greeting_text:
            rx = re.escape(greeting_text.split("¿")[0].strip())
            respuesta = re.sub(rf"^{rx}[\s,¡!.\-]*", "", respuesta, flags=re.IGNORECASE).strip()
            if not respuesta:
                respuesta = _ensure_question(bot, "Gracias por escribirme.", clave_sesion)

        session_history[clave_sesion].append({"role": "assistant", "content": respuesta})
        msg.body(respuesta)

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
            nuevos.append({
                "texto": reg.get("texto", ""),
                "hora": reg.get("hora", ""),
                "tipo": reg.get("tipo", "user"),
                "ts": ts
            })
        if ts > last_ts:
            last_ts = ts

    if since_ms == 0 and not nuevos and historial:
        for reg in historial:
            ts = _hora_to_epoch_ms(reg.get("hora", ""))
            if ts > last_ts:
                last_ts = ts
        nuevos = [{
            "texto": reg.get("texto", ""),
            "hora": reg.get("hora", ""),
            "tipo": reg.get("tipo", "user"),
            "ts": _hora_to_epoch_ms(reg.get("hora", ""))
        } for reg in historial]

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
    # 5 minutos
    time.sleep(300)
    if clave_sesion in last_message_time and time.time() - last_message_time[clave_sesion] >= 300 and not follow_up_flags[clave_sesion]["5min"]:
        send_whatsapp_message(clave_sesion.split("|")[1], "¿Sigues por aquí? Si quieres, te cuento opciones o agendamos una llamada 😊", bot_number)
        follow_up_flags[clave_sesion]["5min"] = True
    # +55 minutos (total ~60)
    time.sleep(3300)
    if clave_sesion in last_message_time and time.time() - last_message_time[clave_sesion] >= 3600 and not follow_up_flags[clave_sesion]["60min"]:
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
    app.run(host="0.0.0.0", port=port)
