import base64
import email as email_lib
import hashlib
import imaplib
import json
import logging
import os
import secrets
import smtplib
import time
import urllib.parse
import urllib.request
from datetime import datetime
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template_string, request, session, url_for
from openai import OpenAI

from nexus_common import (
    DEFAULT_PROFILE,
    ask_ai,
    build_system_prompt,
    ensure_env_keys,
    get_env,
    get_web_session_secret,
    require_openai_key,
    require_web_password,
)

# Data directory: use NEXUS_DATA_DIR env var if set,
# otherwise /app (Render persistent disk) if it exists,
# otherwise fall back to the directory of this file (local dev).
_CODE_DIR = Path(__file__).resolve().parent
_render_disk = Path("/app")
_env_data    = os.environ.get("NEXUS_DATA_DIR", "").strip()
if _env_data:
    BASE_DIR = Path(_env_data)
elif _render_disk.is_dir() and str(_CODE_DIR) != str(_render_disk):
    BASE_DIR = _render_disk
else:
    BASE_DIR = _CODE_DIR
BASE_DIR.mkdir(parents=True, exist_ok=True)

MEMORY_FILE   = BASE_DIR / "nexus_memory.json"
PROFILE_FILE  = BASE_DIR / "nexus_profile.json"
STATE_FILE    = BASE_DIR / "nexus_state.json"
CRM_FILE      = BASE_DIR / "crm_data.json"
ANALYTICS_FILE= BASE_DIR / "analytics_data.json"
REMINDERS_FILE= BASE_DIR / "reminders.json"
CALENDAR_FILE = BASE_DIR / "calendar_data.json"
AUDIT_FILE    = BASE_DIR / "audit_log.json"
UPLOAD_DIR    = BASE_DIR / "chat_uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = get_web_session_secret()
logging.basicConfig(level=logging.INFO, format="%(message)s")

# ── Login lockout (in-memory, resets on restart) ──────────────────────────────
_FAILED: dict = {}   # ip -> {"count": int, "first_ts": float, "locked_until": float}
_MAX_FAILS   = 5
_WINDOW_SEC  = 600   # 10 minutes
_LOCKOUT_SEC = 900   # 15 minutes

def _lockout_check(ip: str):
    """Return seconds remaining in lockout, or 0 if not locked."""
    now = time.time()
    entry = _FAILED.get(ip)
    if not entry:
        return 0
    if now < entry.get("locked_until", 0):
        return int(entry["locked_until"] - now)
    # window expired → reset
    if now - entry.get("first_ts", 0) > _WINDOW_SEC:
        _FAILED.pop(ip, None)
    return 0

def _lockout_fail(ip: str):
    """Record a failed attempt; lock if threshold reached."""
    now = time.time()
    entry = _FAILED.setdefault(ip, {"count": 0, "first_ts": now, "locked_until": 0})
    if now - entry["first_ts"] > _WINDOW_SEC:
        entry.update({"count": 1, "first_ts": now, "locked_until": 0})
    else:
        entry["count"] += 1
    if entry["count"] >= _MAX_FAILS:
        entry["locked_until"] = now + _LOCKOUT_SEC

def _lockout_success(ip: str):
    _FAILED.pop(ip, None)

# ── JSON helpers ──────────────────────────────────────────────────────────────

def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# ── App state ─────────────────────────────────────────────────────────────────

profile = load_json(PROFILE_FILE, DEFAULT_PROFILE.copy())
history = load_json(MEMORY_FILE, [])
DEFAULT_STATE = {
    "users": [
        {"username": "admin", "role": "admin", "password": ""},
        {"username": "partner", "role": "employee", "password": ""},
        {"username": "guest", "role": "guest", "password": ""},
    ],
    "tasks": [],
    "agents": [
        {"id": "restaurant", "name": "AI-агент общепита",   "description": "Меню, клиенты, отзывы, бронирования, ежедневная выручка.", "status": "template"},
        {"id": "aqua",       "name": "AI-агент аква бизнеса","description": "Клиенты, сервис, заявки, поставки, повторные продажи.",   "status": "template"},
        {"id": "sales",      "name": "AI-агент продаж",      "description": "Лиды, follow-up, CRM, скрипты и отчёты.",                 "status": "template"},
    ],
    "integrations": {
        "openai": "env:OPENAI_API_KEY",
        "telegram": "env:TELEGRAM_TOKEN",
        "weather": "env:OPENWEATHER_API_KEY",
        "nova_poshta": "env:NOVA_POSHTA_API_KEY",
        "email": "env:GMAIL+APP_PASSWORD",
        "calendar": "env:GOOGLE_SERVICE_ACCOUNT",
        "monobank": "planned",
        "google_maps": "planned",
        "whatsapp": "planned",
        "instagram": "planned",
        "notion": "planned",
        "database": "env:DATABASE_URL",
    },
}
state = load_json(STATE_FILE, DEFAULT_STATE.copy())

def merge_state_defaults(target, defaults):
    changed = False
    for key, value in defaults.items():
        if key not in target:
            target[key] = value; changed = True
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            changed = merge_state_defaults(target[key], value) or changed
    return changed

if merge_state_defaults(state, DEFAULT_STATE):
    save_json(STATE_FILE, state)

stats = {
    "messages": len([m for m in history if m.get("role") == "user"]),
    "voice": 0, "files": 0, "edits": 0, "briefings": 0, "commands": 0,
}
SYSTEM = build_system_prompt(profile)
RATE_LIMIT = {}
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX_POSTS = 40

# ── Security ──────────────────────────────────────────────────────────────────

def get_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]

def client_key():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "local").split(",")[0].strip()

@app.before_request
def protect_requests():
    if request.method != "POST": return None
    now = time.time(); key = client_key()
    bucket = [s for s in RATE_LIMIT.get(key, []) if now - s < RATE_LIMIT_WINDOW]
    if len(bucket) >= RATE_LIMIT_MAX_POSTS:
        return jsonify({"error": "Rate limit exceeded"}), 429
    bucket.append(now); RATE_LIMIT[key] = bucket
    if request.endpoint == "login": return None
    if logged_in() and request.headers.get("X-CSRF-Token") != session.get("csrf_token"):
        return jsonify({"error": "Bad CSRF token"}), 403
    return None

@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), geolocation=(), payment=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' data:; "
        "connect-src 'self'; base-uri 'self'; form-action 'self'"
    )
    log_event("request", method=request.method, path=request.path, status=response.status_code, ip=client_key())
    return response

@app.errorhandler(404)
def not_found(_error):
    if request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json":
        return jsonify({"error": "Not found"}), 404
    return redirect(url_for("index"))

@app.errorhandler(500)
def server_error(error):
    log_event("server_error", path=request.path, error=str(error))
    return jsonify({"error": "Internal server error"}), 500

def log_event(event, **fields):
    payload = {"event": event, "ts": datetime.utcnow().isoformat() + "Z", **fields}
    app.logger.info(json.dumps(payload, ensure_ascii=False))

def audit(action, detail="", user=None):
    """Append one entry to the persistent audit log."""
    entry = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "action": action,
        "user": user or session.get("username", "system"),
        "detail": str(detail)[:200],
        "ip": client_key(),
    }
    try:
        log = load_json(AUDIT_FILE, [])
        log.append(entry)
        save_json(AUDIT_FILE, log[-2000:])  # keep last 2000 entries
    except Exception:
        pass

# ── Auth helpers ──────────────────────────────────────────────────────────────

def logged_in(): return session.get("logged_in") is True
def message_id(): return datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
def public_history():
    return [{"id": m.get("id",""), "role": m.get("role",""), "content": m.get("content","")} for m in history[-100:]]
def persist_history(): save_json(MEMORY_FILE, history[-160:])
def persist_state():   save_json(STATE_FILE, state)
def current_user():    return {"username": session.get("username","anonymous"), "role": session.get("role","guest")}
def has_role(*roles):  return session.get("role") in roles
def require_roles(*roles):
    if not logged_in(): return jsonify({"error": "Нужен вход."}), 401
    if roles and not has_role(*roles): return jsonify({"error": "Недостаточно прав."}), 403
    return None

