# billing_api.py
# Maestro de Facturación (panel factura clientes)
# - Expone endpoints para clientes, toggle ON/OFF, uso Twilio, uso OpenAI, ítem de servicio e invoice.
# - Persiste en Firebase RTDB sin tocar la lógica de WhatsApp ni GPT.

from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta
import os, json, glob

# Firebase (ya inicializado en main.py)
from firebase_admin import db

# Twilio
from twilio.rest import Client as TwilioClient

billing_bp = Blueprint("billing_bp", __name__)

# =======================
#  Utilidades comunes
# =======================
def _utcdate(s: str):
    return datetime.strptime(s, "%Y-%m-%d").date()

def _period_ym(dt=None):
    dt = dt or datetime.utcnow()
    return dt.strftime("%Y-%m")

def _daterange(d1, d2):
    cur = d1
    while cur <= d2:
        yield cur
        cur += timedelta(days=1)

def _as_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return float(default)

# =======================
#  Bots config loader
# =======================
def load_bots_folder():
    """Carga todos los bots definidos en ./bots/*.json (mismo esquema que main.py)."""
    bots = {}
    base_dir = os.path.dirname(os.path.abspath(__file__))
    bots_dir = os.path.join(base_dir, "bots")
    for path in glob.glob(os.path.join(bots_dir, "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    for k, v in data.items():
                        bots[k] = v
        except Exception as e:
            print(f"[billing_api] ⚠️ No se pudo cargar {path}: {e}")
    return bots

def _normalize_bot_name(bots_config: dict, name: str):
    """Busca el nombre canónico de un bot por su 'name' en config."""
    if not name:
        return None
    for cfg in bots_config.values():
        if isinstance(cfg, dict) and cfg.get("name", "").lower() == str(name).lower():
            return cfg.get("name")
    return None

# =======================
#  RTDB: rutas de billing
# =======================
def _status_ref(bot_name: str):
    return db.reference(f"billing/status/{bot_name}")

def _consumption_ref(bot_name: str, period_ym: str):
    return db.reference(f"billing/consumption/{bot_name}/{period_ym}")

def _rates_ref(bot_name: str):
    return db.reference(f"billing/rates/{bot_name}")

def _service_item_ref(bot_name: str):
    return db.reference(f"billing/service_item/{bot_name}")

def _openai_day_ref(bot_name: str, ymd: str):
    # Guarda agregados diarios de tokens
    return db.reference(f"billing/openai/{bot_name}/{ymd}/aggregate")

# =======================
#  ON/OFF estado
# =======================
def _get_status(bot_name: str) -> str:
    try:
        val = _status_ref(bot_name).get()
        if isinstance(val, bool):
            return "on" if val else "off"
        if isinstance(val, str):
            return "on" if val.lower() == "on" else "off"
        return "off"
    except Exception as e:
        print(f"[billing_api] ⚠️ Error leyendo status: {e}")
        return "off"

def _set_status(bot_name: str, state: str):
    try:
        state_norm = True if state == "on" else False
        _status_ref(bot_name).set(state_norm)
        return True
    except Exception as e:
        print(f"[billing_api] ❌ Error guardando status: {e}")
        return False

# =======================
#  OPENAI: registro y sumatoria
# =======================
def record_openai_usage(bot: str, model: str, input_tokens: int, output_tokens: int):
    """
    Función pública para que main.py registre el uso tras cada respuesta.
    Agrega en RTDB: billing/openai/{bot}/{YYYY-MM-DD}/aggregate
    """
    if not bot:
        return
    today = datetime.utcnow().strftime("%Y-%m-%d")
    ref = _openai_day_ref(bot, today)
    cur = ref.get() or {}
    cur["total_input_tokens"]  = int(cur.get("total_input_tokens", 0)) + int(input_tokens or 0)
    cur["total_output_tokens"] = int(cur.get("total_output_tokens", 0)) + int(output_tokens or 0)
    cur["total_requests"]      = int(cur.get("total_requests", 0)) + 1

    model_counts = cur.get("model_counts", {})
    m = model or "unknown"
    info = model_counts.get(m, {"requests":0,"input_tokens":0,"output_tokens":0})
    info["requests"]      += 1
    info["input_tokens"]  += int(input_tokens or 0)
    info["output_tokens"] += int(output_tokens or 0)
    model_counts[m] = info
    cur["model_counts"] = model_counts
    ref.set(cur)

def _sum_openai(bot: str, d1: str, d2: str, default_in_per_1k: float, default_out_per_1k: float):
    start, end = _utcdate(d1), _utcdate(d2)
    t_in = t_out = t_req = 0
    model_counts = {}

    for d in _daterange(start, end):
        ymd = d.strftime("%Y-%m-%d")
        node = _openai_day_ref(bot, ymd).get() or {}
        t_in  += int(node.get("total_input_tokens", 0))
        t_out += int(node.get("total_output_tokens", 0))
        t_req += int(node.get("total_requests", 0))
        for m, info in (node.get("model_counts", {}) or {}).items():
            acc = model_counts.get(m, {"requests":0,"input_tokens":0,"output_tokens":0})
            acc["requests"]      += int(info.get("requests", 0))
            acc["input_tokens"]  += int(info.get("input_tokens", 0))
            acc["output_tokens"] += int(info.get("output_tokens", 0))
            model_counts[m] = acc

    # Tarifas por BOT o defaults
    bot_rates = _rates_ref(bot).get() or {}
    rate_in  = _as_float(bot_rates.get("openai_input_per_1k", default_in_per_1k))
    rate_out = _as_float(bot_rates.get("openai_output_per_1k", default_out_per_1k))
    cost = (t_in/1000.0)*rate_in + (t_out/1000.0)*rate_out

    return {
        "requests": t_req,
        "input_tokens": t_in,
        "output_tokens": t_out,
        "model_breakdown": model_counts,
        "rate_input_per_1k": rate_in,
        "rate_output_per_1k": rate_out,
        "cost_estimate_usd": round(cost, 4)
    }

# =======================
#  TWILIO: sumatoria de precios por mensajes
# =======================
def _twilio_client():
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    tok = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if not sid or not tok:
        return None
    return TwilioClient(sid, tok)

def _twilio_sum_prices(bot_cfg: dict, start: str, end: str, from_number_override: str = ""):
    """
    Suma Twilio Message.price (USD) para el rango [start,end] inclusive.
    Filtra por 'from' = número del bot si se conoce (o usa override).
    """
    client = _twilio_client()
    res = {"messages": 0, "price_usd": 0.0, "note": "Basado en Message.price; algunos mensajes pueden tardar en reflejar precio definitivo."}
    if not client:
        res["note"] = "Sin credenciales de Twilio en entorno."
        return res

    # Determinar número del bot
    from_number = (from_number_override or "").strip()
    if not from_number:
        from_number = (bot_cfg.get("twilio_number") or bot_cfg.get("whatsapp_number") or "").strip()

    # Límites de fecha (Twilio usa datetime)
    d1 = datetime.strptime(start, "%Y-%m-%d")
    d2 = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)  # exclusivo

    # Paginación simple (Twilio SDK maneja internamente)
    total_msgs = 0
    total_price = 0.0
    try:
        msgs = client.messages.list(date_sent_after=d1, date_sent_before=d2)
        for m in msgs:
            # Filtrar por 'from' si lo tenemos
            if from_number and (str(m.from_) or "").strip() != from_number:
                continue
            total_msgs += 1
            if m.price and m.price_unit == "USD":
                total_price += _as_float(m.price, 0.0)
    except Exception as e:
        print(f"[billing_api] ⚠️ Error Twilio list: {e}")
        res["note"] = "Error consultando Twilio (revisa SID/TOKEN y rango)."

    res["messages"] = total_msgs
    res["price_usd"] = round(total_price, 4)
    return res

# =======================
#  ÍTEM DE SERVICIO
# =======================
def _get_service_item(bot: str):
    n = _service_item_ref(bot).get() or {}
    return {
        "enabled": bool(n.get("enabled", True)),
        "amount":  _as_float(n.get("amount", os.getenv("SERVICE_ITEM_AMOUNT", "200.0"))),
        "label":   str(n.get("label", os.getenv("SERVICE_ITEM_LABEL", "Entrenamiento y mantenimiento de bot (mensual)")))
    }

def _set_service_item(bot: str, enabled: bool, amount: float, label: str):
    payload = {"enabled": bool(enabled), "amount": float(amount), "label": (label or "").strip() or "Servicio"}
    _service_item_ref(bot).set(payload)
    return payload

# =======================
#  Endpoints públicos
# =======================

@billing_bp.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "billing_api", "time": datetime.utcnow().isoformat() + "Z"})

