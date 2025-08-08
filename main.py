from flask import Flask, request, render_template_string, session, redirect, url_for, send_file, jsonify, render_template
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

# Cargar variables de entorno
load_dotenv("/etc/secrets/.env")

INSTAGRAM_TOKEN = os.getenv("META_IG_ACCESS_TOKEN")
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
app = Flask(__name__)
app.secret_key = "supersecreto_sundin_panel_2025"

with open("bots_config.json", "r") as f:
    bots_config = json.load(f)

session_history = {}
last_message_time = {}
follow_up_flags = {}

def guardar_lead(bot_nombre, numero, mensaje):
    try:
        archivo = "leads.json"
        if not os.path.exists(archivo):
            with open(archivo, "w") as f:
                json.dump({}, f, indent=4)

        with open(archivo, "r") as f:
            leads = json.load(f)

        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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

@app.route("/", methods=["GET"])
def home():
    return "✅ Bot inteligente activo en Render."

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
            saludo = "Hola, soy Camila, de Galue Insurance. ¿Te ayudo con tu seguro hoy?"
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

        with open("leads.json", "r") as f:
            leads = json.load(f)
        clave = f"{bot['name']}|{sender_number}"
        if clave in leads:
            leads[clave]["historial"].append({"tipo": "bot", "texto": respuesta, "hora": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
            with open("leads.json", "w") as f:
                json.dump(leads, f, indent=4)

    except Exception as e:
        print(f"❌ Error con GPT: {e}")
        msg.body("Lo siento, hubo un error generando la respuesta.")

    return str(response)

@app.route("/panel", methods=["GET", "POST"])
def panel():
    if not session.get("autenticado"):
        if request.method == "POST":
            if request.form.get("usuario") == "sundin" and request.form.get("clave") == "inhouston2025":
                session["autenticado"] = True
                return redirect(url_for("panel"))
            return render_template("login.html", error=True)
        return render_template("login.html")

    leads_por_bot = {}
    bots_disponibles = {}

    if os.path.exists("leads.json"):
        with open("leads.json", "r") as f:
            leads = json.load(f)
        for clave, data in leads.items():
            bot = data.get("bot", "Desconocido")
            if bot not in leads_por_bot:
                leads_por_bot[bot] = {}
            leads_por_bot[bot][clave] = data

            for config in bots_config.values():
                if config["name"] == bot:
                    bots_disponibles[bot] = config.get("business_name", bot)
                    break

    bot_seleccionado = request.args.get("bot")
    leads_filtrados = leads_por_bot.get(bot_seleccionado, {}) if bot_seleccionado else leads

    return render_template("panel.html", leads=leads_filtrados, bots=bots_disponibles, bot_seleccionado=bot_seleccionado)

@app.route("/conversacion/<bot>/<numero>")
def chat_conversacion(bot, numero):
    clave = f"{bot}|{numero}"
    if not os.path.exists("leads.json"):
        return "No hay historial disponible", 404

    with open("leads.json", "r") as f:
        leads = json.load(f)

    historial = leads.get(clave, {}).get("historial", [])

    mensajes = []
    for registro in historial:
        mensajes.append({
            "texto": registro.get("texto", ""),
            "hora": registro.get("hora", ""),
            "tipo": registro.get("tipo", "user")
        })

    return render_template("chat.html", numero=numero, mensajes=mensajes)

@app.route("/guardar-lead", methods=["POST"])
def guardar_edicion():
    data = request.json
    numero = data.get("numero")
    estado = data.get("estado")
    nota = data.get("nota")

    with open("leads.json", "r") as f:
        leads = json.load(f)

    if numero in leads:
        leads[numero]["status"] = estado
        leads[numero]["notes"] = nota

        with open("leads.json", "w") as f:
            json.dump(leads, f, indent=4)

    return jsonify({"mensaje": "Lead actualizado"})

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("panel"))

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
    print("\ud83d\udce5 Mensaje recibido desde Instagram:", json.dumps(data, indent=2))
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
        print(f"\u274c Error procesando mensaje de Instagram: {e}")
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
        "message": {
            "text": "\u00a1Hola! Gracias por escribirnos por Instagram. Soy Sara, de IN Houston Texas. ¿En qué puedo ayudarte?"
        }
    }
    r = requests.post(url, headers=headers, json=payload)
    print("\ud83d\udce4 Respuesta enviada a Instagram:", r.status_code, r.text)

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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