# ── Templates ─────────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NEXUS - вход</title>
<style>
:root{--bg:#061014;--panel:#101d25;--line:#24414b;--text:#eef8f9;--muted:#8ca3aa;--cyan:#35d7e9;--green:#48e08c}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(circle at 20% 0,#123743,#061014 38%,#03080b);font-family:Inter,'Segoe UI',Arial,sans-serif;color:var(--text);padding:24px}
.box{width:min(420px,100%);border:1px solid var(--line);background:linear-gradient(180deg,rgba(16,29,37,.96),rgba(8,15,20,.96));border-radius:18px;padding:28px;box-shadow:0 34px 90px rgba(0,0,0,.45)}
.brand{display:flex;gap:12px;align-items:center;margin-bottom:24px}.mark{width:42px;height:42px;border-radius:12px;background:linear-gradient(135deg,var(--cyan),var(--green));display:grid;place-items:center;color:#041014;font-weight:900}.name{font-size:22px;font-weight:900;letter-spacing:5px}.sub{font-size:13px;color:var(--muted);margin-top:3px}
label{display:block;color:var(--muted);font-size:12px;margin-bottom:8px}input{width:100%;height:46px;border:1px solid var(--line);border-radius:12px;background:#071218;color:var(--text);padding:0 14px;outline:none;font-size:16px}input:focus{border-color:var(--cyan);box-shadow:0 0 0 3px rgba(53,215,233,.13)}
button{width:100%;height:48px;margin-top:14px;border:0;border-radius:12px;background:linear-gradient(135deg,var(--cyan),var(--green));color:#031014;font-weight:900;cursor:pointer;font-size:15px}.error{min-height:20px;margin-top:12px;color:#ff7c7c;font-size:13px}
.hint{font-size:12px;color:var(--muted);margin-top:8px;text-align:center}
</style></head><body>
{% if totp_required %}
<form class="box" method="post">
  <input type="hidden" name="username" value="{{ username }}">
  <input type="hidden" name="password" value="{{ password_hash }}">
  <input type="hidden" name="totp_step" value="1">
  <div class="brand"><div class="mark">🔐</div><div><div class="name">NEXUS</div><div class="sub">Двофакторна аутентифікація</div></div></div>
  <label for="tc">Код із Google Authenticator</label>
  <input id="tc" name="totp_code" type="text" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" placeholder="000000" autofocus autocomplete="one-time-code">
  <button type="submit">Підтвердити →</button>
  <div class="error">{{ error }}</div>
  <div class="hint">Введіть 6-значний код із програми аутентифікатора</div>
</form>
{% else %}
<form class="box" method="post">
  <div class="brand"><div class="mark">N</div><div><div class="name">NEXUS</div><div class="sub">Центр управления</div></div></div>
  <label for="un">Ім'я користувача</label>
  <input id="un" name="username" type="text" value="admin" autocomplete="username">
  <label for="p" style="margin-top:12px">Пароль доступа</label>
  <input id="p" name="password" type="password" autocomplete="current-password" autofocus>
  <button type="submit">Войти →</button>
  <div class="error">{{ error }}</div>
</form>
{% endif %}
</body></html>"""


HTML = """<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="csrf-token" content="{{ csrf_token }}">
<meta name="theme-color" content="#35d7e9">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="NEXUS">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/static/icon-192.png">
<title>NEXUS</title>
<style>
:root{--bg:#061014;--side:#08141a;--panel:#101d25;--panel2:#142833;--line:#25414b;--text:#eef8f9;--muted:#8da4ab;--soft:#c5d5d9;--cyan:#35d7e9;--green:#48e08c;--red:#ff7272;--amber:#f5bd63;--purple:#b79eff;--r:14px;--shadow:0 22px 60px rgba(0,0,0,.34)}
*{box-sizing:border-box}html,body{height:100%}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,'Segoe UI',Arial,sans-serif}
button,input,textarea,select{font:inherit}button{cursor:pointer}
.app{height:100vh;display:grid;grid-template-columns:260px 1fr;overflow:hidden;background:radial-gradient(circle at 76% -10%,rgba(53,215,233,.12),transparent 35%),var(--bg)}
.sidebar{background:linear-gradient(180deg,var(--side),#04090c);border-right:1px solid var(--line);display:flex;flex-direction:column;overflow-y:auto}
.brand{height:80px;padding:16px 18px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px;flex-shrink:0}
.mark{width:40px;height:40px;border-radius:12px;background:linear-gradient(135deg,var(--cyan),var(--green));display:grid;place-items:center;color:#041014;font-weight:900;font-size:18px;flex-shrink:0}
.brand-title{font-size:19px;font-weight:900;letter-spacing:5px}.brand-sub{font-size:10px;color:var(--muted);margin-top:2px}
.nav{padding:10px 8px;display:grid;gap:3px}
.nav button{height:40px;border:0;background:transparent;color:var(--muted);border-radius:10px;padding:0 11px;text-align:left;display:flex;align-items:center;gap:9px;font-size:13.5px;transition:background .15s,color .15s}
.nav button:hover{background:rgba(255,255,255,.04);color:var(--text)}.nav button.active{background:rgba(53,215,233,.12);color:var(--cyan);box-shadow:inset 3px 0 0 var(--cyan)}
.emoji{width:22px;text-align:center;font-size:15px}
.nav-section{color:var(--muted);font-size:10px;letter-spacing:1.5px;text-transform:uppercase;padding:10px 14px 4px}
.side-bottom{margin-top:auto;border-top:1px solid var(--line);padding:14px 16px;font-size:13px;flex-shrink:0}
.online{display:flex;align-items:center;gap:8px;color:var(--muted)}.dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 14px var(--green)}
.main{min-width:0;display:flex;flex-direction:column}
.topbar{height:60px;border-bottom:1px solid var(--line);background:rgba(8,18,24,.85);backdrop-filter:blur(14px);display:flex;align-items:center;justify-content:space-between;padding:0 22px;flex-shrink:0}
.title{font-size:17px;font-weight:850}.meta{display:flex;align-items:center;gap:12px;color:var(--muted);font-size:13px}
.logout{color:var(--muted);text-decoration:none;border:1px solid var(--line);padding:7px 10px;border-radius:10px;font-size:12px}
.kbd{background:#071d26;border:1px solid var(--line);border-radius:6px;padding:2px 7px;font-size:11px;color:var(--muted);cursor:pointer}
.content{overflow:auto;min-height:0;padding:20px 20px 88px}
.page{display:none}.page.active{display:block}
.grid4{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}
.grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}.stack{display:grid;gap:16px}
.card{background:linear-gradient(180deg,rgba(16,29,37,.94),rgba(10,18,24,.94));border:1px solid var(--line);border-radius:var(--r);padding:18px;box-shadow:var(--shadow)}
.card h3{margin:0 0 14px;font-size:12px;color:var(--cyan);letter-spacing:1px;text-transform:uppercase}
.stat{min-height:110px;display:flex;flex-direction:column;justify-content:space-between}.num{font-size:34px;font-weight:900}.lab{font-size:11px;color:var(--muted);letter-spacing:1px;text-transform:uppercase}.hint{font-size:12px;color:var(--soft)}
.field{width:100%;border:1px solid var(--line);background:#071218;color:var(--text);border-radius:11px;min-height:42px;padding:0 12px;outline:none;margin-bottom:10px}
.field:focus{border-color:var(--cyan);box-shadow:0 0 0 3px rgba(53,215,233,.11)}
textarea.field{padding:11px 12px;resize:vertical;line-height:1.5}select.field{cursor:pointer}
.btn{border:0;border-radius:11px;min-height:40px;padding:0 15px;background:linear-gradient(135deg,var(--cyan),var(--green));color:#041014;font-weight:900;font-size:13px}
.btn.secondary{background:#071218;color:var(--cyan);border:1px solid var(--line)}
.btn.danger{background:rgba(255,114,114,.12);color:var(--red);border:1px solid rgba(255,114,114,.3)}
.btn.sm{min-height:32px;padding:0 12px;font-size:12px}
.row{display:flex;gap:10px;align-items:flex-start}.row .field{margin:0}
.chatbox{height:calc(100vh - 144px);display:flex;flex-direction:column}
.messages{flex:1;overflow:auto;display:flex;flex-direction:column;gap:10px;padding-right:4px;padding-bottom:8px}
.msgwrap{max-width:82%;display:flex;flex-direction:column;gap:4px}.msgwrap.user{align-self:flex-end;align-items:flex-end}.msgwrap.ai{align-self:flex-start}
.speaker{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted)}
.bubble{padding:11px 14px;border-radius:14px;line-height:1.55;font-size:14px;word-break:break-word}
.bubble.user{background:#18374a}.bubble.ai{background:#0f2b25;border:1px solid rgba(72,224,140,.16)}
.bubble pre{background:#050a0d;border:1px solid var(--line);padding:10px;border-radius:10px;overflow:auto;margin:8px 0}
.bubble code{font-family:Consolas,monospace;color:#b7f7ff;font-size:13px}.bubble ul{margin:8px 0 8px 18px}
.edit-btn{border:0;background:transparent;color:var(--muted);font-size:11px;padding:0;margin-top:2px}
.compose{border-top:1px solid var(--line);padding-top:10px;display:grid;grid-template-columns:auto 1fr auto auto auto;gap:8px;align-items:end}
.compose textarea{margin:0;min-height:44px;max-height:130px}
.toolbar{display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
.speak{display:none;color:var(--cyan);font-size:12px;margin-left:auto}
.speak.on{display:flex;align-items:center;gap:6px}
.wave{display:inline-flex;gap:3px;align-items:end}.wave span{width:3px;background:var(--cyan);border-radius:3px;animation:wave 650ms infinite ease-in-out}
.wave span:nth-child(1){height:8px}.wave span:nth-child(2){height:14px;animation-delay:.12s}.wave span:nth-child(3){height:10px;animation-delay:.22s}
@keyframes wave{50%{transform:scaleY(.3)}}
.recording{outline:2px solid rgba(255,114,114,.5);outline-offset:2px}
.item{background:#071218;border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.item-title{font-weight:700;margin-bottom:4px;font-size:14px}.item-meta{color:var(--muted);font-size:13px;line-height:1.45}
.item-actions{display:flex;gap:6px;margin-top:8px}
.list{display:grid;gap:8px}
.alert{border-radius:12px;padding:10px 14px;margin-bottom:12px;font-size:13px}
.alert.ok{background:rgba(72,224,140,.11);color:var(--green);border:1px solid rgba(72,224,140,.26)}
.alert.err{background:rgba(255,114,114,.11);color:var(--red);border:1px solid rgba(255,114,114,.26)}
.badge{display:inline-block;border-radius:20px;padding:2px 9px;font-size:11px;font-weight:700}
.badge.open{background:rgba(53,215,233,.12);color:var(--cyan)}.badge.done{background:rgba(72,224,140,.12);color:var(--green)}
.badge.high{background:rgba(245,189,99,.12);color:var(--amber)}.badge.low{background:rgba(141,164,171,.12);color:var(--muted)}
.badge.planned{color:var(--muted)}.badge.configured,.badge.done2{color:var(--green)}.badge.warn{color:var(--amber)}
.tbl{width:100%;border-collapse:collapse;font-size:13px}.tbl th{text-align:left;color:var(--muted);font-size:11px;letter-spacing:1px;text-transform:uppercase;padding:8px 10px;border-bottom:1px solid var(--line)}
.tbl td{padding:9px 10px;border-bottom:1px solid rgba(37,65,75,.5)}.tbl tr:hover td{background:rgba(255,255,255,.02)}
.status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px}
.chart-wrap{position:relative;height:220px;margin-top:8px}
.bottom-nav{display:none;position:fixed;left:0;right:0;bottom:0;background:rgba(6,16,20,.97);border-top:1px solid var(--line);backdrop-filter:blur(20px);padding:6px 6px env(safe-area-inset-bottom,6px);grid-template-columns:repeat(5,1fr);gap:4px;z-index:200}
.bottom-nav button{border:0;background:transparent;color:var(--muted);border-radius:12px;padding:8px 4px;font-size:10px;display:flex;flex-direction:column;align-items:center;gap:3px;min-height:52px;min-width:44px;cursor:pointer;-webkit-tap-highlight-color:transparent}
.bottom-nav button.active{background:rgba(53,215,233,.14);color:var(--cyan)}
.bottom-nav button span.nav-icon{font-size:18px;line-height:1}
.fab{display:none;position:fixed;right:16px;bottom:80px;z-index:210;width:52px;height:52px;border-radius:50%;background:linear-gradient(135deg,var(--cyan),var(--green));border:0;color:#041014;font-size:24px;box-shadow:0 4px 24px rgba(53,215,233,.4);cursor:pointer;align-items:center;justify-content:center;-webkit-tap-highlight-color:transparent}
.fab-menu{display:none;position:fixed;right:12px;bottom:144px;z-index:211;flex-direction:column;gap:8px;align-items:flex-end}
.fab-menu.open{display:flex}
.fab-item{display:flex;align-items:center;gap:8px;cursor:pointer;-webkit-tap-highlight-color:transparent}
.fab-item-btn{width:40px;height:40px;border-radius:50%;background:var(--panel);border:1px solid var(--line);color:var(--text);font-size:16px;display:flex;align-items:center;justify-content:center}
.fab-item-label{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:4px 10px;font-size:12px;color:var(--soft);white-space:nowrap}
.palette{display:none;position:fixed;top:0;left:0;right:0;bottom:0;z-index:999;background:rgba(4,8,12,.7);backdrop-filter:blur(8px);align-items:flex-start;justify-content:center;padding-top:14vh}
.palette.open{display:flex}
.pal-box{width:min(540px,92%);background:var(--panel);border:1px solid var(--line);border-radius:18px;overflow:hidden;box-shadow:0 40px 100px rgba(0,0,0,.6)}
.pal-input{width:100%;height:54px;background:transparent;border:0;border-bottom:1px solid var(--line);color:var(--text);padding:0 18px;font-size:16px;outline:none}
.pal-results{max-height:320px;overflow-y:auto}
.pal-item{padding:12px 18px;cursor:pointer;display:flex;align-items:center;gap:12px;font-size:14px;color:var(--soft)}
.pal-item:hover,.pal-item.pal-active{background:rgba(53,215,233,.1);color:var(--text)}
.pal-icon{width:28px;height:28px;border-radius:8px;background:rgba(53,215,233,.15);display:grid;place-items:center;font-size:14px;flex-shrink:0}
.pal-desc{font-size:12px;color:var(--muted);margin-top:2px}
@media(max-width:900px){
  .app{grid-template-columns:1fr}
  .sidebar{display:none}
  .bottom-nav{display:grid;grid-template-columns:repeat(auto-fit,minmax(52px,1fr))}
  .fab{display:flex}
  .grid4,.grid3,.grid2{grid-template-columns:1fr}
  .topbar{padding:0 12px}
  .content{padding:12px 12px 90px}
  .chatbox{height:calc(100vh - 120px)}
  .msgwrap{max-width:94%}
  .compose{grid-template-columns:auto 1fr auto}
  .meta span{display:none}
  .btn{min-height:44px}
  .field{min-height:44px}
  .item{padding:14px}
  .stat .num{font-size:28px}
  .card{padding:14px}
  input,select,textarea{font-size:16px!important}
}
</style></head><body>

<!-- Command Palette -->
<div class="palette" id="palette" onclick="closePalette(event)">
  <div class="pal-box">
    <input class="pal-input" id="palInput" placeholder="Поиск команд, страниц, действий..." oninput="filterPalette()" onkeydown="palKey(event)" autocomplete="off">
    <div class="pal-results" id="palResults"></div>
  </div>
</div>

<div class="app">
  <aside class="sidebar">
    <div class="brand"><div class="mark">N</div><div><div class="brand-title">NEXUS</div><div class="brand-sub">ЦЕНТР УПРАВЛЕНИЯ</div></div></div>
    <nav class="nav" id="sideNav"></nav>
    <div class="side-bottom"><div class="online"><span class="dot"></span>NEXUS активен</div></div>
  </aside>
  <main class="main">
    <header class="topbar">
      <div class="title" id="pageTitle">Dashboard</div>
      <div class="meta">
        <span id="clock"></span>
        <span class="kbd" onclick="openPalette()">⌘K</span>
        <span>{{ email }}</span>
        <a class="logout" href="/logout">Выйти</a>
      </div>
    </header>
    <section class="content">
      <div id="alert"></div>

      <!-- DASHBOARD -->
      <div class="page active" id="dashboard">
        <div class="stack">
          <!-- KPI row -->
          <div class="grid4">
            <div class="card stat" onclick="showPage('tasksPage')" style="cursor:pointer">
              <div class="num" id="sTasksOpen">0</div>
              <div><div class="lab">Задач открытых</div><div class="hint" id="sTasksDone">0 выполнено</div></div>
            </div>
            <div class="card stat" onclick="showPage('crmPage')" style="cursor:pointer">
              <div class="num" id="sCrmClients">0</div>
              <div><div class="lab">Клиентов в CRM</div><div class="hint" id="sCrmNew">— новых</div></div>
            </div>
            <div class="card stat" onclick="showPage('monoPage')" style="cursor:pointer">
              <div class="num" id="sMonoBalance">—</div>
              <div><div class="lab">Monobank</div><div class="hint" id="sMonoHint">баланс UAH</div></div>
            </div>
            <div class="card stat">
              <div class="num" id="sMessages">0</div>
              <div><div class="lab">Сообщений AI</div><div class="hint" id="sBriefings">0 брифингов</div></div>
            </div>
          </div>
          <!-- Quick ask + task progress -->
          <div class="grid2">
            <div class="card">
              <h3>⚡ Быстрый вопрос</h3>
              <div class="row"><input class="field" id="quickInput" placeholder="Спроси NEXUS..." onkeydown="if(event.key==='Enter')quickAsk()"><button class="btn" onclick="quickAsk()">→</button></div>
              <div id="quickResult" class="item-meta" style="margin-top:8px;min-height:24px"></div>
            </div>
            <div class="card">
              <h3>📊 Прогресс задач</h3>
              <div style="background:var(--bg2);border-radius:8px;height:8px;margin:8px 0 4px;overflow:hidden">
                <div id="taskProgressFill" style="height:100%;background:linear-gradient(90deg,var(--cyan),var(--green));border-radius:8px;width:0%;transition:width .6s ease"></div>
              </div>
              <div id="taskProgressLabel" class="item-meta" style="margin-bottom:10px">Загрузка...</div>
              <div id="dashTasks" class="list"></div>
            </div>
          </div>
          <!-- Reminders + Briefing -->
          <div class="grid2">
            <div class="card">
              <h3>🔔 Напоминания</h3>
              <div id="dashReminders" class="list"></div>
              <button class="btn secondary sm" onclick="showPage('remindersPage')" style="margin-top:8px;width:100%">Все напоминания →</button>
            </div>
            <div class="card">
              <h3>🌅 Утренний брифинг</h3>
              <div class="row"><input class="field" id="briefCity" placeholder="Город" value="Kyiv"><button class="btn" onclick="loadBriefing()">Собрать</button></div>
              <div id="briefingResult" class="item-meta" style="margin-top:8px"></div>
            </div>
          </div>
        </div>
      </div>

      <!-- CHAT -->
      <div class="page" id="chat">
        <div class="card chatbox">
          <div class="toolbar">
            <input class="field" id="chatSearch" placeholder="Поиск в истории..." oninput="searchHistory('chatSearch','chatHistory')" style="max-width:220px;margin:0">
            <button class="btn secondary sm" onclick="exportChat()">↓ Экспорт</button>
            <button class="btn secondary sm" onclick="clearChat()">🗑 Очистить</button>
            <div class="speak" id="speaking"><span class="wave"><span></span><span></span><span></span></span>NEXUS говорит</div>
          </div>
          <div id="chatHistory" class="list" style="margin-bottom:8px;max-height:80px;overflow:auto"></div>
          <div class="messages" id="messages"></div>
          <div class="compose">
            <button class="btn secondary" id="micBtn" onclick="toggleVoice()" title="Голос">🎤</button>
            <textarea class="field" id="chatInput" placeholder="Напишите сообщение... (Enter — отправить, Shift+Enter — новая строка)" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendMsg(false)}"></textarea>
            <label class="btn secondary" for="chatFile" title="Файл">📎</label><input id="chatFile" type="file" style="display:none" onchange="uploadChatFile(this)">
            <button class="btn" onclick="sendMsg(false)">➤</button>
          </div>
        </div>
      </div>

      <!-- TASKS -->
      <div class="page" id="tasksPage">
        <div class="grid2">
          <div class="card"><h3>Задачи</h3>
            <div class="row" style="margin-bottom:10px">
              <input class="field" id="taskTitle" placeholder="Новая задача..." style="margin:0">
              <select class="field" id="taskPriority" style="width:100px;margin:0"><option value="normal">обычная</option><option value="high">срочная</option><option value="low">низкая</option></select>
              <button class="btn" onclick="createTask()">+</button>
            </div>
            <div style="display:flex;gap:6px;margin-bottom:10px">
              <button class="btn secondary sm" onclick="filterTasks('all')" id="tf_all">Все</button>
              <button class="btn secondary sm" onclick="filterTasks('open')" id="tf_open">Открытые</button>
              <button class="btn secondary sm" onclick="filterTasks('done')" id="tf_done">Готово</button>
            </div>
            <div id="tasksList" class="list"></div>
          </div>
          <div class="card"><h3>Умная команда</h3>
            <input class="field" id="commandInput" placeholder="добавь задачу позвонить клиенту" onkeydown="if(event.key==='Enter')runCommand()">
            <button class="btn" onclick="runCommand()" style="width:100%">Выполнить</button>
            <div id="commandResult" class="item-meta" style="margin-top:10px"></div>
            <h3 style="margin-top:16px">Горячие клавиши</h3>
            <div class="item-meta">
              <b style="color:var(--cyan)">⌘K / Ctrl+K</b> — командная палитра<br>
              <b style="color:var(--cyan)">Ctrl+Enter</b> — быстрый вопрос<br>
              <b style="color:var(--cyan)">Ctrl+E</b> — экспорт чата<br>
              <b style="color:var(--cyan)">Esc</b> — закрыть палитру
            </div>
          </div>
        </div>
      </div>

      <!-- CRM -->
      <div class="page" id="crmPage">
        <div class="stack">
          <div class="card"><h3>Клиенты</h3>
            <div class="row" style="margin-bottom:12px;flex-wrap:wrap;gap:8px">
              <input class="field" id="crmName" placeholder="Имя клиента" style="margin:0">
              <input class="field" id="crmPhone" placeholder="Телефон" style="margin:0;max-width:150px">
              <select class="field" id="crmBusiness" style="margin:0;max-width:160px"><option value="obshchepit">Общепит</option><option value="akva">Аква бизнес</option><option value="prodvizhenie">Продвижение</option><option value="other">Другое</option></select>
              <input class="field" id="crmNote" placeholder="Заметка" style="margin:0">
              <button class="btn" onclick="addClient()">Добавить</button>
            </div>
            <input class="field" id="crmSearch" placeholder="Поиск по имени, телефону..." oninput="renderCrm()">
            <div id="crmTable"></div>
          </div>
          <div class="card" id="clientDetail" style="display:none">
            <h3 id="clientDetailName">Клиент</h3>
            <div id="clientNotes" class="list" style="margin-bottom:10px"></div>
            <div class="row"><textarea class="field" id="newNoteText" placeholder="Добавить заметку..." rows="2" style="margin:0"></textarea><button class="btn" onclick="addNote()">+</button></div>
          </div>
        </div>
      </div>

      <!-- ANALYTICS -->
      <div class="page" id="analyticsPage">
        <div class="stack">
          <div class="grid3">
            <div class="card stat"><div class="num" id="an_total">0</div><div><div class="lab">Выручка</div><div class="hint">За 30 дней, грн</div></div></div>
            <div class="card stat"><div class="num" id="an_expenses">0</div><div><div class="lab">Расходы</div><div class="hint">За 30 дней, грн</div></div></div>
            <div class="card stat"><div class="num" id="an_profit">0</div><div><div class="lab">Прибыль</div><div class="hint">Выручка − расходы</div></div></div>
          </div>
          <div class="card"><h3>График выручки (7 дней)</h3><div class="chart-wrap"><canvas id="revenueChart"></canvas></div></div>
          <div class="grid2">
            <div class="card"><h3>Добавить запись</h3>
              <input class="field" id="an_date" type="date">
              <select class="field" id="an_business"><option value="obshchepit">Общепит</option><option value="akva">Аква</option><option value="prodvizhenie">Продвижение</option></select>
              <input class="field" id="an_revenue" type="number" placeholder="Выручка, грн">
              <input class="field" id="an_expenses" type="number" placeholder="Расходы, грн">
              <input class="field" id="an_clients" type="number" placeholder="Клиентов">
              <input class="field" id="an_comment" placeholder="Комментарий">
              <button class="btn" style="width:100%" onclick="addAnalyticsRecord()">Сохранить</button>
            </div>
            <div class="card"><h3>История записей</h3><div id="analyticsHistory" class="list"></div></div>
          </div>
        </div>
      </div>

      <!-- EMAIL -->
      <div class="page" id="emailPage">
        <div class="grid2">
          <div class="card"><h3>Входящие</h3>
            <button class="btn secondary sm" onclick="loadEmails()" style="margin-bottom:10px">↻ Обновить</button>
            <div id="emailList" class="list"><div class="item-meta">Нажмите "Обновить" для загрузки писем.</div></div>
          </div>
          <div class="card"><h3>Новое письмо</h3>
            <input class="field" id="emailTo" placeholder="Кому (email)">
            <input class="field" id="emailSubject" placeholder="Тема">
            <textarea class="field" id="emailBody" rows="7" placeholder="Текст письма..."></textarea>
            <div class="row">
              <button class="btn secondary" style="flex:1" onclick="aiDraftEmail()">AI-черновик</button>
              <button class="btn" style="flex:1" onclick="sendEmail()">Отправить ✉</button>
            </div>
          </div>
        </div>
      </div>

      <!-- REMINDERS -->
      <div class="page" id="remindersPage">
        <div class="grid2">
          <div class="card"><h3>Напоминания</h3>
            <div id="remindersList" class="list"></div>
            <button class="btn secondary sm" onclick="loadReminders()" style="margin-top:10px">↻ Обновить</button>
          </div>
          <div class="card"><h3>Добавить напоминание</h3>
            <input class="field" id="remText" placeholder="Текст напоминания">
            <input class="field" id="remTime" type="datetime-local">
            <select class="field" id="remRepeat"><option value="once">Один раз</option><option value="daily">Каждый день</option><option value="weekly">Каждую неделю</option></select>
            <button class="btn" style="width:100%" onclick="addReminder()">Добавить</button>
          </div>
        </div>
      </div>

      <!-- NOVA POSHTA -->
      <div class="page" id="novaPage">
        <div class="card" style="max-width:560px">
          <h3>Трекинг Нова Пошта</h3>
          <div class="row">
            <input class="field" id="novaNumber" placeholder="Номер ТТН (14+ цифр)" style="margin:0">
            <button class="btn" onclick="trackNova()">Отследить</button>
          </div>
          <div id="novaResult" style="margin-top:16px"></div>
        </div>
      </div>

      <!-- FILES -->
      <div class="page" id="files">
        <div class="card">
          <h3>Загрузка файлов в чат</h3>
          <div class="item-meta" style="margin-bottom:14px">PDF, DOCX, TXT — NEXUS прочитает и ответит на вопросы по содержимому.</div>
          <label class="btn" for="chatFilePage" style="display:inline-flex;align-items:center;gap:8px">📎 Выбрать файл</label>
          <input id="chatFilePage" type="file" style="display:none" onchange="uploadChatFile(this)">
          <div id="fileResult" class="list" style="margin-top:14px"></div>
        </div>
      </div>

      <!-- AGENTS -->
      <div class="page" id="agentsPage">
        <div class="stack">
          <div class="grid3" id="builtinAgents"></div>
          <div class="card"><h3>Мои агенты</h3>
            <div id="agentsList" class="list" style="margin-bottom:12px"></div>
            <div class="grid2">
              <input class="field" id="agentName" placeholder="Назва агента">
              <input class="field" id="agentDesc" placeholder="Опис та завдання...">
            </div>
            <button class="btn" style="width:100%" onclick="createAgent()">＋ Створити агента</button>
          </div>
        </div>
      </div>

      <!-- SEARCH -->
      <div class="page" id="search">
        <div class="card"><h3>Поиск по истории</h3>
          <input class="field" id="globalSearch" placeholder="Введите фразу..." oninput="searchHistory('globalSearch','globalResults')">
          <div id="globalResults" class="list"></div>
        </div>
      </div>

      <!-- PROFILE -->
      <div class="page" id="profilePage">
        <div class="grid2">
          <div class="card"><h3>Профиль</h3>
            <label style="color:var(--muted);font-size:12px">Имя</label>
            <input class="field" id="profName" placeholder="Никита">
            <label style="color:var(--muted);font-size:12px">Бизнесы</label>
            <input class="field" id="profBusiness" placeholder="общепит, аква бизнес, продвижение">
            <label style="color:var(--muted);font-size:12px">Город (для погоды)</label>
            <input class="field" id="profCity" placeholder="Kyiv">
            <label style="color:var(--muted);font-size:12px">Локация</label>
            <input class="field" id="profLocation" placeholder="Украина">
            <button class="btn" style="width:100%" onclick="saveProfile()">Сохранить профиль</button>
          </div>
          <div class="card"><h3>Установить приложение</h3>
            <div class="item-meta" style="margin-bottom:14px">NEXUS можно установить как приложение на телефон или компьютер — работает без браузера.</div>
            <button class="btn secondary" id="pwaInstallBtn" onclick="installPWA()" style="width:100%;display:none">📲 Установить NEXUS</button>
            <div id="pwaStatus" class="item-meta" style="margin-top:10px"></div>
            <h3 style="margin-top:20px">Горячие клавиши</h3>
            <div class="item-meta">
              <b style="color:var(--cyan)">Ctrl+K</b> — командная палитра<br>
              <b style="color:var(--cyan)">Ctrl+E</b> — экспорт чата<br>
              <b style="color:var(--cyan)">Enter</b> — отправить сообщение<br>
              <b style="color:var(--cyan)">Shift+Enter</b> — новая строка в чате<br>
              <b style="color:var(--cyan)">Esc</b> — закрыть палитру
            </div>
          </div>
        </div>
      </div>

      <!-- MONOBANK -->
      <div class="page" id="monoPage">
        <div class="stack">
          <div class="grid3">
            <div class="card stat"><div class="num" id="monoBalance">—</div><div><div class="lab">Баланс UAH</div><div class="hint">Поточний рахунок</div></div></div>
            <div class="card stat"><div class="num" id="monoIncome" style="color:var(--green)">—</div><div><div class="lab">Надходження</div><div class="hint">За 30 днів</div></div></div>
            <div class="card stat"><div class="num" id="monoExpense" style="color:var(--red)">—</div><div><div class="lab">Витрати</div><div class="hint">За 30 днів</div></div></div>
          </div>
          <div class="card"><h3>Останні транзакції</h3>
            <div class="toolbar"><button class="btn secondary sm" onclick="loadMono()">🔄 Оновити</button><span id="monoSyncTime" class="item-meta"></span></div>
            <div id="monoTxList" class="list"></div>
          </div>
          <div class="card"><h3>Налаштування</h3>
            <p class="item-meta">Додайте <b>MONOBANK_TOKEN</b> в .env файл — особистий токен з додатку Monobank → Налаштування → Розробникам.</p>
            <div id="monoTokenStatus" class="item-meta" style="margin-top:8px"></div>
          </div>
        </div>
      </div>

      <!-- CALENDAR -->
      <div class="page" id="calendarPage">
        <div class="stack">
          <div class="card"><h3>Сьогодні — <span id="calTodayDate"></span></h3>
            <div id="calTodayEvents" class="list"></div>
          </div>
          <div class="card"><h3>Найближчі події</h3>
            <div class="toolbar"><button class="btn secondary sm" onclick="loadCalendar()">🔄 Оновити</button></div>
            <div id="calUpcoming" class="list"></div>
          </div>
          <div class="card"><h3>Додати подію</h3>
            <input class="field" id="calTitle" placeholder="Назва події">
            <div class="grid2">
              <input class="field" id="calDate" type="date">
              <input class="field" id="calTime" type="time" value="09:00">
            </div>
            <input class="field" id="calDesc" placeholder="Опис (необов'язково)">
            <button class="btn" onclick="addCalEvent()">Додати подію</button>
          </div>
        </div>
      </div>

      <!-- AUDIT LOG -->
      <div class="page" id="auditPage">
        <div class="stack">
          <div class="card"><h3>Журнал дій</h3>
            <div class="toolbar">
              <input class="field" id="auditSearch" placeholder="Пошук..." oninput="filterAudit()" style="max-width:220px;margin:0">
              <select class="field" id="auditType" onchange="filterAudit()" style="max-width:160px;margin:0">
                <option value="">Всі події</option>
                <option value="login">Вхід</option>
                <option value="task">Задачі</option>
                <option value="crm">CRM</option>
                <option value="chat">Чат</option>
              </select>
              <button class="btn secondary sm" onclick="loadAudit()">🔄</button>
            </div>
            <div id="auditList" style="margin-top:12px"></div>
          </div>
        </div>
      </div>

      <!-- SETTINGS -->
      <div class="page" id="settingsPage">
        <div class="stack">
          <div class="card"><h3>Інтеграції</h3><div id="integrationsList" class="status-grid"></div></div>
          <div class="card"><h3>Можливості системи</h3><div id="capabilitiesList" class="status-grid"></div></div>
          <div class="card"><h3>Тема</h3>
            <div class="row" style="align-items:center;gap:12px">
              <span class="item-meta">Кольорова схема:</span>
              <button class="btn secondary sm" onclick="setTheme('dark')" id="themeDark">🌙 Темна</button>
              <button class="btn secondary sm" onclick="setTheme('light')" id="themeLight">☀️ Світла</button>
              <button class="btn secondary sm" onclick="setTheme('midnight')" id="themeMidnight">🔵 Midnight</button>
            </div>
          </div>
          <div class="card"><h3>Сповіщення</h3>
            <p class="item-meta" id="notifStatus">Перевірка статусу...</p>
            <button class="btn secondary sm" style="margin-top:8px" onclick="requestNotifPermission()">🔔 Дозволити сповіщення</button>
          </div>
          <div class="card"><h3>🔐 Двофакторна аутентифікація (2FA)</h3>
            <div id="twoFaStatus" class="item-meta" style="margin-bottom:12px">Завантаження...</div>
            <div id="twoFaSetup" style="display:none">
              <p class="item-meta">Відскануйте QR-код у Google Authenticator або Authy:</p>
              <div style="margin:12px 0"><img id="twoFaQr" src="" alt="QR" style="border-radius:10px;max-width:180px;background:#fff;padding:8px"></div>
              <p class="item-meta" style="font-size:11px;word-break:break-all">Ключ: <span id="twoFaKey" style="color:var(--cyan)"></span></p>
              <input class="field" id="twoFaCode" placeholder="Введіть 6-значний код для підтвердження" maxlength="6" inputmode="numeric" style="margin-top:10px">
              <button class="btn" onclick="confirm2FA()">✅ Підтвердити та увімкнути</button>
            </div>
            <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
              <button class="btn secondary sm" onclick="setup2FA()">⚙️ Налаштувати 2FA</button>
              <button class="btn danger sm" id="disable2faBtn" style="display:none" onclick="disable2FA()">❌ Вимкнути 2FA</button>
            </div>
          </div>
          <div class="card"><h3>Google Sheets — синхронізація</h3>
            <p class="item-meta">Вивантаження задач, CRM та аналітики у Google Spreadsheet.</p>
            <p class="item-meta" style="font-size:11px">Потрібно: GOOGLE_SERVICE_ACCOUNT_FILE та GOOGLE_SPREADSHEET_ID у .env</p>
            <button class="btn secondary sm" style="margin-top:8px" onclick="syncSheets()">📊 Синхронізувати з Google Sheets</button>
            <div id="sheetsStatus" class="item-meta" style="margin-top:8px"></div>
          </div>
          <div class="card">
            <h3>💾 Резервная копия</h3>
            <p class="item-meta">Скачать все данные (задачи, CRM, напоминания, календарь) в один .zip файл.</p>
            <div class="row" style="flex-wrap:wrap;gap:8px;margin-top:8px">
              <a class="btn secondary sm" href="/backup" download>⬇ Скачать бэкап</a>
              <a class="btn secondary sm" href="/export" download>📄 Экспорт JSON</a>
            </div>
            <p class="item-meta" style="margin-top:10px">Восстановить из файла:</p>
            <div class="row" style="gap:8px">
              <input type="file" id="restoreFile" accept=".zip" class="field" style="margin:0">
              <button class="btn secondary sm" onclick="restoreBackup()">↑ Загрузить</button>
            </div>
            <div id="restoreStatus" class="item-meta" style="margin-top:6px"></div>
          </div>
          <div class="card"><h3>Користувачі</h3>
            <div id="usersList" class="list" style="margin-bottom:12px"></div>
            <div class="grid2"><input class="field" id="newUsername" placeholder="username"><select class="field" id="newUserRole"><option value="employee">employee</option><option value="guest">guest</option><option value="admin">admin</option></select></div>
            <input class="field" id="newUserPassword" placeholder="пароль">
            <button class="btn" onclick="saveUser()">Зберегти користувача</button>
          </div>
        </div>
      </div>

    </section>
  </main>
</div>
<nav class="bottom-nav" id="bottomNav"></nav>
<div class="fab-menu" id="fabMenu">
  <div class="fab-item" onclick="closeFab();showPage('chat')"><span class="fab-item-label">Чат NEXUS</span><div class="fab-item-btn">💬</div></div>
  <div class="fab-item" onclick="closeFab();showPage('tasksPage')"><span class="fab-item-label">Задача</span><div class="fab-item-btn">✅</div></div>
  <div class="fab-item" onclick="closeFab();showPage('remindersPage')"><span class="fab-item-label">Напоминание</span><div class="fab-item-btn">⏰</div></div>
  <div class="fab-item" onclick="closeFab();showPage('calendarPage')"><span class="fab-item-label">Событие</span><div class="fab-item-btn">📅</div></div>
</div>
<button class="fab" id="fab" onclick="toggleFab()" title="Быстрые действия">＋</button>

<script>
// ── Config ────────────────────────────────────────────────────────────────────
var NAV=[
  ['dashboard','📊','Dashboard'],['chat','💬','Чат'],['tasksPage','✅','Задачи'],
  ['crmPage','👥','CRM'],['analyticsPage','📈','Аналитика'],['emailPage','✉️','Email'],
  ['remindersPage','🔔','Напоминания'],['novaPage','📦','Нова Пошта'],
  ['monoPage','💳','Monobank'],['calendarPage','📅','Календарь'],
  ['files','📎','Файлы'],['agentsPage','🧩','Агенты'],
  ['auditPage','🛡','Аудит'],['search','🔍','Поиск'],['profilePage','👤','Профиль'],['settingsPage','⚙️','Настройки']
];
var csrfToken=document.querySelector('meta[name="csrf-token"]').content;
var messages=[],editingId=null,currentAudio=null,recognition=null,isListening=false;
var crmClients=[],allTasks=[],taskFilter='all';
var currentClientId=null;

function $(id){return document.getElementById(id)}
function esc(t){var d=document.createElement('div');d.textContent=t||'';return d.innerHTML}

// ── Nav ───────────────────────────────────────────────────────────────────────
function renderNav(){
  var side='',bot='';
  NAV.forEach(function(n,i){
    side+='<button class="'+(i===0?'active':'')+'" data-page="'+n[0]+'"><span class="emoji">'+n[1]+'</span>'+n[2]+'</button>';
    if(i<5)bot+='<button class="'+(i===0?'active':'')+'" data-page="'+n[0]+'"><span class="nav-icon">'+n[1]+'</span><span>'+n[2]+'</span></button>';
  });
  $('sideNav').innerHTML=side;$('bottomNav').innerHTML=bot;
  document.querySelectorAll('[data-page]').forEach(function(b){b.onclick=function(){showPage(b.dataset.page)}});
}
var _fabOpen=false;
function toggleFab(){_fabOpen=!_fabOpen;$('fabMenu').classList.toggle('open',_fabOpen);$('fab').textContent=_fabOpen?'✕':'＋';}
function closeFab(){_fabOpen=false;$('fabMenu').classList.remove('open');$('fab').textContent='＋';}
document.addEventListener('click',function(e){if(_fabOpen&&!e.target.closest('#fab')&&!e.target.closest('#fabMenu'))closeFab();});
renderNav();

var PAGE_TITLES={dashboard:'Dashboard',chat:'Чат NEXUS',tasksPage:'Задачи',crmPage:'CRM',analyticsPage:'Аналитика',emailPage:'Email',remindersPage:'Напоминания',novaPage:'Нова Пошта',monoPage:'Monobank',calendarPage:'Календарь',files:'Файлы',agentsPage:'Агенты',auditPage:'Журнал аудита',search:'Поиск',profilePage:'Профиль',settingsPage:'Настройки'};
function showPage(page){
  document.querySelectorAll('.page').forEach(function(p){p.classList.remove('active')});
  var el=$(page);if(el)el.classList.add('active');
  document.querySelectorAll('[data-page]').forEach(function(x){x.classList.toggle('active',x.dataset.page===page)});
  $('pageTitle').textContent=PAGE_TITLES[page]||'NEXUS';
  if(page==='tasksPage')loadTasks();
  if(page==='agentsPage')loadAgents();
  if(page==='settingsPage')loadSettings();
  if(page==='profilePage')loadProfile();
  if(page==='crmPage')loadCrm();
  if(page==='analyticsPage')loadAnalytics();
  if(page==='remindersPage')loadReminders();
  if(page==='monoPage')loadMono();
  if(page==='calendarPage')loadCalendar();
  if(page==='auditPage')loadAudit();
}

// ── Alerts ────────────────────────────────────────────────────────────────────
function alertMsg(t,ok){$('alert').innerHTML='<div class="alert '+(ok?'ok':'err')+'">'+esc(t)+'</div>';setTimeout(function(){$('alert').innerHTML=''},4500)}

// ── API helper ────────────────────────────────────────────────────────────────
function api(url,opts){
  opts=opts||{};opts.headers=opts.headers||{};
  if(opts.method&&opts.method.toUpperCase()==='POST')opts.headers['X-CSRF-Token']=csrfToken;
  return fetch(url,opts).then(function(r){
    if(r.status===401){location.href='/login';return Promise.reject('auth')}
    return r.text().then(function(t){try{return JSON.parse(t)}catch(e){return{error:'parse error: '+url}}})
  })
}



// ── Backup / Restore ──────────────────────────────────────────────────────────
function restoreBackup(){
  var file=$('restoreFile').files[0];
  if(!file){alertMsg('Выберите .zip файл',false);return;}
  var fd=new FormData();fd.append('file',file);
  $('restoreStatus').textContent='⏳ Восстановление...';
  fetch('/restore',{method:'POST',headers:{'X-CSRF-Token':csrfToken},body:fd})
    .then(function(r){return r.json()})
    .then(function(d){
      if(d.success){
        $('restoreStatus').textContent='✅ Восстановлено: '+d.restored.join(', ');
        alertMsg('✅ Данные восстановлены из бэкапа',true);
        setTimeout(function(){location.reload()},1500);
      } else {
        $('restoreStatus').textContent='❌ '+d.error;
        alertMsg('❌ Ошибка восстановления: '+d.error,false);
      }
    }).catch(function(){
      $('restoreStatus').textContent='❌ Ошибка сети';
    });
}

// ── Google Sheets sync ────────────────────────────────────────────────────────
function syncSheets(){
  var btn=event.currentTarget||document.activeElement;
  if(btn){btn.disabled=true;btn.textContent='⏳ Синхронізація...';}
  api('/sheets/sync',{method:'POST'}).then(function(r){
    if(btn){btn.disabled=false;btn.textContent='📊 Синхронізувати з Google Sheets';}
    if(r.success){
      alertMsg('✅ Google Sheets оновлено: '+JSON.stringify(r.updated||r), true);
    } else {
      alertMsg('❌ Помилка: '+(r.error||'невідома'), false);
    }
  }).catch(function(e){
    if(btn){btn.disabled=false;btn.textContent='📊 Синхронізувати з Google Sheets';}
    alertMsg('❌ Помилка мережі', false);
  });
}

// ── Markdown ──────────────────────────────────────────────────────────────────
function md(t){
  var s=esc(t);
  s=s.replace(/```([\s\S]*?)```/g,function(_,c){return'<pre><code>'+c+'</code></pre>'});
  s=s.replace(/`([^`]+)`/g,'<code>$1</code>');
  s=s.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  s=s.replace(/\*([^*]+)\*/g,'<em>$1</em>');
  s=s.replace(/^[-•] (.*)$/gm,'<li>$1</li>');
  s=s.replace(/(<li>[\s\S]*?<\/li>)/g,'<ul>$1</ul>');
  return s.replace(/\n/g,'<br>');
}

// ── Clock ─────────────────────────────────────────────────────────────────────
function updateClock(){$('clock').textContent=new Date().toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'})}
setInterval(updateClock,1000);updateClock();

// ── Stats ─────────────────────────────────────────────────────────────────────
function loadStats(){
  api('/stats').then(function(d){
    if(!d)return;
    $('sMessages').textContent=d.messages||0;
    $('sBriefings').textContent=(d.briefings||0)+' брифингов';
  });
  api('/tasks').then(function(d){
    if(!d)return;
    var tasks=d.tasks||[];
    var open=tasks.filter(function(t){return t.status!=='done'}).length;
    var done=tasks.filter(function(t){return t.status==='done'}).length;
    var total=tasks.length;
    $('sTasksOpen').textContent=open;
    $('sTasksDone').textContent=done+' выполнено';
    var pct=total>0?Math.round(done/total*100):0;
    var fill=$('taskProgressFill');
    if(fill){fill.style.width=pct+'%';}
    var lbl=$('taskProgressLabel');
    if(lbl){lbl.textContent=done+' из '+total+' задач выполнено ('+pct+'%)';}
  });
  api('/crm').then(function(d){
    if(!d)return;
    var clients=d.clients||[];
    $('sCrmClients').textContent=clients.length;
    var now=Date.now();var week=7*86400*1000;
    var newCount=clients.filter(function(c){return c.created&&(now-new Date(c.created).getTime())<week}).length;
    $('sCrmNew').textContent=newCount+' за неделю';
  });
  api('/monobank').then(function(d){
    if(!d||d.error)return;
    var bal=d.balance;
    if(typeof bal==='number'){
      var fmt=new Intl.NumberFormat('uk-UA',{maximumFractionDigits:0}).format(bal);
      $('sMonoBalance').textContent=fmt;
      $('sMonoHint').textContent=(d.income?'↑'+Math.round(d.income)+' ':'')+' UAH';
    }
  }).catch(function(){});
}

// ── Chat ──────────────────────────────────────────────────────────────────────
function loadHistory(){api('/history').then(function(d){messages=d.messages||[];renderMessages();searchHistory()})}
function renderMessages(){
  var box=$('messages');box.innerHTML='';
  messages.slice(-50).forEach(function(m){
    var w=document.createElement('div');w.className='msgwrap '+(m.role==='user'?'user':'ai');w.dataset.id=m.id;
    w.innerHTML='<div class="speaker">'+(m.role==='user'?'Никита':'NEXUS')+'</div>'
      +'<div class="bubble '+(m.role==='user'?'user':'ai')+'">'+md(m.content)+'</div>'
      +(m.role==='user'?'<button class="edit-btn" onclick="editMessage(\''+m.id+'\')">✏ редактировать</button>':'');
    box.appendChild(w);
  });
  box.scrollTop=box.scrollHeight;
}
function appendMessage(role,text,id){messages.push({id:id||String(Date.now()),role:role,content:text});renderMessages()}
function editMessage(id){var m=messages.find(function(x){return x.id===id});if(!m)return;editingId=id;$('chatInput').value=m.content;$('chatInput').focus();alertMsg('Редактируйте и отправьте заново.',true)}

function sendMsg(voice){
  var inp=$('chatInput'),text=inp.value.trim();if(!text)return;
  if(currentAudio){currentAudio.pause();currentAudio=null}
  inp.value='';var payload={message:text,voice:voice,edit_id:editingId};editingId=null;
  appendMessage('user',text);
  var aiId='ai-'+Date.now();appendMessage('assistant','',aiId);
  var aiMsg=messages[messages.length-1];$('speaking').classList.add('on');
  fetch('/chat_stream',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify(payload)})
  .then(function(r){
    var reader=r.body.getReader(),dec=new TextDecoder();
    function pump(){return reader.read().then(function(x){
      if(x.done){$('speaking').classList.remove('on');loadStats();loadHistory();return}
      dec.decode(x.value).split('\n\n').forEach(function(line){
        if(line.indexOf('data: ')===0){try{var d=JSON.parse(line.slice(6));if(d.token){aiMsg.content+=d.token;renderMessages()}if(d.audio)playB64(d.audio)}catch(e){}}
      });return pump();
    })}return pump();
  }).catch(function(e){aiMsg.content='Ошибка: '+e.message;renderMessages();$('speaking').classList.remove('on')});
}

function playB64(b64){if(currentAudio)currentAudio.pause();currentAudio=new Audio('data:audio/mp3;base64,'+b64);currentAudio.play().catch(function(){})}

function quickAsk(){
  var v=$('quickInput').value.trim();if(!v)return;$('quickInput').value='';$('quickResult').textContent='Думаю...';
  api('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:v})})
  .then(function(d){$('quickResult').innerHTML=md(d.reply||'');loadStats();loadHistory()});
}

function searchHistory(inputId,outId){
  var q=($(inputId||'historyQuery')?.value||'').toLowerCase();
  var out=$(outId||'historyResults');if(!out)return;
  if(!q){out.innerHTML='<div class="item-meta">Введите текст для поиска.</div>';return}
  var found=messages.filter(function(m){return m.content.toLowerCase().indexOf(q)>=0}).slice(-10);
  out.innerHTML=found.map(function(m){return'<div class="item"><div class="item-title">'+(m.role==='user'?'Никита':'NEXUS')+'</div><div class="item-meta">'+esc(m.content).slice(0,200)+'</div></div>'}).join('')||'<div class="item-meta">Ничего не найдено.</div>';
}

function exportChat(){
  var text=messages.map(function(m){return(m.role==='user'?'Никита':'NEXUS')+': '+m.content}).join('\n\n');
  var blob=new Blob([text],{type:'text/plain;charset=utf-8'});
  var a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='nexus_chat_'+new Date().toISOString().slice(0,10)+'.txt';a.click();
}

function clearChat(){
  if(!confirm('Очистить историю чата?'))return;
  api('/clear_history',{method:'POST',headers:{'Content-Type':'application/json'}}).then(function(){messages=[];renderMessages();alertMsg('История очищена.',true)});
}

// ── Voice ─────────────────────────────────────────────────────────────────────
function toggleVoice(){
  if(currentAudio){currentAudio.pause();currentAudio=null;$('speaking').classList.remove('on')}
  if(!window.SpeechRecognition&&!window.webkitSpeechRecognition){alertMsg('Голосовой ввод доступен в Chrome.',false);return}
  if(isListening&&recognition){recognition.stop();return}
  var SR=window.SpeechRecognition||window.webkitSpeechRecognition;recognition=new SR();
  recognition.lang='ru-RU';recognition.continuous=false;recognition.interimResults=false;
  recognition.onstart=function(){isListening=true;$('micBtn').classList.add('recording');$('micBtn').textContent='⏹'};
  recognition.onend=function(){isListening=false;$('micBtn').classList.remove('recording');$('micBtn').textContent='🎤'};
  recognition.onerror=recognition.onend;
  recognition.onresult=function(e){var t=e.results[0][0].transcript;$('chatInput').value=t;sendMsg(true)};
  recognition.start();
}

function uploadChatFile(input){
  var f=input.files[0];if(!f)return;
  var fd=new FormData();fd.append('file',f);
  api('/upload_chat',{method:'POST',body:fd}).then(function(d){
    alertMsg(d.message||'Файл загружен',!!d.success);
    if($('fileResult'))$('fileResult').innerHTML='<div class="item"><div class="item-title">'+esc(f.name)+'</div><div class="item-meta">'+esc(d.message||'')+'</div></div>';
    loadStats();loadHistory();
  });
}

// ── Dashboard ─────────────────────────────────────────────────────────────────
function loadBriefing(){
  var city=($('briefCity').value||'Kyiv').trim();$('briefingResult').textContent='Собираю брифинг...';
  api('/morning_briefing?city='+encodeURIComponent(city)).then(function(d){$('briefingResult').innerHTML=md(d.briefing||d.error||'');loadStats()});
}
function loadDashboard(){
  api('/tasks').then(function(d){
    var tasks=d.tasks||[];
    var open=tasks.filter(function(t){return t.status!=='done'});
    var high=open.filter(function(t){return t.priority==='high'});
    var show=(high.length?high:open).slice(0,4);
    $('dashTasks').innerHTML=show.map(function(t){
      var badge=t.priority==='high'?'high':t.status==='done'?'done':'open';
      var label=t.priority==='high'?'🔴 срочная':t.status==='done'?'✅':'🔵';
      return'<div class="item" style="display:flex;align-items:center;gap:8px">'+
        '<span class="badge '+badge+'">'+label+'</span>'+
        '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(t.title)+'</span>'+
        '<button class="btn secondary sm" onclick="doneTask(''+t.id+'')" style="padding:2px 8px;font-size:11px">✓</button>'+
      '</div>';
    }).join('')||'<div class="item-meta">🎉 Все задачи выполнены!</div>';
  });
  api('/reminders').then(function(d){
    var reminders=(d.reminders||[]).slice(0,4);
    $('dashReminders').innerHTML=reminders.map(function(r){
      return'<div class="item">'+
        '<div class="item-title">'+esc(r.text)+'</div>'+
        '<div class="item-meta" style="color:var(--cyan)">'+esc(r.time||r.repeat||'без времени')+'</div>'+
      '</div>';
    }).join('')||'<div class="item-meta">Нет напоминаний.</div>';
  });
}

function doneTask(id){
  var h={'X-CSRF-Token':csrfToken};
  api('/tasks/'+id,{method:'PATCH',headers:{...h,'Content-Type':'application/json'},body:JSON.stringify({status:'done'})})
    .then(function(){loadDashboard();loadStats();alertMsg('✅ Задача выполнена',true)});
}

// ── Tasks ─────────────────────────────────────────────────────────────────────
function loadTasks(){
  api('/tasks').then(function(d){allTasks=d.tasks||[];renderTasks()});
}
function filterTasks(f){taskFilter=f;renderTasks()}
function renderTasks(){
  var list=allTasks.filter(function(t){if(taskFilter==='open')return t.status!=='done';if(taskFilter==='done')return t.status==='done';return true});
  $('tasksList').innerHTML=list.map(function(t){
    var done=t.status==='done';
    return'<div class="item"><div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
      +'<span class="badge '+(done?'done':(t.priority==='high'?'high':'open'))+'">'+esc(done?'✓':t.priority||'обычная')+'</span>'
      +'<span style="'+(done?'text-decoration:line-through;color:var(--muted)':'')+'">'+(esc(t.title))+'</span>'
      +'<span style="margin-left:auto;color:var(--muted);font-size:11px">'+esc(t.owner||'')+'</span>'
      +'</div>'
      +'<div class="item-actions">'
      +'<button class="btn secondary sm" onclick="toggleTask(\''+t.id+'\')">'+(done?'Открыть':'✓ Готово')+'</button>'
      +'<button class="btn danger sm" onclick="deleteTask(\''+t.id+'\')">✕</button>'
      +'</div></div>';
  }).join('')||'<div class="item-meta">Нет задач.</div>';
}
function createTask(){
  var title=($('taskTitle').value||'').trim();var priority=$('taskPriority').value;if(!title)return;
  api('/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:title,priority:priority})})
  .then(function(d){$('taskTitle').value='';alertMsg(d.success?'Задача добавлена':(d.error||'Ошибка'),!!d.success);loadTasks();loadStats()});
}
function toggleTask(id){
  api('/tasks/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'toggle'})})
  .then(function(){loadTasks();loadStats()});
}
function deleteTask(id){
  api('/tasks/'+id,{method:'DELETE',headers:{'Content-Type':'application/json'}}).then(function(){loadTasks();loadStats()});
}
function runCommand(){
  var cmd=($('commandInput').value||'').trim();if(!cmd)return;
  api('/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:cmd})})
  .then(function(d){$('commandResult').innerHTML=d.reply?md(d.reply):('<pre>'+esc(JSON.stringify(d,null,2))+'</pre>');loadTasks();loadStats()});
}

// ── CRM ───────────────────────────────────────────────────────────────────────
function loadCrm(){api('/crm').then(function(d){crmClients=d.clients||[];renderCrm()})}
function renderCrm(){
  var q=($('crmSearch')?.value||'').toLowerCase();
  var list=crmClients.filter(function(c){return!q||c.name.toLowerCase().indexOf(q)>=0||(c.phone||'').indexOf(q)>=0});
  if(!list.length){$('crmTable').innerHTML='<div class="item-meta">Клиентов нет. Добавьте первого.</div>';return}
  var html='<table class="tbl"><thead><tr><th>Имя</th><th>Телефон</th><th>Бизнес</th><th>Заметок</th><th></th></tr></thead><tbody>';
  list.forEach(function(c){
    html+='<tr><td><b>'+esc(c.name)+'</b></td><td>'+esc(c.phone||'—')+'</td><td>'+esc(c.business||'—')+'</td>'
      +'<td>'+((c.notes||[]).length)+'</td>'
      +'<td><button class="btn secondary sm" onclick="openClient(\''+c.id+'\')">Открыть</button></td></tr>';
  });
  html+='</tbody></table>';$('crmTable').innerHTML=html;
}
function addClient(){
  var name=($('crmName').value||'').trim();if(!name){alertMsg('Укажите имя клиента',false);return}
  var data={name:name,phone:$('crmPhone').value.trim(),business:$('crmBusiness').value,note:$('crmNote').value.trim()};
  api('/crm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})
  .then(function(d){alertMsg(d.success?'Клиент добавлен':(d.error||'Ошибка'),!!d.success);if(d.success){$('crmName').value='';$('crmPhone').value='';$('crmNote').value='';loadCrm();loadStats()}});
}
function openClient(id){
  currentClientId=id;var c=crmClients.find(function(x){return x.id===id});if(!c)return;
  $('clientDetailName').textContent=c.name+' — заметки';
  $('clientDetail').style.display='';
  $('clientNotes').innerHTML=(c.notes||[]).map(function(n){return'<div class="item"><div class="item-meta">'+esc(n.text)+'</div><div style="color:var(--muted);font-size:11px">'+esc(n.ts||'')+'</div></div>'}).join('')||'<div class="item-meta">Нет заметок.</div>';
}
function addNote(){
  var text=($('newNoteText').value||'').trim();if(!text||!currentClientId)return;
  api('/crm/note',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({client_id:currentClientId,note:text})})
  .then(function(d){alertMsg(d.success?'Заметка добавлена':(d.error||'Ошибка'),!!d.success);if(d.success){$('newNoteText').value='';loadCrm();setTimeout(function(){openClient(currentClientId)},300)}});
}

// ── Analytics ─────────────────────────────────────────────────────────────────
function loadAnalytics(){
  api('/analytics').then(function(d){
    var records=d.records||[];
    var now=Date.now();var day30=new Date(now-30*864e5).toISOString().slice(0,10);
    var recent=records.filter(function(r){return r.date>=day30});
    var revenue=recent.reduce(function(a,r){return a+parseFloat(r.revenue||0)},0);
    var expenses=recent.reduce(function(a,r){return a+parseFloat(r.expenses||0)},0);
    $('an_total').textContent=Math.round(revenue).toLocaleString();
    $('an_expenses').textContent=Math.round(expenses).toLocaleString();
    $('an_profit').textContent=Math.round(revenue-expenses).toLocaleString();
    drawRevenueChart(records);
    $('analyticsHistory').innerHTML=records.slice(-10).reverse().map(function(r){
      return'<div class="item"><div class="item-title">'+esc(r.date)+' — '+esc(r.business)+'</div>'
        +'<div class="item-meta">Выручка: <b style="color:var(--green)">'+esc(String(r.revenue||0))+'</b> грн | Расходы: '+esc(String(r.expenses||0))+' | Клиентов: '+esc(String(r.clients||0))+(r.comment?' | '+esc(r.comment):'')+'</div></div>';
    }).join('')||'<div class="item-meta">Нет данных.</div>';
  });
}
function drawRevenueChart(records){
  var canvas=$('revenueChart');if(!canvas)return;
  var ctx=canvas.getContext('2d');
  canvas.width=canvas.parentElement.clientWidth||600;canvas.height=220;
  var days=[];for(var i=6;i>=0;i--){var d=new Date(Date.now()-i*864e5);days.push(d.toISOString().slice(0,10))}
  var vals=days.map(function(d){return records.filter(function(r){return r.date===d}).reduce(function(a,r){return a+parseFloat(r.revenue||0)},0)});
  var maxVal=Math.max.apply(null,vals)||1;
  var W=canvas.width,H=canvas.height,pad=40,barW=Math.floor((W-pad*2)/7)-8;
  ctx.clearRect(0,0,W,H);ctx.fillStyle='#0a1a22';ctx.fillRect(0,0,W,H);
  ctx.strokeStyle='#25414b';ctx.lineWidth=1;
  for(var g=0;g<=4;g++){var y=H-pad-(H-pad*2)*(g/4);ctx.beginPath();ctx.moveTo(pad,y);ctx.lineTo(W-20,y);ctx.stroke();ctx.fillStyle='#8da4ab';ctx.font='10px Inter,sans-serif';ctx.fillText(Math.round(maxVal*g/4),2,y+4)}
  days.forEach(function(d,i){
    var x=pad+i*(barW+8);var val=vals[i];var barH=((H-pad*2)*val/maxVal)||2;
    var grad=ctx.createLinearGradient(x,H-pad-barH,x,H-pad);grad.addColorStop(0,'#35d7e9');grad.addColorStop(1,'#48e08c');
    ctx.fillStyle=grad;ctx.beginPath();ctx.roundRect(x,H-pad-barH,barW,barH,4);ctx.fill();
    ctx.fillStyle='#8da4ab';ctx.font='10px Inter,sans-serif';ctx.fillText(d.slice(5),x,H-6);
    if(val>0){ctx.fillStyle='#eef8f9';ctx.fillText(Math.round(val),x,H-pad-barH-4)}
  });
}
function addAnalyticsRecord(){
  var data={date:$('an_date').value||new Date().toISOString().slice(0,10),business:$('an_business').value,revenue:parseFloat($('an_revenue').value||0),expenses:parseFloat($('an_expenses').value||0),clients:parseInt($('an_clients').value||0),comment:$('an_comment').value};
  api('/analytics',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})
  .then(function(d){alertMsg(d.success?'Запись добавлена':(d.error||'Ошибка'),!!d.success);if(d.success){$('an_revenue').value='';$('an_expenses').value='';$('an_clients').value='';$('an_comment').value='';loadAnalytics()}});
}
if(typeof CanvasRenderingContext2D!=='undefined'&&!CanvasRenderingContext2D.prototype.roundRect){CanvasRenderingContext2D.prototype.roundRect=function(x,y,w,h,r){this.moveTo(x+r,y);this.lineTo(x+w-r,y);this.quadraticCurveTo(x+w,y,x+w,y+r);this.lineTo(x+w,y+h-r);this.quadraticCurveTo(x+w,y+h,x+w-r,y+h);this.lineTo(x+r,y+h);this.quadraticCurveTo(x,y+h,x,y+h-r);this.lineTo(x,y+r);this.quadraticCurveTo(x,y,x+r,y);this.closePath();return this}}

// ── Email ─────────────────────────────────────────────────────────────────────
function loadEmails(){
  $('emailList').innerHTML='<div class="item-meta">Загрузка...</div>';
  api('/emails').then(function(d){
    var emails=d.emails||[];
    if(d.error){$('emailList').innerHTML='<div class="item-meta" style="color:var(--red)">'+esc(d.error)+'</div>';return}
    $('emailList').innerHTML=emails.map(function(e){
      var subj=esc(e.subject||''), frm=esc(e.from_addr||''), frname=esc(e.from_name||e.from_addr||'');
      return'<div class="item" style="cursor:pointer">'
        +'<div onclick="$(\'emailTo\').value=\''+frm+'\';$(\'emailSubject\').value=\'Re: '+subj+'\'">'
        +'<div class="item-title">'+esc(e.subject||'(без темы)')+'</div>'
        +'<div class="item-meta">'+frname+'</div>'
        +'<div style="color:var(--muted);font-size:11px">'+esc(e.date||'')+'</div>'
        +'</div>'
        +'<button class="btn secondary sm" style="margin-top:6px;font-size:11px" onclick="event.stopPropagation();aiReply(\''+subj+'\',\''+frm+'\',\'\')">🤖 AI ответ</button>'
        +'</div>';
    }).join('')||'<div class="item-meta">Писем нет.</div>';
  });
}
function sendEmail(){
  var to=$('emailTo').value.trim(),subject=$('emailSubject').value.trim(),body=$('emailBody').value.trim();
  if(!to||!body){alertMsg('Укажите получателя и текст',false);return}
  api('/send_email',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({to:to,subject:subject,body:body})})
  .then(function(d){alertMsg(d.success?'Письмо отправлено!':(d.error||'Ошибка'),!!d.success);if(d.success){$('emailTo').value='';$('emailSubject').value='';$('emailBody').value=''}});
}
function aiDraftEmail(){
  var to=$('emailTo').value.trim(),subject=$('emailSubject').value.trim();
  var prompt='Напиши деловое письмо'+(to?' для '+to:'')+(subject?' на тему: '+subject:'')+'. Кратко и профессионально.';
  api('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:prompt})})
  .then(function(d){$('emailBody').value=d.reply||''});
}
function aiReply(subject,fromAddr,snippet){
  $('emailTo').value=fromAddr;
  $('emailSubject').value=subject?'Re: '+subject:'';
  $('emailBody').value='Генерирую ответ...';
  api('/emails/reply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({subject:subject,from_addr:fromAddr,snippet:snippet})})
  .then(function(d){
    if(d.reply){$('emailBody').value=d.reply;showPage('emailPage')}
    else{$('emailBody').value='';alertMsg(d.error||'Ошибка генерации',false)}
  });
}

// ── Reminders ─────────────────────────────────────────────────────────────────
function loadReminders(){
  api('/reminders').then(function(d){
    var list=d.reminders||[];
    $('remindersList').innerHTML=list.map(function(r){
      return'<div class="item"><div class="item-title">'+esc(r.text)+'</div>'
        +'<div class="item-meta">'+esc(r.time||'')+(r.repeat!=='once'?' · '+esc(r.repeat):'')+'</div>'
        +'<div class="item-actions"><button class="btn danger sm" onclick="deleteReminder(\''+r.id+'\')">✕</button></div></div>';
    }).join('')||'<div class="item-meta">Нет напоминаний.</div>';
  });
}
function addReminder(){
  var text=($('remText').value||'').trim();if(!text){alertMsg('Укажите текст',false);return}
  api('/reminders',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:text,time:$('remTime').value,repeat:$('remRepeat').value})})
  .then(function(d){alertMsg(d.success?'Напоминание добавлено':(d.error||'Ошибка'),!!d.success);if(d.success){$('remText').value='';$('remTime').value='';loadReminders()}});
}
function deleteReminder(id){
  api('/reminders/'+id,{method:'DELETE',headers:{'Content-Type':'application/json'}}).then(function(){loadReminders()});
}

// ── Nova Poshta ───────────────────────────────────────────────────────────────
function trackNova(){
  var num=($('novaNumber').value||'').trim();if(!num){alertMsg('Введите номер ТТН',false);return}
  $('novaResult').innerHTML='<div class="item-meta">Отслеживаю...</div>';
  api('/nova_poshta/track?number='+encodeURIComponent(num)).then(function(d){
    if(!d.success){$('novaResult').innerHTML='<div class="item-meta" style="color:var(--red)">'+esc(d.error||'Ошибка')+'</div>';return}
    var docs=(d.data||[]);
    $('novaResult').innerHTML=docs.map(function(doc){
      return'<div class="card"><div class="item-title">'+esc(doc.Number||num)+'</div>'
        +'<div class="item-meta"><b style="color:var(--green)">'+esc(doc.Status||'')+'</b><br>'
        +esc(doc.StatusDescription||'')+'<br>'
        +(doc.RecipientFullName?'Получатель: '+esc(doc.RecipientFullName):'')+'</div></div>';
    }).join('')||'<div class="item-meta">Данных нет.</div>';
  });
}

// ── Agents ────────────────────────────────────────────────────────────────────
var BUILTIN_AGENTS=[
  {id:'catering',icon:'🍽️',name:'Общепит',color:'#f5bd63',
   desc:'Меню, закупки, персонал, касса, жалобы гостей',
   prompt:'Ты бизнес-ассистент для ресторана/кафе. Помогай с: меню, закупками продуктов, управлением персоналом, финансами заведения, жалобами гостей. Отвечай кратко и по делу.'},
  {id:'aqua',icon:'💧',name:'Аква бізнес',color:'#35d7e9',
   desc:'Продажи воды, оборудование, маршруты, клиенты',
   prompt:'Ты бизнес-ассистент для аква-бизнеса (доставка воды, кулеры). Помогай с: маршрутами доставки, клиентской базой, обслуживанием оборудования, продажами. Отвечай кратко.'},
  {id:'marketing',icon:'📣',name:'Маркетинг',color:'#48e08c',
   desc:'SMM, реклама, контент, аналитика, продвижение',
   prompt:'Ты маркетинговый ассистент. Помогай с: контент-планом для соцсетей, настройкой рекламы, анализом конкурентов, SEO, email-маркетингом, стратегией продвижения. Отвечай кратко.'},
];
function loadAgents(){
  $('builtinAgents').innerHTML=BUILTIN_AGENTS.map(function(a){
    return'<div class="card" style="border-top:3px solid '+a.color+'">'
      +'<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">'
      +'<span style="font-size:26px">'+a.icon+'</span>'
      +'<div><div class="item-title" style="font-size:15px">'+a.name+'</div>'
      +'<div class="item-meta">'+a.desc+'</div></div></div>'
      +'<button class="btn sm" style="width:100%;background:linear-gradient(135deg,'+a.color+','+a.color+'cc);color:#041014" '
      +'onclick="activateAgent(''+a.id+'')">💬 Чат с агентом</button>'
      +'</div>';
  }).join('');
  api('/agents').then(function(d){
    $('agentsList').innerHTML=(d.agents||[]).map(function(a){
      return'<div class="item" style="display:flex;justify-content:space-between;align-items:center">'
        +'<div><div class="item-title">'+esc(a.name)+'</div>'
        +'<div class="item-meta">'+esc(a.description||'')+'</div></div>'
        +'<button class="btn secondary sm" onclick="activateCustomAgent(''+esc(a.name)+'',''+esc(a.description||'')+'')">Чат</button>'
        +'</div>';
    }).join('')||'<div class="item-meta">Кастомных агентов нет.</div>';
  });
}
function activateAgent(id){
  var a=BUILTIN_AGENTS.find(function(x){return x.id===id});if(!a)return;
  activateCustomAgent(a.name,a.prompt);
}
function activateCustomAgent(name,prompt){
  var intro='[Агент: '+name+'] '+prompt+'\n\nЧем могу помочь?';
  messages=[{role:'assistant',content:intro}];
  renderMessages();showPage('chat');
  $('chatInput').placeholder='Чат с агентом: '+name+' (напишите что угодно...)';
}
function createAgent(){
  var name=($('agentName').value||'').trim(),desc=($('agentDesc').value||'').trim();if(!name)return;
  api('/agents',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,description:desc})})
  .then(function(d){alertMsg(d.success?'Агент создан':(d.error||'Ошибка'),!!d.success);if(d.success){$('agentName').value='';$('agentDesc').value='';loadAgents()}});
}

// ── Settings ──────────────────────────────────────────────────────────────────
function statusBadge(s){if(s==='configured'||s==='done')return'<span class="badge done2">✓ '+esc(s)+'</span>';if(s==='planned')return'<span class="badge planned">— '+esc(s)+'</span>';return'<span class="badge warn">! '+esc(s)+'</span>'}
function loadSettings(){
  api('/capabilities').then(function(d){
    var intg=d.integrations||{};
    $('integrationsList').innerHTML=Object.keys(intg).map(function(k){return'<div class="item"><div class="item-title">'+esc(k)+'</div>'+statusBadge(intg[k].status)+'</div>'}).join('');
    var caps=d.capabilities||{};
    $('capabilitiesList').innerHTML=Object.keys(caps).map(function(k){return'<div class="item"><div class="item-title">'+esc(k)+'</div>'+statusBadge(caps[k])+'</div>'}).join('');
  });
  api('/users').then(function(d){
    $('usersList').innerHTML=(d.users||[]).map(function(u){return'<div class="item"><div class="item-title">'+esc(u.username)+'</div><div class="item-meta">'+esc(u.role)+'</div></div>'}).join('');
  });
  load2FAStatus();
}
// ── 2FA ───────────────────────────────────────────────────────────────────────
function load2FAStatus(){
  api('/2fa/status').then(function(d){
    var enabled=d.enabled;
    $('twoFaStatus').textContent=enabled?'✅ 2FA увімкнено':'⚠️ 2FA вимкнено — рекомендується увімкнути.';
    $('disable2faBtn').style.display=enabled?'':'none';
    $('twoFaSetup').style.display='none';
  });
}
function setup2FA(){
  api('/2fa/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})})
  .then(function(d){
    if(d.qr_url){$('twoFaQr').src=d.qr_url;$('twoFaKey').textContent=d.secret;$('twoFaSetup').style.display=''}
    else alertMsg(d.error||'Помилка',false);
  });
}
function confirm2FA(){
  var code=($('twoFaCode').value||'').replace(/\s/g,'');if(code.length!==6){alertMsg('Введіть 6 цифр',false);return;}
  api('/2fa/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:code})})
  .then(function(d){alertMsg(d.success?'2FA увімкнено!':(d.error||'Невірний код'),!!d.success);if(d.success)load2FAStatus();});
}
function disable2FA(){
  if(!confirm('Вимкнути 2FA?'))return;
  api('/2fa/disable',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})})
  .then(function(d){alertMsg(d.success?'2FA вимкнено':(d.error||'Помилка'),!!d.success);load2FAStatus();});
}
function saveUser(){
  var un=($('newUsername').value||'').trim(),role=$('newUserRole').value,pw=$('newUserPassword').value;if(!un)return;
  api('/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:un,role:role,password:pw})})
  .then(function(d){alertMsg(d.success?'Пользователь сохранён':(d.error||'Ошибка'),!!d.success);loadSettings()});
}

// ── Command Palette ───────────────────────────────────────────────────────────
var PAL_CMDS=[
  {icon:'📊',label:'Dashboard',desc:'Перейти на главную',action:function(){showPage('dashboard');closePal()}},
  {icon:'💬',label:'Чат',desc:'Открыть чат с NEXUS',action:function(){showPage('chat');closePal()}},
  {icon:'✅',label:'Задачи',desc:'Управление задачами',action:function(){showPage('tasksPage');closePal()}},
  {icon:'👥',label:'CRM',desc:'База клиентов',action:function(){showPage('crmPage');closePal()}},
  {icon:'📈',label:'Аналитика',desc:'Выручка и расходы',action:function(){showPage('analyticsPage');closePal()}},
  {icon:'✉️',label:'Email',desc:'Почта',action:function(){showPage('emailPage');closePal()}},
  {icon:'🔔',label:'Напоминания',desc:'Добавить или посмотреть',action:function(){showPage('remindersPage');closePal()}},
  {icon:'📦',label:'Нова Пошта',desc:'Трекинг ТТН',action:function(){showPage('novaPage');closePal()}},
  {icon:'🌅',label:'Брифинг',desc:'Собрать утренний брифинг',action:function(){showPage('dashboard');closePal();loadBriefing()}},
  {icon:'↓',label:'Экспорт чата',desc:'Скачать историю',action:function(){exportChat();closePal()}},
  {icon:'🚪',label:'Выйти',desc:'Выход из системы',action:function(){location.href='/logout'}},
];
var palIdx=0,palFiltered=PAL_CMDS.slice();
function openPalette(){$('palette').classList.add('open');$('palInput').value='';palFiltered=PAL_CMDS.slice();renderPalette();$('palInput').focus()}
function closePal(){$('palette').classList.remove('open')}
function closePalette(e){if(e.target===$('palette'))closePal()}
function renderPalette(){
  $('palResults').innerHTML=palFiltered.map(function(c,i){
    return'<div class="pal-item'+(i===palIdx?' pal-active':'')+'" onclick="palRun('+i+')">'
      +'<div class="pal-icon">'+c.icon+'</div><div><div>'+esc(c.label)+'</div><div class="pal-desc">'+esc(c.desc)+'</div></div></div>';
  }).join('');
}
function filterPalette(){
  var q=$('palInput').value.toLowerCase();palIdx=0;
  palFiltered=PAL_CMDS.filter(function(c){return c.label.toLowerCase().indexOf(q)>=0||c.desc.toLowerCase().indexOf(q)>=0});
  renderPalette();
}
function palKey(e){
  if(e.key==='Escape'){closePal();return}
  if(e.key==='ArrowDown'){palIdx=Math.min(palIdx+1,palFiltered.length-1);renderPalette()}
  if(e.key==='ArrowUp'){palIdx=Math.max(palIdx-1,0);renderPalette()}
  if(e.key==='Enter'&&palFiltered[palIdx])palRun(palIdx);
}
function palRun(i){if(palFiltered[i])palFiltered[i].action()}

// ── Keyboard shortcuts ────────────────────────────────────────────────────────
document.addEventListener('keydown',function(e){
  if((e.metaKey||e.ctrlKey)&&e.key==='k'){e.preventDefault();openPalette();return}
  if(e.key==='Escape')closePal();
  if((e.ctrlKey)&&e.key==='e'){e.preventDefault();exportChat()}
});

// ── Profile ───────────────────────────────────────────────────────────────────
function loadProfile(){
  api('/profile').then(function(d){
    var p=d.profile||{};
    if($('profName'))$('profName').value=p.name||'';
    if($('profBusiness'))$('profBusiness').value=p.business||'';
    if($('profCity'))$('profCity').value=p.city||p.location||'';
    if($('profLocation'))$('profLocation').value=p.location||'';
  });
}
function saveProfile(){
  var data={name:$('profName').value.trim(),business:$('profBusiness').value.trim(),city:$('profCity').value.trim(),location:$('profLocation').value.trim()};
  api('/profile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})
  .then(function(d){alertMsg(d.success?'Профиль сохранён!':(d.error||'Ошибка'),!!d.success)});
}

// ── PWA Install ───────────────────────────────────────────────────────────────
var pwaPrompt=null;
window.addEventListener('beforeinstallprompt',function(e){
  e.preventDefault();pwaPrompt=e;
  var btn=$('pwaInstallBtn');if(btn)btn.style.display='';
  var s=$('pwaStatus');if(s)s.textContent='Приложение готово к установке.';
});
window.addEventListener('appinstalled',function(){
  var s=$('pwaStatus');if(s)s.textContent='✅ NEXUS установлен!';
  var btn=$('pwaInstallBtn');if(btn)btn.style.display='none';
});
function installPWA(){
  if(!pwaPrompt){alertMsg('Используй меню браузера → "Установить приложение"',true);return}
  pwaPrompt.prompt();
  pwaPrompt.userChoice.then(function(r){
    if(r.outcome==='accepted'){var s=$('pwaStatus');if(s)s.textContent='✅ Установка запущена!'}
    pwaPrompt=null;
  });
}
// Register service worker
if('serviceWorker' in navigator){
  navigator.serviceWorker.register('/sw.js',{scope:'/'}).catch(function(){});
}

// ── Monobank ──────────────────────────────────────────────────────────────────
function loadMono(){
  $('monoTxList').innerHTML='<div class="item-meta">Завантаження...</div>';
  api('/monobank').then(function(d){
    $('monoTokenStatus').innerHTML=d.token_ok
      ?'<span style="color:var(--green)">✅ Токен активний · '+esc(d.name||'')+'</span>'
      :'<span style="color:var(--amber)">⚠ Токен не задано — додайте MONOBANK_TOKEN в .env</span>';
    if(d.error&&!d.token_ok){
      $('monoTxList').innerHTML='<div class="alert err">'+esc(d.error)+'</div>';
      $('monoBalance').textContent='—';$('monoIncome').textContent='—';$('monoExpense').textContent='—';return;
    }
    var fmt=function(k){return k!=null?(k/100).toLocaleString('uk-UA',{minimumFractionDigits:2}):' — ';};
    $('monoBalance').textContent=fmt(d.balance);
    $('monoIncome').textContent=d.income?'+'+fmt(d.income):'—';
    $('monoExpense').textContent=d.expense?fmt(d.expense):'—';
    var txs=d.transactions||[];
    if(!txs.length){$('monoTxList').innerHTML='<div class="item-meta">Транзакцій немає. Можливо, лімiт API — спробуйте пізніше.</div>';
      $('monoSyncTime').textContent='Оновлено: '+new Date().toLocaleTimeString('uk-UA');return;}
    $('monoTxList').innerHTML=txs.map(function(t){
      var amt=t.amount/100,sign=amt>=0?'+':'',col=amt>=0?'var(--green)':'var(--red)';
      var dt=new Date(t.time*1000).toLocaleDateString('uk-UA');
      var bal=t.balance!=null?' · залишок: '+(t.balance/100).toFixed(2):'';
      return'<div class="item">'
        +'<div style="display:flex;justify-content:space-between;align-items:center">'
        +'<span class="item-title" style="font-size:13px">'+esc(t.description||('MCC '+t.mcc)||'Транзакція')+'</span>'
        +'<b style="color:'+col+';white-space:nowrap;margin-left:12px">'+sign+amt.toFixed(2)+' грн</b>'
        +'</div>'
        +'<div class="item-meta">'+dt+esc(bal)+'</div>'
        +'</div>';
    }).join('');
    $('monoSyncTime').textContent='Оновлено: '+new Date().toLocaleTimeString('uk-UA');
  });
}

// ── Calendar ──────────────────────────────────────────────────────────────────
function loadCalendar(){
  var today=new Date();
  $('calTodayDate').textContent=today.toLocaleDateString('uk-UA',{weekday:'long',day:'numeric',month:'long'});
  if($('calDate')&&!$('calDate').value)$('calDate').value=today.toISOString().slice(0,10);
  api('/calendar').then(function(d){
    var events=d.events||[];
    var todayStr=today.toISOString().slice(0,10);
    var todayEvs=events.filter(function(e){return e.date===todayStr});
    var upcoming=events.filter(function(e){return e.date>todayStr}).slice(0,10);
    $('calTodayEvents').innerHTML=todayEvs.length?todayEvs.map(calEventHTML).join(''):'<div class="item-meta">Сьогодні подій немає</div>';
    $('calUpcoming').innerHTML=upcoming.length?upcoming.map(calEventHTML).join(''):'<div class="item-meta">Найближчих подій немає</div>';
  });
}
function calEventHTML(e){
  return'<div class="item"><div style="display:flex;justify-content:space-between;align-items:center"><div><div class="item-title">'+esc(e.title)+'</div>'+(e.desc?'<div class="item-meta">'+esc(e.desc)+'</div>':'')+'</div><div style="text-align:right"><div class="badge open">'+esc(e.date)+'</div>'+(e.time?'<div class="item-meta">'+esc(e.time)+'</div>':'')+'</div></div>'
    +'<div class="item-actions"><button class="btn danger sm" onclick="deleteCalEvent(\''+e.id+'\')">✕</button></div></div>';
}
function addCalEvent(){
  var title=($('calTitle').value||'').trim();var date=$('calDate').value;var time=$('calTime').value;var desc=($('calDesc').value||'').trim();
  if(!title||!date)return alertMsg('Заповніть назву та дату',false);
  api('/calendar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:title,date:date,time:time,desc:desc})})
  .then(function(d){if(d.success){$('calTitle').value='';$('calDesc').value='';alertMsg('Подія додана!',true);loadCalendar()}else alertMsg(d.error||'Помилка',false)});
}
function deleteCalEvent(id){
  api('/calendar/'+id,{method:'DELETE',headers:{'Content-Type':'application/json'}})
  .then(function(){loadCalendar()});
}

// ── Audit log ─────────────────────────────────────────────────────────────────
var allAuditRows=[];
function loadAudit(){
  api('/audit').then(function(d){
    allAuditRows=d.entries||[];filterAudit();
  });
}
function filterAudit(){
  var q=($('auditSearch')?.value||'').toLowerCase();
  var t=($('auditType')?.value||'');
  var rows=allAuditRows.filter(function(r){
    var matchQ=!q||(r.action||'').toLowerCase().indexOf(q)>=0||(r.user||'').toLowerCase().indexOf(q)>=0||(r.detail||'').toLowerCase().indexOf(q)>=0;
    var matchT=!t||r.action.indexOf(t)>=0;
    return matchQ&&matchT;
  });
  if(!rows.length){$('auditList').innerHTML='<div class="item-meta">Записів немає</div>';return}
  $('auditList').innerHTML='<table class="tbl"><thead><tr><th>Час</th><th>Дія</th><th>Користувач</th><th>Деталі</th></tr></thead><tbody>'
    +rows.slice(-200).reverse().map(function(r){
      return'<tr><td style="white-space:nowrap">'+esc((r.ts||'').replace('T',' ').slice(0,19))+'</td><td><span class="badge open">'+esc(r.action||'')+'</span></td><td>'+esc(r.user||'—')+'</td><td class="item-meta">'+esc(r.detail||'')+'</td></tr>';
    }).join('')+'</tbody></table>';
}

// ── Theme ─────────────────────────────────────────────────────────────────────
var THEMES={
  dark:{bg:'#061014',side:'#08141a',panel:'#101d25',line:'#25414b',text:'#eef8f9',muted:'#8da4ab',cyan:'#35d7e9',green:'#48e08c'},
  light:{bg:'#f0f4f6',side:'#e2eaee',panel:'#ffffff',line:'#c8d8de',text:'#0a1e28',muted:'#5a7a88',cyan:'#0099b0',green:'#1aaa60'},
  midnight:{bg:'#040810',side:'#060c18',panel:'#0a1228',line:'#1a2d50',text:'#dde8ff',muted:'#6a82aa',cyan:'#4a90ff',green:'#44d8a0'}
};
function setTheme(name){
  var t=THEMES[name]||THEMES.dark;
  var r=document.documentElement.style;
  Object.entries(t).forEach(function(kv){r.setProperty('--'+kv[0],kv[1])});
  localStorage.setItem('nexus_theme',name);
  ['themeDark','themeLight','themeMidnight'].forEach(function(id){var b=$(id);if(b)b.classList.remove('btn')});
  var active=$('theme'+name.charAt(0).toUpperCase()+name.slice(1));if(active)active.classList.add('btn');
}
(function(){var t=localStorage.getItem('nexus_theme');if(t)setTheme(t)})();

// ── Browser notifications ─────────────────────────────────────────────────────
function updateNotifStatus(){
  var s=$('notifStatus');if(!s)return;
  if(!('Notification' in window)){s.textContent='Браузер не підтримує сповіщення';return}
  s.textContent={'granted':'✅ Сповіщення дозволені','denied':'❌ Сповіщення заблоковані','default':'⚠ Дозвіл не надано'}[Notification.permission]||'';
}
function requestNotifPermission(){
  if(!('Notification' in window)){alertMsg('Браузер не підтримує сповіщення',false);return}
  Notification.requestPermission().then(function(r){
    updateNotifStatus();
    if(r==='granted')alertMsg('✅ Сповіщення увімкнено!',true);
    else alertMsg('Сповіщення не дозволені. Дозвольте в налаштуваннях браузера.',false);
  });
}
function showNotif(title,body){
  if(Notification.permission==='granted')new Notification(title,{body:body,icon:'/static/icon-192.png'});
}
// Poll reminders every 60s for browser notifications
setInterval(function(){
  if(Notification.permission!=='granted')return;
  api('/reminders').then(function(d){
    var now=new Date();var nowISO=now.toISOString().slice(0,16);
    (d.reminders||[]).forEach(function(r){
      if(r.time&&r.time.slice(0,16)===nowISO&&!r.notified){
        showNotif('⏰ NEXUS: '+r.text,'Нагадування');
      }
    });
  });
},60000);

// ── Init ──────────────────────────────────────────────────────────────────────
loadStats();loadHistory();loadDashboard();
updateNotifStatus();
var an_dateEl=$('an_date');if(an_dateEl)an_dateEl.value=new Date().toISOString().slice(0,10);
setInterval(loadStats,10000);
</script>
</body></html>"""


# ── Integration helpers ───────────────────────────────────────────────────────

def integration_status():
    result = {}
    for name, marker in state.get("integrations", {}).items():
        if marker == "planned":
            result[name] = {"status": "planned"}
        elif marker.startswith("env:"):
            env_names = marker[4:].split("+")
            configured = all(bool(get_env(e, "").strip()) for e in env_names)
            result[name] = {"status": "configured" if configured else "missing_env", "env": env_names}
        else:
            result[name] = {"status": "unknown"}
    return result

def storage_status():
    db = get_env("DATABASE_URL", "").strip()
    if db.startswith(("postgres://", "postgresql://")):
        return {"backend": "postgresql_ready", "configured": True, "active": False}
    return {"backend": "json_files", "configured": False, "active": True, "note": "Используются JSON файлы."}

def capability_map():
    return {
        "text_chat": "done", "streaming": "done", "markdown": "done",
        "voice_input": "browser_basic", "wake_word": "browser_experimental",
        "history_search": "done", "message_editing": "done",
        "file_upload": "done", "chat_export": "done",
        "csrf": "done", "rate_limit": "done", "security_headers": "done",
        "login_lockout": "done",
        "2fa_totp": "done" if _totp_enabled() else "available",
        "multi_user": "done", "roles": "done",
        "tasks_crud": "done", "task_priority": "done",
        "crm": "done", "crm_notes": "done",
        "analytics_chart": "done",
        "email_inbox": "env_required", "email_send": "env_required",
        "ai_email_reply": "done",
        "reminders": "done",
        "nova_poshta": "env_required",
        "weather_briefing": "env_required",
        "invoice_pdf": "done",
        "business_agents": "template",
        "command_palette": "done",
        "theme_switcher": "done", "browser_notifications": "done",
        "mobile_pwa": "done", "mobile_fab": "done",
        "audit_log": "done", "monobank": "env_required",
        "calendar": "done",
        "rag_vector_search": "done",
        "postgresql": "planned", "google_maps": "planned", "whatsapp_bot": "planned",
    }

# ── Auth routes ───────────────────────────────────────────────────────────────

def _totp_enabled() -> bool:
    return bool(state.get("totp_secret") and state.get("totp_enabled"))

def _totp_verify(code: str) -> bool:
    secret = state.get("totp_secret", "")
    if not secret:
        return False
    try:
        import pyotp
        return pyotp.TOTP(secret).verify(str(code).strip(), valid_window=1)
    except Exception:
        return False

@app.route("/login", methods=["GET", "POST"])
def login():
    ip = request.remote_addr or "unknown"
    error = ""
    locked_secs = _lockout_check(ip)
    if locked_secs > 0:
        mins = locked_secs // 60 + 1
        error = f"Слишком много попыток. Повторите через ~{mins} мин."
        return render_template_string(LOGIN_HTML, error=error, totp_required=False, username="", password_hash="")

    # ── 2FA step 2: TOTP code verification ────────────────────────────────────
    if request.method == "POST" and request.form.get("totp_step"):
        p_username  = request.form.get("username", "")
        p_hash      = request.form.get("password_hash", "")
        totp_code   = request.form.get("totp_code", "")
        # Verify that the stored hash matches (prevent tampering)
        import hashlib
        expected = hashlib.sha256(f"{p_username}{require_web_password()}".encode()).hexdigest()[:16]
        if p_hash != expected:
            return render_template_string(LOGIN_HTML, error="Сессия истекла.", totp_required=False, username="", password_hash="")
        if _totp_verify(totp_code):
            _lockout_success(ip)
            session["logged_in"] = True
            session["username"] = p_username
            session["role"] = "admin"
            audit("login_2fa", "success", user=p_username)
            return redirect(url_for("index"))
        else:
            _lockout_fail(ip)
            audit("login_fail_2fa", f"ip={ip}", user=p_username)
            return render_template_string(LOGIN_HTML, error="Невірний код 2FA.", totp_required=True,
                                          username=p_username, password_hash=expected)

    # ── Step 1: password check ─────────────────────────────────────────────────
    if request.method == "POST":
        username = request.form.get("username", "admin").strip() or "admin"
        password = request.form.get("password", "")
        pw_ok = False
        role  = "admin"
        if password == require_web_password():
            pw_ok = True
        else:
            for user in state.get("users", []):
                if user.get("username") == username and user.get("password") == password:
                    pw_ok = True
                    role  = user.get("role", "guest")
                    break

        if pw_ok:
            if _totp_enabled():
                import hashlib
                ph = hashlib.sha256(f"{username}{require_web_password()}".encode()).hexdigest()[:16]
                return render_template_string(LOGIN_HTML, error="", totp_required=True, username=username, password_hash=ph)
            _lockout_success(ip)
            session["logged_in"] = True
            session["username"] = username
            session["role"] = role
            audit("login", f"success role={role}", user=username)
            return redirect(url_for("index"))

        _lockout_fail(ip)
        audit("login_fail", f"username={username} ip={ip}", user=username)
        remaining = _lockout_check(ip)
        if remaining > 0:
            mins = remaining // 60 + 1
            error = f"Аккаунт заблокирован на ~{mins} мин. из-за множества попыток."
        else:
            entry = _FAILED.get(ip, {})
            left = _MAX_FAILS - entry.get("count", 0)
            error = f"Неверный пароль. Осталось попыток: {max(left,0)}."
    return render_template_string(LOGIN_HTML, error=error, totp_required=False, username="", password_hash="")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ── 2FA management ────────────────────────────────────────────────────────────

@app.route("/2fa/status")
def totp_status():
    if not logged_in(): return jsonify({"error": "Нужен вход."}), 401
    return jsonify({"enabled": _totp_enabled()})

@app.route("/2fa/setup", methods=["POST"])
def totp_setup():
    if not logged_in(): return jsonify({"error": "Нужен вход."}), 401
    try:
        import pyotp, qrcode, io, base64
        secret = pyotp.random_base32()
        session["totp_pending_secret"] = secret
        name = get_env("TELEGRAM_ALLOWED_USER_IDS", "Nikita").split(",")[0].strip() or "admin"
        uri = pyotp.totp.TOTP(secret).provisioning_uri(name=f"NEXUS/{name}", issuer_name="NEXUS")
        img = qrcode.make(uri)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qr_b64 = base64.b64encode(buf.getvalue()).decode()
        return jsonify({"secret": secret, "qr_url": f"data:image/png;base64,{qr_b64}"})
    except ImportError:
        return jsonify({"error": "Встановіть pyotp та qrcode: pip install pyotp qrcode[pil]"})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/2fa/confirm", methods=["POST"])
def totp_confirm():
    if not logged_in(): return jsonify({"error": "Нужен вход."}), 401
    secret = session.get("totp_pending_secret", "")
    if not secret:
        return jsonify({"success": False, "error": "Спочатку викличте /2fa/setup"})
    data = request.get_json(silent=True) or {}
    code = str(data.get("code", "")).strip()
    try:
        import pyotp
        if pyotp.TOTP(secret).verify(code, valid_window=1):
            state["totp_secret"] = secret
            state["totp_enabled"] = True
            persist_state()
            session.pop("totp_pending_secret", None)
            audit("2fa_enabled", "TOTP enabled", user=session.get("username", "?"))
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "Невірний код, спробуйте ще раз"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/2fa/disable", methods=["POST"])
def totp_disable():
    if not logged_in(): return jsonify({"error": "Нужен вход."}), 401
    state.pop("totp_secret", None)
    state["totp_enabled"] = False
    persist_state()
    audit("2fa_disabled", "TOTP disabled", user=session.get("username", "?"))
    return jsonify({"success": True})

@app.route("/")
def index():
    if not logged_in():
        return redirect(url_for("login"))
    return render_template_string(HTML, email=get_env("GMAIL", ""), csrf_token=get_csrf_token())

# ── Chat ──────────────────────────────────────────────────────────────────────

@app.route("/chat", methods=["POST"])
def chat():
    if not logged_in(): return jsonify({"reply": "Нужен вход."}), 401
    data = request.get_json(silent=True) or {}
    answer = handle_chat(data, stream=False)
    return jsonify({"reply": answer, "audio": make_audio(answer) if data.get("voice") else None})

@app.route("/chat_stream", methods=["POST"])
def chat_stream():
    if not logged_in(): return jsonify({"reply": "Нужен вход."}), 401
    data = request.get_json(silent=True) or {}
    def generate():
        answer = handle_chat(data, stream=False)
        for char in answer:
            yield "data: " + json.dumps({"token": char}, ensure_ascii=False) + "\n\n"
        if data.get("voice"):
            audio = make_audio(answer)
            if audio:
                yield "data: " + json.dumps({"audio": audio}, ensure_ascii=False) + "\n\n"
    return Response(generate(), mimetype="text/event-stream")

@app.route("/history")
def get_history():
    if not logged_in(): return jsonify({"messages": []}), 401
    return jsonify({"messages": public_history()})

@app.route("/clear_history", methods=["POST"])
def clear_history():
    if not logged_in(): return jsonify({"success": False}), 401
    history.clear()
    persist_history()
    return jsonify({"success": True})

def handle_chat(data, stream=False):
    msg = (data.get("message") or "").strip()
    if not msg: return "Напишите сообщение."
    edit_id = data.get("edit_id")
    if edit_id:
        for item in history:
            if item.get("id") == edit_id and item.get("role") == "user":
                item["content"] = msg; stats["edits"] += 1; break
    else:
        history.append({"id": message_id(), "role": "user", "content": msg})
    stats["messages"] += 1
    if data.get("voice"): stats["voice"] += 1
    # RAG: inject relevant document context into the system prompt
    rag_extra = rag_context(msg)
    system_with_rag = SYSTEM + rag_extra if rag_extra else SYSTEM
    try:
        answer = ask_ai([{"role": "system", "content": system_with_rag}, *history[-20:]])
    except Exception as exc:
        answer = "Ошибка AI: " + str(exc)
    history.append({"id": message_id(), "role": "assistant", "content": answer})
    persist_history()
    audit("chat", f"q={msg[:80]}", user=session.get("username", "user"))
    return answer

# ── Tasks ─────────────────────────────────────────────────────────────────────

@app.route("/tasks", methods=["GET", "POST"])
def tasks():
    guard = require_roles("admin", "employee")
    if guard: return guard
    if request.method == "GET":
        return jsonify({"tasks": state.get("tasks", [])})
    data = request.get_json(silent=True) or {}
    title = str(data.get("title", "")).strip()
    if not title: return jsonify({"success": False, "error": "Укажите задачу."})
    task = {
        "id": message_id(), "title": title,
        "status": "open", "priority": data.get("priority", "normal"),
        "owner": session.get("username", "admin"),
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    state.setdefault("tasks", []).append(task)
    persist_state()
    return jsonify({"success": True, "task": task})

@app.route("/tasks/<task_id>", methods=["PATCH", "DELETE"])
def task_item(task_id):
    guard = require_roles("admin", "employee")
    if guard: return guard
    tasks_list = state.get("tasks", [])
    task = next((t for t in tasks_list if t.get("id") == task_id), None)
    if not task: return jsonify({"success": False, "error": "Задача не найдена."})
    if request.method == "DELETE":
        state["tasks"] = [t for t in tasks_list if t.get("id") != task_id]
        persist_state()
        return jsonify({"success": True})
    data = request.get_json(silent=True) or {}
    if data.get("action") == "toggle":
        task["status"] = "open" if task.get("status") == "done" else "done"
    if "title" in data: task["title"] = str(data["title"]).strip()
    if "priority" in data: task["priority"] = str(data["priority"])
    persist_state()
    return jsonify({"success": True, "task": task})

# ── Command ───────────────────────────────────────────────────────────────────

@app.route("/command", methods=["POST"])
def command():
    guard = require_roles("admin", "employee")
    if guard: return guard
    data = request.get_json(silent=True) or {}
    text = str(data.get("command", "")).strip()
    if not text: return jsonify({"success": False, "error": "Команда пустая."})
    stats["commands"] += 1
    lower = text.lower()

    # Task creation
    if any(lower.startswith(p) for p in ["добавь задачу", "создай задачу", "добавить задачу"]):
        title = text.split(" ", 2)[-1].strip()
        task = {"id": message_id(), "title": title, "status": "open", "priority": "normal",
                "owner": session.get("username", "admin"), "created_at": datetime.utcnow().isoformat() + "Z"}
        state.setdefault("tasks", []).append(task)
        persist_state()
        return jsonify({"success": True, "action": "task_created", "task": task, "reply": f"✅ Задача добавлена: {title}"})

    # Reminder
    if "напомни" in lower or "напоминание" in lower:
        return jsonify({"success": True, "action": "reminder_hint", "reply": "Перейдите в раздел Напоминания, чтобы добавить напоминание."})


    # Briefing
    if "брифинг" in lower:
        return jsonify({"success": True, "action": "briefing_hint", "reply": "Перейдите на Dashboard и нажмите «Собрать» для утреннего брифинга."})

    # CRM
    if "добавь клиента" in lower or "новый клиент" in lower:
        return jsonify({"success": True, "action": "crm_hint", "reply": "Перейдите в раздел CRM, чтобы добавить клиента."})

    # Fallback to AI
    answer = handle_chat({"message": text}, stream=False)
    return jsonify({"success": True, "action": "chat", "reply": answer})

# ── CRM ───────────────────────────────────────────────────────────────────────

def load_crm():
    data = load_json(CRM_FILE, {"clients": []})
    if isinstance(data, list): data = {"clients": data}
    return data

def save_crm(data): save_json(CRM_FILE, data)

@app.route("/crm", methods=["GET", "POST"])
def crm():
    guard = require_roles("admin", "employee")
    if guard: return guard
    db = load_crm()
    if request.method == "GET":
        return jsonify({"clients": db.get("clients", [])})
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name: return jsonify({"success": False, "error": "Укажите имя клиента."})
    client = {
        "id": message_id(),
        "name": name,
        "phone": str(data.get("phone", "")).strip(),
        "business": str(data.get("business", "other")),
        "notes": [],
        "created_at": datetime.utcnow().strftime("%d.%m.%Y"),
    }
    if data.get("note"):
        client["notes"].append({"text": str(data["note"]).strip(), "ts": datetime.utcnow().strftime("%d.%m.%Y %H:%M")})
    db.setdefault("clients", []).append(client)
    save_crm(db)
    audit("crm_add", f"client={name}", user=current_user()["username"])
    return jsonify({"success": True, "client": client})

@app.route("/crm/note", methods=["POST"])
def crm_note():
    guard = require_roles("admin", "employee")
    if guard: return guard
    data = request.get_json(silent=True) or {}
    client_id = str(data.get("client_id", ""))
    note_text = str(data.get("note", "")).strip()
    if not note_text: return jsonify({"success": False, "error": "Пустая заметка."})
    db = load_crm()
    client = next((c for c in db.get("clients", []) if c.get("id") == client_id), None)
    if not client: return jsonify({"success": False, "error": "Клиент не найден."})
    client.setdefault("notes", []).append({
        "text": note_text, "ts": datetime.utcnow().strftime("%d.%m.%Y %H:%M"),
        "author": session.get("username", "admin"),
    })
    save_crm(db)
    return jsonify({"success": True})

# ── Analytics ─────────────────────────────────────────────────────────────────

def load_analytics_data():
    raw = load_json(ANALYTICS_FILE, {})
    if isinstance(raw, dict) and "records" not in raw:
        records = []
        for biz, entries in raw.items():
            if isinstance(entries, list):
                for e in entries:
                    e = dict(e); e["business"] = biz
                    records.append(e)
        return records
    return raw.get("records", []) if isinstance(raw, dict) else []

def save_analytics_data(records):
    save_json(ANALYTICS_FILE, {"records": records})

@app.route("/analytics", methods=["GET", "POST"])
def analytics():
    guard = require_roles("admin", "employee")
    if guard: return guard
    if request.method == "GET":
        return jsonify({"records": load_analytics_data()})
    data = request.get_json(silent=True) or {}
    records = load_analytics_data()
    records.append({
        "date": data.get("date") or datetime.utcnow().strftime("%Y-%m-%d"),
        "business": str(data.get("business", "obshchepit")),
        "revenue": float(data.get("revenue") or 0),
        "expenses": float(data.get("expenses") or 0),
        "clients": int(data.get("clients") or 0),
        "comment": str(data.get("comment") or ""),
    })
    save_analytics_data(records)
    return jsonify({"success": True})

# ── Reminders ─────────────────────────────────────────────────────────────────

def load_reminders_data(): return load_json(REMINDERS_FILE, {"reminders": []})
def save_reminders_data(data): save_json(REMINDERS_FILE, data)

@app.route("/reminders", methods=["GET", "POST"])
def reminders_route():
    guard = require_roles("admin", "employee")
    if guard: return guard
    db = load_reminders_data()
    if request.method == "GET":
        return jsonify({"reminders": db.get("reminders", [])})
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", "")).strip()
    if not text: return jsonify({"success": False, "error": "Пустое напоминание."})
    reminder = {
        "id": message_id(), "text": text,
        "time": str(data.get("time") or ""),
        "repeat": str(data.get("repeat") or "once"),
        "created_by": session.get("username", "admin"),
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    db.setdefault("reminders", []).append(reminder)
    save_reminders_data(db)
    audit("reminder_add", text[:60], user=current_user()["username"])
    return jsonify({"success": True, "reminder": reminder})

@app.route("/reminders/<rem_id>", methods=["DELETE"])
def delete_reminder(rem_id):
    guard = require_roles("admin", "employee")
    if guard: return guard
    db = load_reminders_data()
    db["reminders"] = [r for r in db.get("reminders", []) if r.get("id") != rem_id]
    save_reminders_data(db)
    return jsonify({"success": True})

# ── Email ─────────────────────────────────────────────────────────────────────

def decode_mime_header(value):
    parts = decode_header(value or "")
    out = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            out += part.decode(enc or "utf-8", errors="replace")
        else:
            out += str(part)
    return out

@app.route("/emails")
def get_emails():
    if not logged_in(): return jsonify({"error": "Нужен вход."}), 401
    gmail = get_env("GMAIL", "").strip()
    app_password = get_env("APP_PASSWORD", "").strip()
    if not gmail or not app_password:
        return jsonify({"error": "GMAIL и APP_PASSWORD не настроены в .env", "emails": []})
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(gmail, app_password)
        mail.select("INBOX")
        _, data = mail.search(None, "ALL")
        ids = data[0].split()[-20:]
        emails = []
        for eid in reversed(ids):
            _, msg_data = mail.fetch(eid, "(RFC822)")
            raw = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw)
            subject = decode_mime_header(msg.get("Subject", ""))
            from_raw = decode_mime_header(msg.get("From", ""))
            date_raw = msg.get("Date", "")
            from_name, from_addr = "", from_raw
            if "<" in from_raw:
                parts = from_raw.split("<")
                from_name = parts[0].strip().strip('"')
                from_addr = parts[1].rstrip(">").strip()
            emails.append({"subject": subject, "from_name": from_name, "from_addr": from_addr, "date": date_raw[:25]})
        mail.logout()
        return jsonify({"emails": emails})
    except Exception as e:
        return jsonify({"error": str(e), "emails": []})

@app.route("/send_email", methods=["POST"])
def send_email():
    if not logged_in(): return jsonify({"success": False}), 401
    gmail = get_env("GMAIL", "").strip()
    app_password = get_env("APP_PASSWORD", "").strip()
    if not gmail or not app_password:
        return jsonify({"success": False, "error": "GMAIL и APP_PASSWORD не настроены в .env"})
    data = request.get_json(silent=True) or {}
    to = str(data.get("to", "")).strip()
    subject = str(data.get("subject", "Письмо от NEXUS")).strip()
    body = str(data.get("body", "")).strip()
    if not to or not body:
        return jsonify({"success": False, "error": "Укажите получателя и текст."})
    try:
        msg = MIMEMultipart()
        msg["From"] = gmail; msg["To"] = to; msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail, app_password)
            server.sendmail(gmail, to, msg.as_string())
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/emails/reply", methods=["POST"])
def emails_reply():
    if not logged_in(): return jsonify({"error": "Нужен вход."}), 401
    data = request.get_json(silent=True) or {}
    subject   = str(data.get("subject", "")).strip()
    from_addr = str(data.get("from_addr", "")).strip()
    snippet   = str(data.get("snippet", "")).strip()
    prompt_parts = ["Напиши краткий деловой ответ на email."]
    if from_addr:
        prompt_parts.append(f"Отправитель: {from_addr}.")
    if subject:
        prompt_parts.append(f"Тема: {subject}.")
    if snippet:
        prompt_parts.append(f"Исходное письмо: {snippet[:500]}")
    prompt_parts.append("Ответ должен быть вежливым, кратким, деловым. Только текст письма, без лишних пояснений.")
    try:
        profile = load_json(MEMORY_FILE, {}).get("profile", {})
        messages = [
            {"role": "system", "content": build_system_prompt(profile)},
            {"role": "user", "content": " ".join(prompt_parts)},
        ]
        reply = ask_ai(messages)
        audit("ai_email_reply", f"to={from_addr} subj={subject[:40]}", session.get("user", "web"))
        return jsonify({"reply": reply})
    except Exception as e:
        return jsonify({"error": str(e)})

# ── Weather / Briefing ────────────────────────────────────────────────────────

@app.route("/weather")
def weather():
    if not logged_in(): return jsonify({"error": "Нужен вход."}), 401
    city = request.args.get("city", get_env("DEFAULT_WEATHER_CITY", "Kyiv")).strip() or "Kyiv"
    return jsonify(load_weather(city))

@app.route("/morning_briefing")
def morning_briefing():
    if not logged_in(): return jsonify({"error": "Нужен вход."}), 401
    city = request.args.get("city", get_env("DEFAULT_WEATHER_CITY", "Kyiv")).strip() or "Kyiv"
    weather_data = load_weather(city)
    stats["briefings"] += 1
    open_tasks = [t for t in state.get("tasks", []) if t.get("status") != "done"][:5]
    tasks_str = "\n".join(f"- {t['title']}" for t in open_tasks) or "нет задач"
    prompt = (
        f"Короткий утренний брифинг для Никиты.\n"
        f"Погода: {weather_data.get('summary', 'недоступна')}\n"
        f"Открытых задач: {len(open_tasks)}\n{tasks_str}\n"
        "Формат: 1) погода, 2) фокус дня, 3) топ-3 действия. Коротко!"
    )
    try:
        briefing = ask_ai([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}])
    except Exception:
        briefing = f"**Погода:** {weather_data.get('summary','N/A')}\n**Задачи:** {len(open_tasks)}"
    return jsonify({"briefing": briefing, "weather": weather_data})

def load_weather(city):
    api_key = get_env("OPENWEATHER_API_KEY", "").strip()
    if not api_key:
        return {"city": city, "error": "OPENWEATHER_API_KEY не задан."}
    params = urllib.parse.urlencode({"q": city, "appid": api_key, "units": "metric", "lang": "ru"})
    url = "https://api.openweathermap.org/data/2.5/weather?" + params
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        temp = round(data["main"]["temp"]); feels = round(data["main"]["feels_like"])
        wind = data.get("wind", {}).get("speed", 0)
        desc = (data.get("weather") or [{}])[0].get("description", "")
        name = data.get("name", city)
        return {"city": name, "temp": temp, "feels_like": feels, "wind": wind, "description": desc,
                "summary": f"{name}: {temp}°C, ощущается {feels}°C, {desc}, ветер {wind} м/с."}
    except Exception as exc:
        return {"city": city, "error": str(exc)}

# ── Nova Poshta ───────────────────────────────────────────────────────────────

@app.route("/nova_poshta/track")
def nova_poshta_track():
    if not logged_in(): return jsonify({"error": "Нужен вход."}), 401
    number = request.args.get("number", "").strip()
    if not number: return jsonify({"success": False, "error": "Укажите номер ТТН."})
    api_key = get_env("NOVA_POSHTA_API_KEY", "").strip()
    if not api_key: return jsonify({"success": False, "error": "NOVA_POSHTA_API_KEY не задан."})
    payload = json.dumps({"apiKey": api_key, "modelName": "TrackingDocument",
                          "calledMethod": "getStatusDocuments",
                          "methodProperties": {"Documents": [{"DocumentNumber": number}]}}).encode()
    req = urllib.request.Request("https://api.novaposhta.ua/v2.0/json/", data=payload,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        return jsonify({"success": True, "data": data.get("data", [])})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

# ── Invoice PDF ───────────────────────────────────────────────────────────────

@app.route("/invoice_pdf", methods=["POST"])
def invoice_pdf():
    if not logged_in(): return jsonify({"error": "Нужен вход."}), 401
    data = request.get_json(silent=True) or {}
    client = str(data.get("client", "Client"))
    invoice_id = str(data.get("invoice_id", datetime.utcnow().strftime("%Y%m%d%H%M")))
    items = data.get("items") or [{"name": "Service", "qty": 1, "price": float(data.get("amount", 0) or 0)}]
    lines = [f"NEXUS Invoice #{invoice_id}", f"Client: {client}", ""]
    total = 0.0
    for item in items:
        name = str(item.get("name", "Item")); qty = float(item.get("qty", 1) or 1)
        price = float(item.get("price", 0) or 0); amount = qty * price; total += amount
        lines.append(f"{name}  {qty:g} x {price:.2f} = {amount:.2f} UAH")
    lines += ["", f"TOTAL: {total:.2f} UAH"]
    pdf = build_simple_pdf(lines)
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="invoice_{invoice_id}.pdf"'})

def build_simple_pdf(lines):
    def clean(t): return str(t).encode("latin-1", errors="replace").decode("latin-1")
    content = ["BT", "/F1 12 Tf", "50 800 Td"]
    for i, line in enumerate(lines):
        if i: content.append("0 -18 Td")
        escaped = clean(line).replace("\\","\\\\").replace("(","\\(").replace(")","\\)")
        content.append(f"({escaped}) Tj")
    content.append("ET")
    stream = "\n".join(content).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    pdf = bytearray(b"%PDF-1.4\n"); offsets = [0]
    for idx, obj in enumerate(objects, 1):
        offsets.append(len(pdf)); pdf.extend(f"{idx} 0 obj\n".encode()); pdf.extend(obj); pdf.extend(b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]: pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(f"trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(pdf)

# ── Users / Agents ────────────────────────────────────────────────────────────

@app.route("/users", methods=["GET", "POST"])
def users():
    guard = require_roles("admin")
    if guard: return guard
    if request.method == "GET":
        return jsonify({"users": [{k: v for k, v in u.items() if k != "password"} for u in state.get("users", [])]})
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip(); role = str(data.get("role", "guest"))
    password = str(data.get("password", ""))
    if not username or role not in {"admin", "employee", "guest"}:
        return jsonify({"success": False, "error": "Укажите username и роль."})
    for user in state.get("users", []):
        if user.get("username") == username:
            user["role"] = role
            if password: user["password"] = password
            persist_state()
            audit("user_update", f"username={username} role={role}", user=current_user()["username"])
            return jsonify({"success": True, "updated": True})
    state.setdefault("users", []).append({"username": username, "role": role, "password": password})
    persist_state()
    audit("user_create", f"username={username} role={role}", user=current_user()["username"])
    return jsonify({"success": True, "created": True})

@app.route("/agents", methods=["GET", "POST"])
def agents():
    guard = require_roles("admin", "employee")
    if guard: return guard
    if request.method == "GET":
        return jsonify({"agents": state.get("agents", [])})
    data = request.get_json(silent=True) or {}
    agent_id = str(data.get("id", "")).strip()
    for agent in state.get("agents", []):
        if agent.get("id") == agent_id:
            if data.get("status"): agent["status"] = str(data["status"])
            if data.get("name"):   agent["name"]   = str(data["name"])
            persist_state()
            return jsonify({"success": True, "agent": agent})
    return jsonify({"success": False, "error": "Агент не найден."})

# ── Capabilities / Healthz / Stats ────────────────────────────────────────────

@app.route("/capabilities")
def capabilities():
    if not logged_in(): return jsonify({"error": "Нужен вход."}), 401
    return jsonify({"capabilities": capability_map(), "integrations": integration_status(), "storage": storage_status()})

@app.route("/healthz")
def healthz():
    return jsonify({
        "ok": True, "app": "nexus_web", "time": datetime.utcnow().isoformat() + "Z",
        "data_dir": str(BASE_DIR),
        "storage": storage_status(),
        "features": {
            "chat_stream": True, "csrf": True,
            "weather": bool(get_env("OPENWEATHER_API_KEY", "").strip()),
            "nova_poshta": bool(get_env("NOVA_POSHTA_API_KEY", "").strip()),
            "email": bool(get_env("GMAIL", "").strip() and get_env("APP_PASSWORD", "").strip()),
            "monobank": bool(get_env("MONOBANK_TOKEN", "").strip()),
            "calendar": True,
            "audit_log": True,
            "rag": True,
        },
    })

@app.route("/stats")
def get_stats():
    if not logged_in(): return jsonify({}), 401
    return jsonify({**stats, "uptime": "running", "version": "2.1.0"})

# ── File upload for chat ──────────────────────────────────────────────────────

@app.route("/upload_chat", methods=["POST"])
def upload_chat():
    if not logged_in(): return jsonify({"error": "Нужен вход."}), 401
    f = request.files.get("file")
    if not f: return jsonify({"success": False, "error": "Нет файла."})
    filename = f.filename or "upload"
    stats["files"] += 1
    if filename.lower().rsplit(".", 1)[-1] in {"png", "jpg", "jpeg", "gif", "webp"}:
        raw = f.read()
        b64 = base64.b64encode(raw).decode("ascii")
        ext = filename.rsplit(".", 1)[-1].lower()
        mime = {"jpg": "jpeg", "jpeg": "jpeg"}.get(ext, ext)
        try:
            oai = OpenAI(api_key=require_openai_key())
            resp = oai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": "Describe this image in detail. If it is a document, read the text."}
                ]}],
                max_tokens=800,
            )
            desc = resp.choices[0].message.content
        except Exception as e:
            desc = f"Image received. Analysis error: {e}"
        history.append({"id": message_id(), "role": "user", "content": f"[Image: {filename}]"})
        history.append({"id": message_id(), "role": "assistant", "content": desc})
        persist_history()
        return jsonify({"success": True, "description": desc})
    raw = f.read()
    try:
        text_content = raw.decode("utf-8", errors="replace")
    except Exception:
        text_content = ""
    saved = UPLOAD_DIR / filename
    saved.write_bytes(raw)
    try:
        from nexus_rag import index_document, extract_text as rag_extract
        import io as _io
        text_for_rag = rag_extract(_io.BytesIO(raw), filename) or text_content
        if text_for_rag.strip():
            n = index_document(text_for_rag, filename)
            rag_note = f" Indexed {n} chunks for search."
        else:
            rag_note = ""
    except Exception:
        rag_note = ""
    history.append({"id": message_id(), "role": "user", "content": f"[File: {filename}] {text_content[:2000]}"})
    reply = f"File **{filename}** received ({len(raw)//1024 or 1}KB).{rag_note}"
    history.append({"id": message_id(), "role": "assistant", "content": reply})
    persist_history()
    return jsonify({"success": True, "reply": reply})

# -- Search -------------------------------------------------------------------

@app.route("/search")
def search():
    if not logged_in(): return jsonify({"error": "Login required."}), 401
    q = request.args.get("q", "").strip().lower()
    if not q: return jsonify({"results": []})
    results = []
    for t in state.get("tasks", []):
        if q in (t.get("title","") + t.get("desc","")).lower():
            results.append({"type": "task", "title": t.get("title",""), "id": t.get("id","")})
    crm = load_json(CRM_FILE, {"clients": []})
    for c in crm.get("clients", []):
        if q in (c.get("name","") + c.get("company","") + c.get("notes","")).lower():
            results.append({"type": "client", "title": c.get("name",""), "id": c.get("id","")})
    for msg in history:
        if q in str(msg.get("content","")).lower():
            snippet = str(msg.get("content",""))[:80]
            results.append({"type": "chat", "title": snippet})
    return jsonify({"results": results[:30]})


def make_audio(text: str):
    try:
        import base64 as _b64
        model = get_env("OPENAI_TTS_MODEL", "tts-1")
        voice = get_env("OPENAI_TTS_VOICE", "onyx")
        client = OpenAI(api_key=require_openai_key())
        resp = client.audio.speech.create(model=model, voice=voice, input=text[:300])
        return _b64.b64encode(resp.content).decode("ascii")
    except Exception:
        return None


@app.route("/manifest.json")
def pwa_manifest():
    return jsonify({
        "name": "NEXUS", "short_name": "NEXUS",
        "start_url": "/", "display": "standalone",
        "background_color": "#061014", "theme_color": "#35d7e9",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    })


@app.route("/sw.js")
def service_worker():
    sw = (
        "self.addEventListener('install',e=>self.skipWaiting());"
        "self.addEventListener('activate',e=>clients.claim());"
        "self.addEventListener('fetch',e=>{"
        "if(e.request.method!=='GET')return;"
        "e.respondWith(fetch(e.request).catch(()=>new Response('Offline',{status:503})));"
        "});"
    )
    from flask import Response as _R
    return _R(sw, mimetype="application/javascript")


@app.route("/profile", methods=["GET", "POST"])
def profile_route():
    if not logged_in(): return jsonify({"error": "Login required."}), 401
    global profile
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        allowed = ("name","business","location","city","phone","email")
        profile.update({k: v for k, v in data.items() if k in allowed})
        save_json(PROFILE_FILE, profile)
        return jsonify({"success": True, "profile": profile})
    return jsonify({"profile": profile})



@app.route("/sheets/sync", methods=["POST"])
def sheets_sync():
    if not logged_in(): return jsonify({"error": "Login required."}), 401
    sa_file = get_env("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    sheet_id = get_env("GOOGLE_SPREADSHEET_ID", "").strip()
    if not sa_file or not sheet_id:
        return jsonify({"success": False, "error": "GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_SPREADSHEET_ID must be set in .env"})
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
        import pathlib

        sa_path = BASE_DIR / sa_file
        if not sa_path.exists():
            return jsonify({"success": False, "error": f"Service account file not found: {sa_path}"})

        creds = Credentials.from_service_account_file(
            str(sa_path),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        sheets  = service.spreadsheets()

        # --- Tasks sheet ---
        tasks = state.get("tasks", [])
        task_rows = [["ID","Title","Status","Priority","Created"]] + [
            [t.get("id",""), t.get("title",""), t.get("status",""),
             t.get("priority",""), t.get("created","")]
            for t in tasks
        ]

        # --- CRM sheet ---
        crm = load_json(CRM_FILE, {"clients": []})
        crm_rows = [["ID","Name","Company","Phone","Email","Status","Notes"]] + [
            [c.get("id",""), c.get("name",""), c.get("company",""),
             c.get("phone",""), c.get("email",""), c.get("status",""),
             str(c.get("notes",""))[:100]]
            for c in crm.get("clients", [])
        ]

        # --- Analytics sheet ---
        analytics = load_json(ANALYTICS_FILE, {"records": []})
        ana_rows = [["Date","Revenue","Expenses","Category","Note"]] + [
            [r.get("date",""), r.get("revenue",0), r.get("expenses",0),
             r.get("category",""), r.get("note","")]
            for r in analytics.get("records", [])
        ]

        def write_sheet(title, rows):
            # Try to find existing sheet or create it
            meta = sheets.get(spreadsheetId=sheet_id).execute()
            existing = [s["properties"]["title"] for s in meta.get("sheets", [])]
            if title not in existing:
                sheets.batchUpdate(spreadsheetId=sheet_id, body={
                    "requests": [{"addSheet": {"properties": {"title": title}}}]
                }).execute()
            range_name = f"{title}!A1"
            sheets.values().clear(spreadsheetId=sheet_id, range=range_name).execute()
            sheets.values().update(
                spreadsheetId=sheet_id, range=range_name,
                valueInputOption="USER_ENTERED",
                body={"values": rows},
            ).execute()

        write_sheet("Tasks",     task_rows)
        write_sheet("CRM",       crm_rows)
        write_sheet("Analytics", ana_rows)

        audit("sheets_sync", f"tasks={len(tasks)} crm={len(crm_rows)-1} analytics={len(ana_rows)-1}",
              user=current_user()["username"])
        return jsonify({"success": True,
                        "tasks": len(tasks),
                        "clients": len(crm_rows)-1,
                        "analytics": len(ana_rows)-1})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/monobank")
def monobank():
    if not logged_in(): return jsonify({"error": "Login required."}), 401
    token = get_env("MONOBANK_TOKEN", "").strip()
    if not token:
        return jsonify({"token_ok": False, "balance": None, "income": None,
                        "expense": None, "transactions": [],
                        "error": "MONOBANK_TOKEN not set in .env"})
    try:
        import urllib.request as _ur
        import time as _time

        def _mono(url):
            req = _ur.Request(url, headers={"X-Token": token})
            with _ur.urlopen(req, timeout=10) as r:
                return json.loads(r.read())

        info = _mono("https://api.monobank.ua/personal/client-info")
        # Pick first UAH account (currencyCode 980)
        uah_acc = next(
            (a for a in info.get("accounts", []) if a.get("currencyCode") == 980),
            info.get("accounts", [{}])[0] if info.get("accounts") else {}
        )
        account_id = uah_acc.get("id", "")
        balance_kopecks = uah_acc.get("balance", 0)

        # Fetch last 30 days of transactions
        transactions, income, expense = [], 0, 0
        if account_id:
            now = int(_time.time())
            frm = now - 30 * 86400
            try:
                stmt = _mono(f"https://api.monobank.ua/personal/statement/{account_id}/{frm}/{now}")
                for tx in (stmt or [])[:50]:
                    amt = tx.get("amount", 0)
                    if amt > 0:
                        income += amt
                    else:
                        expense += amt
                    transactions.append({
                        "time":        tx.get("time", 0),
                        "description": tx.get("description", ""),
                        "amount":      amt,
                        "mcc":         tx.get("mcc", 0),
                        "balance":     tx.get("balance", 0),
                        "cashback":    tx.get("cashbackAmount", 0),
                        "currency":    tx.get("currencyCode", 980),
                    })
            except Exception:
                pass  # statement may fail with 429 rate limit — balance still shown

        return jsonify({
            "token_ok":    True,
            "name":        info.get("name", ""),
            "balance":     balance_kopecks,
            "income":      income,
            "expense":     expense,
            "transactions": transactions,
            "accounts": [
                {
                    "id":      a.get("id",""),
                    "masked":  (a.get("maskedPan") or [""])[-1],
                    "type":    a.get("type",""),
                    "balance": a.get("balance",0) / 100,
                    "currency": a.get("currencyCode", 980),
                }
                for a in info.get("accounts", [])
            ],
        })
    except Exception as e:
        return jsonify({"token_ok": False, "error": str(e), "transactions": []})



# ── Backup / Restore / Export ─────────────────────────────────────────────────

@app.route("/backup")
def backup_download():
    """Download all JSON data as a single .zip archive."""
    if not logged_in(): return jsonify({"error": "Login required."}), 401
    import zipfile, io
    buf = io.BytesIO()
    data_files = [
        ("nexus_state.json",    STATE_FILE),
        ("crm_data.json",       CRM_FILE),
        ("analytics_data.json", ANALYTICS_FILE),
        ("reminders.json",      REMINDERS_FILE),
        ("calendar_data.json",  CALENDAR_FILE),
        ("audit_log.json",      AUDIT_FILE),
        ("nexus_memory.json",   MEMORY_FILE),
        ("nexus_profile.json",  PROFILE_FILE),
    ]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, fpath in data_files:
            if fpath.exists():
                zf.write(str(fpath), arcname=name)
    buf.seek(0)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M")
    audit("backup_download", f"ts={ts}", user=current_user()["username"])
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename=nexus_backup_{ts}.zip"}
    )


@app.route("/restore", methods=["POST"])
def restore_upload():
    """Restore data from a .zip backup file."""
    if not logged_in(): return jsonify({"error": "Login required."}), 401
    import zipfile, io
    f = request.files.get("file")
    if not f:
        return jsonify({"success": False, "error": "No file uploaded"})
    try:
        zf = zipfile.ZipFile(io.BytesIO(f.read()))
        allowed = {
            "nexus_state.json":    STATE_FILE,
            "crm_data.json":       CRM_FILE,
            "analytics_data.json": ANALYTICS_FILE,
            "reminders.json":      REMINDERS_FILE,
            "calendar_data.json":  CALENDAR_FILE,
            "nexus_memory.json":   MEMORY_FILE,
            "nexus_profile.json":  PROFILE_FILE,
        }
        restored = []
        for name in zf.namelist():
            if name in allowed:
                data = json.loads(zf.read(name).decode("utf-8"))
                save_json(allowed[name], data)
                restored.append(name)
        audit("restore", f"files={restored}", user=current_user()["username"])
        return jsonify({"success": True, "restored": restored})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/export")
def export_data():
    """Export all data as a single JSON."""
    if not logged_in(): return jsonify({"error": "Login required."}), 401
    export = {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "version": "2.1",
        "tasks":     load_json(STATE_FILE, {}).get("tasks", []),
        "crm":       load_json(CRM_FILE, []),
        "analytics": load_json(ANALYTICS_FILE, {}),
        "reminders": load_json(REMINDERS_FILE, []),
        "calendar":  load_json(CALENDAR_FILE, []),
        "profile":   load_json(PROFILE_FILE, {}),
    }
    audit("export", f"tasks={len(export['tasks'])} crm={len(export['crm'])}", user=current_user()["username"])
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M")
    return Response(
        json.dumps(export, ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename=nexus_export_{ts}.json"}
    )

@app.route("/calendar", methods=["GET", "POST"])
def calendar():
    if not logged_in(): return jsonify({"error": "Login required."}), 401
    events = load_json(CALENDAR_FILE, [])
    if request.method == "GET":
        events_sorted = sorted(events, key=lambda e: (e.get("date",""), e.get("time","")))
        return jsonify({"events": events_sorted})
    data = request.get_json(silent=True) or {}
    title = str(data.get("title","")).strip()
    date  = str(data.get("date","")).strip()
    if not title or not date:
        return jsonify({"success": False, "error": "Need title and date."})
    event = {
        "id":      secrets.token_hex(6),
        "title":   title,
        "date":    date,
        "time":    str(data.get("time","")).strip(),
        "desc":    str(data.get("desc","")).strip(),
        "created": datetime.utcnow().isoformat() + "Z",
    }
    events.append(event)
    save_json(CALENDAR_FILE, events)
    audit("calendar_add", f"{date} {title}", user=current_user()["username"])
    return jsonify({"success": True, "event": event})


@app.route("/calendar/<event_id>", methods=["DELETE"])
def calendar_delete(event_id):
    if not logged_in(): return jsonify({"error": "Login required."}), 401
    events = load_json(CALENDAR_FILE, [])
    before = len(events)
    events = [e for e in events if e.get("id") != event_id]
    save_json(CALENDAR_FILE, events)
    audit("calendar_delete", f"id={event_id}", user=current_user()["username"])
    return jsonify({"success": before != len(events)})


@app.route("/audit")
def audit_log():
    if not logged_in(): return jsonify({"error": "Login required."}), 401
    entries = load_json(AUDIT_FILE, [])
    action_filter = request.args.get("action", "").strip()
    if action_filter:
        entries = [e for e in entries if e.get("action") == action_filter]
    return jsonify({"entries": list(reversed(entries[-500:]))})


def rag_context(query: str, n: int = 3) -> str:
    try:
        from nexus_rag import search_documents
        results = search_documents(query, n=n)
        if not results:
            return ""
        snippets = "\n\n".join(f"[doc] {r}" for r in results)
        return f"\n\n---\nRelevant documents:\n{snippets}\n---"
    except Exception:
        return ""


@app.route("/rag/docs")
def rag_docs():
    if not logged_in(): return jsonify({"error": "Login required."}), 401
    try:
        from nexus_rag import get_document_list
        return jsonify({"documents": get_document_list()})
    except Exception as e:
        return jsonify({"documents": [], "error": str(e)})


if __name__ == "__main__":
    ensure_env_keys()
    port = int(get_env("PORT", "10000"))
    print("NEXUS Web starting...")
    app.run(host="0.0.0.0", port=port, debug=False)
