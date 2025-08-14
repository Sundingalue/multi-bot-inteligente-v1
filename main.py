# main.py — core minimalista: TODO el comportamiento vive en bots/*.json
from flask import Flask, request, session, redirect, url_for, send_file, jsonify, render_template
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from dotenv import load_dotenv
import os, json, time, csv, glob, re, hashlib
from threading import Thread
from datetime import datetime
from io import StringIO

# Firebase
import firebase_admin
from firebase_admin import credentials, db

# =======================
#  Entorno
# =======================
load_dotenv("/etc/secrets/.env")
load_dotenv()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") or ""
client = OpenAI(api_key=OPENAI_API_KEY)

app = Flask(__name__)
app.secret_key = "supersecreto_sundin_panel_2025"

# =======================
#  Firebase (con RTDB garantizada)
# =======================
firebase_key_path = "/etc/secrets/firebase.json"
# URL por defecto de tu RTDB (ajústala si tu proyecto cambia)
DEFAULT_DB_URL = "https://inhouston-209c0-default-rtdb.firebaseio.com/"
firebase_db_url = os.getenv("FIREBASE_DB_URL", DEFAULT_DB_URL).strip()

if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_key_path)
    if firebase_db_url:
        firebase_admin.initialize_app(cred, {'databaseURL': firebase_db_url})
        print(f"[BOOT] Firebase inicializado con RTDB: {firebase_db_url}")
    else:
        firebase_admin.initialize_app(cred)
        print("[BOOT] Firebase inicializado SIN databaseURL (no recomendado)")

