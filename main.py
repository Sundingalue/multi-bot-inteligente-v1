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
    bots_config = json.load(f)["bots"]

# Diccionario para almacenar historial por número de cliente
session_history = {}

@app.route("/", methods=["GET"])
def home():
    return "✅ Bot inteligente activo en Render."

@app.route("/webhook", methods=["POST"])
def whatsapp_bot():
    incoming_msg = request.values.get("Body", "").strip()
    sender_number = request.values.get("From", "")
    bot_number = request.values.get("To", "")
    print(f"📥 Mensaje de WhatsApp recibido de {sender_number} para {bot_number}: {incoming_msg}")

    response = MessagingResponse()
    msg = response.message()

    # Verificar si el número de destino está en bots_config
    bot = next((b for b in bots_config if b["twilio_number"] == bot_number), None)
    if not bot:
        print(f"⚠️ Número no asignado a ningún bot: {bot_number}")
        msg.body("Lo siento, este número no está asignado a ningún bot.")
        return str(response)

    # Iniciar historial si es nuevo
    if sender_number not in session_history:
        session_history[sender_number] = [{"role": "system", "content": bot["system_prompt"]}]

    # Saludo inicial
    if any(word in incoming_msg.lower() for word in ["hola", "hello", "buenas", "hey"]):
        saludo = f"Hola, soy {bot['name']}, la asistente virtual de {bot['business_name']}. ¿Con quién tengo el gusto?"
        print(f"🤖 Enviando saludo: {saludo}")
        msg.body(saludo)
        return str(response)

    # Agregar mensaje del usuario al historial
    session_history[sender_number].append({"role": "user", "content": incoming_msg})

    try:
        completion = client.chat.completions.create(
            model="gpt-4o",
            messages=session_history[sender_number]
        )
        respuesta = completion.choices[0].message.content.strip()
        session_history[sender_number].append({"role": "assistant", "content": respuesta})
        print(f"💬 Respuesta generada por GPT: {respuesta}")
        msg.body(respuesta)
    except Exception as e:
        print(f"❌ Error generando respuesta con OpenAI: {e}")
        msg.body("Lo siento, hubo un error generando la respuesta.")

    return str(response)
