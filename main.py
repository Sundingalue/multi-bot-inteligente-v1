# main.py — core genérico (sin conocimiento de marca en el core)
from gevent import monkey
monkey.patch_all()

from flask import Flask, request, session, redirect, url_for, send_file, jsonify, render_template, make_response
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse, Connect
from openai import OpenAI
from dotenv import load_dotenv
import os, json, time, csv, re, glob, random, hashlib
from threading import Thread
from datetime import datetime, timedelta
from io import StringIO

# Twilio REST
from twilio.rest import Client as TwilioClient

# Firebase
import firebase_admin
from firebase_admin import credentials, db, messaging as fcm

# WebSocket / Realtime
import ssl
try:
    from flask_sock import Sock
    import websocket  # websocket-client
except Exception:
    print("⚠️ Falta dependencia: pip install flask-sock websocket-client")

# ========= Config y env =========
load_dotenv("/etc/secrets/.env")
load_dotenv()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY","")
TWILIO_ACCOUNT_SID = (os.environ.get("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN  = (os.environ.get("TWILIO_AUTH_TOKEN") or "").strip()

BOOKING_URL_FALLBACK = (os.environ.get("BOOKING_URL","").strip())
APP_DOWNLOAD_URL_FALLBACK = (os.environ.get("APP_DOWNLOAD_URL","").strip())

API_BEARER_TOKEN = (os.environ.get("API_BEARER_TOKEN") or "").strip()
OPENAI_REALTIME_MODEL = os.environ.get("OPENAI_REALTIME_MODEL","gpt-4o-realtime-preview").strip()
OPENAI_REALTIME_VOICE = os.environ.get("OPENAI_REALTIME_VOICE","alloy").strip()

def _valid_url(u:str)->bool:
    return isinstance(u,str) and (u.startswith("http://") or u.startswith("https://"))

client = OpenAI(api_key=OPENAI_API_KEY)
app = Flask(__name__)
app.secret_key = "supersecreto_sundin_panel_2025"
app.permanent_session_lifetime = timedelta(days=60)
app.config.update({
    "SESSION_COOKIE_SAMESITE": "Lax",
    "SESSION_COOKIE_SECURE": False if os.getenv("DEV_HTTP","").lower()=="true" else True
})

@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

def _bearer_ok(req)->bool:
    if not API_BEARER_TOKEN: return True
    return (req.headers.get("Authorization") or "").strip() == f"Bearer {API_BEARER_TOKEN}"

# ========= Firebase =========
firebase_key_path = "/etc/secrets/firebase.json"
firebase_db_url = (os.getenv("FIREBASE_DB_URL") or "").strip()
if not firebase_db_url:
    try:
        with open("/etc/secrets/FIREBASE_DB_URL","r",encoding="utf-8") as f:
            firebase_db_url = f.read().strip().strip('"').strip("'")
            if firebase_db_url: print("[BOOT] FIREBASE_DB_URL leído desde Secret File.")
    except Exception:
        pass

if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_key_path)
    if firebase_db_url:
        firebase_admin.initialize_app(cred, {'databaseURL': firebase_db_url})
        print(f"[BOOT] Firebase inicializado con RTDB: {firebase_db_url}")
    else:
        firebase_admin.initialize_app(cred)
        print("⚠️ Firebase sin databaseURL (configura FIREBASE_DB_URL).")

# ========= Twilio REST =========
twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        print("[BOOT] Twilio REST client inicializado.")
    except Exception as e:
        print(f"⚠️ No se pudo inicializar Twilio REST client: {e}")
else:
    print("⚠️ TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN no configurados.")

# ========= Bots =========
def load_bots_folder():
    bots={}
    for path in glob.glob(os.path.join("bots","*.json")):
        try:
            with open(path,"r",encoding="utf-8") as f:
                data=json.load(f)
                if isinstance(data,dict):
                    bots.update(data)
        except Exception as e:
            print(f"⚠️ No se pudo cargar {path}: {e}")
    return bots

bots_config = load_bots_folder()
if not bots_config: print("⚠️ No se encontraron bots en ./bots/*.json")

# ========= Billing API & mobile API =========
from billing_api import billing_bp, record_openai_usage
app.register_blueprint(billing_bp, url_prefix="/billing")

from bots.api_mobile import mobile_bp
app.register_blueprint(mobile_bp, url_prefix="/api/mobile")

# ========= Estado en memoria =========
session_history={}
last_message_time={}
follow_up_flags={}
agenda_state={}
greeted_state={}

# ========= Helpers =========
def _hora_to_epoch_ms(h):
    try: return int(datetime.strptime(h,"%Y-%m-%d %H:%M:%S").timestamp()*1000)
    except: return 0

def _normalize_bot_name(name:str):
    for cfg in bots_config.values():
        if cfg.get("name","").lower()==str(name).lower():
            return cfg.get("name")
    return None

def _get_bot_cfg_by_name(name:str):
    if not name: return None
    for cfg in bots_config.values():
        if isinstance(cfg,dict) and cfg.get("name","").lower()==name.lower():
            return cfg
    return None

def _get_bot_cfg_by_number(to_number:str):
    return bots_config.get(to_number)

def _canonize_phone(raw:str)->str:
    s=str(raw or "").strip()
    for p in ("whatsapp:","tel:","sip:","client:"):
        if s.startswith(p): s=s[len(p):]
    digits="".join(ch for ch in s if ch.isdigit())
    if not digits: return ""
    if len(digits)==11 and digits.startswith("1"): return "+"+digits
    if len(digits)==10: digits="1"+digits
    return "+"+digits

def _get_bot_cfg_by_any_number(to_number:str):
    if not to_number: return None
    target=_canonize_phone(to_number)
    if to_number in bots_config: return bots_config[to_number]
    if f"whatsapp:{target}" in bots_config: return bots_config[f"whatsapp:{target}"]
    if target in bots_config: return bots_config[target]
    for key,cfg in bots_config.items():
        try:
            if _canonize_phone(key)==target: return cfg
        except: pass
    try:
        if len(bots_config)==1: return list(bots_config.values())[0]
    except: pass
    return None

def _get_bot_number_by_name(bot_name:str)->str:
    for number_key,cfg in bots_config.items():
        if isinstance(cfg,dict) and cfg.get("name","").strip().lower()==(bot_name or "").strip().lower():
            return number_key
    return ""

def _split_sentences(text:str):
    parts=re.split(r'(?<=[\.\!\?])\s+', (text or "").strip())
    if len(parts)==1 and len(text or "")>280:
        parts=[text[:200].strip(), text[200:].strip()]
    return [p for p in parts if p]

def _apply_style(bot_cfg, text):
    style=(bot_cfg or {}).get("style",{}) or {}
    short=bool(style.get("short_replies",True))
    max_sents=int(style.get("max_sentences",2)) if style.get("max_sentences") is not None else 2
    if not text: return text
    if short:
        sents=_split_sentences(text)
        text=" ".join(sents[:max_sents]).strip()
    return text

def _next_probe_from_bot(bot_cfg):
    probes=[(p or "").strip() for p in (bot_cfg or {}).get("style",{}).get("probes",[]) if isinstance(p,str) and p.strip()]
    return random.choice(probes) if probes else ""

def _ensure_question(bot_cfg,text,force_question):
    txt=re.sub(r"\s+"," ", (text or "")).strip()
    if not force_question: return txt
    if "?" in txt: return txt
    if not txt.endswith((".", "!", "…")): txt+="."
    probe=_next_probe_from_bot(bot_cfg)
    return f"{txt} {probe}".strip() if probe else txt

def _make_system_message(bot_cfg:dict)->str:
    return (bot_cfg or {}).get("system_prompt","") or ""

def _drill_get(d:dict,path:str):
    cur=d
    for k in path.split("."):
        if isinstance(cur,dict) and k in cur: cur=cur[k]
        else: return None
    return cur

def _effective_booking_url(bot_cfg:dict)->str:
    for p in ["links.booking_url","booking_url","calendar_booking_url","google_calendar_booking_url","agenda.booking_url"]:
        val=_drill_get(bot_cfg or {}, p)
        val=(val or "").strip() if isinstance(val,str) else ""
        if _valid_url(val): return val
    return BOOKING_URL_FALLBACK if _valid_url(BOOKING_URL_FALLBACK) else ""

def _effective_app_url(bot_cfg:dict)->str:
    for p in ["links.app_download_url","links.app_url","app_download_url","app_url","download_url","link_app"]:
        val=_drill_get(bot_cfg or {}, p)
        val=(val or "").strip() if isinstance(val,str) else ""
        if _valid_url(val): return val
    return APP_DOWNLOAD_URL_FALLBACK if _valid_url(APP_DOWNLOAD_URL_FALLBACK) else ""

SCHEDULE_OFFER_PAT=re.compile(r"\b(enlace|link|calendar|calendario|agendar|agenda|reservar|reserva|cita|schedule|book|appointment|meeting|call)\b", re.IGNORECASE)
def _wants_link(text:str)->bool: return bool(SCHEDULE_OFFER_PAT.search(text or ""))

def _wants_app_download(text:str)->bool:
    t=(text or "").lower()
    has_app=any(w in t for w in ["app","aplicación","aplicacion","ios","android","play store","app store"])
    has_dl=any(w in t for w in ["descargar","download","bajar","instalar","link","enlace"])
    return ("descargar app" in t) or ("download app" in t) or (has_app and has_dl)

def _is_affirmative(t:str)->bool:
    if not t: return False
    t=t.strip().lower()
    afirm={"si","sí","ok","okay","dale","va","claro","por favor","hagamoslo","hagámoslo","perfecto","de una","yes","yep","yeah","sure","please"}
    return any(t==a or t.startswith(a+" ") for a in afirm)

def _is_negative(t:str)->bool:
    if not t: return False
    t=re.sub(r'[.,;:!?]+$','',t.strip().lower()); t=re.sub(r'\s+',' ',t)
    return t in {"no","nop","no gracias","ahora no","luego","después","despues","not now"}

def _is_scheduled_confirmation(t:str)->bool:
    if not t: return False
    t=t.lower()
    kws=["ya agende","ya agendé","agende","agendé","ya programe","ya programé","ya agendado","agendado","confirmé","confirmado","listo","done","booked","i booked","i scheduled","scheduled"]
    return any(k in t for k in kws)

def _is_polite_closure(t:str)->bool:
    if not t: return False
    t=t.strip().lower()
    cierres={"gracias","muchas gracias","ok gracias","listo gracias","perfecto gracias","estamos en contacto","por ahora está bien","por ahora esta bien","luego te escribo","luego hablamos","hasta luego","buen día","buen dia","buenas noches","nos vemos","chao","bye","eso es todo","todo bien gracias"}
    return any(t==c or t.startswith(c+" ") for c in cierres)

def _now(): return int(time.time())
def _minutes_since(ts): return (_now()-int(ts or 0))/60.0
def _hash_text(s:str)->str: return hashlib.md5((s or "").strip().lower().encode("utf-8")).hexdigest()

def _get_agenda(k):
    return agenda_state.get(k) or {"awaiting_confirm":False,"status":"none","last_update":0,"last_link_time":0,"last_bot_hash":"","closed":False}

def _set_agenda(k,**kw):
    st=_get_agenda(k); st.update(kw); st["last_update"]=_now(); agenda_state[k]=st; return st

def _can_send_link(k, cooldown_min=10):
    st=_get_agenda(k)
    if st.get("status") in ("link_sent","confirmed") and _minutes_since(st.get("last_link_time"))<cooldown_min: return False
    return True

# ========= Firebase helpers =========
def _lead_ref(bot_nombre, numero): return db.reference(f"leads/{bot_nombre}/{numero}")

def fb_get_lead(bot_nombre, numero): return _lead_ref(bot_nombre,numero).get() or {}

def fb_append_historial(bot_nombre, numero, entrada):
    ref=_lead_ref(bot_nombre, numero)
    lead=ref.get() or {}
    historial=lead.get("historial",[])
    if isinstance(historial,dict): historial=[historial[k] for k in sorted(historial.keys())]
    historial.append(entrada)
    lead["historial"]=historial
    lead["last_message"]=entrada.get("texto","")
    lead["last_seen"]=entrada.get("hora", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lead["messages"]=int(lead.get("messages",0))+1
    lead.setdefault("bot",bot_nombre); lead.setdefault("numero",numero); lead.setdefault("status","nuevo"); lead.setdefault("notes","")
    ref.set(lead)

def fb_list_leads_all():
    root=db.reference("leads").get() or {}
    leads={}
    if not isinstance(root,dict): return leads
    for bot_nombre,numeros in root.items():
        if not isinstance(numeros,dict): continue
        for numero,data in numeros.items():
            clave=f"{bot_nombre}|{numero}"
            leads[clave]={
                "bot":bot_nombre,"numero":numero,
                "first_seen":data.get("first_seen",""),
                "last_message":data.get("last_message",""),
                "last_seen":data.get("last_seen",""),
                "messages":int(data.get("messages",0)),
                "status":data.get("status","nuevo"),
                "notes":data.get("notes","")
            }
    return leads

def fb_list_leads_by_bot(bot_nombre):
    numeros=db.reference(f"leads/{bot_nombre}").get() or {}
    leads={}
    if not isinstance(numeros,dict): return leads
    for numero,data in numeros.items():
        clave=f"{bot_nombre}|{numero}"
        leads[clave]={
            "bot":bot_nombre,"numero":numero,
            "first_seen":data.get("first_seen",""),
            "last_message":data.get("last_message",""),
            "last_seen":data.get("last_seen",""),
            "messages":int(data.get("messages",0)),
            "status":data.get("status","nuevo"),
            "notes":data.get("notes","")
        }
    return leads

def fb_delete_lead(bot_nombre, numero):
    try: _lead_ref(bot_nombre,numero).delete(); return True
    except Exception as e: print(f"❌ Error eliminando lead {bot_nombre}/{numero}: {e}"); return False

def fb_clear_historial(bot_nombre, numero):
    try:
        ref=_lead_ref(bot_nombre,numero)
        lead=ref.get() or {}
        lead["historial"]=[]; lead["messages"]=0; lead["last_message"]=""; lead["last_seen"]=""
        lead.setdefault("status","nuevo"); lead.setdefault("notes",""); lead.setdefault("bot",bot_nombre); lead.setdefault("numero",numero)
        ref.set(lead); return True
    except Exception as e:
        print(f"❌ Error vaciando historial {bot_nombre}/{numero}: {e}"); return False

def fb_is_bot_on(bot_name:str)->bool:
    try:
        val=db.reference(f"billing/status/{bot_name}").get()
        if isinstance(val,bool): return val
        if isinstance(val,str):  return val.lower()=="on"
    except Exception as e:
        print(f"⚠️ Error leyendo status del bot '{bot_name}': {e}")
    return True

def fb_is_conversation_on(bot_nombre:str, numero:str)->bool:
    try:
        lead=_lead_ref(bot_nombre,numero).get() or {}
        val=lead.get("bot_enabled",None)
        if isinstance(val,bool): return val
        if isinstance(val,str):  return val.lower() in ("on","true","1","yes","si","sí")
    except Exception as e:
        print(f"⚠️ Error leyendo bot_enabled en {bot_nombre}/{numero}: {e}")
    return True

def fb_set_conversation_on(bot_nombre:str, numero:str, enabled:bool):
    try:
        ref=_lead_ref(bot_nombre,numero); cur=ref.get() or {}; cur["bot_enabled"]=bool(enabled); ref.set(cur); return True
    except Exception as e:
        print(f"⚠️ Error guardando bot_enabled en {bot_nombre}/{numero}: {e}"); return False

def _hydrate_session_from_firebase(clave_sesion, bot_cfg, sender_number):
    if clave_sesion in session_history: return
    bot_name=(bot_cfg or {}).get("name","");  lead=fb_get_lead(bot_name, sender_number) if bot_name else {}
    historial=lead.get("historial",[])
    if isinstance(historial,dict): historial=[historial[k] for k in sorted(historial.keys())]
    msgs=[]; sysmsg=_make_system_message(bot_cfg)
    if sysmsg: msgs.append({"role":"system","content":sysmsg})
    for reg in historial:
        texto=reg.get("texto","");  role="assistant" if (reg.get("tipo","user")!="user") else "user"
        if texto: msgs.append({"role":role,"content":texto})
    if msgs:
        session_history[clave_sesion]=msgs; greeted_state[clave_sesion]=True; follow_up_flags[clave_sesion]={"5min":False,"60min":False}

# ========= Panel/UI =========
def _load_users():
    users_from_json={}
    def _normalize_list_scope(scope_val):
        if isinstance(scope_val,str):
            scope_val=scope_val.strip()
            if scope_val=="*": return ["*"]
            return [_normalize_bot_name(scope_val) or scope_val]
        elif isinstance(scope_val,list):
            allowed=[]
            for s in scope_val:
                s=(s or "").strip()
                if not s: continue
                if s=="*": return ["*"]
                allowed.append(_normalize_bot_name(s) or s)
            return allowed or []
        return []

    for cfg in bots_config.values():
        if not isinstance(cfg,dict): continue
        bot_name=(cfg.get("name") or "").strip()
        if not bot_name: continue
        logins=[]
        if isinstance(cfg.get("login"),dict): logins.append(cfg["login"])
        if isinstance(cfg.get("logins"),list): logins.extend([x for x in cfg["logins"] if isinstance(x,dict)])
        if isinstance(cfg.get("auth"),dict):  logins.append(cfg["auth"])
        for entry in logins:
            username=(entry.get("username") or "").strip()
            password=(entry.get("password") or "").strip()
            scope_val=entry.get("scope")
            panel_hint=(entry.get("panel") or "").strip().lower()
            if not username or not password: continue
            allowed_bots=_normalize_list_scope(scope_val)
            if not allowed_bots and panel_hint:
                if panel_hint=="panel": allowed_bots=["*"]
                elif panel_hint.startswith("panel-bot/"):
                    only_bot=panel_hint.split("/",1)[1].strip()
                    if only_bot: allowed_bots=[_normalize_bot_name(only_bot) or only_bot]
            if not allowed_bots: allowed_bots=[bot_name]
            if username in users_from_json:
                prev=users_from_json[username].get("bots",[])
                users_from_json[username]["bots"] = ["*"] if ("*" in prev or "*" in allowed_bots) else list(dict.fromkeys(prev+allowed_bots))
                users_from_json[username]["password"]=password or users_from_json[username]["password"]
            else:
                users_from_json[username]={"password":password,"bots":allowed_bots}

    if users_from_json: return users_from_json

    env_users={}
    for key,val in os.environ.items():
        if not key.startswith("USER_"): continue
        alias=key[len("USER_"):]
        username=(val or "").strip()
        password=(os.environ.get(f"PASS_{alias}","") or "").strip()
        panel=(os.environ.get(f"PANEL_{alias}","") or "").strip()
        if not username or not password or not panel: continue
        if panel.lower()=="panel": bots_list=["*"]
        elif panel.lower().startswith("panel-bot/"):
            bot_name=panel.split("/",1)[1].strip(); bots_list=[_normalize_bot_name(bot_name) or bot_name] if bot_name else []
        else: bots_list=[]
        if bots_list: env_users[username]={"password":password,"bots":bots_list}
    if env_users: return env_users

    return {"sundin":{"password":"inhouston2025","bots":["*"]}}

def _auth_user(username,password):
    users=_load_users(); rec=users.get(username)
    return {"username":username,"bots":rec.get("bots",[])} if rec and rec.get("password")==password else None

def _is_admin():
    bots=session.get("bots_permitidos",[]); return isinstance(bots,list) and ("*" in bots)

def _first_allowed_bot():
    bots=session.get("bots_permitidos",[])
    if isinstance(bots,list):
        for b in bots:
            if b!="*": return b
    return None

def _user_can_access_bot(bot_name):
    return True if _is_admin() else bot_name in (session.get("bots_permitidos",[]) or [])

@app.route("/panel-bot/<bot_nombre>")
def panel_exclusivo_bot(bot_nombre):
    if not session.get("autenticado"): return redirect(url_for("panel"))
    bot_normalizado=_normalize_bot_name(bot_nombre)
    if not bot_normalizado: return f"Bot '{bot_nombre}' no encontrado",404
    if not _user_can_access_bot(bot_normalizado): return "No autorizado para este bot",403
    leads_filtrados=fb_list_leads_by_bot(bot_normalizado)
    nombre_comercial=next((c.get("business_name",bot_normalizado) for c in bots_config.values() if c.get("name")==bot_normalizado), bot_normalizado)
    return render_template("panel_bot.html", leads=leads_filtrados, bot=bot_normalizado, nombre_comercial=nombre_comercial)

@app.route("/", methods=["GET"])
def home():
    print(f"[BOOT] BOOKING_URL_FALLBACK={BOOKING_URL_FALLBACK}")
    print(f"[BOOT] APP_DOWNLOAD_URL_FALLBACK={APP_DOWNLOAD_URL_FALLBACK}")
    return "✅ Bot inteligente activo."

@app.route("/login", methods=["GET"])
def login_redirect(): return redirect(url_for("panel"))

@app.route("/login.html", methods=["GET"])
def login_html_redirect(): return redirect(url_for("panel"))

@app.route("/panel", methods=["GET","POST"])
def panel():
    if not session.get("autenticado"):
        if request.method=="POST":
            usuario=(request.form.get("usuario") or request.form.get("username") or request.form.get("email") or "").strip()
            clave=request.form.get("clave");  clave=(clave if (clave is not None and clave!="") else request.form.get("password","")).strip()
            remember_flag=(request.form.get("recordarme") or request.form.get("remember") or "").strip().lower()
            remember_on=remember_flag in ("on","1","true","yes","si","sí")
            auth=_auth_user(usuario,clave)
            if auth:
                session["autenticado"]=True; session["usuario"]=auth["username"]; session["bots_permitidos"]=auth["bots"]; session.permanent=bool(remember_on)
                destino_resp = redirect(url_for("panel")) if "*" in auth["bots"] else redirect(url_for("panel_exclusivo_bot", bot_nombre=_first_allowed_bot() or ""))
                resp=make_response(destino_resp); max_age=60*24*60*60
                if remember_on:
                    resp.set_cookie("remember_login","1",max_age=max_age,samesite="Lax",secure=app.config["SESSION_COOKIE_SECURE"])
                    resp.set_cookie("last_username",usuario,max_age=max_age,samesite="Lax",secure=app.config["SESSION_COOKIE_SECURE"])
                else:
                    resp.delete_cookie("remember_login"); resp.delete_cookie("last_username")
                return resp
            return render_template("login.html", error=True)
        return render_template("login.html")

    if not _is_admin():
        destino=_first_allowed_bot()
        if destino: return redirect(url_for("panel_exclusivo_bot", bot_nombre=destino))

    leads_todos=fb_list_leads_all()
    bots_disponibles={cfg["name"]:cfg.get("business_name",cfg["name"]) for cfg in bots_config.values()}
    bot_sel=request.args.get("bot")
    if bot_sel:
        bot_norm=_normalize_bot_name(bot_sel) or bot_sel
        leads_filtrados={k:v for k,v in leads_todos.items() if v.get("bot")==bot_norm}
    else:
        leads_filtrados=leads_todos
    return render_template("panel.html", leads=leads_todos, bots=bots_disponibles, bot_seleccionado=bot_sel)

@app.route("/logout", methods=["GET","POST"])
def logout():
    session.clear()
    resp=make_response(redirect(url_for("panel")))
    resp.delete_cookie("remember_login"); resp.delete_cookie("last_username")
    return resp

# ========= Guardar/Exportar =========
@app.route("/guardar-lead", methods=["POST"])
def guardar_edicion():
    data=request.json or {}
    numero_key=(data.get("numero") or "").strip()
    if "|" not in numero_key: return jsonify({"error":"Parámetro 'numero' inválido"}),400
    bot_nombre,numero=numero_key.split("|",1)
    bot_normalizado=_normalize_bot_name(bot_nombre) or bot_nombre
    try:
        ref=_lead_ref(bot_normalizado,numero)
        current=ref.get() or {}
        estado=(data.get("estado") or "").strip(); nota=(data.get("nota") or "").strip()
        if estado: current["status"]=estado
        if nota!="": current["notes"]=nota
        current.setdefault("bot",bot_normalizado); current.setdefault("numero",numero)
        ref.set(current)
    except Exception as e:
        print(f"⚠️ No se pudo actualizar en Firebase: {e}")
    return jsonify({"mensaje":"Lead actualizado"})

@app.route("/exportar")
def exportar():
    if not session.get("autenticado"): return redirect(url_for("panel"))
    leads=fb_list_leads_all()
    output=StringIO(); writer=csv.writer(output)
    writer.writerow(["Bot","Número","Primer contacto","Último mensaje","Última vez","Mensajes","Estado","Notas"])
    for _,d in leads.items():
        writer.writerow([d.get("bot",""),d.get("numero",""),d.get("first_seen",""),d.get("last_message",""),d.get("last_seen",""),d.get("messages",""),d.get("status",""),d.get("notes","")])
    output.seek(0)
    return send_file(output, mimetype="text/csv", download_name="leads.csv", as_attachment=True)

# ========= Borrar / Vaciar =========
@app.route("/borrar-conversacion", methods=["POST"])
def borrar_conversacion_post():
    if not session.get("autenticado"): return jsonify({"error":"No autenticado"}),401
    data=request.json or {}; numero_key=(data.get("numero") or "").strip()
    if "|" not in numero_key: return jsonify({"error":"Parámetro 'numero' inválido (esperado 'Bot|whatsapp:+1...')"}),400
    bot_nombre,numero=numero_key.split("|",1)
    bot_normalizado=_normalize_bot_name(bot_nombre) or bot_nombre
    ok=fb_delete_lead(bot_normalizado,numero); return jsonify({"ok":ok,"bot":bot_normalizado,"numero":numero})

@app.route("/borrar-conversacion/<bot>/<numero>", methods=["GET"])
def borrar_conversacion_get(bot,numero):
    if not session.get("autenticado"): return redirect(url_for("panel"))
    bot_normalizado=_normalize_bot_name(bot) or bot
    ok=fb_delete_lead(bot_normalizado,numero); return redirect(url_for("panel", bot=bot_normalizado))

@app.route("/vaciar-historial", methods=["POST"])
def vaciar_historial_post():
    if not session.get("autenticado"): return jsonify({"error":"No autenticado"}),401
    data=request.json or {}; numero_key=(data.get("numero") or "").strip()
    if "|" not in numero_key: return jsonify({"error":"Parámetro 'numero' inválido (esperado 'Bot|whatsapp:+1...')"}),400
    bot_nombre,numero=numero_key.split("|",1)
    bot_normalizado=_normalize_bot_name(bot_nombre) or bot_nombre
    ok=fb_clear_historial(bot_normalizado,numero); return jsonify({"ok":ok,"bot":bot_normalizado,"numero":numero})

@app.route("/vaciar-historial/<bot>/<numero>", methods=["GET"])
def vaciar_historial_get(bot,numero):
    if not session.get("autenticado"): return redirect(url_for("panel"))
    bot_normalizado=_normalize_bot_name(bot) or bot
    ok=fb_clear_historial(bot_normalizado,numero)
    return redirect(url_for("conversacion_general", bot=bot_normalizado, numero=numero))

@app.route("/api/delete_chat", methods=["POST"])
def api_delete_chat():
    if not session.get("autenticado"): return jsonify({"error":"No autenticado"}),401
    data=request.json or {}; bot=(data.get("bot") or "").strip(); numero=(data.get("numero") or "").strip()
    if not bot or not numero: return jsonify({"error":"Parámetros inválidos (requiere bot y numero)"}),400
    bot_normalizado=_normalize_bot_name(bot) or bot
    ok=fb_delete_lead(bot_normalizado,numero); return jsonify({"ok":ok,"bot":bot_normalizado,"numero":numero})

# ========= API manual / ON-OFF =========
@app.route("/api/send_manual", methods=["POST","OPTIONS"])
def api_send_manual():
    if request.method=="OPTIONS": return ("",204)
    if not session.get("autenticado") and not _bearer_ok(request): return jsonify({"error":"No autenticado"}),401
    data=request.json or {}
    bot_nombre=(data.get("bot") or "").strip(); numero=(data.get("numero") or "").strip(); texto=(data.get("texto") or "").strip()
    if not bot_nombre or not numero or not texto: return jsonify({"error":"Parámetros inválidos (bot, numero, texto)"}),400
    bot_normalizado=_normalize_bot_name(bot_nombre) or bot_nombre
    if session.get("autenticado") and not _user_can_access_bot(bot_normalizado): return jsonify({"error":"No autorizado para este bot"}),403
    from_number=_get_bot_number_by_name(bot_normalizado)
    if not from_number: return jsonify({"error":f"No se encontró el número del bot para '{bot_normalizado}'"}),400
    if not twilio_client: return jsonify({"error":"Twilio REST no configurado"}),500
    try:
        twilio_client.messages.create(from_=from_number, to=numero, body=texto)
        ahora=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fb_append_historial(bot_normalizado, numero, {"tipo":"admin","texto":texto,"hora":ahora})
        return jsonify({"ok":True})
    except Exception as e:
        print(f"❌ Error enviando por Twilio: {e}")
        return jsonify({"error":"Fallo enviando el mensaje"}),500

@app.route("/api/conversation_bot", methods=["POST","OPTIONS"])
def api_conversation_bot():
    if request.method=="OPTIONS": return ("",204)
    if not session.get("autenticado") and not _bearer_ok(request): return jsonify({"error":"No autenticado"}),401
    data=request.json or {}; bot_nombre=(data.get("bot") or "").strip(); numero=(data.get("numero") or "").strip(); enabled=data.get("enabled",None)
    if enabled is None or not bot_nombre or not numero: return jsonify({"error":"Parámetros inválidos (bot, numero, enabled)"}),400
    bot_normalizado=_normalize_bot_name(bot_nombre) or bot_nombre
    if session.get("autenticado") and not _user_can_access_bot(bot_normalizado): return jsonify({"error":"No autorizado para este bot"}),403
    ok=fb_set_conversation_on(bot_normalizado, numero, bool(enabled))
    return jsonify({"ok":bool(ok), "enabled":bool(enabled)})

# ========= PUSH =========
def _push_common_data(payload:dict)->dict:
    data={}
    for k,v in (payload or {}).items():
        if v is None: continue
        data[str(k)]=str(v)
    return data

@app.route("/push/topic", methods=["POST","OPTIONS"])
@app.route("/api/push/topic", methods=["POST","OPTIONS"])
def push_topic():
    if request.method=="OPTIONS": return ("",204)
    if not _bearer_ok(request): return jsonify({"success":False,"message":"Unauthorized"}),401
    body=request.get_json(silent=True) or {}
    title=(body.get("title") or body.get("titulo") or "").strip()
    body_text=(body.get("body") or body.get("descripcion") or "").strip()
    topic=(body.get("topic") or body.get("segmento") or "todos").strip() or "todos"
    if not title or not body_text: return jsonify({"success":False,"message":"title/body requeridos"}),400
    data=_push_common_data({"link":body.get("link") or "", "screen":body.get("screen") or "", "empresaId":body.get("empresaId") or "", "categoria":body.get("categoria") or ""})
    try:
        msg=fcm.Message(topic=topic, notification=fcm.Notification(title=title, body=body_text), data=data)
        msg_id=fcm.send(msg); return jsonify({"success":True,"id":msg_id})
    except Exception as e:
        print(f"❌ Error FCM topic: {e}"); return jsonify({"success":False,"message":"FCM error"}),500

@app.route("/push/token", methods=["POST","OPTIONS"])
@app.route("/api/push/token", methods=["POST","OPTIONS"])
def push_token():
    if request.method=="OPTIONS": return ("",204)
    if not _bearer_ok(request): return jsonify({"success":False,"message":"Unauthorized"}),401
    body=request.get_json(silent=True) or {}
    title=(body.get("title") or body.get("titulo") or "").strip()
    body_text=(body.get("body") or body.get("descripcion") or "").strip()
    token=(body.get("token") or "").strip()
    tokens=body.get("tokens") if isinstance(body.get("tokens"),list) else None
    if not title or not body_text: return jsonify({"success":False,"message":"title/body requeridos"}),400
    data=_push_common_data({"link":body.get("link") or "","screen":body.get("screen") or "","empresaId":body.get("empresaId") or "","categoria":body.get("categoria") or ""})
    try:
        if tokens and len(tokens)>0:
            multi=fcm.MulticastMessage(tokens=[str(t).strip() for t in tokens if str(t).strip()], notification=fcm.Notification(title=title, body=body_text), data=data)
            resp=fcm.send_multicast(multi); return jsonify({"success":True,"sent":resp.success_count,"failed":resp.failure_count})
        elif token:
            msg=fcm.Message(token=token, notification=fcm.Notification(title=title, body=body_text), data=data)
            msg_id=fcm.send(msg); return jsonify({"success":True,"id":msg_id})
        else:
            return jsonify({"success":False,"message":"token(s) requerido(s)"}),400
    except Exception as e:
        print(f"❌ Error FCM token: {e}"); return jsonify({"success":False,"message":"FCM error"}),500

@app.route("/push/health", methods=["GET"])
def push_health(): return jsonify({"ok":True,"service":"push"})

@app.route("/push", methods=["POST","OPTIONS"])
@app.route("/api/push", methods=["POST","OPTIONS"])
@app.route("/push/send", methods=["POST","OPTIONS"])
@app.route("/api/push/send", methods=["POST","OPTIONS"])
def push_universal():
    if request.method=="OPTIONS": return ("",204)
    if not _bearer_ok(request): return jsonify({"success":False,"message":"Unauthorized"}),401
    body=request.get_json(silent=True) or {}
    title=(body.get("title") or body.get("titulo") or "").strip()
    body_text=(body.get("body") or body.get("descripcion") or "").strip()
    topic=(body.get("topic") or body.get("segmento") or "").strip()
    token=(body.get("token") or "").strip()
    tokens=body.get("tokens") if isinstance(body.get("tokens"),list) else None
    if not title or not body_text: return jsonify({"success":False,"message":"title/body requeridos"}),400
    data=_push_common_data({"link":body.get("link") or "","screen":body.get("screen") or "","empresaId":body.get("empresaId") or "","categoria":body.get("categoria") or ""})
    try:
        if topic:
            msg=fcm.Message(topic=topic or "todos", notification=fcm.Notification(title=title, body=body_text), data=data)
            msg_id=fcm.send(msg); return jsonify({"success":True,"mode":"topic","id":msg_id})
        elif tokens and len(tokens)>0:
            multi=fcm.MulticastMessage(tokens=[str(t).strip() for t in tokens if str(t).strip()], notification=fcm.Notification(title=title, body=body_text), data=data)
            resp=fcm.send_multicast(multi); return jsonify({"success":True,"mode":"tokens","sent":resp.success_count,"failed":resp.failure_count})
        elif token:
            msg=fcm.Message(token=token, notification=fcm.Notification(title=title, body=body_text), data=data)
            msg_id=fcm.send(msg); return jsonify({"success":True,"mode":"token","id":msg_id})
        else:
            return jsonify({"success":False,"message":"Falta topic o token(s)"}),400
    except Exception as e:
        print(f"❌ Error FCM universal: {e}"); return jsonify({"success":False,"message":"FCM error"}),500

# ========= Webhook WhatsApp =========
@app.route("/webhook", methods=["GET"])
def verify_whatsapp():
    VERIFY_TOKEN=os.environ.get("VERIFY_TOKEN_WHATSAPP")
    mode=request.args.get("hub.mode"); token=request.args.get("hub.verify_token"); challenge=request.args.get("hub.challenge")
    if mode=="subscribe" and token==VERIFY_TOKEN: return challenge,200
    return "Token inválido",403

def _compose_with_link(prefix, link): return f"{prefix.strip()} {link}".strip() if _valid_url(link) else prefix.strip()

@app.route("/webhook", methods=["POST"])
def whatsapp_bot():
    incoming_msg=(request.values.get("Body","") or "").strip()
    sender_number=request.values.get("From",""); bot_number=request.values.get("To","")
    clave_sesion=f"{bot_number}|{sender_number}"
    bot=_get_bot_cfg_by_number(bot_number)
    if not bot:
        resp=MessagingResponse(); resp.message("Este número no está asignado a ningún bot."); return str(resp)

    _hydrate_session_from_firebase(clave_sesion, bot, sender_number)
    try:
        ahora=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fb_append_historial(bot["name"], sender_number, {"tipo":"user","texto":incoming_msg,"hora":ahora})
    except Exception as e:
        print(f"❌ Error guardando lead: {e}")

    bot_name=bot.get("name","")
    if bot_name and not fb_is_bot_on(bot_name): return str(MessagingResponse())
    if not fb_is_conversation_on(bot_name, sender_number): return str(MessagingResponse())

    response=MessagingResponse(); msg=response.message()

    if _wants_app_download(incoming_msg):
        url_app=_effective_app_url(bot)
        if url_app:
            links_cfg=bot.get("links") or {}
            app_msg=(links_cfg.get("app_message") or "").strip() if isinstance(links_cfg,dict) else ""
            texto=app_msg if (app_msg and ("http://" in app_msg or "https://" in app_msg)) else _compose_with_link(app_msg or "Aquí tienes:", url_app)
            msg.body(texto); _set_agenda(clave_sesion, status="app_link_sent"); agenda_state[clave_sesion]["closed"]=True
        else:
            msg.body("No tengo enlace de app disponible.")
        last_message_time[clave_sesion]=time.time(); return str(response)

    if _is_negative(incoming_msg):
        cierre=_compose_with_link("Entendido.", _effective_booking_url(bot)); msg.body(cierre)
        agenda_state.setdefault(clave_sesion,{})["closed"]=True; last_message_time[clave_sesion]=time.time(); return str(response)

    if _is_polite_closure(incoming_msg):
        cierre=bot.get("policies",{}).get("polite_closure_message","Gracias por contactarnos. ¡Hasta pronto!")
        msg.body(cierre); agenda_state.setdefault(clave_sesion,{})["closed"]=True; last_message_time[clave_sesion]=time.time(); return str(response)

    st=_get_agenda(clave_sesion); agenda_cfg=(bot.get("agenda") or {}) if isinstance(bot,dict) else {}
    confirm_q=re.sub(r"\{\{?\s*GOOGLE_CALENDAR_BOOKING_URL\s*\}?\}", (_effective_booking_url(bot) or ""), (agenda_cfg.get("confirm_question") or ""), flags=re.IGNORECASE)
    decline_msg=re.sub(r"\{\{?\s*GOOGLE_CALENDAR_BOOKING_URL\s*\}?\}", (_effective_booking_url(bot) or ""), (agenda_cfg.get("decline_message") or ""), flags=re.IGNORECASE)
    closing_default=re.sub(r"\{\{?\s*GOOGLE_CALENDAR_BOOKING_URL\s*\}?\}", (_effective_booking_url(bot) or ""), (agenda_cfg.get("closing_message") or ""), flags=re.IGNORECASE)

    if _is_scheduled_confirmation(incoming_msg):
        texto=closing_default or "Agendado."; msg.body(texto)
        _set_agenda(clave_sesion, status="confirmed"); agenda_state[clave_sesion]["closed"]=True
        last_message_time[clave_sesion]=time.time(); return str(response)

    if st.get("awaiting_confirm"):
        if _is_affirmative(incoming_msg):
            if _can_send_link(clave_sesion, cooldown_min=10):
                link=_effective_booking_url(bot)
                link_message=(agenda_cfg.get("link_message") or "").strip()
                link_message=re.sub(r"\{\{?\s*GOOGLE_CALENDAR_BOOKING_URL\s*\}?\}", (link or ""), link_message, flags=re.IGNORECASE)
                texto=link_message if link_message else (_compose_with_link("Enlace:", link) if link else "Sin enlace disponible.")
                msg.body(texto)
                _set_agenda(clave_sesion, awaiting_confirm=False, status="link_sent", last_link_time=int(time.time()), last_bot_hash=_hash_text(texto))
                agenda_state[clave_sesion]["closed"]=True
                try:
                    ahora_bot=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    fb_append_historial(bot["name"], sender_number, {"tipo":"bot","texto":texto,"hora":ahora_bot})
                except Exception as e:
                    print(f"⚠️ No se pudo guardar respuesta AGENDA: {e}")
            else:
                msg.body("Enlace enviado recientemente."); _set_agenda(clave_sesion, awaiting_confirm=False)
            last_message_time[clave_sesion]=time.time(); return str(response)
        elif _is_negative(incoming_msg):
            if decline_msg: msg.body(decline_msg)
            _set_agenda(clave_sesion, awaiting_confirm=False); agenda_state[clave_sesion]["closed"]=True
            last_message_time[clave_sesion]=time.time(); return str(response)
        else:
            if confirm_q: msg.body(confirm_q)
            last_message_time[clave_sesion]=time.time(); return str(response)

    if any(k in (incoming_msg or "").lower() for k in (bot.get("agenda",{}).get("keywords",[]) or [])):
        if confirm_q: msg.body(confirm_q)
        _set_agenda(clave_sesion, awaiting_confirm=True); last_message_time[clave_sesion]=time.time(); return str(response)

    if clave_sesion not in session_history:
        sysmsg=_make_system_message(bot)
        session_history[clave_sesion]=[{"role":"system","content":sysmsg}] if sysmsg else []
        follow_up_flags[clave_sesion]={"5min":False,"60min":False}; greeted_state[clave_sesion]=False

    greeting_text=(bot.get("greeting") or "").strip(); intro_keywords=(bot.get("intro_keywords") or [])
    if (not greeted_state.get(clave_sesion)) and greeting_text and any(w in incoming_msg.lower() for w in intro_keywords):
        msg.body(greeting_text); greeted_state[clave_sesion]=True; last_message_time[clave_sesion]=time.time(); return str(response)

    session_history.setdefault(clave_sesion,[]).append({"role":"user","content":incoming_msg})
    last_message_time[clave_sesion]=time.time()

    try:
        model_name=(bot.get("model") or "gpt-4o").strip()
        temperature=float(bot.get("temperature",0.6)) if isinstance(bot.get("temperature",None),(int,float)) else 0.6
        completion=client.chat.completions.create(model=model_name, temperature=temperature, messages=session_history[clave_sesion])
        respuesta=(completion.choices[0].message.content or "").strip()
        respuesta=_apply_style(bot, respuesta)
        must_ask=bool((bot.get("style") or {}).get("always_question", False))
        respuesta=_ensure_question(bot, respuesta, force_question=must_ask)
        if _hash_text(respuesta)==agenda_state.get(clave_sesion,{}).get("last_bot_hash"):
            probe=_next_probe_from_bot(bot)
            if probe and probe not in respuesta:
                if not respuesta.endswith((".", "!", "…", "¿", "?")): respuesta+="."
                respuesta=f"{respuesta} {probe}".strip()
        session_history[clave_sesion].append({"role":"assistant","content":respuesta})
        msg.body(respuesta)
        agenda_state.setdefault(clave_sesion,{}); agenda_state[clave_sesion]["last_bot_hash"]=_hash_text(respuesta)
        try:
            usage=getattr(completion,"usage",None)
            if usage:
                record_openai_usage(bot.get("name",""), model_name, int(getattr(usage,"prompt_tokens",0) or 0), int(getattr(usage,"completion_tokens",0) or 0))
        except Exception as e:
            print(f"⚠️ No se pudo registrar tokens en billing: {e}")
        try:
            ahora_bot=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fb_append_historial(bot["name"], sender_number, {"tipo":"bot","texto":respuesta,"hora":ahora_bot})
        except Exception as e:
            print(f"⚠️ No se pudo guardar respuesta del bot: {e}")
    except Exception as e:
        print(f"❌ Error con GPT: {e}"); msg.body("Error generando la respuesta.")
    return str(response)

# ========= VOZ Realtime + Twilio Media Streams =========
sock = Sock(app)

def _wss_base():
    base=(request.url_root or "").strip().rstrip("/")
    if base.startswith("http://"):  base="wss://"+base[len("http://"):]
    elif base.startswith("https://"): base="wss://"+base[len("https://"):]
    else: base="wss://"+base
    return base

@app.route("/voice", methods=["POST","GET"])
def voice_entry():
    to_number=(request.values.get("To") or "").strip()
    bot_cfg=_get_bot_cfg_by_any_number(to_number) or {}
    bot_name=bot_cfg.get("name","") or "default"
    vr=VoiceResponse()
    connect=Connect(); connect.stream(url=f"{_wss_base()}/twilio-media-stream?bot={bot_name}")
    vr.append(connect)
    return str(vr),200,{"Content-Type":"text/xml"}

def _openai_realtime_connect(model:str, voice:str, system_prompt:str):
    # Abrimos sesión WebSocket con Realtime
    headers=[f"Authorization: Bearer {OPENAI_API_KEY}", "OpenAI-Beta: realtime=v1"]
    url=f"wss://api.openai.com/v1/realtime?model={model}"
    ws_ai=websocket.WebSocket()
    # CERT_REQUIRED es recomendable; si te falla en dev, podrías rebajarlo a CERT_NONE
    ws_ai.connect(url, header=headers, sslopt={"cert_reqs": ssl.CERT_REQUIRED})
    # Config de sesión (voz + formatos mulaw 8k)
    session_update={
        "type":"session.update",
        "session":{
            "voice": voice,
            "instructions": system_prompt or "Eres un asistente de voz amable y natural.",
            "input_audio_format":{"mime_type":"audio/mulaw;rate=8000","channels":1},
            "output_audio_format":{"mime_type":"audio/mulaw;rate=8000","channels":1},
            "turn_detection":{"type":"server_vad"}
        }
    }
    ws_ai.send(json.dumps(session_update))
    return ws_ai

@sock.route("/twilio-media-stream")
def twilio_media_stream(ws_twi):
    # Preparar bot/modelo/voz
    args=request.args or {}
    bot_name=(args.get("bot") or "default").strip()
    bot_cfg=_get_bot_cfg_by_name(bot_name) or {}
    sysmsg=_make_system_message(bot_cfg)

    # preferimos voice.openai_voice; si no, voice.voice_name; si no, env var
    voice = ((bot_cfg.get("voice") or {}).get("openai_voice") or (bot_cfg.get("voice") or {}).get("voice_name") or OPENAI_REALTIME_VOICE).strip()
    model = (bot_cfg.get("realtime_model") or OPENAI_REALTIME_MODEL).strip()
    print(f"[VOICE] Conectando Realtime model={model}, voice={voice}")

    try:
        ws_ai=_openai_realtime_connect(model, voice, sysmsg)
    except Exception as e:
        print("❌ No se pudo conectar a OpenAI Realtime:", e)
        try: ws_twi.send(json.dumps({"event":"stop"}))
        except Exception: pass
        return

    stream_sid=None
    last_media_ts=time.time()
    buffer_has_audio=False
    run_vad=True

    # Lector de AI -> envía audio a Twilio
    def _ai_reader():
        nonlocal stream_sid
        while True:
            try:
                msg=ws_ai.recv()
                if not msg: continue
                data=json.loads(msg)
                if data.get("type")=="response.audio.delta":
                    payload=data.get("delta") or ""
                    if payload and stream_sid:
                        ws_twi.send(json.dumps({"event":"media","streamSid":stream_sid,"media":{"payload":payload}}))
                # logs útiles:
                if data.get("type")=="response.completed":
                    pass
            except Exception as e:
                print("ℹ️ AI reader finalizado:", e)
                break

    Thread(target=_ai_reader, daemon=True).start()

    # VAD por silencio (commit si 700ms sin audio entrante)
    def _vad_loop():
        nonlocal buffer_has_audio, last_media_ts, run_vad
        while run_vad:
            try:
                if buffer_has_audio and (time.time()-last_media_ts)>=0.7:
                    ws_ai.send(json.dumps({"type":"input_audio_buffer.commit"}))
                    ws_ai.send(json.dumps({"type":"response.create","response":{"conversation":True}}))
                    buffer_has_audio=False
            except Exception:
                pass
            time.sleep(0.1)

    Thread(target=_vad_loop, daemon=True).start()

    # Loop Twilio -> AI
    try:
        while True:
            raw=ws_twi.receive()
            if raw is None: break
            try:
                evt=json.loads(raw)
            except Exception:
                continue
            etype=evt.get("event")

            if etype=="start":
                stream_sid=((evt.get("start") or {}).get("streamSid")) or stream_sid
                print(f"[VOICE] Twilio stream started: {stream_sid}")

            elif etype=="media":
                chunk=((evt.get("media") or {}).get("payload") or "")
                if chunk:
                    ws_ai.send(json.dumps({"type":"input_audio_buffer.append","audio":chunk,"mime_type":"audio/mulaw;rate=8000"}))
                    last_media_ts=time.time(); buffer_has_audio=True

            elif etype=="mark":
                # Si Twilio mandara 'mark', forzamos commit inmediato
                ws_ai.send(json.dumps({"type":"input_audio_buffer.commit"}))
                ws_ai.send(json.dumps({"type":"response.create","response":{"conversation":True}}))
                buffer_has_audio=False

            elif etype=="stop":
                break
    except Exception as e:
        print("⚠️ WS Twilio error:", e)
    finally:
        run_vad=False
        try: ws_ai.close()
        except Exception: pass

# ========= Vistas conversación (Firebase) =========
@app.route("/conversacion_general/<bot>/<numero>")
def chat_general(bot,numero):
    if not session.get("autenticado"): return redirect(url_for("panel"))
    bot_normalizado=_normalize_bot_name(bot)
    if not bot_normalizado: return "Bot no encontrado",404
    if not _user_can_access_bot(bot_normalizado): return "No autorizado para este bot",403
    bot_cfg=_get_bot_cfg_by_name(bot_normalizado) or {}
    company_name=bot_cfg.get("business_name", bot_normalizado)
    data=fb_get_lead(bot_normalizado,numero); historial=data.get("historial",[])
    if isinstance(historial,dict): historial=[historial[k] for k in sorted(historial.keys())]
    mensajes=[{"texto":r.get("texto",""),"hora":r.get("hora",""),"tipo":r.get("tipo","user")} for r in historial]
    return render_template("chat.html", numero=numero, mensajes=mensajes, bot=bot_normalizado, bot_data=bot_cfg, company_name=company_name)

@app.route("/conversacion_bot/<bot>/<numero>")
def chat_bot(bot,numero):
    if not session.get("autenticado"): return redirect(url_for("panel"))
    bot_normalizado=_normalize_bot_name(bot)
    if not bot_normalizado: return "Bot no encontrado",404
    if not _user_can_access_bot(bot_normalizado): return "No autorizado para este bot",403
    bot_cfg=_get_bot_cfg_by_name(bot_normalizado) or {}
    company_name=bot_cfg.get("business_name", bot_normalizado)
    data=fb_get_lead(bot_normalizado,numero); historial=data.get("historial",[])
    if isinstance(historial,dict): historial=[historial[k] for k in sorted(historial.keys())]
    mensajes=[{"texto":r.get("texto",""),"hora":r.get("hora",""),"tipo":r.get("tipo","user")} for r in historial]
    return render_template("chat_bot.html", numero=numero, mensajes=mensajes, bot=bot_normalizado, bot_data=bot_cfg, company_name=company_name)

# ========= API polling =========
@app.route("/api/chat/<bot>/<numero>", methods=["GET","OPTIONS"])
def api_chat(bot,numero):
    if request.method=="OPTIONS": return ("",204)
    if not session.get("autenticado") and not _bearer_ok(request): return jsonify({"error":"No autenticado"}),401
    bot_normalizado=_normalize_bot_name(bot)
    if not bot_normalizado: return jsonify({"error":"Bot no encontrado"}),404
    if session.get("autenticado") and not _user_can_access_bot(bot_normalizado): return jsonify({"error":"No autorizado"}),403
    since_param=request.args.get("since","").strip()
    try: since_ms=int(since_param) if since_param else 0
    except ValueError: since_ms=0
    data=fb_get_lead(bot_normalizado,numero); historial=data.get("historial",[])
    if isinstance(historial,dict): historial=[historial[k] for k in sorted(historial.keys())]
    nuevos=[]; last_ts=since_ms
    for reg in historial:
        ts=_hora_to_epoch_ms(reg.get("hora",""))
        if ts>since_ms: nuevos.append({"texto":reg.get("texto",""),"hora":reg.get("hora",""),"tipo":reg.get("tipo","user"),"ts":ts})
        if ts>last_ts: last_ts=ts
    if since_ms==0 and not nuevos and historial:
        for reg in historial:
            ts=_hora_to_epoch_ms(reg.get("hora",""));  last_ts=max(last_ts, ts)
        nuevos=[{"texto":reg.get("texto",""),"hora":reg.get("hora",""),"tipo":reg.get("tipo","user"),"ts":_hora_to_epoch_ms(reg.get("hora",""))} for reg in historial]
    bot_enabled=fb_is_conversation_on(bot_normalizado, numero)
    return jsonify({"mensajes":nuevos,"last_ts":last_ts,"bot_enabled":bool(bot_enabled)})

# ========= Run =========
if __name__ == "__main__":
    port=int(os.environ.get("PORT",5000))
    print(f"[BOOT] BOOKING_URL_FALLBACK={BOOKING_URL_FALLBACK}")
    print(f"[BOOT] APP_DOWNLOAD_URL_FALLBACK={APP_DOWNLOAD_URL_FALLBACK}")
    app.run(host="0.0.0.0", port=port)
