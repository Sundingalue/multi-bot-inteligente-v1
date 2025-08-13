from flask import Flask, request, session, redirect, url_for, send_file, jsonify, render_template, Response
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

# 🔹 Firebase
import firebase_admin
from firebase_admin import credentials, db

# =======================
#  Cargar variables de entorno (Render -> Secret File)
# =======================
load_dotenv("/etc/secrets/.env")
load_dotenv()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# 🔗 Agenda (Google Calendar - enlace público)
# Ejemplo: GOOGLE_CALENDAR_BOOKING_URL=https://calendar.app.google/2PAh6A4Lkxw3qxLC9
CALENDAR_URL = os.getenv("GOOGLE_CALENDAR_BOOKING_URL") or os.getenv("CALENDAR_URL", "").strip()

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
#  Configuración de bots
# =======================
with open("bots_config.json", "r") as f:
    bots_config = json.load(f)

# Memoria simple por sesión de chat (para WhatsApp)
session_history = {}
last_message_time = {}
follow_up_flags = {}

# Estado simple de agenda por conversación (confirmación Sí/No)
agenda_state = {}  # clave_sesion -> {"awaiting_confirm": bool}

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
    for config in bots_config.values():
        if config["name"].lower() == name.lower():
            return config["name"]
    return None

def _get_bot_cfg_by_name(name: str):
    if not name:
        return None
    for cfg in bots_config.values():
        if isinstance(cfg, dict) and cfg.get("name", "").lower() == name.lower():
            return cfg
    return None

# ===== Intención de agenda y confirmación =====
def _wants_to_schedule(texto: str) -> bool:
    if not texto:
        return False
    t = texto.lower()
    keys = {
        "agenda", "agendar", "agendame", "agéndame", "cita", "agendar cita",
        "reunion", "reunión", "llamada", "call", "schedule", "book",
        "booking", "appointment", "meet"
    }
    return any(k in t for k in keys)

def _is_affirmative(texto: str) -> bool:
    if not texto:
        return False
    t = texto.strip().lower()
    afirm = {
        "si", "sí", "ok", "okay", "dale", "va", "claro", "por favor",
        "hagamoslo", "hagámoslo", "perfecto", "de una", "yes", "yep", "yeah"
    }
    return any(t == a or t.startswith(a + " ") for a in afirm)

