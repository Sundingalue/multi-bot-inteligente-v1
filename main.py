from flask import Flask, request, session, redirect, url_for, send_file, jsonify, render_template, Response
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from dotenv import load_dotenv
import os
import json
import time
from threading import Thread
from datetime import datetime, timedelta
import csv
from io import StringIO
from twilio.twiml.voice_response import VoiceResponse
import requests
from hashlib import md5
from pathlib import Path

# 🔹 Firebase
import firebase_admin
from firebase_admin import credentials, db

# =======================
#  Cargar variables de entorno (Render -> Secret File)
# =======================
load_dotenv("/etc/secrets/.env")
load_dotenv()

INSTAGRAM_TOKEN = os.getenv("META_IG_ACCESS_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# 🔊 Config de voz
OPENAI_TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
OPENAI_TTS_VOICE = os.getenv("OPENAI_TTS_VOICE", "verse")
VOICE_LANG = os.getenv("VOICE_LANG", "es-US")

# 🔗 Agenda (Google Calendar - enlace público)
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

session_history = {}
last_message_time = {}
follow_up_flags = {}
voice_attempts = {}

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

def _find_bot_for_to_number(to_number_raw: str):
    if not to_number_raw:
        return next(iter(bots_config.values())) if bots_config else None

    to_num = to_number_raw.strip()
    if to_num in bots_config:
        return bots_config[to_num]
    if to_num.startswith("+"):
        candidate = f"whatsapp:{to_num}"
        if candidate in bots_config:
            return bots_config[candidate]
    if to_num.startswith("whatsapp:+"):
        candidate = to_num.replace("whatsapp:", "")
        if candidate in bots_config:
            return bots_config[candidate]
    return next(iter(bots_config.values())) if bots_config else None

def _voice_greeting(bot_cfg):
    negocio = bot_cfg.get("business_name", "nuestra empresa")
    return f"Gracias por llamar a {negocio} en Houston, Texas. ¿En qué puedo ayudarte?"

def _build_messages_from_firebase(bot_cfg, numero_tel: str):
    messages = []
    system_prompt = bot_cfg.get("system_prompt", "").strip()
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    data = fb_get_lead(bot_cfg["name"], numero_tel)
    historial = data.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]

    for reg in historial[-16:]:
        texto = reg.get("texto", "")
        tipo = reg.get("tipo", "user")
        role = "assistant" if tipo == "bot" else "user"
        if texto:
            messages.append({"role": role, "content": texto})
    return messages

# ========= OpenAI TTS (crear y servir MP3) =========
def _tts_key_for_text(text: str) -> str:
    return md5(text.encode("utf-8")).hexdigest()[:24]

def _tts_file_path(key: str) -> Path:
    return Path(f"/tmp/tts-{key}.mp3")

def _make_tts_mp3(text: str) -> str:
    key = _tts_key_for_text(text)
    out_path = _tts_file_path(key)
    if out_path.exists():
        return key
    try:
        with client.audio.speech.with_streaming_response.create(
            model=OPENAI_TTS_MODEL,
            voice=OPENAI_TTS_VOICE,
            input=text,
            format="mp3"
        ) as resp:
            resp.stream_to_file(str(out_path))
        return key
    except Exception as e:
        print(f"❌ Error generando TTS OpenAI: {e}")
        return ""

def _tts_url_for_text(text: str) -> str:
    key = _make_tts_mp3(text)
    if not key:
        return ""
    base = request.url_root.rstrip("/")
    return f"{base}/tts?key={key}"

@app.route("/tts", methods=["GET"])
def serve_tts():
    key = (request.args.get("key") or "").strip()
    path = _tts_file_path(key)
    if not key or not path.exists():
        return "Not Found", 404
    return send_file(str(path), mimetype="audio/mpeg")

