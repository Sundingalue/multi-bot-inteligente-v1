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

                if sender_id and message.get("text"):
                    print("📨 Texto recibido desde Instagram:", message["text"])
                    enviar_respuesta_instagram(sender_id)
        return "EVENT_RECEIVED", 200
    except Exception as e:
        print(f"❌ Error procesando mensaje de Instagram: {e}")
        return "Error", 500