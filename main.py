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
from twilio.twiml.voice_response import VoiceResponse
import requests

# 🔹 Firebase
import firebase_admin
from firebase_admin import credentials, db

# =======================
#  Cargar variables de entorno (Render -> Secret File)
# =======================
load_dotenv("/etc/secrets/.env")

INSTAGRAM_TOKEN = os.getenv("META_IG_ACCESS_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)
app = Flask(__name__)
app.secret_key = "supersecreto_sundin_panel_2025"

# =======================
#  Inicializar Firebase
# =======================
firebase_key_path = "/etc/secrets/firebase.json"  # <- subiste este archivo en Render (Secret Files)
firebase_db_url = os.getenv("FIREBASE_DB_URL", "https://inhouston-209c0-default-rtdb.firebaseio.com/")

if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_key_path)
    firebase_admin.initialize_app(cred, {'databaseURL': firebase_db_url})

# =======================
#  Configuración de bots
# =======================
with open("bots_config.json", "r") as f:
    bots_config = json.load(f)

session_history = {}
last_message_time = {}
follow_up_flags = {}

# =======================
#  Helpers generales
# =======================
def _hora_to_epoch_ms(hora_str: str) -> int:
    """Convierte 'YYYY-MM-DD HH:MM:SS' a epoch ms (0 si falla)."""
    try:
        dt = datetime.strptime(hora_str, "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0

def _normalize_bot_name(name: str):
    """Devuelve el nombre oficial del bot según bots_config (o None si no existe)."""
    for config in bots_config.values():
        if config["name"].lower() == name.lower():
            return config["name"]
    return None

# =======================
#  Gestión de usuarios (login)
# =======================
def _load_users():
    """
    Orden de lectura de credenciales:
    1) Tripletas USER_*/PASS_*/PANEL_* desde variables de entorno.
       - PANEL = "panel"            => admin ("*")
       - PANEL = "panel-bot/<Bot>"  => acceso solo a ese bot
    2) PANEL_USERS_JSON (si existe)
    3) Fallback admin: sundin / inhouston2025 con acceso total.
    """
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

    # Compatibilidad JSON anterior
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
    """
    Añade una entrada al historial y actualiza metadatos:
    entrada = {"tipo":"user"|"bot", "texto":"...", "hora":"YYYY-MM-DD HH:MM:SS"}
    """
    ref = _lead_ref(bot_nombre, numero)
    lead = ref.get() or {}
    historial = lead.get("historial", [])

    # Normalizamos si por alguna razón es dict (clave -> item)
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]

    historial.append(entrada)

    lead["historial"] = historial
    lead["last_message"] = entrada.get("texto", "")
    lead["last_seen"] = entrada.get("hora", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lead["messages"] = int(lead.get("messages", 0)) + 1
    ref.set(lead)

# =======================
#  Leads y WhatsApp
# =======================
def guardar_lead(bot_nombre, numero, mensaje):
    """
    Guarda el mensaje del usuario en Firebase (fuente de verdad) y, como espejo de compatibilidad, en leads.json
    """
    try:
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1) Firebase
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

        # 2) Espejo local (opcional) para export/compatibilidad
        archivo = "leads.json"
        if not os.path.exists(archivo):
            with open(archivo, "w") as f:
                json.dump({}, f, indent=4)

        with open(archivo, "r") as f:
            leads = json.load(f)

        clave = f"{bot_nombre}|{numero}"
        if clave not in leads:
            leads[clave] = {
                "bot": bot_nombre,
                "numero": numero,
                "first_seen": ahora,
                "last_message": mensaje,
                "last_seen": ahora,
                "messages": 1,
                "status": "nuevo",
                "notes": "",
                "historial": [{"tipo": "user", "texto": mensaje, "hora": ahora}]
            }
        else:
            leads[clave]["messages"] += 1
            leads[clave]["last_message"] = mensaje
            leads[clave]["last_seen"] = ahora
            leads[clave]["historial"].append({"tipo": "user", "texto": mensaje, "hora": ahora})

        with open(archivo, "w") as f:
            json.dump(leads, f, indent=4)

    except Exception as e:
        print(f"❌ Error guardando lead: {e}")

@app.after_request
def permitir_iframe(response):
    response.headers["X-Frame-Options"] = "ALLOWALL"
    return response

