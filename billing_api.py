# billing_api.py
# Maestro de Facturación (panel factura clientes)
# - Endpoints: clients, toggle, consumption (legacy), service-item, usage, invoice
# - Registro de uso OpenAI por día
# - NUEVO: /billing/panel (HTML con tabla y modal de detalle)

from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta
import os, json, glob

from firebase_admin import db
from twilio.rest import Client as TwilioClient

billing_bp = Blueprint(
    "billing_bp",
    __name__,
)

# =======================
# Helpers
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
# Bots loader
# =======================
def load_bots_folder():
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
    if not name:
        return None
    for cfg in bots_config.values():
        if isinstance(cfg, dict) and cfg.get("name", "").lower() == str(name).lower():
            return cfg.get("name")
    return None

# =======================
# RTDB paths
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
    return db.reference(f"billing/openai/{bot_name}/{ymd}/aggregate")

# =======================
# ON/OFF
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
        _status_ref(bot_name).set(True if state == "on" else False)
        return True
    except Exception as e:
        print(f"[billing_api] ❌ Error guardando status: {e}")
        return False

# =======================
# OpenAI usage
# =======================
def record_openai_usage(bot: str, model: str, input_tokens: int, output_tokens: int):
    """Llamado por main.py después de cada respuesta del modelo."""
    if not bot:
        return
    today = datetime.utcnow().strftime("%Y-%m-%d")
    ref = _openai_day_ref(bot, today)
    cur = ref.get() or {}
    cur["total_input_tokens"]  = int(cur.get("total_input_tokens", 0)) + int(input_tokens or 0)
    cur["total_output_tokens"] = int(cur.get("total_output_tokens", 0)) + int(output_tokens or 0)
    cur["total_requests"]      = int(cur.get("total_requests", 0)) + 1

    m = (model or "unknown")
    model_counts = cur.get("model_counts", {})
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
# Twilio usage
# =======================
def _twilio_client():
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    tok = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if not sid or not tok:
        return None
    return TwilioClient(sid, tok)

def _twilio_sum_prices(bot_cfg: dict, start: str, end: str, from_number_override: str = ""):
    client = _twilio_client()
    res = {"messages": 0, "price_usd": 0.0, "note": "Basado en Message.price; algunos mensajes pueden tardar en reflejar precio definitivo."}
    if not client:
        res["note"] = "Sin credenciales de Twilio en entorno."
        return res

    from_number = (from_number_override or "").strip()
    if not from_number:
        from_number = (bot_cfg.get("twilio_number") or bot_cfg.get("whatsapp_number") or "").strip()

    d1 = datetime.strptime(start, "%Y-%m-%d")
    d2 = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)

    total_msgs = 0
    total_price = 0.0
    try:
        msgs = client.messages.list(date_sent_after=d1, date_sent_before=d2)
        for m in msgs:
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
# Ítem fijo de servicio
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
# Endpoints públicos JSON
# =======================
@billing_bp.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "billing_api", "time": datetime.utcnow().isoformat() + "Z"})

@billing_bp.route("/clients", methods=["GET"])
def list_clients():
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

        val = _consumption_ref(bot_name, period).get()
        consumo_cents = int((val or {}).get("cents", 0) if isinstance(val, dict) else (val or 0))

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
    period = request.args.get("period") or _period_ym()
    bots_config = load_bots_folder()
    bot_norm = _normalize_bot_name(bots_config, bot_name) or bot_name

    val = _consumption_ref(bot_norm, period).get()
    cents = int((val or {}).get("cents", 0) if isinstance(val, dict) else (val or 0))
    return jsonify({"success": True, "bot": bot_norm, "period": period, "consumo_cents": cents})

@billing_bp.route("/service-item/<bot>", methods=["GET", "POST"])
def service_item(bot):
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

    default_in_per_1k  = _as_float(os.getenv("OAI_INPUT_PER_1K", "0.00"))
    default_out_per_1k = _as_float(os.getenv("OAI_OUTPUT_PER_1K", "0.00"))

    tw = _twilio_sum_prices(bot_cfg, start, end, from_number_override=from_number)
    oa = _sum_openai(bot_name, start, end, default_in_per_1k, default_out_per_1k)
    svc = _get_service_item(bot_name)

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
    return usage(bot)