# =======================
#  Bots desde carpeta bots/
# =======================
def load_bots_folder():
    bots = {}
    for path in glob.glob(os.path.join("bots", "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    bots.update(data)
        except Exception as e:
            print(f"⚠️ No se pudo cargar {path}: {e}")
    return bots

bots_config = load_bots_folder()
if not bots_config:
    print("⚠️ No se encontraron bots en ./bots/*.json")

def _get_bot_cfg_by_number(to_number: str):
    return bots_config.get(to_number)

# =======================
#  Estado runtime (solo hist.)
# =======================
session_history = {}   # clave_sesion -> list(messages)
contact_name = {}      # clave_sesion -> nombre detectado (si aparece)

def _hora_to_epoch_ms(hora_str: str) -> int:
    try:
        dt = datetime.strptime(hora_str, "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0

# =======================
#  Firebase leads helpers
# =======================
def _lead_ref(bot_nombre, numero):
    return db.reference(f"leads/{bot_nombre}/{numero}")

def fb_get_lead(bot_nombre, numero):
    return _lead_ref(bot_nombre, numero).get() or {}

def fb_append_historial(bot_nombre, numero, entrada):
    ref = _lead_ref(bot_nombre, numero)
    lead = ref.get() or {}
    historial = lead.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]
    historial.append(entrada)
    lead["historial"] = historial
    lead["last_message"] = entrada.get("texto", "")
    lead["last_seen"] = entrada.get("hora", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lead["messages"] = int(lead.get("messages", 0)) + 1
    lead.setdefault("bot", bot_nombre)
    lead.setdefault("numero", numero)
    lead.setdefault("status", "nuevo")
    lead.setdefault("notes", "")
    ref.set(lead)

def fb_list_leads_all():
    root = db.reference("leads").get() or {}
    leads = {}
    if not isinstance(root, dict):
        return leads
    for bot_nombre, numeros in root.items():
        if not isinstance(numeros, dict):
            continue
        for numero, data in numeros.items():
            clave = f"{bot_nombre}|{numero}"
            leads[clave] = {
                "bot": bot_nombre,
                "numero": numero,
                "first_seen": data.get("first_seen", ""),
                "last_message": data.get("last_message", ""),
                "last_seen": data.get("last_seen", ""),
                "messages": int(data.get("messages", 0)),
                "status": data.get("status", "nuevo"),
                "notes": data.get("notes", "")
            }
    return leads

def fb_list_leads_by_bot(bot_nombre):
    numeros = db.reference(f"leads/{bot_nombre}").get() or {}
    leads = {}
    if not isinstance(numeros, dict):
        return leads
    for numero, data in numeros.items():
        clave = f"{bot_nombre}|{numero}"
        leads[clave] = {
            "bot": bot_nombre,
            "numero": numero,
            "first_seen": data.get("first_seen", ""),
            "last_message": data.get("last_message", ""),
            "last_seen": data.get("last_seen", ""),
            "messages": int(data.get("messages", 0)),
            "status": data.get("status", "nuevo"),
            "notes": data.get("notes", "")
        }
    return leads

def guardar_lead(bot_nombre, numero, mensaje):
    try:
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lead = fb_get_lead(bot_nombre, numero)
        if not lead:
            lead = {
                "bot": bot_nombre,
                "numero": numero,
                "first_seen": ahora,
                "last_message": mensaje,
                "last_seen": ahora,
                "messages": 0,
                "status": "nuevo",
                "notes": "",
                "historial": []
            }
            _lead_ref(bot_nombre, numero).set(lead)
        fb_append_historial(bot_nombre, numero, {"tipo": "user", "texto": mensaje, "hora": ahora})
    except Exception as e:
        print(f"❌ Error guardando lead: {e}")
    else:
        print(f"✅ Lead guardado: bot={bot_nombre} numero={numero} texto='{(mensaje or '')[:80]}'")

# =======================
#  Utilidades
# =======================
def _extract_name(text: str) -> str:
    if not text:
        return ""
    t = text.strip()
    m = re.search(r"(?:me llamo|mi nombre es)\s+([A-Za-zÁÉÍÓÚÑáéíóúñ]+)", t, re.IGNORECASE)
    if m: return m.group(1).strip().capitalize()
    m = re.search(r"^soy\s+([A-Za-zÁÉÍÓÚÑáéíóúñ]+)", t, re.IGNORECASE)
    if m: return m.group(1).strip().capitalize()
    m = re.search(r"^(?:hola[,!\s]+)?([A-Za-zÁÉÍÓÚÑáéíóúñ]{3,})\b", t, re.IGNORECASE)
    if m:
        posible = m.group(1).strip().capitalize()
        if posible.lower() not in {"hola","buenas","buenos","dias","días","tardes","noches"}:
            return posible
    return ""

def _dump_bot_context(bot_cfg: dict) -> str:
    """
    Pasa variables del bot al modelo como contexto, para que TODO se controle por JSON.
    No decide lógica: solo expone datos.
    """
    ctx = {
        "name": bot_cfg.get("name"),
        "business_name": bot_cfg.get("business_name"),
        "links": bot_cfg.get("links"),
        "style": bot_cfg.get("style"),
        "behavior": bot_cfg.get("behavior"),
        "agenda": bot_cfg.get("agenda"),
        "nlu": bot_cfg.get("nlu"),
        "questionnaire": bot_cfg.get("questionnaire"),
        "policies": bot_cfg.get("policies"),
        "preamble": bot_cfg.get("preamble"),
    }
    ctx = {k: v for k, v in ctx.items() if v is not None}
    return "BOT_CONTEXT_JSON = " + json.dumps(ctx, ensure_ascii=False)

def _build_system(bot_cfg: dict) -> str:
    base = (bot_cfg or {}).get("system_prompt", "") or ""
    ctx = _dump_bot_context(bot_cfg)
    return (base + "\n\n" + ctx).strip()

# =======================
#  Rutas
# =======================
@app.after_request
def permitir_iframe(response):
    response.headers["X-Frame-Options"] = "ALLOWALL"
    return response

@app.route("/", methods=["GET"])
def home():
    return "✅ Bot inteligente activo (core minimalista)."

@app.route("/login", methods=["GET"])
def login_redirect():
    return redirect(url_for("panel"))

@app.route("/login.html", methods=["GET"])
def login_html_redirect():
    return redirect(url_for("panel"))

def _load_users():
    env_users = {}
    for key, val in os.environ.items():
        if not key.startswith("USER_"):
            continue
        alias = key[len("USER_"):]
        username = val.strip()
        password = os.environ.get(f"PASS_{alias}", "").strip()
        panel = os.environ.get(f"PANEL_{alias}", "").strip()
        if not username or not password or not panel:
            continue
        if panel.lower() == "panel":
            bots_list = ["*"]
        elif panel.lower().startswith("panel-bot/"):
            bot_name = panel.split("/", 1)[1].strip()
            bots_list = [bot_name] if bot_name else []
        else:
            bots_list = []
        if bots_list:
            env_users[username] = {"password": password, "bots": bots_list}
    if env_users:
        return env_users

    default_users = {"sundin": {"password": "inhouston2025", "bots": ["*"]}}
    raw = os.getenv("PANEL_USERS_JSON")
    if not raw:
        return default_users
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return default_users
        norm = {}
        for user, rec in data.items():
            pwd = rec.get("password") if isinstance(rec, dict) else None
            bots = rec.get("bots") if isinstance(rec, dict) else None
            if isinstance(pwd, str) and isinstance(bots, list) and bots:
                norm[user] = {"password": pwd, "bots": bots}
        return norm or default_users
    except Exception as e:
        print(f"⚠️ PANEL_USERS_JSON inválido: {e}")
        return default_users

def _auth_user(username, password):
    users = _load_users()
    rec = users.get(username)
    if rec and rec.get("password") == password:
        return {"username": username, "bots": rec.get("bots", [])}
    return None

def _is_admin():
    bots = session.get("bots_permitidos", [])
    return isinstance(bots, list) and ("*" in bots)

def _first_allowed_bot():
    bots = session.get("bots_permitidos", [])
    if isinstance(bots, list):
        for b in bots:
            if b != "*":
                return b
    return None

def _normalize_bot_name(name: str):
    for cfg in bots_config.values():
        if cfg.get("name", "").lower() == str(name).lower():
            return cfg.get("name")
    return None

def _get_bot_cfg_by_name(name: str):
    if not name:
        return None
    for cfg in bots_config.values():
        if isinstance(cfg, dict) and cfg.get("name", "").lower() == name.lower():
            return cfg
    return None

@app.route("/panel", methods=["GET", "POST"])
def panel():
    if not session.get("autenticado"):
        if request.method == "POST":
            usuario = request.form.get("usuario", "").strip()
            clave = request.form.get("clave", "").strip()
            auth = _auth_user(usuario, clave)
            if auth:
                session["autenticado"] = True
                session["usuario"] = auth["username"]
                session["bots_permitidos"] = auth["bots"]
                if "*" in auth["bots"]:
                    return redirect(url_for("panel"))
                destino = _first_allowed_bot()
                if destino:
                    return redirect(url_for("panel_exclusivo_bot", bot_nombre=destino))
                return redirect(url_for("panel"))
            return render_template("login.html", error=True)
        return render_template("login.html")

    if not _is_admin():
        destino = _first_allowed_bot()
        if destino:
            return redirect(url_for("panel_exclusivo_bot", bot_nombre=destino))

    leads_todos = fb_list_leads_all()
    bots_disponibles = {}
    for cfg in bots_config.values():
        bots_disponibles[cfg["name"]] = cfg.get("business_name", cfg["name"])

    bot_seleccionado = request.args.get("bot")
    if bot_seleccionado:
        bot_norm = _normalize_bot_name(bot_seleccionado) or bot_seleccionado
        leads_filtrados = {k: v for k, v in leads_todos.items() if v.get("bot") == bot_norm}
    else:
        leads_filtrados = leads_todos

    return render_template("panel.html", leads=leads_filtrados, bots=bots_disponibles, bot_seleccionado=bot_seleccionado)

@app.route("/panel-bot/<bot_nombre>")
def panel_exclusivo_bot(bot_nombre):
    if not session.get("autenticado"):
        return redirect(url_for("panel"))
    bot_normalizado = _normalize_bot_name(bot_nombre)
    if not bot_normalizado:
        return f"Bot '{bot_nombre}' no encontrado", 404
    bots_ok = session.get("bots_permitidos", [])
    if not (_is_admin() or bot_normalizado in bots_ok):
        return "No autorizado para este bot", 403
    leads_filtrados = fb_list_leads_by_bot(bot_normalizado)
    nombre_comercial = next(
        (config.get("business_name", bot_normalizado)
         for config in bots_config.values()
         if config.get("name") == bot_normalizado),
        bot_normalizado
    )
    return render_template("panel_bot.html", leads=leads_filtrados, bot=bot_normalizado, nombre_comercial=nombre_comercial)

@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("panel"))

@app.route("/exportar")
def exportar():
    if not session.get("autenticado"):
        return redirect(url_for("panel"))
    leads = fb_list_leads_all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Bot", "Número", "Primer contacto", "Último mensaje", "Última vez", "Mensajes", "Estado", "Notas"])
    for _, datos in leads.items():
        writer.writerow([
            datos.get("bot", ""), datos.get("numero", ""), datos.get("first_seen", ""),
            datos.get("last_message", ""), datos.get("last_seen", ""), datos.get("messages", ""),
            datos.get("status", ""), datos.get("notes", "")
        ])
    output.seek(0)
    return send_file(output, mimetype="text/csv", download_name="leads.csv", as_attachment=True)

# =======================
#  WhatsApp Webhook — SIN REGLAS duras
# =======================
@app.route("/webhook", methods=["GET"])
def verify_whatsapp():
    VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN_WHATSAPP")
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    else:
        return "Token inválido", 403

@app.route("/webhook", methods=["POST"])
def whatsapp_bot():
    incoming_msg = (request.values.get("Body", "") or "").strip()
    sender_number = request.values.get("From", "")
    bot_number = request.values.get("To", "")

    clave_sesion = f"{bot_number}|{sender_number}"
    bot = _get_bot_cfg_by_number(bot_number)

    response = MessagingResponse()
    msg = response.message()

    if not bot:
        print(f"⚠️ To={bot_number} no está mapeado en bots/*.json")
        msg.body("Lo siento, este número no está asignado a ningún bot.")
        return str(response)

    # ===== Persistencia lead
    guardar_lead(bot.get("name", "bot"), sender_number, incoming_msg)

    # ===== Historial + System (100% desde JSON)
    if clave_sesion not in session_history:
        sysmsg = _build_system(bot)
        session_history[clave_sesion] = [{"role": "system", "content": sysmsg}]

    # Detecta nombre de pasada
    nombre_detectado = _extract_name(incoming_msg)
    if nombre_detectado:
        contact_name[clave_sesion] = nombre_detectado

    session_history[clave_sesion].append({"role": "user", "content": incoming_msg})

    # ===== Llamada al modelo configurado por bot
    model_name = (bot.get("model") or "gpt-4o")
    temperature = float(bot.get("temperature", 0.6))
    try:
        completion = client.chat.completions.create(
            model=model_name,
            temperature=temperature,
            messages=session_history[clave_sesion]
        )
        respuesta = (completion.choices[0].message.content or "").strip()
        session_history[clave_sesion].append({"role": "assistant", "content": respuesta})
        msg.body(respuesta)

        # Guardar respuesta del bot en Firebase
        try:
            ahora_bot = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fb_append_historial(bot.get("name", "bot"), sender_number, {"tipo": "bot", "texto": respuesta, "hora": ahora_bot})
        except Exception as e:
            print(f"⚠️ No se pudo guardar respuesta del bot: {e}")

    except Exception as e:
        print(f"❌ Error con GPT: {e}")
        msg.body("Lo siento, hubo un error generando la respuesta.")

    return str(response)

# =======================
#  API de polling (lee Firebase)
# =======================
@app.route("/api/chat/<bot>/<numero>", methods=["GET"])
def api_chat(bot, numero):
    if not session.get("autenticado"):
        return jsonify({"error": "No autenticado"}), 401
    bot_normalizado = _normalize_bot_name(bot)
    if not bot_normalizado:
        return jsonify({"error": "Bot no encontrado"}), 404
    bots_ok = session.get("bots_permitidos", [])
    if not (_is_admin() or bot_normalizado in bots_ok):
        return jsonify({"error": "No autorizado"}), 403

    since_param = request.args.get("since", "").strip()
    try:
        since_ms = int(since_param) if since_param else 0
    except ValueError:
        since_ms = 0

    data = fb_get_lead(bot_normalizado, numero)
    historial = data.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]

    nuevos, last_ts = [], since_ms
    for reg in historial:
        ts = _hora_to_epoch_ms(reg.get("hora", ""))
        if ts > since_ms:
            nuevos.append({"texto": reg.get("texto", ""), "hora": reg.get("hora", ""), "tipo": reg.get("tipo", "user"), "ts": ts})
        if ts > last_ts:
            last_ts = ts

    if since_ms == 0 and not nuevos and historial:
        nuevos = [{"texto": r.get("texto", ""), "hora": r.get("hora", ""), "tipo": r.get("tipo", "user"), "ts": _hora_to_epoch_ms(r.get("hora", ""))} for r in historial]
        last_ts = max([_hora_to_epoch_ms(r.get("hora", "")) for r in historial] or [0])

    return jsonify({"mensajes": nuevos, "last_ts": last_ts})

# =======================
#  Run
# =======================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
