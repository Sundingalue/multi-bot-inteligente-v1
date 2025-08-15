# billing_api.py — Facturación central (Twilio + OpenAI + Ítem servicio)
from flask import Blueprint, request, jsonify
from dotenv import load_dotenv
import os, datetime, math

# Firebase RTDB
import firebase_admin
from firebase_admin import db

# Twilio
from twilio.rest import Client as TwilioClient

# Cargar entorno
load_dotenv("/etc/secrets/.env")
load_dotenv()

billing_bp = Blueprint("billing_bp", __name__)

# =========================
# Helpers de entorno
# =========================
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN", "").strip()

# Tarifas estimadas para OpenAI (fallback por 1K tokens).
# Puedes sobreescribir por bot en Firebase: billing/rates/{bot}/openai_input_per_1k, openai_output_per_1k
DEFAULT_OAI_INPUT_PER_1K  = float(os.getenv("OAI_INPUT_PER_1K", "0.00"))   # ej: 2.50
DEFAULT_OAI_OUTPUT_PER_1K = float(os.getenv("OAI_OUTPUT_PER_1K", "0.00"))  # ej: 10.00

# Ítem fijo de servicio por bot (fallback global si no hay en Firebase)
DEFAULT_SERVICE_ENABLED = True
DEFAULT_SERVICE_AMOUNT  = float(os.getenv("SERVICE_ITEM_AMOUNT", "200.0"))
DEFAULT_SERVICE_LABEL   = os.getenv("SERVICE_ITEM_LABEL", "Entrenamiento y mantenimiento de bot (mensual)")

def _date(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d").date()

def _daterange(d1, d2):
    cur = d1
    while cur <= d2:
        yield cur
        cur += datetime.timedelta(days=1)

def _get_bot_rates(bot):
    """Lee tarifas por bot desde Firebase (si existen); si no, usa DEFAULT_*."""
    try:
        node = db.reference(f"billing/rates/{bot}").get() or {}
        inp = float(node.get("openai_input_per_1k", DEFAULT_OAI_INPUT_PER_1K))
        out = float(node.get("openai_output_per_1k", DEFAULT_OAI_OUTPUT_PER_1K))
        return inp, out
    except Exception:
        return DEFAULT_OAI_INPUT_PER_1K, DEFAULT_OAI_OUTPUT_PER_1K

def _get_service_item(bot):
    """Lee configuración del ítem de servicio desde Firebase."""
    try:
        node = db.reference(f"billing/service_item/{bot}").get() or {}
        enabled = bool(node.get("enabled", DEFAULT_SERVICE_ENABLED))
        amount  = float(node.get("amount", DEFAULT_SERVICE_AMOUNT))
        label   = str(node.get("label", DEFAULT_SERVICE_LABEL))
        return {"enabled": enabled, "amount": amount, "label": label}
    except Exception:
        return {"enabled": DEFAULT_SERVICE_ENABLED, "amount": DEFAULT_SERVICE_AMOUNT, "label": DEFAULT_SERVICE_LABEL}

def _set_service_item(bot, enabled, amount, label):
    payload = {
        "enabled": bool(enabled),
        "amount": float(amount),
        "label": label.strip() if isinstance(label, str) else DEFAULT_SERVICE_LABEL
    }
    db.reference(f"billing/service_item/{bot}").set(payload)
    return payload

# =========================
# REGISTRO DE USO OPENAI (por bot y por día)
#  - main.py llamará a record_openai_usage(...) después de cada respuesta
#  - Se almacena en: billing/openai/{bot}/{YYYY-MM-DD}/aggregate
# =========================
def record_openai_usage(bot: str, model: str, input_tokens: int, output_tokens: int):
    """Guardar tokens por día para el bot."""
    if not bot:
        return
    today = datetime.date.today().strftime("%Y-%m-%d")
    ref = db.reference(f"billing/openai/{bot}/{today}/aggregate")
    cur = ref.get() or {}
    cur["model_counts"] = cur.get("model_counts", {})
    cur["total_input_tokens"]  = int(cur.get("total_input_tokens", 0)) + int(input_tokens or 0)
    cur["total_output_tokens"] = int(cur.get("total_output_tokens", 0)) + int(output_tokens or 0)
    cur["total_requests"]      = int(cur.get("total_requests", 0)) + 1

    m = str(model or "unknown")
    mc = cur["model_counts"].get(m, {"requests": 0, "input_tokens": 0, "output_tokens": 0})
    mc["requests"]      = int(mc.get("requests", 0)) + 1
    mc["input_tokens"]  = int(mc.get("input_tokens", 0)) + int(input_tokens or 0)
    mc["output_tokens"] = int(mc.get("output_tokens", 0)) + int(output_tokens or 0)
    cur["model_counts"][m] = mc

    ref.set(cur)

# =========================
# CÁLCULO DE COSTO OPENAI (suma por rango)
# =========================
def _sum_openai_usage(bot: str, start_date: str, end_date: str):
    d1, d2 = _date(start_date), _date(end_date)
    total_in  = 0
    total_out = 0
    total_req = 0
    model_counts = {}
    for d in _daterange(d1, d2):
        day = d.strftime("%Y-%m-%d")
        node = db.reference(f"billing/openai/{bot}/{day}/aggregate").get() or {}
        total_in  += int(node.get("total_input_tokens", 0))
        total_out += int(node.get("total_output_tokens", 0))
        total_req += int(node.get("total_requests", 0))
        for m, info in (node.get("model_counts", {}) or {}).items():
            acc = model_counts.get(m, {"requests":0, "input_tokens":0, "output_tokens":0})
            acc["requests"]      += int(info.get("requests", 0))
            acc["input_tokens"]  += int(info.get("input_tokens", 0))
            acc["output_tokens"] += int(info.get("output_tokens", 0))
            model_counts[m] = acc

    rate_in, rate_out = _get_bot_rates(bot)
    cost = (total_in/1000.0)*rate_in + (total_out/1000.0)*rate_out

    return {
        "requests": total_req,
        "input_tokens": total_in,
        "output_tokens": total_out,
        "model_breakdown": model_counts,
        "rate_input_per_1k": rate_in,
        "rate_output_per_1k": rate_out,
        "cost_estimate_usd": round(cost, 4)
    }

# =========================
# CONSUMO TWILIO (precio real por mensajes del número del bot)
#  - Requiere pasar el número WhatsApp del bot (from_) y rango de fechas
#  - Suma price de Message records (puede tardar en reflejar costos definitivos)
# =========================
def _sum_twilio_messages(from_number: str, start_date: str, end_date: str):
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and from_number):
        return {"messages": 0, "price_usd": 0.0, "note": "Faltan credenciales o número remitente."}

    client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    d1 = _date(start_date)
    d2 = _date(end_date) + datetime.timedelta(days=1)  # Twilio usa < end, por eso +1 día

    total_msgs = 0
    total_price = 0.0

    # Listado por fecha y from_. Twilio puede demorar en fijar el price en algunos casos.
    # Ver: Usage Records y Message Resource price fields. :contentReference[oaicite:0]{index=0}
    messages = client.messages.list(
        date_sent_after=d1,
        date_sent_before=d2,
        from_=from_number,
        limit=1000
    )
    for m in messages:
        total_msgs += 1
        try:
            # m.price puede venir como string tipo "-0.00500" (negativo = cargo)
            if m.price:
                total_price += abs(float(m.price))
        except Exception:
            pass

    return {
        "messages": total_msgs,
        "price_usd": round(total_price, 4),
        "note": "Basado en Message.price; algunos mensajes pueden tardar en reflejar precio definitivo."
    }