@billing_bp.route("/clients", methods=["GET"])
def list_clients():
    """
    Devuelve la lista de clientes (bots) con:
      - id (bot_name)
      - name (business_name o name)
      - email/phone si están en config
      - consumo_cents (periodo actual) [campo legacy]
      - consumo_period (YYYY-MM)
      - bot_status (on/off)
    """
    bots_config = load_bots_folder()
    period = request.args.get("period") or _period_ym()

    items = []
    for cfg in bots_config.values():
        if not isinstance(cfg, dict):
            continue
        bot_name = cfg.get("name") or ""
        if not bot_name:
            continue

        business_name = cfg.get("business_name", bot_name)
        email = cfg.get("email") or (cfg.get("contact", {}) or {}).get("email") or ""
        phone = cfg.get("phone") or (cfg.get("contact", {}) or {}).get("phone") or ""

        # consumo_cents legacy (si existía algo previo)
        val = _consumption_ref(bot_name, period).get()
        if isinstance(val, dict):
            consumo_cents = int(val.get("cents", 0) or 0)
        else:
            consumo_cents = int(val or 0)

        status = _get_status(bot_name)

        items.append({
            "id": bot_name,
            "name": business_name,
            "email": email,
            "phone": phone,
            "consumo_cents": consumo_cents,
            "consumo_period": period,
            "bot_status": status,
        })

    return jsonify({"success": True, "data": items})

