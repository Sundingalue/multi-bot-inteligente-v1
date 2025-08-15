# billing_api.py
# Blueprint de API de facturación/consumo independiente del core del bot.
# - No modifica la lógica de WhatsApp ni GPT.
# - Lee bots desde ./bots/*.json
# - Persiste estado ON/OFF y consumo en Firebase RTDB.

from flask import Blueprint, request, jsonify
from datetime import datetime
import os, json, glob

# Firebase ya está inicializado en main.py; aquí solo usamos db.
from firebase_admin import db

billing_bp = Blueprint("billing_bp", __name__)

# =======================
#  Utilidades locales
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

def _period_ym(dt=None):
    dt = dt or datetime.utcnow()
    return dt.strftime("%Y-%m")

def _consumption_ref(bot_name: str, period_ym: str):
    # Ruta en RTDB para consumo: billing/consumption/<bot_name>/<YYYY-MM>
    return db.reference(f"billing/consumption/{bot_name}/{period_ym}")

def _status_ref(bot_name: str):
    # Ruta en RTDB para estado ON/OFF: billing/status/<bot_name>
    return db.reference(f"billing/status/{bot_name}")

def _get_consumo_cents(bot_name: str, period_ym: str):
    try:
        val = _consumption_ref(bot_name, period_ym).get()
        # Admite enteros o dict con "cents"
        if isinstance(val, dict):
            val = val.get("cents", 0)
        return int(val or 0)
    except Exception as e:
        print(f"[billing_api] ⚠️ Error leyendo consumo: {e}")
        return 0

def _get_status(bot_name: str) -> str:
    try:
        val = _status_ref(bot_name).get()
        # Permitimos booleano o cadena
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
#  Endpoints
# =======================

@billing_bp.route("/clients", methods=["GET"])
def list_clients():
    """
    Devuelve la lista de clientes (bots) con:
      - id (bot_name)
      - name (business_name o name)
      - email (si existe en config)
      - phone (si existe en config)
      - consumo_cents (periodo actual)
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

        consumo = _get_consumo_cents(bot_name, period)
        status = _get_status(bot_name)

        items.append({
            "id": bot_name,
            "name": business_name,
            "email": email,
            "phone": phone,
            "consumo_cents": consumo,
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
    Devuelve el consumo del bot en cents para un período:
      GET /consumption/<bot_name>?period=YYYY-MM
    """
    period = request.args.get("period") or _period_ym()
    bots_config = load_bots_folder()
    bot_norm = _normalize_bot_name(bots_config, bot_name) or bot_name

    cents = _get_consumo_cents(bot_norm, period)
    return jsonify({
        "success": True,
        "bot": bot_norm,
        "period": period,
        "consumo_cents": cents
    })