# =======================
#  Rutas UI: Paneles
# =======================
@app.route("/panel-bot/<bot_nombre>")
def panel_exclusivo_bot(bot_nombre):
    if not session.get("autenticado"):
        return redirect(url_for("panel"))

    bot_normalizado = _normalize_bot_name(bot_nombre)
    if not bot_normalizado:
        return f"Bot '{bot_nombre}' no encontrado", 404
    if not _user_can_access_bot(bot_normalizado):
        return "No autorizado para este bot", 403

    # Para el panel listamos usando el espejo local (simple y suficiente)
    if not os.path.exists("leads.json"):
        return render_template("panel_bot.html", leads={}, bot=bot_normalizado, nombre_comercial=bot_normalizado)

    with open("leads.json", "r") as f:
        leads = json.load(f)

    leads_filtrados = {clave: datos for clave, datos in leads.items() if datos.get("bot") == bot_normalizado}

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

    guardar_lead(bot["name"], sender_number, incoming_msg)

    if clave_sesion not in session_history:
        session_history[clave_sesion] = [{"role": "system", "content": bot["system_prompt"]}]
        follow_up_flags[clave_sesion] = {"5min": False, "60min": False}

    response = MessagingResponse()
    msg = response.message()

    if any(word in incoming_msg.lower() for word in ["hola", "hello", "buenas", "hey"]):
        if bot["name"] == "Camila":
            saludo = "Hola, soy Camila, especialista en polizas de gastos finales de Senior Life. ¿Con quien tengo el gusto?"
        else:
            saludo = f"Hola, soy {bot['name']}, la asistente del Sr Sundin Galué, CEO de {bot['business_name']}. ¿Con quién tengo el gusto?"
        msg.body(saludo)
        last_message_time[clave_sesion] = time.time()
        Thread(target=follow_up_task, args=(clave_sesion, bot_number)).start()
        return str(response)

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

        # 🔹 Guardar respuesta del bot también en Firebase y espejo local
        try:
            ahora_bot = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fb_append_historial(bot["name"], sender_number, {"tipo": "bot", "texto": respuesta, "hora": ahora_bot})

            # espejo local
            if os.path.exists("leads.json"):
                with open("leads.json", "r") as f:
                    leads = json.load(f)
            else:
                leads = {}
            clave = f"{bot['name']}|{sender_number}"
            if clave not in leads:
                leads[clave] = {
                    "bot": bot["name"],
                    "numero": sender_number,
                    "first_seen": ahora_bot,
                    "last_message": respuesta,
                    "last_seen": ahora_bot,
                    "messages": 1,
                    "status": "nuevo",
                    "notes": "",
                    "historial": [{"tipo": "bot", "texto": respuesta, "hora": ahora_bot}]
                }
            else:
                leads[clave]["messages"] = int(leads[clave].get("messages", 0)) + 1
                leads[clave]["last_message"] = respuesta
                leads[clave]["last_seen"] = ahora_bot
                leads[clave]["historial"].append({"tipo": "bot", "texto": respuesta, "hora": ahora_bot})
            with open("leads.json", "w") as f:
                json.dump(leads, f, indent=4)
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

    data = fb_get_lead(bot_normalizado, numero)
    historial = data.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]
    mensajes = [{"texto": r.get("texto",""), "hora": r.get("hora",""), "tipo": r.get("tipo","user")} for r in historial]
    return render_template("chat.html", numero=numero, mensajes=mensajes, bot=bot_normalizado)

@app.route("/conversacion_bot/<bot>/<numero>")
def chat_bot(bot, numero):
    if not session.get("autenticado"):
        return redirect(url_for("panel"))

    bot_normalizado = _normalize_bot_name(bot)
    if not bot_normalizado:
        return "Bot no encontrado", 404
    if not _user_can_access_bot(bot_normalizado):
        return "No autorizado para este bot", 403

    data = fb_get_lead(bot_normalizado, numero)
    historial = data.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]
    mensajes = [{"texto": r.get("texto",""), "hora": r.get("hora",""), "tipo": r.get("tipo","user")} for r in historial]
    return render_template("chat_bot.html", numero=numero, mensajes=mensajes, bot=bot_normalizado)

# =======================
#  API de polling (ahora lee Firebase)
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
#  🔺 NUEVO: Borrar conversación (Firebase + leads.json)
# =======================
@app.route("/api/delete_chat", methods=["POST"])
def api_delete_chat():
    # ✅ Autenticación
    if not session.get("autenticado"):
        return jsonify({"error": "No autenticado"}), 401

    data = request.get_json(silent=True) or {}
    bot_nombre = (data.get("bot") or "").strip()
    numero = (data.get("numero") or "").strip()

    # Validación básica
    if not bot_nombre or not numero:
        return jsonify({"error": "Faltan parámetros 'bot' y/o 'numero'"}), 400

    # Normalización/permiso
    bot_normalizado = _normalize_bot_name(bot_nombre)
    if not bot_normalizado:
        return jsonify({"error": "Bot no encontrado"}), 404
    if not _user_can_access_bot(bot_normalizado):
        return jsonify({"error": "No autorizado"}), 403

    # 1) Borra en Firebase
    try:
        ref = _lead_ref(bot_normalizado, numero)
        ref.delete()
    except Exception as e:
        print(f"⚠️ No se pudo eliminar en Firebase: {e}")

    # 2) Borra en espejo local leads.json (si existe)
    try:
        if os.path.exists("leads.json"):
            with open("leads.json", "r") as f:
                leads = json.load(f)
            clave = f"{bot_normalizado}|{numero}"
            if clave in leads:
                del leads[clave]
                with open("leads.json", "w") as f:
                    json.dump(leads, f, indent=4)
    except Exception as e:
        print(f"⚠️ No se pudo actualizar leads.json: {e}")

    return jsonify({"ok": True})

