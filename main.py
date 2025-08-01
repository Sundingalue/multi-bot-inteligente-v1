from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from dotenv import load_dotenv
import os
import json

# Cargar variables de entorno
load_dotenv("/etc/secrets/.env")

# Configurar OpenAI
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# Crear la app Flask
app = Flask(__name__)

# Cargar configuración de bots desde archivo JSON
with open("bots_config.json", "r") as f:
    bots_config = json.load(f)

# Historial por número
session_history = {}

@app.route("/", methods=["GET"])
def home():
    return "✅ Bot inteligente activo en Render."

# ✅ Verificación del webhook (para Meta)
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    VERIFY_TOKEN = "1234"
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("🔐 Webhook verificado correctamente por Meta.")
        return challenge, 200
    else:
        print("❌ Falló la verificación del webhook.")
        return "Token inválido", 403

# ✅ Recepción de mensajes de WhatsApp
@app.route("/webhook", methods=["POST"])
def whatsapp_bot():
    incoming_msg = request.values.get("Body", "").strip()
    sender_number = request.values.get("From", "")
    bot_number = request.values.get("To", "")
    print(f"📥 Mensaje recibido de {sender_number} para {bot_number}: {incoming_msg}")

    response = MessagingResponse()
    msg = response.message()

    bot = bots_config.get(bot_number)
    if not bot:
        print(f"⚠️ Número no asignado a ningún bot: {bot_number}")
        msg.body("Lo siento, este número no está asignado a ningún bot.")
        return str(response)

    if sender_number not in session_history:
        session_history[sender_number] = [{"role": "system", "content": bot["system_prompt"]}]

    if any(word in incoming_msg.lower() for word in ["hola", "hello", "buenas", "hey"]):
        saludo = f"Hola, soy {bot['name']}, la asistente virtual de {bot['business_name']}. ¿Con quién tengo el gusto?"
        print(f"🤖 Enviando saludo: {saludo}")
        msg.body(saludo)
        return str(response)

    session_history[sender_number].append({"role": "user", "content": incoming_msg})

    try:
        completion = client.chat.completions.create(
            model="gpt-4o",
            messages=session_history[sender_number]
        )
        respuesta = completion.choices[0].message.content.strip()
        session_history[sender_number].append({"role": "assistant", "content": respuesta})
        print(f"💬 GPT respondió: {respuesta}")
        msg.body(respuesta)
    except Exception as e:
        print(f"❌ Error con GPT: {e}")
        msg.body("Lo siento, hubo un error generando la respuesta.")

    return str(response)