def _is_negative(texto: str) -> bool:
    if not texto:
        return False
    t = texto.strip().lower()
    neg = {"no", "nop", "no gracias", "ahora no", "luego", "después", "despues"}
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
         if config["name"] == bot_normalizado),
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
    incoming_msg = request.values.get("Body", "").strip()
    sender_number = request.values.get("From", "")
    bot_number = request.values.get("To", "")

    clave_sesion = f"{bot_number}|{sender_number}"
    bot = bots_config.get(bot_number)

    if not bot:
        response = MessagingResponse()
        response.message("Lo siento, este número no está asignado a ningún bot.")
        return str(response)

    # Guarda el mensaje del usuario
    guardar_lead(bot["name"], sender_number, incoming_msg)

    # Respuesta Twilio
    response = MessagingResponse()
    msg = response.message()

    # ====== FLUJO AGENDA con confirmación (Google Calendar público) ======
    st = agenda_state.get(clave_sesion, {"awaiting_confirm": False})
    if st.get("awaiting_confirm"):
        if _is_affirmative(incoming_msg):
            if CALENDAR_URL:
                texto_agenda = (
                    "¡Excelente! Aquí puedes **agendar tu cita o llamada con el Sr. Sundin**:\n"
                    f"{CALENDAR_URL}\n\n"
                    "Elige el día y la hora que te convengan; te llegará la invitación por correo 📩."
                )
            else:
                texto_agenda = (
                    "Puedo agendarte por Google Calendar. Envíame **dos opciones de día y hora** "
                    "y te mando la confirmación enseguida."
                )
            msg.body(texto_agenda)
            try:
                ahora_bot = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                fb_append_historial(bot["name"], sender_number, {"tipo": "bot", "texto": texto_agenda, "hora": ahora_bot})
            except Exception as e:
                print(f"⚠️ No se pudo guardar respuesta AGENDA: {e}")

            agenda_state[clave_sesion] = {"awaiting_confirm": False}
            last_message_time[clave_sesion] = time.time()
            if clave_sesion not in follow_up_flags:
                follow_up_flags[clave_sesion] = {"5min": False, "60min": False}
            Thread(target=follow_up_task, args=(clave_sesion, bot_number)).start()
            return str(response)

        elif _is_negative(incoming_msg):
            msg.body("¡Sin problema! Si más tarde quieres agendar, dime *agenda* o *cita* y te envío el enlace.")
            agenda_state[clave_sesion] = {"awaiting_confirm": False}
            last_message_time[clave_sesion] = time.time()
            Thread(target=follow_up_task, args=(clave_sesion, bot_number)).start()
            return str(response)

        else:
            msg.body("¿Deseas que te envíe el enlace para **agendar**? Responde *Sí* o *No* 👍")
            last_message_time[clave_sesion] = time.time()
            Thread(target=follow_up_task, args=(clave_sesion, bot_number)).start()
            return str(response)

    if _wants_to_schedule(incoming_msg):
        msg.body("¿Quieres **agendar** una *cita o llamada* con el Sr. Sundin? Responde *Sí* y te envío el enlace para elegir fecha y hora.")
        agenda_state[clave_sesion] = {"awaiting_confirm": True}
        last_message_time[clave_sesion] = time.time()
        if clave_sesion not in follow_up_flags:
            follow_up_flags[clave_sesion] = {"5min": False, "60min": False}
        Thread(target=follow_up_task, args=(clave_sesion, bot_number)).start()
        return str(response)
    # ====== FIN FLUJO AGENDA ======


    # Sesión / saludo
    if clave_sesion not in session_history:
        session_history[clave_sesion] = [{"role": "system", "content": bot["system_prompt"]}]
        follow_up_flags[clave_sesion] = {"5min": False, "60min": False}

    if any(word in incoming_msg.lower() for word in ["hola", "hello", "buenas", "hey"]):
        if bot["name"] == "Camila":
            saludo = "Hola, soy Camila, especialista en pólizas de gastos finales de Senior Life. ¿Con quién tengo el gusto?"
        else:
            saludo = f"Hola, soy {bot['name']}, de {bot['business_name']}. ¿Con quién tengo el gusto?"
        msg.body(saludo)
        last_message_time[clave_sesion] = time.time()
        Thread(target=follow_up_task, args=(clave_sesion, bot_number)).start()
        return str(response)

    # Continuación normal (GPT)
    session_history[clave_sesion].append({"role": "user", "content": incoming_msg})
    last_message_time[clave_sesion] = time.time()
    Thread(target=follow_up_task, args=(clave_sesion, bot_number)).start()
    

    try:
        completion = client.chat.completions.create(
            model="gpt-4o",
            messages=session_history[clave_sesion]
        )
        respuesta = completion.choices[0].message.content.strip()
        session_history[clave_sesion].append({"role": "assistant", "content": respuesta})
        msg.body(respuesta)

        try:
            ahora_bot = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fb_append_historial(bot["name"], sender_number, {"tipo": "bot", "texto": respuesta, "hora": ahora_bot})
        except Exception as e:
            print(f"⚠️ No se pudo guardar respuesta del bot: {e}")

    except Exception as e:
        print(f"❌ Error con GPT: {e}")
        msg.body("Lo siento, hubo un error generando la respuesta.")

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
        send_whatsapp_message(clave_sesion.split("|")[1], "¿Sigues por aquí? Si tienes alguna duda, estoy lista para ayudarte 😊", bot_number)
        follow_up_flags[clave_sesion]["5min"] = True
    # +55 minutos (total ~60)
    time.sleep(3300)
    if clave_sesion in last_message_time and time.time() - last_message_time[clave_sesion] >= 3600 and not follow_up_flags[clave_sesion]["60min"]:
        send_whatsapp_message(clave_sesion.split("|")[1], "Solo quería confirmar si deseas que agendemos tu cita con el Sr. Sundin Galue. Si prefieres escribir más tarde, aquí estaré 😉", bot_number)
        follow_up_flags[clave_sesion]["60min"] = True

def send_whatsapp_message(to_number, message, bot_number=None):
    from twilio.rest import Client
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = bot_number if bot_number else os.environ.get("TWILIO_WHATSAPP_NUMBER")
    client_twilio = Client(account_sid, auth_token)
    client_twilio.messages.create(body=message, from_=from_number, to=to_number)

    # ===== System prompt breve: respuestas cortas y humanas =====
def _compose_system_prompt(bot_cfg: dict) -> str:
    base = (bot_cfg.get("system_prompt") or "").strip()
    estilo = (
        "Estilo: responde en español natural y cercano. Sé breve (máx. 1–2 frases). "
        "Evita párrafos largos y listas. Haz 1 pregunta clarificadora cuando falte contexto. "
        "Tono cálido y profesional. Si saludan, saluda y pregunta en qué puedes ayudar."
    )
    if base:
        return f"{base}\n\n{estilo}"
    return estilo

# =======================
#  Run
# =======================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
