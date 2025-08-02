from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from dotenv import load_dotenv
import os
import json
import time
from threading import Thread

# ✅ Importación adicional para llamadas de voz
from twilio.twiml.voice_response import VoiceResponse

# Cargar variables de entorno
load_dotenv("/etc/secrets/.env")

# Configurar OpenAI
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# Crear la app Flask
app = Flask(__name__)

# Cargar configuración de bots desde archivo JSON
with open("bots_config.json", "r") as f:
    bots_config = json.load(f)

# Historial por número y seguimiento de actividad
session_history = {}
last_message_time = {}
follow_up_flags = {}

@app.route("/", methods=["GET"])
def home():
    return "✅ Bot inteligente activo en Render."

# ✅ Verificación del webhook de WhatsApp
@app.route("/webhook", methods=["GET"])
def verify_whatsapp():
    VERIFY_TOKEN = "1234"
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("🔐 Webhook de WhatsApp verificado correctamente por Meta.")
        return challenge, 200
    else:
        print("❌ Falló la verificación del webhook de WhatsApp.")
        return "Token inválido", 403

# ✅ Verificación del webhook de Instagram (misma lógica)
@app.route("/instagram", methods=["GET", "POST"])
def instagram_webhook():
    VERIFY_TOKEN = "1234"
    
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        
        if mode == "subscribe" and token == VERIFY_TOKEN:
            print("🔐 Webhook de Instagram verificado correctamente por Meta.")
            return challenge, 200
        else:
            print("❌ Falló la verificación del webhook de Instagram.")
            return "Token inválido", 403

    if request.method == "POST":
        print("📩 Instagram webhook POST recibido:")
        print(request.json)
        return "✅ Instagram Webhook recibido correctamente", 200

# ✅ Ruta para responder llamadas telefónicas entrantes
@app.route("/voice", methods=["POST"])
def voice():
    response = VoiceResponse()
    response.say(
        "Hola, soy Sara, la asistente virtual del señor Sundin Galué. "
        "Si estás interesado en publicidad o deseas más información sobre la revista In Houston Texas, "
        "por favor deja tu mensaje después del tono. Te devolveremos la llamada lo antes posible.",
        voice="woman",
        language="es-MX"
    )
    response.record(
        timeout=10,
        maxLength=30,
        play_beep=True
    )
    response.hangup()
    return str(response)

# Función para enviar recordatorios por inactividad
def follow_up_task(sender_number, bot_number):
    time.sleep(300)  # Esperar 5 minutos
    if sender_number in last_message_time and time.time() - last_message_time[sender_number] >= 300 and not follow_up_flags[sender_number]["5min"]:
        print(f"⏰ Enviando recordatorio de 5 minutos a {sender_number}")
        send_whatsapp_message(sender_number, "¿Sigues por aquí? Si tienes alguna duda, estoy lista para ayudarte 😊")
        follow_up_flags[sender_number]["5min"] = True

    time.sleep(3300)  # Esperar 55 minutos más (total 60)
    if sender_number in last_message_time and time.time() - last_message_time[sender_number] >= 3600 and not follow_up_flags[sender_number]["60min"]:
        print(f"⏰ Enviando recordatorio de 60 minutos a {sender_number}")
        send_whatsapp_message(sender_number, "Solo quería confirmar si deseas que agendemos tu cita con el Sr. Sundin Galue. Si prefieres escribir más tarde, aquí estaré 😉")
        follow_up_flags[sender_number]["60min"] = True

# Función auxiliar para enviar mensajes salientes con Twilio
def send_whatsapp_message(to_number, message):
    from twilio.rest import Client
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_WHATSAPP_NUMBER")
    client_twilio = Client(account_sid, auth_token)
    client_twilio.messages.create(
        body=message,
        from_=from_number,
        to=to_number
    )

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
        follow_up_flags[sender_number] = {"5min": False, "60min": False}

    if any(word in incoming_msg.lower() for word in ["hola", "hello", "buenas", "hey"]):
        saludo = f"Hola, soy {bot['name']}, la asistente del Sr Sundin Galué, CEO de la revista, {bot['business_name']}. ¿Con quién tengo el gusto?"
        print(f"🤖 Enviando saludo: {saludo}")
        msg.body(saludo)
        last_message_time[sender_number] = time.time()
        follow_up_flags[sender_number] = {"5min": False, "60min": False}
        Thread(target=follow_up_task, args=(sender_number, bot_number)).start()
        return str(response)

    session_history[sender_number].append({"role": "user", "content": incoming_msg})
    last_message_time[sender_number] = time.time()
    follow_up_flags[sender_number] = {"5min": False, "60min": False}
    Thread(target=follow_up_task, args=(sender_number, bot_number)).start()

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