@billing_bp.route("/toggle", methods=["POST"])
def toggle_bot():
    """
    Cambia el estado ON/OFF del bot.
    Body JSON:
      { "client_id": "<bot_name>", "state": "on" | "off" }
    """
    data = request.get_json(silent=True) or {}
    client_id = (data.get("client_id") or "").strip()
    state = (data.get("state") or "").strip().lower()

    if state not in ("on", "off") or not client_id:
        return jsonify({"success": False, "message": "Parámetros inválidos"}), 400

    bots_config = load_bots_folder()
    bot_norm = _normalize_bot_name(bots_config, client_id) or client_id
    ok = _set_status(bot_norm, state)
    if not ok:
        return jsonify({"success": False, "message": "No se pudo guardar en Firebase"}), 500

    return jsonify({"success": True})

@billing_bp.route("/consumption/<bot_name>", methods=["GET"])
def get_consumption(bot_name):
    """
    Devuelve el consumo 'legacy' en cents para un período:
      GET /billing/consumption/<bot_name>?period=YYYY-MM
    """
    period = request.args.get("period") or _period_ym()
    bots_config = load_bots_folder()
    bot_norm = _normalize_bot_name(bots_config, bot_name) or bot_name

    val = _consumption_ref(bot_norm, period).get()
    if isinstance(val, dict):
        cents = int(val.get("cents", 0) or 0)
    else:
        cents = int(val or 0)
    return jsonify({
        "success": True,
        "bot": bot_norm,
        "period": period,
        "consumo_cents": cents
    })

@billing_bp.route("/service-item/<bot>", methods=["GET", "POST"])
def service_item(bot):
    """
    GET  -> obtiene el ítem fijo (enabled, amount, label)
    POST -> guarda: {enabled: bool, amount: number, label: str}
    """
    bots_config = load_bots_folder()
    bot_norm = _normalize_bot_name(bots_config, bot) or bot

    if request.method == "GET":
        return jsonify({"success": True, "service_item": _get_service_item(bot_norm)})

    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", True))
    amount  = _as_float(data.get("amount", 0.0))
    label   = str(data.get("label", "") or "")
    saved = _set_service_item(bot_norm, enabled, amount, label)
    return jsonify({"success": True, "service_item": saved})

@billing_bp.route("/usage/<bot>", methods=["GET"])
def usage(bot):
    """
    GET /billing/usage/<bot>?start=YYYY-MM-DD&end=YYYY-MM-DD[&from_number=whatsapp:+1...]
    Devuelve el desglose: Twilio, OpenAI, Service Item, Subtotal, Total.
    """
    start = (request.args.get("start") or "").strip()
    end   = (request.args.get("end") or "").strip()
    from_number = (request.args.get("from_number") or "").strip()

    if not start or not end:
        return jsonify({"success": False, "message": "start y end son requeridos (YYYY-MM-DD)"}), 400

    bots_config = load_bots_folder()
    bot_cfg = None
    bot_name = None
    for cfg in bots_config.values():
        if cfg.get("name", "").lower() == bot.lower():
            bot_cfg = cfg
            bot_name = cfg.get("name")
            break
    if not bot_name:
        bot_name = bot
        bot_cfg = {}

    # Defaults de tarifas OpenAI por entorno
    default_in_per_1k  = _as_float(os.getenv("OAI_INPUT_PER_1K", "0.00"))
    default_out_per_1k = _as_float(os.getenv("OAI_OUTPUT_PER_1K", "0.00"))

    # Twilio
    tw = _twilio_sum_prices(bot_cfg, start, end, from_number_override=from_number)

    # OpenAI
    oa = _sum_openai(bot_name, start, end, default_in_per_1k, default_out_per_1k)

    # Ítem servicio
    svc = _get_service_item(bot_name)

    # Totales
    subtotal = oa.get("cost_estimate_usd", 0.0) + tw.get("price_usd", 0.0)
    total = subtotal + (svc["amount"] if svc["enabled"] else 0.0)

    payload = {
        "bot": bot_name,
        "range": {"start": start, "end": end},
        "twilio": tw,
        "openai": oa,
        "service_item": svc,
        "subtotal_usd": round(subtotal, 4),
        "total_usd": round(total, 4)
    }
    return jsonify(payload)

@billing_bp.route("/invoice/<bot>", methods=["GET"])
def invoice(bot):
    """
    Alias de /usage pensado para UI de 'Panel Factura Cliente'
    """
    return usage(bot)

# (Opcional) Endpoint para registrar uso via HTTP si no quieres tocar main.py:
@billing_bp.route("/track/openai", methods=["POST"])
def track_openai():
    """
    Body:
    {
      "bot": "NombreBot",
      "model": "gpt-4o",
      "input_tokens": 123,
      "output_tokens": 456
    }
    """
    data = request.get_json(silent=True) or {}
    bot = (data.get("bot") or "").strip()
    model = (data.get("model") or "").strip()
    itok = int(data.get("input_tokens") or 0)
    otok = int(data.get("output_tokens") or 0)
    if not bot:
        return jsonify({"success": False, "message": "bot requerido"}), 400
    record_openai_usage(bot, model, itok, otok)
    return jsonify({"success": True})