@billing_bp.route("/track/openai", methods=["POST"])
def track_openai():
    data = request.get_json(silent=True) or {}
    bot = (data.get("bot") or "").strip()
    model = (data.get("model") or "").strip()
    itok = int(data.get("input_tokens") or 0)
    otok = int(data.get("output_tokens") or 0)
    if not bot:
        return jsonify({"success": False, "message": "bot requerido"}), 400
    record_openai_usage(bot, model, itok, otok)
    return jsonify({"success": True})

# =======================
# NUEVO: Página HTML del panel (click + modal)
# =======================
@billing_bp.route("/panel", methods=["GET"])
def billing_panel():
    # HTML auto-contenido para embeber en iframe o abrir directo
    return ("""
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Facturación y Clientes</title>
  <style>
    body{font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; background:#0a0a0a; color:#eee; margin:0}
    .wrap{max-width:1100px; margin:24px auto; padding:0 16px}
    .badge{display:inline-block; padding:6px 10px; border-radius:999px; background:#222; color:#ffd166; font-weight:600; margin-bottom:8px}
    table{width:100%; border-collapse:separate; border-spacing:0 10px}
    th{background:#111; text-align:left; padding:12px; font-size:14px; color:#bbb; border-bottom:1px solid #222}
    td{background:#151515; padding:14px; vertical-align:middle; border-top:1px solid #222; border-bottom:1px solid #222}
    td:first-child, th:first-child{border-left:1px solid #222; border-top-left-radius:10px; border-bottom-left-radius:10px}
    td:last-child, th:last-child{border-right:1px solid #222; border-top-right-radius:10px; border-bottom-right-radius:10px}
    .name{font-weight:700; color:#fff}
    .sub{font-size:12px; color:#aaa}
    .pill{display:inline-flex; align-items:center; gap:8px}
    .btn{background:#1f1f1f; border:1px solid #333; padding:10px 12px; border-radius:10px; color:#ddd; text-decoration:none; cursor:pointer}
    .btn:hover{background:#242424}
    .btn-primary{border-color:#5b5; color:#efe}
    .switch{display:inline-flex; gap:8px; align-items:center}
    .dot{width:10px; height:10px; border-radius:50%; background:#2d2}
    .dot.off{background:#e66}
    .consumo a{color:#ffd166; text-decoration:underline; cursor:pointer}
    .row-empty td{opacity:.6}
    /* Modal */
    .modal{position:fixed; inset:0; display:none; align-items:center; justify-content:center; background:rgba(0,0,0,.6); z-index:1000}
    .card{background:#0f0f0f; border:1px solid #222; border-radius:14px; max-width:720px; width:92%; padding:18px}
    .card h3{margin:.2rem 0 .6rem}
    .grid{display:grid; grid-template-columns: 1fr 1fr; gap:12px}
    .stat{background:#151515; border:1px solid #222; border-radius:12px; padding:12px}
    .muted{color:#aaa; font-size:12px}
    .x{float:right; cursor:pointer; color:#aaa}
    .row-actions{display:flex; gap:8px; align-items:center}
    .datebox{display:flex; gap:8px; align-items:center; margin:8px 0 16px}
    input[type=date]{background:#111; color:#eee; border:1px solid #333; border-radius:8px; padding:8px}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="badge">Stripe Modo: <b>LIVE</b></div>
    <h2>Facturación y Clientes</h2>

    <div class="datebox">
      <label>Desde <input id="dStart" type="date"></label>
      <label>Hasta <input id="dEnd" type="date"></label>
      <button class="btn" id="btnReload">Actualizar</button>
    </div>

    <table id="tbl">
      <thead>
        <tr>
          <th>Cliente</th><th>Email</th><th>Teléfono</th><th>Consumo</th><th>Bot</th><th>Acciones</th>
        </tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>

  <div class="modal" id="modal">
    <div class="card">
      <div class="x" onclick="closeModal()">✕</div>
      <h3 id="md-title">Detalle</h3>
      <div class="muted" id="md-range"></div>
      <div class="grid" style="margin-top:10px">
        <div class="stat">
          <b>OpenAI</b>
          <div class="muted" id="md-openai-rates"></div>
          <div id="md-openai"></div>
        </div>
        <div class="stat">
          <b>Twilio</b>
          <div id="md-twilio"></div>
        </div>
        <div class="stat">
          <b>Servicio</b>
          <div id="md-service"></div>
        </div>
        <div class="stat">
          <b>Totales</b>
          <div id="md-totales"></div>
        </div>
      </div>
    </div>
  </div>

<script>
const tbody = document.getElementById('tbody');
const modal = document.getElementById('modal');
const dStart = document.getElementById('dStart');
const dEnd = document.getElementById('dEnd');

function fmtUSD(n){ return '$' + (Number(n||0).toFixed(2)); }
function periodDefaults(){
  const now = new Date();
  const y = now.getFullYear(), m = String(now.getMonth()+1).padStart(2,'0');
  const first = `${y}-${m}-01`;
  const last = new Date(y, now.getMonth()+1, 0).getDate();
  const end = `${y}-${m}-${String(Math.min(now.getDate(), last)).padStart(2,'0')}`;
  dStart.value = dStart.value || first;
  dEnd.value = dEnd.value || end;
}
periodDefaults();

async function loadClients(){
  tbody.innerHTML = '<tr class="row-empty"><td colspan="6">Cargando...</td></tr>';
  const res = await fetch('/billing/clients');
  const js = await res.json();
  if(!js.success){ tbody.innerHTML = '<tr class="row-empty"><td colspan="6">Error cargando clientes</td></tr>'; return; }
  const rows = js.data.map(row => {
    const consumo = (row.consumo_cents||0)/100;
    return `
      <tr>
        <td>
          <div class="name">${row.name||row.id}</div>
          <div class="sub">ID: ${row.id}</div>
        </td>
        <td>${row.email||'-'}</td>
        <td>${row.phone||'-'}</td>
        <td class="consumo">
          <a href="#" data-bot="${row.id}" onclick="openDetail(event)">${fmtUSD(consumo)}</a>
          <div class="sub">${row.consumo_period||''}</div>
        </td>
        <td>
          <div class="switch">
            <span>${row.bot_status==='on'?'ON':'OFF'}</span>
            <span class="dot ${row.bot_status==='on'?'':'off'}"></span>
          </div>
        </td>
        <td>
          <div class="row-actions">
            <button class="btn btn-primary" onclick="openDetail(event)" data-bot="${row.id}">Ver detalle</button>
            <a class="btn" href="#" onclick="alert('Integración Stripe pendiente aquí')">Crear factura (Stripe)</a>
          </div>
        </td>
      </tr>
    `;
  }).join('');
  tbody.innerHTML = rows || '<tr class="row-empty"><td colspan="6">Sin clientes</td></tr>';
}

async function openDetail(ev){
  ev.preventDefault();
  const bot = ev.target.getAttribute('data-bot');
  if(!bot) return;
  const start = dStart.value, end = dEnd.value;
  const res = await fetch(`/billing/usage/${encodeURIComponent(bot)}?start=${start}&end=${end}`);
  const js = await res.json();
  document.getElementById('md-title').textContent = `Detalle: ${js.bot}`;
  document.getElementById('md-range').textContent = `Rango: ${js.range.start} → ${js.range.end}`;

  const oa = js.openai || {};
  const tw = js.twilio || {};
  const svc = js.service_item || {};

  document.getElementById('md-openai-rates').textContent =
    `Tarifas: in ${oa.rate_input_per_1k||0}/1k tok · out ${oa.rate_output_per_1k||0}/1k tok`;

  document.getElementById('md-openai').innerHTML = `
    • Requests: <b>${oa.requests||0}</b><br/>
    • Input tokens: <b>${oa.input_tokens||0}</b><br/>
    • Output tokens: <b>${oa.output_tokens||0}</b><br/>
    • Costo estimado: <b>${fmtUSD(oa.cost_estimate_usd||0)}</b>
  `;

  document.getElementById('md-twilio').innerHTML = `
    • Mensajes: <b>${tw.messages||0}</b><br/>
    • Costo: <b>${fmtUSD(tw.price_usd||0)}</b><br/>
    <span class="muted">${tw.note||''}</span>
  `;

  document.getElementById('md-service').innerHTML = `
    • ${svc.label || 'Servicio'}: <b>${svc.enabled? fmtUSD(svc.amount||0) : 'Deshabilitado'}</b>
  `;

  document.getElementById('md-totales').innerHTML = `
    • Subtotal (OpenAI + Twilio): <b>${fmtUSD(js.subtotal_usd||0)}</b><br/>
    • Total (+ Servicio): <b>${fmtUSD(js.total_usd||0)}</b>
  `;

  modal.style.display = 'flex';
}

function closeModal(){ modal.style.display='none'; }
document.getElementById('btnReload').addEventListener('click', loadClients);
window.addEventListener('load', loadClients);
window.addEventListener('keydown', (e)=>{ if(e.key==='Escape') closeModal(); });
</script>
</body></html>
    """), 200, {"Content-Type": "text/html; charset=utf-8"}