# =======================
#  Login / Logout / Panel principal
# =======================
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

    leads_por_bot = {}
    bots_disponibles = {}
    leads = {}
    if os.path.exists("leads.json"):
        with open("leads.json", "r") as f:
            leads = json.load(f)
        for clave, data in leads.items():
            bot_name = data.get("bot", "Desconocido")
            if bot_name not in leads_por_bot:
                leads_por_bot[bot_name] = {}
            leads_por_bot[bot_name][clave] = data

            for config in bots_config.values():
                if config["name"] == bot_name:
                    bots_disponibles[bot_name] = config.get("business_name", bot_name)
                    break

    bot_seleccionado = request.args.get("bot")
    leads_filtrados = leads_por_bot.get(bot_seleccionado, {}) if bot_seleccionado else leads
    return render_template("panel.html", leads=leads_filtrados, bots=bots_disponibles, bot_seleccionado=bot_seleccionado)

@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("panel"))

# =======================
#  Guardar/Exportar/Instagram
# =======================
@app.route("/guardar-lead", methods=["POST"])
def guardar_edicion():
    data = request.json
    numero_key = data.get("numero")  # viene como "<bot>|<numero>"
    estado = data.get("estado")
    nota = data.get("nota")

    with open("leads.json", "r") as f:
        leads = json.load(f)

    if numero_key in leads:
        leads[numero_key]["status"] = estado
        leads[numero_key]["notes"] = nota

        # También reflejamos en Firebase
        try:
            bot_nombre = leads[numero_key]["bot"]
            numero = leads[numero_key]["numero"]
            ref = _lead_ref(bot_nombre, numero)
            ref.update({"status": estado, "notes": nota})
        except Exception as e:
            print(f"⚠️ No se pudo actualizar en Firebase: {e}")

        with open("leads.json", "w") as f:
            json.dump(leads, f, indent=4)

    return jsonify({"mensaje": "Lead actualizado"})

@app.route("/exportar")
def exportar():
    if not session.get("autenticado"):
        return redirect(url_for("panel"))

    if not os.path.exists("leads.json"):
        return "No hay leads disponibles"

    with open("leads.json", "r") as f:
        leads = json.load(f)

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Bot", "Número", "Primer contacto", "Último mensaje", "Última vez", "Mensajes", "Estado", "Notas"])
    for clave, datos in leads.items():
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

@app.route("/webhook_instagram", methods=["GET"])
def verify_instagram():
    VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN_INSTAGRAM")
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    else:
        return "Token inválido", 403

@app.route("/webhook_instagram", methods=["POST"])
def recibir_instagram():
    data = request.json
    print("📥 Mensaje recibido desde Instagram:", json.dumps(data, indent=2))
    try:
        for entry in data.get("entry", []):
            for messaging_event in entry.get("messaging", []):
                sender_id = messaging_event.get("sender", {}).get("id")
                message = messaging_event.get("message", {})
                if message.get("is_echo"):
                    continue
                if sender_id and message.get("text"):
                    enviar_respuesta_instagram(sender_id)
        return "EVENT_RECEIVED", 200
    except Exception as e:
        print(f"❌ Error procesando mensaje de Instagram: {e}")
        return "Error", 500

def enviar_respuesta_instagram(psid):
    url = "https://graph.facebook.com/v18.0/me/messages"
    headers = {
        "Authorization": f"Bearer {INSTAGRAM_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_type": "RESPONSE",
        "recipient": {"id": psid},
        "message": {"text": "¡Hola! Gracias por escribirnos por Instagram. Soy Sara, de IN Houston Texas. ¿En qué puedo ayudarte?"}
    }
    r = requests.post(url, headers=headers, json=payload)
    print("📤 Respuesta enviada a Instagram:", r.status_code, r.text)

# =======================
#  Follow-up y Twilio
# =======================
def follow_up_task(clave_sesion, bot_number):
    time.sleep(300)
    if clave_sesion in last_message_time and time.time() - last_message_time[clave_sesion] >= 300 and not follow_up_flags[clave_sesion]["5min"]:
        send_whatsapp_message(clave_sesion.split("|")[1], "¿Sigues por aquí? Si tienes alguna duda, estoy lista para ayudarte 😊", bot_number)
        follow_up_flags[clave_sesion]["5min"] = True
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

# =======================
#  Run
# =======================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
