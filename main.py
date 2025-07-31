from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from dotenv import load_dotenv
import os
import json

# Cargar variables de entorno
load_dotenv()

# Configurar cliente OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Crear la app Flask
app = Flask(__name__)

# Almacén de historial por número
session_history = {}

# Cargar bots desde archivo JSON
with open("bots_config.json", "r") as f:
    bots_config = json.load(f)["bots"]

# Ruta raíz
@app.route("/", methods=["GET"])
def home():
    return "✅ Bot activo y esperando mensajes de WhatsApp."

# Ruta de webhook para mensajes de WhatsApp
@app.route("/webhook", methods=["POST"])
def whatsapp_bot():
    incoming_msg = request.values.get("Body", "").strip()
    sender_number = request.values.get("From", "").strip()

    print(f"📩 Mensaje recibido de {sender_number}: {incoming_msg}")

    response = MessagingResponse()
    msg = response.message()

    # Buscar el bot correspondiente
    bot = next((b for b in bots_config if b["twilio_number"] == sender_number), None)

    if not bot:
        print(f"⚠️ Número no asignado a ningún bot: {sender_number}")
        msg.body("Lo siento, este número no está asignado a ningún bot.")
        return str(response)

    # Inicializar historial si no existe
    if sender_number not in session_history:
        session_history[sender_number] = [
            {"role": "system", "content": bot["system_prompt"]}
        ]

    # Atajos básicos
    lowered = incoming_msg.lower()
    if any(w in lowered for w in ["hola", "hello", "buenas", "buenos días", "buenas tardes"]):
        msg.body("Hola, bienvenido a In Houston Texas. Soy Sara. ¿Con quién tengo el gusto?")
        return str(response)
    elif "quién eres" in lowered or "sara" in lowered:
        msg.body("Soy Sara, la asistente del Sr. Sundin Galue, CEO de In Houston Texas. Estoy aquí para ayudarte.")
        return str(response)

    # Añadir mensaje del usuario al historial
    session_history[sender_number].append({"role": "user", "content": incoming_msg})

    try:
        completion = client.chat.completions.create(
            model="gpt-4o",
            messages=session_history[sender_number]
        )
        respuesta = completion.choices[0].message.content.strip()
        print(f"✅ GPT respondió: {respuesta}")

        # Añadir respuesta de Sara al historial
        session_history[sender_number].append({"role": "assistant", "content": respuesta})
        msg.body(respuesta)
    except Exception as e:
        print(f"❌ Error con GPT: {e}")
        msg.body("Lo siento, hubo un error procesando tu mensaje. Intenta de nuevo más tarde.")

    return str(response)

# Iniciar Flask
if __name__ == "__main__":
    app.run(port=5000)
