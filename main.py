from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import openai
import os
import json
from dotenv import load_dotenv
import requests
import datetime

load_dotenv("/etc/secrets/.env")

app = Flask(__name__)

openai.api_key = os.environ.get("OPENAI_API_KEY")
VERIFY_TOKEN_WHATSAPP = os.environ.get("VERIFY_TOKEN_WHATSAPP")
VERIFY_TOKEN_INSTAGRAM = os.environ.get("VERIFY_TOKEN_INSTAGRAM")
META_IG_ACCESS_TOKEN = os.environ.get("META_IG_ACCESS_TOKEN")

# ========== CARGAR CONFIGURACIÓN DE BOTS ==========
def load_bot_config(to_number):
    try:
        with open("bots_config.json", "r") as f:
            bots = json.load(f)
        return bots.get(to_number, None)
    except Exception as e:
        print(f"❌ Error cargando bots_config.json: {e}")
        return None

# ========== FLUJO DE WHATSAPP ==========
@app.route("/webhook", methods=["GET", "POST"])
def whatsapp_webhook():
    if request.method == "GET":
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if token == VERIFY_TOKEN_WHATSAPP:
            return challenge
        return "Token de verificación inválido", 403

    if request.method == "POST":
        try:
            data = request.get_json()
            print("📩 Mensaje recibido desde WhatsApp:", json.dumps(data, indent=2))

            message = data["entry"][0]["changes"][0]["value"]["messages"][0]
            from_number = message["from"]
            to_number = data["entry"][0]["changes"][0]["value"]["metadata"]["display_phone_number"]
            text = message["text"]["body"]

            print(f"📞 De: {from_number} | Para: {to_number} | Texto: {text}")

            bot = load_bot_config(f"whatsapp:+{to_number}")
            if not bot:
                print("⚠️ Bot no encontrado para ese número.")
                return "ok", 200

            messages = [
                {"role": "system", "content": bot["system_prompt"]},
                {"role": "user", "content": text}
            ]

            completion = openai.chat.completions.create(
                model="gpt-4",
                messages=messages
            )

            reply = completion.choices[0].message.content.strip()
            print("🤖 Respuesta de GPT:", reply)

            response = MessagingResponse()
            response.message(reply)
            return str(response)

        except Exception as e:
            print(f"❌ Error procesando mensaje de WhatsApp: {e}")
            response = MessagingResponse()
            response.message("Lo siento, hubo un error generando la respuesta.")
            return str(response)

# ========== FLUJO DE INSTAGRAM ==========
@app.route("/webhook_instagram", methods=["GET", "POST"])
def instagram_webhook():
    if request.method == "GET":
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if token == VERIFY_TOKEN_INSTAGRAM:
            return challenge
        return "Token de verificación inválido", 403

    if request.method == "POST":
        try:
            data = request.get_json()
            print("📩 Mensaje recibido desde Instagram:", json.dumps(data, indent=2))

            entry = data.get("entry", [])[0]
            messaging_event = entry.get("messaging", [])[0]
            sender_id = messaging_event.get("sender", {}).get("id")
            message = messaging_event.get("message", {})

            if message.get("is_echo"):
                print("🪞 Mensaje tipo echo recibido. No se responderá.")
                return "ok", 200

            user_message = message.get("text", "")
            print(f"🧑 Usuario: {sender_id} | Mensaje: {user_message}")

            if not user_message:
                print("⚠️ No se recibió texto para procesar.")
                return "ok", 200

            # Aquí puedes definir el comportamiento específico de Sara para Instagram
            sara_prompt = """
Eres Sara, una asistente virtual amable, profesional y muy humana. Respondes con frases cortas, claras y naturales. Estás conectada a la cuenta de Instagram de la revista IN Houston Texas. Tu trabajo es responder mensajes de forma cordial, explicar qué hace la revista y cómo pueden anunciarse. Si alguien pregunta por precios, solo los das si insisten dos veces. Tu tono es cálido, cercano y directo.
"""

            messages = [
                {"role": "system", "content": sara_prompt},
                {"role": "user", "content": user_message}
            ]

            completion = openai.chat.completions.create(
                model="gpt-4",
                messages=messages
            )

            reply = completion.choices[0].message.content.strip()
            print("🤖 Respuesta de Sara en Instagram:", reply)

            url = f"https://graph.facebook.com/v19.0/me/messages?access_token={META_IG_ACCESS_TOKEN}"
            headers = {"Content-Type": "application/json"}
            payload = {
                "recipient": {"id": sender_id},
                "message": {"text": reply}
            }

            r = requests.post(url, headers=headers, json=payload)
            print(f"📤 Respuesta enviada a Instagram: {r.status_code} - {r.text}")

            return "ok", 200

        except Exception as e:
            print(f"❌ Error procesando mensaje de Instagram: {e}")
            return "error", 500

# ========== RUTA DE VOZ (llamadas Twilio) ==========
@app.route("/voice", methods=["POST"])
def voice():
    from twilio.twiml.voice_response import VoiceResponse
    response = VoiceResponse()
    response.say("Gracias por llamar a In Houston Texas. En este momento no podemos atenderte. Por favor, deja tu mensaje después del tono.", voice='woman', language='es-US')
    response.record(maxLength="30", action="/voice", method="POST")
    return str(response)

# ========== RUTA DE PRUEBA ==========
@app.route("/", methods=["GET"])
def home():
    return "Sara está en línea ✅"

# ========== INICIAR SERVIDOR ==========
if __name__ == "__main__":
    app.run(debug=False, port=5000)
