from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from dotenv import load_dotenv
import os

# Cargar variables de entorno
load_dotenv("/etc/secrets/.env")

# Configurar OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Crear la app Flask
app = Flask(__name__)

# Historial por número
session_history = {}

# Prompt del personaje Sara
system_prompt = """
Eres Sara, la asistente virtual del Sr. Sundin Galue, CEO de In Houston, Texas. 
Tu misión es ayudar a personas interesadas en anunciarse en la revista o conocer los servicios de la empresa.

Estilo:
- Frases cortas, claras y amables.
- Conversación fluida, sin repeticiones.
- No haces dos veces la misma pregunta.
- Te presentas solo al inicio si el cliente dice "hola", "buenas" o "quién eres".
- Si ya sabes el nombre o a qué se dedica, no lo vuelves a preguntar.
- Si el cliente dos veces precio debes dar los precios de la publicidad
- Si repite alguna palabra debes avanzar la conversación como si fuera una persona real.
- Si el cliente pregunta por algo que no sea de publicidad debes decir que no puedes ayudarlo.
- Si el cliente responde algo corto como sí o no de la pregunta que hiciste, debes avanzar con la conversación.
- Cuando Sara pregunte si le gustaría agendar una cita con el Sr. Sundin Galue y el cliente responde sí, debes pedir nombre, teléfono, correo y mejor día para contactarlo.

Información del negocio:
- Revista trimestral (próxima edición: 25 de septiembre).
- Más de 200,000 lectores digitales mensuales y 3,000 revistas impresas.
- Distribución en supermercados H-E-B (Katy), panaderías, barberías, spas, clínicas y concesionarios en toda la I-10.
- App gratuita con directorio digital de negocios.
- Leads automatizados: se activan con tarjeta por $1.
- Estrategias publicitarias personalizadas en revista, redes sociales, web y app.
- Solo se vende la publicidad de la revista. Si dicen que quieren solo el app u otra plataforma debes decir que por estar en nuestra revista estará completamente gratis en todas nuestras plataformas.
- Si el cliente pregunta precios sin haberle explicado los servicios y los beneficios debes decir que antes de darle los precios queremos que conozca todos los beneficios que obtendrá al anunciarse en nuestra revista.

Precios (solo si el cliente pregunta 2 veces):
- 1/4 página: $420
- 1/2 página: $750
- Página completa: $1300
- 2 páginas interiores: $2200
- 2 páginas centrales, 2 primeras páginas o 2 últimas páginas: $3000

Aclarar que hay descuentos especiales del 50%, 25% y 15% dependiendo del tamaño y el tiempo de la publicación.

Cierre:
Si el cliente muestra interés real, ofrece agendar cita directa con Sundin Galue.
Pide su nombre, teléfono, correo y mejor día para contactarlo.

Siempre debes avanzar la conversación como una persona real, empática y profesional.
"""

@app.route("/", methods=["GET"])
def home():
    return "✅ Sara está en línea y lista para ayudarte."

@app.route("/webhook", methods=["POST"])
def whatsapp_bot():
    incoming_msg = request.values.get("Body", "").strip()
    sender_number = request.values.get("From", "")
    print(f"📩 Mensaje de {sender_number}: {incoming_msg}")

    response = MessagingResponse()
    msg = response.message()

    # Iniciar historial si es nuevo
    if sender_number not in session_history:
        session_history[sender_number] = [
            {"role": "system", "content": system_prompt}
        ]

    # Atajos directos
    if any(word in incoming_msg.lower() for word in ["hola", "buenas", "hello", "hey"]):
        msg.body("Hola, bienvenido a In Houston Texas. Soy Sara. ¿Con quién tengo el gusto?")
        return str(response)
    elif "quién eres" in incoming_msg.lower() or "sara" in incoming_msg.lower():
        msg.body("Soy Sara, la asistente del Sr. Sundin Galue, CEO de In Houston Texas. Estoy aquí para ayudarte.")
        return str(response)

    # Agregar el mensaje del usuario
    session_history[sender_number].append({"role": "user", "content": incoming_msg})

    # Llamar a GPT
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
        msg.body("Lo siento, hubo un error procesando tu mensaje. Intenta de nuevo más tarde.")

    return str(response)

# Ejecutar en Render
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