# =========================
# ENDPOINTS
# =========================

@billing_bp.route("/service-item/<bot>", methods=["GET"])
def get_service_item(bot):
    return jsonify(_get_service_item(bot))

@billing_bp.route("/service-item/<bot>", methods=["POST"])
def set_service_item(bot):
    data = request.get_json(silent=True) or {}
    enabled = data.get("enabled", DEFAULT_SERVICE_ENABLED)
    amount  = data.get("amount", DEFAULT_SERVICE_AMOUNT)
    label   = data.get("label", DEFAULT_SERVICE_LABEL)
    payload = _set_service_item(bot, enabled, amount, label)
    return jsonify({"ok": True, "service_item": payload})

@billing_bp.route("/usage/<bot>", methods=["GET"])
def usage_summary(bot):
    """
    Parámetros:
      - start=YYYY-MM-DD
      - end=YYYY-MM-DD
      - from_number=whatsapp:+1XXXXXXXXXX  (número del bot en Twilio)
    """
    start = request.args.get("start")
    end   = request.args.get("end")
    from_number = request.args.get("from_number", "").strip()

    if not (start and end):
        return jsonify({"error": "Parámetros 'start' y 'end' son obligatorios (YYYY-MM-DD)."}), 400

    oai = _sum_openai_usage(bot, start, end)
    tw  = _sum_twilio_messages(from_number, start, end) if from_number else {"messages":0,"price_usd":0.0,"note":"Sin 'from_number'."}
    service_item = _get_service_item(bot)

    subtotal = oai["cost_estimate_usd"] + tw["price_usd"]
    total = subtotal + (service_item["amount"] if service_item["enabled"] else 0.0)

    return jsonify({
        "bot": bot,
        "range": {"start": start, "end": end},
        "openai": oai,
        "twilio": tw,
        "service_item": service_item,
        "subtotal_usd": round(subtotal, 4),
        "total_usd": round(total, 4)
    })

# ===== UTILIDAD: endpoint simple para registrar uso (si quieres registrar vía HTTP) =====
@billing_bp.route("/track/openai", methods=["POST"])
def http_track_openai():
    """
    Body JSON:
      { "bot": "Sara", "model": "gpt-4o", "input_tokens": 123, "output_tokens": 456 }
    """
    data = request.get_json(silent=True) or {}
    bot   = data.get("bot")
    model = data.get("model", "")
    itok  = int(data.get("input_tokens", 0))
    otok  = int(data.get("output_tokens", 0))
    if not bot:
        return jsonify({"error": "Falta 'bot'"}), 400
    record_openai_usage(bot, model, itok, otok)
    return jsonify({"ok": True})
