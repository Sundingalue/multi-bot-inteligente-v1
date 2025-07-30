from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse
import openai
import os
import json
from dotenv import load_dotenv

load_dotenv()

openai.api_key = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

app = Flask(__name__)

# Cargar configuración de los bots
with open("bots_config.json") as f:
    bots_data = json.load(f)

def get_bot_by_number(to_number):
    for bot in bots_data["bots"]:
        if bot["twilio_number"] == to_number:
            return bot
    return None

@app.route("/", methods=["GET"])
def home():
    return "Sistema multibot activo."

@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.values.get("Body", "")
    to_number = request.values.get("To", "")
    from_number = request.values.get("From", "")

    bot = get_bot_by_number(to_number)

    if not bot:
        return "Bot no encontrado para este número.", 404

    system_prompt = bot["system_prompt"]

    # Generar respuesta con OpenAI
    response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": incoming_msg}
        ]
    )

    reply = response.choices[0].message.content.strip()

    # Crear respuesta para WhatsApp o SMS
    twilio_response = MessagingResponse()
    twilio_response.message(reply)

    return str(twilio_response)

@app.route("/voice", methods=["POST"])
def voice():
    response = VoiceResponse()
    response.say("Gracias por llamar a In Houston, Texas. En breve el Sr. Sundin le devolverá la llamada.", voice='woman', language='es-ES')
    return str(response)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