# =======================
#  Gestión de usuarios (login)
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

        archivo = "leads.json"
        if not os.path.exists(archivo):
            with open(archivo, "w") as f:
                json.dump({}, f, indent=4)
        with open(archivo, "r") as f:
            leads = json.load(f)
        clave = f"{bot_nombre}|{numero}"
        if clave not in leads:
            leads[clave] = {
                "bot": bot_nombre, "numero": numero,
                "first_seen": ahora, "last_message": mensaje, "last_seen": ahora,
                "messages": 1, "status": "nuevo", "notes": "",
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

# =======================
#  ✅ VOZ (Twilio Voice Webhook - IVR clásico)
# =======================
@app.route("/voice", methods=["GET", "POST"])
def voice_incoming():
    try:
        from_num = request.values.get("From", "")
        to_num = request.values.get("To", "")
        print(f"📞 Llamada entrante (IVR clásico) -> From={from_num} To={to_num} @ {datetime.now()}")
    except Exception as e:
        print(f"⚠️ Error leyendo parámetros de voz: {e}")

    vr = VoiceResponse()
    with vr.gather(
        num_digits=1,
        action="/voice/menu",
        method="POST",
        timeout=6
    ) as g:
        g.say("Gracias por llamar a In Houston Texas. "
              "Para ventas, marque uno. "
              "Para información de revista y distribución, marque dos. "
              "Para dejar un mensaje, quédese en la línea.",
              voice="Polly.Lupe-Neural", language="es-US")
    vr.say("No recibí una selección. Por favor, deje su mensaje después del tono. "
           "Presione numeral para finalizar.", voice="Polly.Lupe-Neural", language="es-US")
    vr.record(max_length=120, play_beep=True, finish_on_key="#")
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")

@app.route("/voice/menu", methods=["POST"])
def voice_menu():
    digit = request.values.get("Digits", "")
    vr = VoiceResponse()
    if digit == "1":
        vr.say("Gracias. Te comunicamos con ventas. En este momento todos nuestros asesores "
               "están ocupados. Deja tu mensaje y te regresamos la llamada.",
               voice="Polly.Lupe-Neural", language="es-US")
        vr.record(max_length=120, play_beep=True, finish_on_key="#")
        vr.hangup()
    elif digit == "2":
        vr.say("Información de revista y distribución. Visita nuestra página o deja tu mensaje ahora.",
               voice="Polly.Lupe-Neural", language="es-US")
        vr.record(max_length=120, play_beep=True, finish_on_key="#")
        vr.hangup()
    else:
        vr.redirect("/voice")
    return Response(str(vr), mimetype="text/xml")

# =======================
#  🧠 VOZ IA (Twilio + GPT) + OpenAI TTS
# =======================
@app.route("/voice/ai", methods=["GET", "POST"])
def voice_ai():
    call_sid = request.values.get("CallSid", "")
    from_num = request.values.get("From", "")
    to_num = request.values.get("To", "")
    speech = (request.values.get("SpeechResult", "") or "").strip()

    bot_cfg = _find_bot_for_to_number(to_num)
    if not bot_cfg:
        vr = VoiceResponse()
        vr.say("Lo siento, el sistema no está disponible en este momento.", voice="Polly.Lupe-Neural", language="es-US")
        vr.hangup()
        return Response(str(vr), mimetype="text/xml")

    bot_name = bot_cfg["name"]
    lead_num = f"tel:{from_num}"
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"🎙️ VOICE-AI -> CallSid={call_sid} From={from_num} To={to_num} Bot={bot_name}")
    if call_sid not in voice_attempts:
        voice_attempts[call_sid] = 0

    vr = VoiceResponse()

    if not speech:
        voice_attempts[call_sid] += 1
        greeting = _voice_greeting(bot_cfg)
        audio_url = _tts_url_for_text(greeting)
        with vr.gather(
            input="speech",
            language=VOICE_LANG,
            action="/voice/ai",
            method="POST",
            speech_timeout="auto",
            hints="publicidad, revista, anuncio, precios, cita, distribución, Houston, Texas, In Houston Texas"
        ) as g:
            if audio_url:
                g.play(audio_url)
            else:
                g.say(greeting, voice="Polly.Lupe-Neural", language="es-US")

        if voice_attempts[call_sid] >= 2:
            msg = "No recibí audio. Por favor, deja tu mensaje después del tono. Presiona numeral para finalizar."
            url = _tts_url_for_text(msg)
            if url:
                vr.play(url)
            else:
                vr.say(msg, voice="Polly.Lupe-Neural", language="es-US")
            vr.record(max_length=120, play_beep=True, finish_on_key="#")
            vr.hangup()
        return Response(str(vr), mimetype="text/xml")

    print(f"👤 STT: {speech}")
    try:
        if not fb_get_lead(bot_name, lead_num):
            guardar_lead(bot_name, lead_num, speech)
        else:
            fb_append_historial(bot_name, lead_num, {"tipo": "user", "texto": speech, "hora": ahora})

        messages = _build_messages_from_firebase(bot_cfg, lead_num)
        messages.append({"role": "user", "content": speech})

        completion = client.chat.completions.create(
            model="gpt-4o",
            messages=messages
        )
        respuesta = completion.choices[0].message.content.strip()

        fb_append_historial(bot_name, lead_num, {"tipo": "bot", "texto": respuesta, "hora": ahora})

        url_resp = _tts_url_for_text(respuesta)
        if url_resp:
            vr.play(url_resp)
        else:
            vr.say(respuesta, voice="Polly.Lupe-Neural", language="es-US")

        follow = "¿Te ayudo con algo más?"
        url_follow = _tts_url_for_text(follow)
        with vr.gather(
            input="speech",
            language=VOICE_LANG,
            action="/voice/ai",
            method="POST",
            speech_timeout="auto",
            hints="publicidad, revista, anuncio, precios, cita, distribución, Houston, Texas, In Houston Texas"
        ) as g:
            if url_follow:
                g.play(url_follow)
            else:
                g.say(follow, voice="Polly.Lupe-Neural", language="es-US")

        return Response(str(vr), mimetype="text/xml")

    except Exception as e:
        print(f"❌ Error en VOICE-AI: {e}")
        msg = "Tuve un inconveniente procesando tu solicitud. Por favor deja tu mensaje después del tono."
        url_err = _tts_url_for_text(msg)
        if url_err:
            vr.play(url_err)
        else:
            vr.say(msg, voice="Polly.Lupe-Neural", language="es-US")
        vr.record(max_length=120, play_beep=True, finish_on_key="#")
        vr.hangup()
        return Response(str(vr), mimetype="text/xml")

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

