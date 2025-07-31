from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from dotenv import load_dotenv
import os
import json

# Cargar variables de entorno
load_dotenv()

# Configurar OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Crear app Flask
app = Flask(__name__)

# Historial por número
session_history = {}

# Cargar configuración de bots
def cargar_configuracion_bots():
    with open("bots_config.json", "r", encoding="utf-8") as f:
        return json.load(f)["bots"]

bots = cargar_configuracion_bots()

# Ruta raíz
@app.route("/", methods=["GET"])
def home():
    return "✅ Bot multibot activo y esperando mensajes de WhatsApp."

# Ruta Webhook
@app.route("/webhook", methods=["POST"])
def whatsapp_webhook():
    incoming_msg = request.values.get("Body", "").strip()
    sender_number = request.values.get("From", "")
    to_number = request.values.get("To", "")  # Número que recibió el mensaje

    print(f"📩 Mensaje de {sender_number} para {to_number}: {incoming_msg}")

    response = MessagingResponse()
    msg = response.message()

    # Buscar bot correspondiente por número
    bot = next((b for b in bots if b["twilio_number"] == to_number), None)

    if not bot:
        msg.body("⚠️ Lo siento, este número no está asignado a ningún bot.")
        return str(response)

    # Iniciar historial si es nuevo
    if sender_number not in session_history:
        session_history[sender_number] = [
            {"role": "system", "content": bot["system_prompt"]}
        ]

    # Mensajes clave para presentación
    if any(word in incoming_msg.lower() for word in ["hola", "buenas", "hello", "hey"]):
        msg.body("Hola, bienvenido a In Houston Texas. Soy Sara. ¿Con quién tengo el gusto?")
        return str(response)
    elif "quién eres" in incoming_msg.lower() or "sara" in incoming_msg.lower():
        msg.body("Soy Sara, la asistente del Sr. Sundin Galue, CEO de In Houston Texas. Estoy aquí para ayudarte.")
        return str(response)

    # Añadir mensaje del usuario al historial
    session_history[sender_number].append({"role": "user", "content": incoming_msg})

    # Consultar OpenAI
    try:
        completion = client.chat.completions.create(
            model="gpt-4o",
            messages=session_history[sender_number]
        )
        respuesta = completion.choices[0].message.content.strip()
        session_history[sender_number].append({"role": "assistant", "content": respuesta})
        msg.body(respuesta)
    except Exception as e:
        print(f"❌ Error GPT: {e}")
        msg.body("Lo siento, hubo un error generando la respuesta. Intenta de nuevo más tarde.")

    return str(response)

# Ejecutar app en entorno local (opcional para pruebas)
if __name__ == "__main__":
    app.run(port=5000)
