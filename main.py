from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import openai
import os
import json
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv("/etc/secrets/.env")

# Configurar claves API
openai.api_key = os.environ.get("OPENAI_API_KEY")

# Inicializar Flask
app = Flask(__name__)

# Historial por sesión
session_history = {}

# Cargar configuración de bots desde archivo JSON
with open("bots_config.json", "r") as file:
    bots_config = json.load(file)["bots"]

# Crear índice por número de teléfono
bots_index = {bot["twilio_number"]: bot for bot in bots_config}

@app.route("/", methods=["GET"])
def home():
    return "✅ Servidor multibot activo. Esperando mensajes de WhatsApp."

@app.route("/webhook", methods=["POST"])
def whatsapp_webhook():
    incoming_msg = request.values.get("Body", "").strip()
    from_number = request.values.get("From", "").strip()
    print(f"📩 Mensaje de WhatsApp recibido de {from_number}: {incoming_msg}")

    response = MessagingResponse()
    msg = response.message()

    # Validar si el número está asignado a un bot
    if from_number not in bots_index:
        msg.body("Lo siento, este número no está asignado a ningún bot. Si necesitas asistencia, contacta con soporte.")
        return str(response)

    bot = bots_index[from_number]
    system_prompt = bot["system_prompt"]

    # Iniciar historial si es nuevo
    if from_number not in session_history:
        session_history[from_number] = [
            {"role": "system", "content": system_prompt}
        ]

    # Atajos de presentación
    if any(word in incoming_msg.lower() for word in ["hola", "buenas", "hello", "hey"]):
        msg.body(f"Hola, bienvenido a {bot['business_name']}. Soy {bot['name']}. ¿Con quién tengo el gusto?")
        return str(response)

    if "quién eres" in incoming_msg.lower() or bot['name'].lower() in incoming_msg.lower():
        msg.body(f"Soy {bot['name']}, la asistente virtual del Sr. Sundin Galue. Estoy aquí para ayudarte con {bot['business_name']}.")
        return str(response)

    # Agregar mensaje al historial
    session_history[from_number].append({"role": "user", "content": incoming_msg})

    try:
        completion = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=session_history[from_number]
        )
        respuesta = completion.choices[0].message["content"].strip()
        session_history[from_number].append({"role": "assistant", "content": respuesta})
        print(f"🤖 Respuesta generada: {respuesta}")
        msg.body(respuesta)
    except Exception as e:
        print(f"❌ Error con OpenAI: {e}")
        msg.body("Lo siento, hubo un error generando la respuesta. Intenta de nuevo más tarde.")

    return str(response)

@app.route("/voice", methods=["POST"])
def voice():
    from twilio.twiml.voice_response import VoiceResponse
    resp = VoiceResponse()
    resp.say("Hola, gracias por llamar a In Houston, Texas. Este número es solo para mensajes de WhatsApp. Por favor, escríbenos por allí.", voice='woman', language='es-MX')
    return str(resp)