def _is_agenda(texto: str) -> bool:
    if not texto:
        return False
    t = texto.strip().lower()
    keywords = {"agenda", "agendar", "cita", "agendar cita", "agendar reunión", "agendar reunion"}
    return any(k in t for k in keywords)

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

    # ====== FLUJO AGENDA (Google Calendar) ======
    if _is_agenda(incoming_msg):
        print("FLOW:AGENDA", {"to": bot_number, "from": sender_number, "has_url": bool(CALENDAR_URL)})
        if CALENDAR_URL:
            texto_agenda = (
                "¡Perfecto! Aquí puedes **agendar tu cita** directamente en mi Google Calendar:\n"
                f"{CALENDAR_URL}\n\n"
                "Elige el día y la hora que te convengan; recibirás confirmación automática. "
            
            )
        else:
            texto_agenda = (
                "Puedo agendarte en Google Calendar. Por favor dime **dos opciones de día y hora** "
                "y te envío la confirmación enseguida. (Tip: también puedes configurar la variable "
                "`GOOGLE_CALENDAR_BOOKING_URL` en Render para compartir el enlace de agenda)."
            )

        msg.body(texto_agenda)

        # Guardar respuesta del bot en Firebase + leads.json
        try:
            ahora_bot = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fb_append_historial(bot["name"], sender_number, {"tipo": "bot", "texto": texto_agenda, "hora": ahora_bot})
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
                    "last_message": texto_agenda,
                    "last_seen": ahora_bot,
                    "messages": 1,
                    "status": "nuevo",
                    "notes": "",
                    "historial": [{"tipo": "bot", "texto": texto_agenda, "hora": ahora_bot}]
                }
            else:
                leads[clave]["messages"] = int(leads[clave].get("messages", 0)) + 1
                leads[clave]["last_message"] = texto_agenda
                leads[clave]["last_seen"] = ahora_bot
                leads[clave]["historial"].append({"tipo": "bot", "texto": texto_agenda, "hora": ahora_bot})
            with open("leads.json", "w") as f:
                json.dump(leads, f, indent=4)
        except Exception as e:
            print(f"⚠️ No se pudo guardar respuesta AGENDA: {e}")

        # Marcar actividad y follow-up
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
            saludo = "Hola, soy Camila, especialista en polizas de gastos finales de Senior Life. ¿Con quien tengo el gusto?"
        else:
            saludo = f"Hola, soy {bot['name']}, la asistente del Sr Sundin Galué, CEO de {bot['business_name']}. ¿Con quién tengo el gusto?"
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
#  🔺 Borrar conversación (Firebase + leads.json)
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
#  Guardar/Exportar/Instagram
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
        current["status"] = estado or current.get("status", "nuevo")
        current["notes"] = nota if nota != "" else current.get("notes", "")
        current.setdefault("bot", bot_normalizado)
        current.setdefault("numero", numero)
        ref.set(current)
    except Exception as e:
        print(f"⚠️ No se pudo actualizar en Firebase: {e}")

    try:
        if os.path.exists("leads.json"):
            with open("leads.json", "r") as f:
                leads = json.load(f)
            if numero_key in leads:
                leads[numero_key]["status"] = estado or leads[numero_key].get("status", "nuevo")
                if nota != "":
                    leads[numero_key]["notes"] = nota
            else:
                leads[numero_key] = {
                    "bot": bot_normalizado,
                    "numero": numero,
                    "first_seen": current.get("first_seen", ""),
                    "last_message": current.get("last_message", ""),
                    "last_seen": current.get("last_seen", ""),
                    "messages": int(current.get("messages", 0)),
                    "status": estado or current.get("status", "nuevo"),
                    "notes": nota or current.get("notes", "")
                }
            with open("leads.json", "w") as f:
                json.dump(leads, f, indent=4)
    except Exception as e:
        print(f"⚠️ No se pudo actualizar leads.json: {e}")

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

