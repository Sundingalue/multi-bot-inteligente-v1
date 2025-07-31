from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse, Dial
import openai
import os
import json
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv("/etc/secrets/.env")

# Configurar claves API
client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

app = Flask(__name__)

# Historial de conversación por número
session_history = {}

# Cargar configuración de bots desde archivo JSON
with open("bots_config.json") as f:
    bots_data = json.load(f)

def get_bot_by_number(to_number):
    for bot in bots_data["bots"]:
        if bot["twilio_number"] == to_number:
            return bot
    return None

@app.route("/", methods=["GET"])
def home():
    return "✅ Sistema multibot activo en Render."

@app.route("/whatsapp/", methods=["GET"])
def verify():
    verify_token = "sundinwhatsapp2025"
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')

    if mode and token:
        if mode == 'subscribe' and token == verify_token:
            return challenge, 200
    return 'Verificación fallida', 403

@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.values.get("Body", "").strip()
    to_number = request.values.get("To", "").strip()
    from_number = request.values.get("From", "").strip()

    bot = get_bot_by_number(to_number)
    if not bot:
        return "❌ Bot no encontrado para este número.", 404

    system_prompt = bot["system_prompt"]

    # Inicializar historial si no existe
    if from_number not in session_history:
        session_history[from_number] = [{"role": "system", "content": system_prompt}]

    # Agregar mensaje del usuario al historial
    session_history[from_number].append({"role": "user", "content": incoming_msg})

    try:
        # Generar respuesta con historial
        response = client.chat.completions.create(
            model="gpt-4",
            messages=session_history[from_number]
        )
        reply = response.choices[0].message.content.strip()
        # Agregar respuesta del asistente al historial
        session_history[from_number].append({"role": "assistant", "content": reply})
    except Exception as e:
        print("❌ ERROR AL GENERAR RESPUESTA CON OPENAI:")
        print(e)
        reply = "Lo siento, hubo un error generando la respuesta. Código 500."

    twilio_response = MessagingResponse()
    twilio_response.message(reply)
    return str(twilio_response)

@app.route("/voice", methods=["POST"])
def voice():
    response = VoiceResponse()
    response.say("Conectando su llamada con el Sr. Sundin Galue. Un momento por favor.", voice='woman', language='es-ES')
    dial = Dial()
    dial.number("+18323790809")
    response.append(dial)
    return str(response)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)