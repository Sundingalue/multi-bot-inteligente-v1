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

# Tokens desde entorno
INSTAGRAM_TOKEN = os.getenv("META_IG_ACCESS_TOKEN")

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
app = Flask(__name__)
app.secret_key = "supersecreto_sundin_panel_2025"

with open("bots_config.json", "r") as f:
    bots_config = json.load(f)

session_history = {}
last_message_time = {}
follow_up_flags = {}

def guardar_lead(numero, mensaje):
    try:
        archivo = "leads.json"
        if not os.path.exists(archivo):
            with open(archivo, "w") as f:
                json.dump({}, f, indent=4)

        with open(archivo, "r") as f:
            leads = json.load(f)

        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if numero not in leads:
            leads[numero] = {
                "first_seen": ahora,
                "last_message": mensaje,
                "last_seen": ahora,
                "messages": 1,
                "status": "nuevo",
                "notes": "",
                "historial": [{"tipo": "user", "texto": mensaje, "hora": ahora}]
            }
        else:
            leads[numero]["messages"] += 1
            leads[numero]["last_message"] = mensaje
            leads[numero]["last_seen"] = ahora
            if "historial" not in leads[numero]:
                leads[numero]["historial"] = []
            leads[numero]["historial"].append({"tipo": "user", "texto": mensaje, "hora": ahora})

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
    guardar_lead(sender_number, incoming_msg)

    response = MessagingResponse()
    msg = response.message()
    bot = bots_config.get(bot_number)
    if not bot:
        msg.body("Lo siento, este número no está asignado a ningún bot.")
        return str(response)

    if sender_number not in session_history:
        session_history[sender_number] = [{"role": "system", "content": bot["system_prompt"]}]
        follow_up_flags[sender_number] = {"5min": False, "60min": False}

    if any(word in incoming_msg.lower() for word in ["hola", "hello", "buenas", "hey"]):
        saludo = f"Hola, soy {bot['name']}, la asistente del Sr Sundin Galué, CEO de la revista, {bot['business_name']}. ¿Con quién tengo el gusto?"
        msg.body(saludo)
        last_message_time[sender_number] = time.time()
        Thread(target=follow_up_task, args=(sender_number, bot_number)).start()
        return str(response)

    session_history[sender_number].append({"role": "user", "content": incoming_msg})
    last_message_time[sender_number] = time.time()
    Thread(target=follow_up_task, args=(sender_number, bot_number)).start()

    try:
        completion = client.chat.completions.create(
            model="gpt-4o",
            messages=session_history[sender_number]
        )
        respuesta = completion.choices[0].message.content.strip()
        session_history[sender_number].append({"role": "assistant", "content": respuesta})
        msg.body(respuesta)

        archivo = "leads.json"
        if os.path.exists(archivo):
            with open(archivo, "r") as f:
                leads = json.load(f)
            if sender_number in leads:
                leads[sender_number]["historial"].append({"tipo": "bot", "texto": respuesta, "hora": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                with open(archivo, "w") as f:
                    json.dump(leads, f, indent=4)

    except Exception as e:
        print(f"❌ Error con GPT: {e}")
        msg.body("Lo siento, hubo un error generando la respuesta.")

    return str(response)

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
                    print("ℹ️ Mensaje tipo echo recibido. No se responderá.")
                    continue

                if sender_id and "text" in message:
                    print("📨 Texto recibido desde Instagram:", message["text"])
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
        "message": {
            "text": "¡Hola! Gracias por escribirnos por Instagram. Soy Sara, de IN Houston Texas. ¿En qué puedo ayudarte?"
        }
    }
    r = requests.post(url, headers=headers, json=payload)
    print("📤 Respuesta enviada a Instagram:", r.status_code, r.text)

@app.route("/panel", methods=["GET", "POST"])
def panel():
    if not session.get("autenticado"):
        if request.method == "POST":
            if request.form.get("usuario") == "sundin" and request.form.get("clave") == "inhouston2025":
                session["autenticado"] = True
                return redirect(url_for("panel"))
            return render_template("login.html", error=True)
        return render_template("login.html")

    if not os.path.exists("leads.json"):
        leads = {}
    else:
        with open("leads.json", "r") as f:
            leads = json.load(f)

    return render_template("panel.html", leads=leads)

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
    writer.writerow(["Número", "Primer contacto", "Último mensaje", "Última vez", "Mensajes", "Estado", "Notas"])
    for numero, datos in leads.items():
        writer.writerow([
            numero,
            datos.get("first_seen", ""),
            datos.get("last_message", ""),
            datos.get("last_seen", ""),
            datos.get("messages", ""),
            datos.get("status", ""),
            datos.get("notes", "")
        ])

    output.seek(0)
    return send_file(output, mimetype="text/csv", download_name="leads.csv", as_attachment=True)

@app.route("/conversacion/<numero>")
def chat_conversacion(numero):
    if not os.path.exists("leads.json"):
        return "No hay historial disponible", 404

    with open("leads.json", "r") as f:
        leads = json.load(f)

    historial = leads.get(numero, {}).get("historial", [])

    mensajes = []
    for registro in historial:
        mensajes.append({
            "texto": registro.get("texto", ""),
            "hora": registro.get("hora", ""),
            "tipo": registro.get("tipo", "user")
        })

    return render_template("chat.html", numero=numero, mensajes=mensajes)

@app.route("/ver-leads-json")
def ver_leads_json():
    try:
        if not os.path.exists("leads.json"):
            return jsonify({"error": "El archivo leads.json no existe."}), 404

        with open("leads.json", "r") as f:
            leads = json.load(f)

        return jsonify(leads)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def follow_up_task(sender_number, bot_number):
    time.sleep(300)
    if sender_number in last_message_time and time.time() - last_message_time[sender_number] >= 300 and not follow_up_flags[sender_number]["5min"]:
        send_whatsapp_message(sender_number, "¿Sigues por aquí? Si tienes alguna duda, estoy lista para ayudarte 😊")
        follow_up_flags[sender_number]["5min"] = True
    time.sleep(3300)
    if sender_number in last_message_time and time.time() - last_message_time[sender_number] >= 3600 and not follow_up_flags[sender_number]["60min"]:
        send_whatsapp_message(sender_number, "Solo quería confirmar si deseas que agendemos tu cita con el Sr. Sundin Galue. Si prefieres escribir más tarde, aquí estaré 😉")
        follow_up_flags[sender_number]["60min"] = True

def send_whatsapp_message(to_number, message):
    from twilio.rest import Client
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_WHATSAPP_NUMBER")
    client_twilio = Client(account_sid, auth_token)
    client_twilio.messages.create(body=message, from_=from_number, to=to_number)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