# ======================================================
#   Google OAuth + Calendar + Sheets (NUEVO)
# ======================================================
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/spreadsheets"
]
GOOGLE_CLIENT_FILE = os.getenv("GOOGLE_CLIENT_FILE", "credentials/google_oauth_client.json")
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID", "")

def _oauth_store(creds_dict, owner_key="global"):
    try:
        session["google_creds"] = creds_dict
    except Exception:
        pass
    try:
        ref = db.reference(f"oauth_tokens/{owner_key}")
        ref.set(creds_dict)
    except Exception as e:
        print(f"⚠️ No pude guardar tokens en Firebase: {e}")

def _oauth_load(owner_key="global"):
    try:
        if "google_creds" in session:
            return session["google_creds"]
    except Exception:
        pass
    try:
        ref = db.reference(f"oauth_tokens/{owner_key}")
        data = ref.get()
        return data or None
    except Exception as e:
        print(f"⚠️ No pude leer tokens en Firebase: {e}")
        return None

def _get_creds(owner_key="global"):
    data = _oauth_load(owner_key)
    if not data:
        return None
    creds = Credentials.from_authorized_user_info(data, GOOGLE_SCOPES)
    return creds

@app.route("/google/auth")
def google_auth_start():
    owner = request.args.get("owner", "global")
    session["oauth_owner"] = owner
    redirect_uri = url_for("google_oauth_callback", _external=True)
    flow = Flow.from_client_secrets_file(
        GOOGLE_CLIENT_FILE,
        scopes=GOOGLE_SCOPES,
        redirect_uri=redirect_uri
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent"
    )
    session["oauth_state"] = state
    return redirect(auth_url)

@app.route("/oauth2callback")
def google_oauth_callback():
    state = session.get("oauth_state")
    owner = session.get("oauth_owner", "global")
    redirect_uri = url_for("google_oauth_callback", _external=True)
    flow = Flow.from_client_secrets_file(
        GOOGLE_CLIENT_FILE,
        scopes=GOOGLE_SCOPES,
        redirect_uri=redirect_uri
    )
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    creds_dict = {
        "token": creds.token,
        "refresh_token": getattr(creds, "refresh_token", None),
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }
    _oauth_store(creds_dict, owner_key=owner)
    return "✅ Permisos concedidos. Ya puedes usar Calendar y Sheets."

@app.route("/gcal/test")
def gcal_test():
    owner = request.args.get("owner", "global")
    creds = _get_creds(owner)
    if not creds:
        return redirect(url_for("google_auth_start", owner=owner))

    service = build("calendar", "v3", credentials=creds)
    start = (datetime.now() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    end   = start.replace(hour=10, minute=30)
    event = {
        "summary": "Cita de prueba (IN-Houston CRM)",
        "description": "Evento de verificación",
        "start": {"dateTime": start.isoformat(), "timeZone": "America/Chicago"},
        "end":   {"dateTime": end.isoformat(),   "timeZone": "America/Chicago"},
    }
    created = service.events().insert(calendarId="primary", body=event).execute()
    return f"✅ Evento creado: {created.get('htmlLink')}"

@app.route("/gsheets/test")
def gsheets_test():
    owner = request.args.get("owner", "global")
    if not GOOGLE_SHEETS_ID:
        return "Configura GOOGLE_SHEETS_ID en variables de entorno.", 400

    creds = _get_creds(owner)
    if not creds:
        return redirect(url_for("google_auth_start", owner=owner))

    sheets = build("sheets", "v4", credentials=creds)
    valores = [[
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Prueba", "Contacto Demo", owner
    ]]
    body = {"values": valores}
    sheets.spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEETS_ID,
        range="Hoja1!A:D",
        valueInputOption="USER_ENTERED",
        body=body
    ).execute()
    return "✅ Fila agregada a Google Sheets."

# =======================
#  Run
# =======================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
