# billing_api.py
# Maestro de Facturación (panel factura clientes) + Gráficos en vivo
# - Endpoints: clients, toggle, consumption (legacy), service-item, usage, invoice, usage_ts, track/openai
# - Página /billing/panel: tabla + modal de detalle + sección de gráficos en vivo

from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta
import os, json, glob

from firebase_admin import db
from twilio.rest import Client as TwilioClient

billing_bp = Blueprint("billing_bp", __name__)

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
# OpenAI usage (aggregate y serie)
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

def _get_openai_rates(bot: str):
    bot_rates = _rates_ref(bot).get() or {}
    return (
        _as_float(bot_rates.get("openai_input_per_1k", os.getenv("OAI_INPUT_PER_1K", "0.00"))),
        _as_float(bot_rates.get("openai_output_per_1k", os.getenv("OAI_OUTPUT_PER_1K", "0.00")))
    )

def _sum_openai(bot: str, d1: str, d2: str):
    start, end = _utcdate(d1), _utcdate(d2)
    t_in = t_out = t_req = 0
    model_counts = {}
    per_day = []
    rate_in, rate_out = _get_openai_rates(bot)

    for d in _daterange(start, end):
        ymd = d.strftime("%Y-%m-%d")
        node = _openai_day_ref(bot, ymd).get() or {}
        di  = int(node.get("total_input_tokens", 0))
        do  = int(node.get("total_output_tokens", 0))
        dr  = int(node.get("total_requests", 0))
        cost = (di/1000.0)*rate_in + (do/1000.0)*rate_out
        per_day.append({
            "date": ymd,
            "input_tokens": di,
            "output_tokens": do,
            "requests": dr,
            "cost_estimate_usd": round(cost, 6)
        })
        t_in  += di; t_out += do; t_req += dr
        for m, info in (node.get("model_counts", {}) or {}).items():
            acc = model_counts.get(m, {"requests":0,"input_tokens":0,"output_tokens":0})
            acc["requests"]      += int(info.get("requests", 0))
            acc["input_tokens"]  += int(info.get("input_tokens", 0))
            acc["output_tokens"] += int(info.get("output_tokens", 0))
            model_counts[m] = acc

    total_cost = (t_in/1000.0)*rate_in + (t_out/1000.0)*rate_out
    return {
        "requests": t_req,
        "input_tokens": t_in,
        "output_tokens": t_out,
        "model_breakdown": model_counts,
        "rate_input_per_1k": rate_in,
        "rate_output_per_1k": rate_out,
        "cost_estimate_usd": round(total_cost, 4),
        "per_day": per_day
    }

# =======================
# Twilio usage (aggregate y serie)
# =======================
def _twilio_client():
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    tok = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if not sid or not tok:
        return None
    return TwilioClient(sid, tok)

def _get_bot_twilio_number(cfg: dict) -> str:
    return (cfg.get("twilio_number") or cfg.get("whatsapp_number") or "").strip()

def _twilio_sum_prices(bot_cfg: dict, start: str, end: str, from_number_override: str = ""):
    client = _twilio_client()
    res = {"messages": 0, "price_usd": 0.0, "note": "Basado en Message.price; algunos mensajes pueden tardar en reflejar precio definitivo."}
    if not client:
        res["note"] = "Sin credenciales de Twilio en entorno."
        return res

    from_number = (from_number_override or "").strip()
    if not from_number:
        from_number = _get_bot_twilio_number(bot_cfg)

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

def _twilio_series(bot_cfg: dict, start: str, end: str, from_number_override: str = ""):
    """Serie diaria: mensajes y costo."""
    client = _twilio_client()
    per_day = []
    total_msgs = 0
    total_price = 0.0
    note = "Basado en Message.price; algunos mensajes pueden tardar en reflejar precio definitivo."

    if not client:
        return {"per_day": [], "messages": 0, "price_usd": 0.0, "note": "Sin credenciales de Twilio en entorno."}

    from_number = (from_number_override or "").strip()
    if not from_number:
        from_number = _get_bot_twilio_number(bot_cfg)

    s, e = _utcdate(start), _utcdate(end)
    try:
        for day in _daterange(s, e):
            d1 = datetime(day.year, day.month, day.day)
            d2 = d1 + timedelta(days=1)
            msgs = client.messages.list(date_sent_after=d1, date_sent_before=d2)
            cnt = 0
            cost = 0.0
            for m in msgs:
                if from_number and (str(m.from_) or "").strip() != from_number:
                    continue
                cnt += 1
                if m.price and m.price_unit == "USD":
                    cost += _as_float(m.price, 0.0)
            per_day.append({"date": day.strftime("%Y-%m-%d"), "messages": cnt, "price_usd": round(cost, 6)})
            total_msgs += cnt
            total_price += cost
    except Exception as e:
        print(f"[billing_api] ⚠️ Error Twilio series: {e}")
        note = "Error consultando Twilio (revisa SID/TOKEN y rango)."

    return {"per_day": per_day, "messages": total_msgs, "price_usd": round(total_price, 4), "note": note}

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

    oa = _sum_openai(bot_name, start, end)
    tw = _twilio_sum_prices(bot_cfg, start, end, from_number_override=from_number)
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

