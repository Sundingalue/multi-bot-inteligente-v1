# bots/api_mobile.py
from flask import Blueprint, request, jsonify, current_app
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from firebase_admin import db
import glob, os, json
from functools import wraps

mobile_bp = Blueprint("mobile", __name__)

# ====== Token firmado (sin dependencias extras) ======
def _serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="mobile-api")

def _issue_token(payload: dict):
    s = _serializer()
    return s.dumps(payload)  # caduca al verificar (max_age)

def _verify_token(token):
    s = _serializer()
    try:
        # Validez: 60 días
        return s.loads(token, max_age=60*24*60*60)
    except (BadSignature, SignatureExpired):
        return None

def _auth_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = (request.headers.get("Authorization") or "").strip()
        if auth.startswith("Bearer "):
            data = _verify_token(auth[7:])
            if data:
                request.mobile_user = data  # {'username':..., 'bots':[...]}
                return f(*args, **kwargs)
        return jsonify({"error": "Unauthorized"}), 401
    return wrapper

# ====== Usuarios (mismo esquema que tus bots/*.json) ======
def _normalize_bot_name(name: str):
    return (name or "").strip()

def _load_users():
    users = {}
    # Lee credenciales desde bots/*.json  (login / logins / auth)
    for path in glob.glob(os.path.join("bots", "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict): 
                continue
            for _, cfg in data.items():
                if not isinstance(cfg, dict): 
                    continue
                bot_name = (cfg.get("name") or "").strip()
                entries = []
                if isinstance(cfg.get("login"), dict): entries.append(cfg["login"])
                if isinstance(cfg.get("logins"), list): entries += [x for x in cfg["logins"] if isinstance(x, dict)]
                if isinstance(cfg.get("auth"), dict): entries.append(cfg["auth"])
                for e in entries:
                    u = (e.get("username") or "").strip()
                    p = (e.get("password") or "").strip()
                    scope = e.get("scope", bot_name)
                    if not u or not p:
                        continue
                    if scope == "*":
                        allowed = ["*"]
                    elif isinstance(scope, list):
                        allowed = [ _normalize_bot_name(s) for s in scope if str(s).strip() ] or [bot_name]
                    else:
                        allowed = [_normalize_bot_name(scope)]
                    if u in users:
                        prev = users[u]["bots"]
                        users[u]["bots"] = ["*"] if ("*" in prev or "*" in allowed) else list(dict.fromkeys(prev + allowed))
                    else:
                        users[u] = {"password": p, "bots": allowed}
        except Exception:
            continue

    # Soporte ENV legacy (opcional)
    for key, val in os.environ.items():
        if not key.startswith("USER_"): 
            continue
        alias = key[len("USER_"):]
        username = (val or "").strip()
        password = (os.environ.get(f"PASS_{alias}", "") or "").strip()
        panel = (os.environ.get(f"PANEL_{alias}", "") or "").strip()
        if not username or not password or not panel:
            continue
        if panel.lower() == "panel":
            bots_list = ["*"]
        elif panel.lower().startswith("panel-bot/"):
            bots_list = [panel.split("/",1)[1].strip()]
        else:
            bots_list = []
        if bots_list:
            users[username] = {"password": password, "bots": bots_list}

    if not users:
        users["sundin"] = {"password": "inhouston2025", "bots": ["*"]}
    return users

def _user_can_access(user_bots, bot_name):
    return ("*" in (user_bots or [])) or ((bot_name or "") in (user_bots or []))

# ====== Firebase helpers (solo lo que usa el móvil) ======
def _leads_all():
    root = db.reference("leads").get() or {}
    out = []
    if not isinstance(root, dict): return out
    for bot, nums in root.items():
        if not isinstance(nums, dict): continue
        for numero, data in nums.items():
            out.append({
                "bot": bot,
                "numero": numero,
                "first_seen": data.get("first_seen",""),
                "last_message": data.get("last_message",""),
                "last_seen": data.get("last_seen",""),
                "messages": int(data.get("messages",0)),
                "status": data.get("status","nuevo"),
                "notes": data.get("notes",""),
            })
    return out

def _leads_by_bot(bot):
    nums = db.reference(f"leads/{bot}").get() or {}
    out = []
    if not isinstance(nums, dict): return out
    for numero, data in nums.items():
        out.append({
            "bot": bot,
            "numero": numero,
            "first_seen": data.get("first_seen",""),
            "last_message": data.get("last_message",""),
            "last_seen": data.get("last_seen",""),
            "messages": int(data.get("messages",0)),
            "status": data.get("status","nuevo"),
            "notes": data.get("notes",""),
        })
    return out

# ====== Endpoints ======
@mobile_bp.route("/login", methods=["POST"])
def mobile_login():
    body = request.get_json(silent=True) or {}
    u = (body.get("username") or "").strip()
    p = (body.get("password") or "").strip()
    users = _load_users()
    rec = users.get(u)
    if rec and rec.get("password") == p:
        bots = rec.get("bots", [])
        token = _issue_token({"username": u, "bots": bots})
        return jsonify({"ok": True, "token": token, "bots": bots, "user": u})
    return jsonify({"ok": False, "error": "Credenciales inválidas"}), 401

@mobile_bp.route("/leads", methods=["GET"])
@_auth_required
def mobile_leads():
    user_bots = (getattr(request, "mobile_user", {}) or {}).get("bots", [])
    q_bot = (request.args.get("bot") or "").strip()
    if q_bot:
        if not _user_can_access(user_bots, q_bot):
            return jsonify({"leads": []})
        out = _leads_by_bot(q_bot)
    else:
        out = _leads_all() if "*" in user_bots else sum([_leads_by_bot(b) for b in user_bots], [])
    out.sort(key=lambda x: x.get("last_seen",""), reverse=True)
    return jsonify({"leads": out})

@mobile_bp.route("/lead", methods=["POST"])
@_auth_required
def mobile_update_lead():
    body = request.get_json(silent=True) or {}
    bot = (body.get("bot") or "").strip()
    numero = (body.get("numero") or "").strip()
    if not _user_can_access(request.mobile_user.get("bots", []), bot):
        return jsonify({"ok": False, "error":"forbidden"}), 403
    estado = (body.get("estado") or "").strip()
    nota = body.get("nota")
    ref = db.reference(f"leads/{bot}/{numero}")
    cur = ref.get() or {}
    if estado: cur["status"] = estado
    if nota is not None: cur["notes"] = nota
    cur.setdefault("bot", bot)
    cur.setdefault("numero", numero)
    ref.set(cur)
    return jsonify({"ok": True})

@mobile_bp.route("/delete", methods=["POST"])
@_auth_required
def mobile_delete():
    body = request.get_json(silent=True) or {}
    bot = (body.get("bot") or "").strip()
    numero = (body.get("numero") or "").strip()
    if not _user_can_access(request.mobile_user.get("bots", []), bot):
        return jsonify({"ok": False, "error":"forbidden"}), 403
    try:
        db.reference(f"leads/{bot}/{numero}").delete()
        return jsonify({"ok": True})
    except Exception:
        return jsonify({"ok": False}), 500