@billing_bp.route("/usage_ts/<bot>", methods=["GET"])
def usage_ts(bot):
    """Serie diaria para gráficos."""
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

    oa_all = _sum_openai(bot_name, start, end)
    tw_all = _twilio_series(bot_cfg, start, end, from_number_override=from_number)

    return jsonify({
        "success": True,
        "bot": bot_name,
        "range": {"start": start, "end": end},
        "openai": {
            "rate_input_per_1k": oa_all["rate_input_per_1k"],
            "rate_output_per_1k": oa_all["rate_output_per_1k"],
            "totals": {
                "requests": oa_all["requests"],
                "input_tokens": oa_all["input_tokens"],
                "output_tokens": oa_all["output_tokens"],
                "cost_estimate_usd": oa_all["cost_estimate_usd"]
            },
            "per_day": oa_all["per_day"]
        },
        "twilio": {
            "totals": {"messages": tw_all["messages"], "price_usd": tw_all["price_usd"], "note": tw_all["note"]},
            "per_day": tw_all["per_day"]
        }
    })

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
# Página HTML del panel + gráficos
# =======================
@billing_bp.route("/panel", methods=["GET"])
def billing_panel():
    # HTML auto-contenido
    return ("""
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Facturación y Clientes</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
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
    .btn{background:#1f1f1f; border:1px solid #333; padding:10px 12px; border-radius:10px; color:#ddd; text-decoration:none; cursor:pointer}
    .btn:hover{background:#242424}
    .btn-primary{border-color:#5b5; color:#efe}
    .switch{display:inline-flex; gap:8px; align-items:center}
    .dot{width:10px; height:10px; border-radius:50%; background:#2d2}
    .dot.off{background:#e66}
    .consumo a{color:#ffd166; text-decoration:underline; cursor:pointer}
    .row-empty td{opacity:.6}
    .row-actions{display:flex; gap:8px; align-items:center}
    .datebox{display:flex; gap:8px; align-items:center; margin:8px 0 16px; flex-wrap:wrap}
    input[type=date], select{background:#111; color:#eee; border:1px solid #333; border-radius:8px; padding:8px}
    .charts{margin-top:28px}
    .card{background:#0f0f0f; border:1px solid #222; border-radius:14px; padding:14px; margin-bottom:16px}
    .flex{display:flex; gap:16px; align-items:center; flex-wrap:wrap}
    /* Modal */
    .modal{position:fixed; inset:0; display:none; align-items:center; justify-content:center; background:rgba(0,0,0,.6); z-index:1000}
    .mdcard{background:#0f0f0f; border:1px solid #222; border-radius:14px; max-width:720px; width:92%; padding:18px}
    .x{float:right; cursor:pointer; color:#aaa}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="badge">Stripe Modo: <b>LIVE</b></div>
    <h2>Facturación y Clientes</h2>

    <div class="datebox">
      <label>Desde <input id="dStart" type="date"></label>
      <label>Hasta <input id="dEnd" type="date"></label>
      <button class="btn" id="btnReload">Actualizar tabla</button>
    </div>

    <table id="tbl">
      <thead>
        <tr><th>Cliente</th><th>Email</th><th>Teléfono</th><th>Consumo</th><th>Bot</th><th>Acciones</th></tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>

    <!-- ========== NUEVA SECCIÓN: Consumo en vivo con gráficos ========== -->
    <div class="charts">
      <div class="flex">
        <h3 style="margin:0">Consumo en vivo</h3>
        <label>Bot
          <select id="selBot"></select>
        </label>
        <label>Desde <input id="cStart" type="date"></label>
        <label>Hasta <input id="cEnd" type="date"></label>
        <button class="btn" id="btnCharts">Actualizar gráficos</button>
        <label class="flex" style="gap:6px; align-items:center">
          <input type="checkbox" id="liveChk">
          Live (30s)
        </label>
      </div>

      <div class="card"><canvas id="chOATokens" height="120"></canvas></div>
      <div class="card"><canvas id="chOACost"   height="120"></canvas></div>
      <div class="card"><canvas id="chTWMsgs"   height="120"></canvas></div>
      <div class="card"><canvas id="chTWCost"   height="120"></canvas></div>
    </div>
  </div>

  <!-- Modal de detalle (se mantiene) -->
  <div class="modal" id="modal">
    <div class="mdcard">
      <div class="x" onclick="closeModal()">✕</div>
      <h3 id="md-title">Detalle</h3>
      <div id="md-range" style="color:#aaa"></div>
      <div id="md-body" style="margin-top:10px"></div>
    </div>
  </div>

<script>
const tbody = document.getElementById('tbody');
const modal = document.getElementById('modal');
const dStart = document.getElementById('dStart');
const dEnd   = document.getElementById('dEnd');

const selBot = document.getElementById('selBot');
const cStart = document.getElementById('cStart');
const cEnd   = document.getElementById('cEnd');
const liveChk= document.getElementById('liveChk');

let timerLive = null;

function fmtUSD(n){ return '$' + (Number(n||0).toFixed(2)); }
function periodDefaults(inpStart, inpEnd){
  const now = new Date();
  const y = now.getFullYear(), m = String(now.getMonth()+1).padStart(2,'0');
  const first = `${y}-${m}-01`;
  const lastD = new Date(y, now.getMonth()+1, 0).getDate();
  const end = `${y}-${m}-${String(Math.min(now.getDate(), lastD)).padStart(2,'0')}`;
  if(inpStart && !inpStart.value) inpStart.value = first;
  if(inpEnd   && !inpEnd.value)   inpEnd.value   = end;
}
periodDefaults(dStart, dEnd);
periodDefaults(cStart, cEnd);

async function loadClients(){
  tbody.innerHTML = '<tr class="row-empty"><td colspan="6">Cargando...</td></tr>';
  const res = await fetch('/billing/clients');
  const js = await res.json();
  if(!js.success){ tbody.innerHTML = '<tr class="row-empty"><td colspan="6">Error cargando clientes</td></tr>'; return; }
  const rows = js.data.map(row => {
    const consumo = (row.consumo_cents||0)/100;
    return `
      <tr>
        <td><div class="name">${row.name||row.id}</div><div class="sub">ID: ${row.id}</div></td>
        <td>${row.email||'-'}</td>
        <td>${row.phone||'-'}</td>
        <td class="consumo"><a href="#" data-bot="${row.id}" onclick="openDetail(event)">${fmtUSD(consumo)}</a><div class="sub">${row.consumo_period||''}</div></td>
        <td><span class="dot ${row.bot_status==='on'?'':'off'}"></span> ${row.bot_status==='on'?'ON':'OFF'}</td>
        <td><button class="btn btn-primary" onclick="openDetail(event)" data-bot="${row.id}">Ver detalle</button></td>
      </tr>`;
  }).join('');
  tbody.innerHTML = rows || '<tr class="row-empty"><td colspan="6">Sin clientes</td></tr>';

  // llenar selector de bot si está vacío
  if(!selBot.options.length){
    js.data.forEach(r=>{
      const opt = document.createElement('option');
      opt.value = r.id; opt.textContent = r.name || r.id;
      selBot.appendChild(opt);
    });
  }
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
  const oa = js.openai || {}; const tw = js.twilio || {}; const svc = js.service_item || {};
  document.getElementById('md-body').innerHTML = `
    <div><b>OpenAI</b><br/>Requests: ${oa.requests||0} · Tokens in/out: ${oa.input_tokens||0} / ${oa.output_tokens||0} · Costo: <b>${fmtUSD(oa.cost_estimate_usd||0)}</b></div>
    <div style="margin-top:8px"><b>Twilio</b><br/>Mensajes: ${tw.messages||0} · Costo: <b>${fmtUSD(tw.price_usd||0)}</b></div>
    <div style="margin-top:8px"><b>Servicio</b><br/>${svc.label||'Servicio'}: <b>${svc.enabled?fmtUSD(svc.amount||0):'Deshabilitado'}</b></div>
    <div style="margin-top:8px"><b>Total</b><br/>Subtotal (OAI+Tw): ${fmtUSD(js.subtotal_usd||0)} · Total: <b>${fmtUSD(js.total_usd||0)}</b></div>`;
  modal.style.display='flex';
}
function closeModal(){ modal.style.display='none'; }

document.getElementById('btnReload').addEventListener('click', loadClients);
window.addEventListener('load', loadClients);
window.addEventListener('keydown', (e)=>{ if(e.key==='Escape') closeModal(); });

/* =========== GRÁFICOS =========== */
let chOATokens, chOACost, chTWMsgs, chTWCost;

function buildOrUpdate(chartRef, ctx, type, data, options){
  if(chartRef && chartRef.destroy) chartRef.destroy();
  return new Chart(ctx, {type, data, options});
}

async function loadCharts(){
  if(!selBot.value) return;
  const bot = selBot.value;
  const start = cStart.value, end = cEnd.value;
  const res = await fetch(`/billing/usage_ts/${encodeURIComponent(bot)}?start=${start}&end=${end}`);
  const js = await res.json();
  if(!js.success){ console.error(js); return; }
  const oa = js.openai, tw = js.twilio;

  const labels = oa.per_day.map(x=>x.date);

  // OpenAI tokens (líneas: input & output)
  chOATokens = buildOrUpdate(
    chOATokens,
    document.getElementById('chOATokens'),
    'line',
    {
      labels,
      datasets: [
        {label:'Input tokens', data: oa.per_day.map(x=>x.input_tokens), borderWidth:2, tension:.25},
        {label:'Output tokens', data: oa.per_day.map(x=>x.output_tokens), borderWidth:2, tension:.25}
      ]
    },
    {responsive:true, plugins:{legend:{labels:{color:'#ddd'}}}, scales:{x:{ticks:{color:'#aaa'}}, y:{ticks:{color:'#aaa'}}}}
  );

  // OpenAI costo (línea)
  chOACost = buildOrUpdate(
    chOACost,
    document.getElementById('chOACost'),
    'line',
    {
      labels,
      datasets: [{label:'Costo OpenAI (USD)', data: oa.per_day.map(x=>x.cost_estimate_usd), borderWidth:2, tension:.25}]
    },
    {responsive:true, plugins:{legend:{labels:{color:'#ddd'}}}, scales:{x:{ticks:{color:'#aaa'}}, y:{ticks:{color:'#aaa'}}}}
  );

  // Twilio mensajes (barras)
  const labelsTw = tw.per_day.map(x=>x.date);
  chTWMsgs = buildOrUpdate(
    chTWMsgs,
    document.getElementById('chTWMsgs'),
    'bar',
    {labels: labelsTw, datasets: [{label:'Mensajes Twilio', data: tw.per_day.map(x=>x.messages)}]},
    {responsive:true, plugins:{legend:{labels:{color:'#ddd'}}}, scales:{x:{ticks:{color:'#aaa'}}, y:{ticks:{color:'#aaa'}}}}
  );

  // Twilio costo (línea)
  chTWCost = buildOrUpdate(
    chTWCost,
    document.getElementById('chTWCost'),
    'line',
    {labels: labelsTw, datasets: [{label:'Costo Twilio (USD)', data: tw.per_day.map(x=>x.price_usd), borderWidth:2, tension:.25}]},
    {responsive:true, plugins:{legend:{labels:{color:'#ddd'}}}, scales:{x:{ticks:{color:'#aaa'}}, y:{ticks:{color:'#aaa'}}}}
  );
}

document.getElementById('btnCharts').addEventListener('click', loadCharts);
selBot.addEventListener('change', loadCharts);

liveChk.addEventListener('change', ()=>{
  if(liveChk.checked){
    if(timerLive) clearInterval(timerLive);
    timerLive = setInterval(loadCharts, 30000); // 30s
    loadCharts();
  }else{
    if(timerLive) clearInterval(timerLive);
    timerLive = null;
  }
});
window.addEventListener('load', ()=>{ if(selBot.value) loadCharts(); });

</script>
</body></html>
    """), 200, {"Content-Type": "text/html; charset=utf-8"}
