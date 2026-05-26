import base64
import json
import logging
import os
import secrets
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template_string, request, session, url_for
from openai import OpenAI

from nexus_common import (
    DEFAULT_PROFILE,
    ask_ai,
    build_system_prompt,
    get_env,
    get_web_session_secret,
    require_openai_key,
    require_web_password,
)

BASE_DIR = Path(__file__).resolve().parent
MEMORY_FILE = BASE_DIR / "nexus_memory.json"
PROFILE_FILE = BASE_DIR / "nexus_profile.json"
STATE_FILE = BASE_DIR / "nexus_state.json"
UPLOAD_DIR = BASE_DIR / "chat_uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = get_web_session_secret()
logging.basicConfig(level=logging.INFO, format="%(message)s")


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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
        {
            "id": "restaurant",
            "name": "AI-агент общепита",
            "description": "Меню, клиенты, отзывы, бронирования, ежедневная выручка.",
            "status": "template",
        },
        {
            "id": "aqua",
            "name": "AI-агент аква бизнеса",
            "description": "Клиенты, сервис, заявки, поставки, повторные продажи.",
            "status": "template",
        },
        {
            "id": "sales",
            "name": "AI-агент продаж",
            "description": "Лиды, follow-up, CRM, скрипты и отчеты по продажам.",
            "status": "template",
        },
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
        "discord": "planned",
        "database": "env:DATABASE_URL",
    },
}
state = load_json(STATE_FILE, DEFAULT_STATE.copy())


def merge_state_defaults(target, defaults):
    changed = False
    for key, value in defaults.items():
        if key not in target:
            target[key] = value
            changed = True
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            nested_changed = merge_state_defaults(target[key], value)
            changed = changed or nested_changed
    return changed


if merge_state_defaults(state, DEFAULT_STATE):
    save_json(STATE_FILE, state)
stats = {
    "messages": len([m for m in history if m.get("role") == "user"]),
    "voice": 0,
    "files": 0,
    "edits": 0,
    "briefings": 0,
    "commands": 0,
}
SYSTEM = build_system_prompt(profile)
RATE_LIMIT = {}
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX_POSTS = 40


def get_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def client_key():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "local").split(",")[0].strip()


@app.before_request
def protect_requests():
    if request.method != "POST":
        return None

    now = time.time()
    key = client_key()
    bucket = [stamp for stamp in RATE_LIMIT.get(key, []) if now - stamp < RATE_LIMIT_WINDOW]
    if len(bucket) >= RATE_LIMIT_MAX_POSTS:
        return jsonify({"error": "Rate limit exceeded"}), 429
    bucket.append(now)
    RATE_LIMIT[key] = bucket

    if request.endpoint == "login":
        return None
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
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "media-src 'self' data:; "
        "connect-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    log_event(
        "request",
        method=request.method,
        path=request.path,
        status=response.status_code,
        ip=client_key(),
    )
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


LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NEXUS - вход</title>
<style>
:root{--bg:#061014;--panel:#101d25;--line:#24414b;--text:#eef8f9;--muted:#8ca3aa;--cyan:#35d7e9;--green:#48e08c}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(circle at 20% 0,#123743 0,#061014 38%,#03080b 100%);font-family:Inter,'Segoe UI',Arial,sans-serif;color:var(--text);padding:24px}
.box{width:min(420px,100%);border:1px solid var(--line);background:linear-gradient(180deg,rgba(16,29,37,.96),rgba(8,15,20,.96));border-radius:18px;padding:28px;box-shadow:0 34px 90px rgba(0,0,0,.45)}
.brand{display:flex;gap:12px;align-items:center;margin-bottom:24px}.mark{width:42px;height:42px;border-radius:12px;background:linear-gradient(135deg,var(--cyan),var(--green));display:grid;place-items:center;color:#041014;font-weight:900}.name{font-size:22px;font-weight:900;letter-spacing:5px}.sub{font-size:13px;color:var(--muted);margin-top:3px}
label{display:block;color:var(--muted);font-size:12px;margin-bottom:8px}input{width:100%;height:46px;border:1px solid var(--line);border-radius:12px;background:#071218;color:var(--text);padding:0 14px;outline:none}input:focus{border-color:var(--cyan);box-shadow:0 0 0 3px rgba(53,215,233,.13)}
button{width:100%;height:46px;margin-top:14px;border:0;border-radius:12px;background:linear-gradient(135deg,var(--cyan),var(--green));color:#031014;font-weight:900;cursor:pointer}.error{min-height:20px;margin-top:12px;color:#ff7c7c;font-size:13px}
</style>
</head>
<body>
<form class="box" method="post">
  <div class="brand"><div class="mark">N</div><div><div class="name">NEXUS</div><div class="sub">Центр управления Никиты</div></div></div>
  <label for="password">Пароль доступа</label>
  <input id="password" name="password" type="password" autocomplete="current-password" autofocus>
  <button type="submit">Войти</button>
  <div class="error">{{ error }}</div>
</form>
</body>
</html>"""


HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="csrf-token" content="{{ csrf_token }}">
<title>NEXUS - Центр управления</title>
<style>
:root{--bg:#061014;--side:#08141a;--panel:#101d25;--panel2:#142833;--line:#25414b;--text:#eef8f9;--muted:#8da4ab;--soft:#c5d5d9;--cyan:#35d7e9;--green:#48e08c;--red:#ff7272;--amber:#f5bd63;--r:14px;--shadow:0 22px 60px rgba(0,0,0,.34)}
*{box-sizing:border-box}html,body{height:100%}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,'Segoe UI',Arial,sans-serif;letter-spacing:0}
button,input,textarea{font:inherit}button{cursor:pointer}.app{height:100vh;display:grid;grid-template-columns:260px 1fr;overflow:hidden;background:radial-gradient(circle at 76% -10%,rgba(53,215,233,.14),transparent 35%),var(--bg)}
.sidebar{background:linear-gradient(180deg,var(--side),#04090c);border-right:1px solid var(--line);display:flex;flex-direction:column}.brand{height:84px;padding:20px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px}.mark{width:42px;height:42px;border-radius:12px;background:linear-gradient(135deg,var(--cyan),var(--green));display:grid;place-items:center;color:#041014;font-weight:900}.brand-title{font-size:20px;font-weight:900;letter-spacing:5px}.brand-sub{font-size:11px;color:var(--muted);margin-top:3px}
.nav{padding:12px 10px;display:grid;gap:4px}.nav button{height:43px;border:0;background:transparent;color:var(--muted);border-radius:10px;padding:0 12px;text-align:left;display:flex;align-items:center;gap:10px}.nav button:hover{background:rgba(255,255,255,.04);color:var(--text)}.nav button.active{background:rgba(53,215,233,.12);color:var(--cyan);box-shadow:inset 3px 0 0 var(--cyan)}.emoji{width:24px;text-align:center}
.side-bottom{margin-top:auto;border-top:1px solid var(--line);padding:16px 18px;color:var(--muted);font-size:13px}.online{display:flex;align-items:center;gap:8px}.dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 16px var(--green)}
.main{min-width:0;display:flex;flex-direction:column}.topbar{height:64px;border-bottom:1px solid var(--line);background:rgba(8,18,24,.8);backdrop-filter:blur(14px);display:flex;align-items:center;justify-content:space-between;padding:0 24px}.title{font-size:18px;font-weight:850}.meta{display:flex;align-items:center;gap:14px;color:var(--muted);font-size:13px}.logout{color:var(--muted);text-decoration:none;border:1px solid var(--line);padding:8px 10px;border-radius:10px}
.content{overflow:auto;min-height:0;padding:22px 22px 92px}.page{display:none}.page.active{display:block}.grid4{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}.stack{display:grid;gap:16px}
.card{background:linear-gradient(180deg,rgba(16,29,37,.94),rgba(10,18,24,.94));border:1px solid var(--line);border-radius:var(--r);padding:18px;box-shadow:var(--shadow)}.card h3{margin:0 0 14px;font-size:13px;color:var(--cyan);letter-spacing:1px;text-transform:uppercase}.stat{min-height:114px;display:flex;flex-direction:column;justify-content:space-between}.num{font-size:34px;font-weight:900}.lab{font-size:12px;color:var(--muted);letter-spacing:1px;text-transform:uppercase}.hint{font-size:12px;color:var(--soft)}
.field{width:100%;border:1px solid var(--line);background:#071218;color:var(--text);border-radius:11px;min-height:44px;padding:0 13px;outline:none;margin-bottom:10px}.field:focus{border-color:var(--cyan);box-shadow:0 0 0 3px rgba(53,215,233,.12)}textarea.field{padding:12px 13px;resize:vertical;line-height:1.5}.btn{border:0;border-radius:11px;min-height:42px;padding:0 15px;background:linear-gradient(135deg,var(--cyan),var(--green));color:#041014;font-weight:900}.btn.secondary{background:#071218;color:var(--cyan);border:1px solid var(--line)}.row{display:flex;gap:10px;align-items:center}.row .field{margin:0}
.chatbox{height:calc(100vh - 150px);display:flex;flex-direction:column}.toolbar{display:flex;gap:8px;align-items:center;margin-bottom:10px}.toolbar .field{margin:0}.messages{flex:1;overflow:auto;display:flex;flex-direction:column;gap:12px;padding-right:4px}.msgwrap{max-width:82%;display:flex;flex-direction:column;gap:4px}.msgwrap.user{align-self:flex-end;align-items:flex-end}.msgwrap.ai{align-self:flex-start}.speaker{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--muted)}.bubble{padding:12px 14px;border-radius:14px;line-height:1.55;font-size:14px;word-break:break-word}.bubble.user{background:#18374a}.bubble.ai{background:#0f2b25;border:1px solid rgba(72,224,140,.18)}.bubble pre{background:#050a0d;border:1px solid var(--line);padding:12px;border-radius:10px;overflow:auto}.bubble code{font-family:Consolas,monospace;color:#b7f7ff}.bubble ul{margin:8px 0 8px 18px}.edit-btn{border:0;background:transparent;color:var(--muted);font-size:12px;padding:0}.compose{border-top:1px solid var(--line);padding-top:12px;display:grid;grid-template-columns:auto 1fr auto auto;gap:10px;align-items:end}.compose textarea{margin:0;min-height:46px;max-height:140px}.speak{display:none;color:var(--cyan);font-size:13px;margin-left:auto}.speak.on{display:block}.wave{display:inline-flex;gap:3px;align-items:end;margin-right:6px}.wave span{width:3px;background:var(--cyan);border-radius:3px;animation:wave 650ms infinite}.wave span:nth-child(1){height:8px}.wave span:nth-child(2){height:15px;animation-delay:.12s}.wave span:nth-child(3){height:11px;animation-delay:.22s}@keyframes wave{50%{transform:scaleY(.35)}}.recording{outline:2px solid rgba(255,114,114,.5)}
.item{background:#071218;border:1px solid var(--line);border-radius:12px;padding:12px}.item-title{font-weight:750;margin-bottom:5px}.item-meta{color:var(--muted);font-size:13px;line-height:1.45}.list{display:grid;gap:10px}.alert{border-radius:12px;padding:11px 13px;margin-bottom:12px;font-size:13px}.alert.ok{background:rgba(72,224,140,.12);color:var(--green);border:1px solid rgba(72,224,140,.28)}.alert.err{background:rgba(255,114,114,.12);color:var(--red);border:1px solid rgba(255,114,114,.28)}
.bottom-nav{display:none;position:fixed;left:0;right:0;bottom:0;background:rgba(6,16,20,.94);border-top:1px solid var(--line);backdrop-filter:blur(14px);padding:8px;grid-template-columns:repeat(5,1fr);gap:6px}.bottom-nav button{border:0;background:transparent;color:var(--muted);border-radius:10px;padding:8px 4px;font-size:11px}.bottom-nav button.active{background:rgba(53,215,233,.12);color:var(--cyan)}
.status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}.status-ok{color:var(--green)}.status-warn{color:var(--amber)}.status-plan{color:var(--muted)}
@media(max-width:900px){.app{grid-template-columns:1fr}.sidebar{display:none}.bottom-nav{display:grid;grid-template-columns:repeat(auto-fit,minmax(54px,1fr));overflow-x:auto}.grid4,.grid2{grid-template-columns:1fr}.topbar{padding:0 14px}.content{padding:14px 14px 88px}.chatbox{height:calc(100vh - 128px)}.msgwrap{max-width:94%}.compose{grid-template-columns:auto 1fr auto}.compose .file-label{display:none}.meta span:nth-child(2){display:none}}
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="brand"><div class="mark">N</div><div><div class="brand-title">NEXUS</div><div class="brand-sub">ЦЕНТР УПРАВЛЕНИЯ</div></div></div>
    <nav class="nav" id="sideNav"></nav>
    <div class="side-bottom"><div class="online"><span class="dot"></span>Никита онлайн</div></div>
  </aside>
  <main class="main">
    <header class="topbar"><div class="title" id="pageTitle">Dashboard</div><div class="meta"><span id="clock"></span><span>{{ email }}</span><a class="logout" href="/logout">Выйти</a></div></header>
    <section class="content">
      <div id="alert"></div>
      <div class="page active" id="dashboard">
        <div class="stack">
          <div class="grid4">
            <div class="card stat"><div class="num" id="sMessages">0</div><div><div class="lab">Сообщений</div><div class="hint">AI диалоги</div></div></div>
            <div class="card stat"><div class="num" id="sVoice">0</div><div><div class="lab">Голосовых</div><div class="hint">Команды голосом</div></div></div>
            <div class="card stat"><div class="num" id="sFiles">0</div><div><div class="lab">Файлов</div><div class="hint">Загружено в чат</div></div></div>
            <div class="card stat"><div class="num" id="sBriefings">0</div><div><div class="lab">Брифингов</div><div class="hint">Погода и план</div></div></div>
          </div>
          <div class="grid2">
            <div class="card"><h3>Быстрый запрос</h3><div class="row"><input class="field" id="quickInput" placeholder="Спроси NEXUS..." onkeydown="if(event.key==='Enter')quickAsk()"><button class="btn" onclick="quickAsk()">Спросить</button></div><div id="quickResult" class="item-meta"></div></div>
            <div class="card"><h3>История и поиск</h3><input class="field" id="historyQuery" placeholder="Поиск по разговорам..." oninput="searchHistory()"><div id="historyResults" class="list"></div></div>
          </div>
          <div class="card"><h3>Утренний брифинг</h3><div class="row"><input class="field" id="briefCity" placeholder="Город, например Kyiv" value="Kyiv"><button class="btn" onclick="loadBriefing()">Собрать</button></div><div id="briefingResult" class="item-meta"></div></div>
        </div>
      </div>
      <div class="page" id="chat">
        <div class="card chatbox">
          <div class="toolbar"><input class="field" id="chatSearch" placeholder="Поиск в истории..." oninput="searchHistory('chatSearch','chatHistory')"><div class="speak" id="speaking"><span class="wave"><span></span><span></span><span></span></span>NEXUS говорит</div></div>
          <div id="chatHistory" class="list" style="margin-bottom:10px"></div>
          <div class="messages" id="messages"></div>
          <div class="compose">
            <button class="btn secondary" id="micBtn" onclick="toggleVoice()">🎤</button>
            <textarea class="field" id="chatInput" placeholder="Напишите сообщение. Markdown поддерживается..." onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendMsg(false)}"></textarea>
            <label class="btn secondary file-label" for="chatFile">📎</label><input id="chatFile" type="file" style="display:none" onchange="uploadChatFile(this)">
            <button class="btn" onclick="sendMsg(false)">➤</button>
          </div>
        </div>
      </div>
      <div class="page" id="voice"><div class="card"><h3>Voice Engine</h3><div class="item-meta">Доступно: запись, автоотправка, прерывание текущей озвучки, индикатор записи и ответа. Wake word работает в браузере как эксперимент: скажите “Эй NEXUS”, затем команду.</div><button class="btn" style="margin-top:14px" onclick="toggleVoice()">Запустить голос</button></div></div>
      <div class="page" id="search"><div class="card"><h3>Поиск по истории</h3><input class="field" id="globalSearch" placeholder="Введите фразу..." oninput="searchHistory('globalSearch','globalResults')"><div id="globalResults" class="list"><div class="item-meta">Введите текст для поиска.</div></div></div></div>
      <div class="page" id="files"><div class="card"><h3>Files</h3><div class="item-meta" style="margin-bottom:12px">Upload business documents directly into the NEXUS chat history.</div><label class="btn" for="chatFilePage">Upload file</label><input id="chatFilePage" type="file" style="display:none" onchange="uploadChatFile(this)"><div id="fileResult" class="list" style="margin-top:12px"></div></div></div>
      <div class="page" id="tasksPage"><div class="grid2"><div class="card"><h3>Tasks</h3><div class="row"><input class="field" id="taskTitle" placeholder="New task"><button class="btn" onclick="createTask()">Add</button></div><div id="tasksList" class="list"></div></div><div class="card"><h3>Command Center</h3><input class="field" id="commandInput" placeholder="добавь задачу проверить продажи" onkeydown="if(event.key==='Enter')runCommand()"><button class="btn" onclick="runCommand()">Run command</button><div id="commandResult" class="item-meta" style="margin-top:12px"></div></div></div></div>
      <div class="page" id="agentsPage"><div class="grid2"><div class="card"><h3>Business agents</h3><div id="agentsList" class="list"></div></div><div class="card"><h3>Create agent</h3><input class="field" id="agentName" placeholder="Agent name"><textarea class="field" id="agentDescription" placeholder="What this agent does"></textarea><button class="btn" onclick="createAgent()">Create agent</button></div></div></div>
      <div class="page" id="settingsPage"><div class="stack"><div class="card"><h3>Integrations</h3><div id="integrationsList" class="status-grid"></div></div><div class="card"><h3>Capabilities</h3><div id="capabilitiesList" class="status-grid"></div></div><div class="card"><h3>Users</h3><div id="usersList" class="list"></div><div class="grid2" style="margin-top:12px"><input class="field" id="newUsername" placeholder="username"><select class="field" id="newUserRole"><option value="employee">employee</option><option value="guest">guest</option><option value="admin">admin</option></select></div><input class="field" id="newUserPassword" placeholder="temporary password"><button class="btn" onclick="saveUser()">Save user</button></div></div></div>
    </section>
  </main>
</div>
<nav class="bottom-nav" id="bottomNav"></nav>
<script>
var navItems=[['dashboard','📊','Dashboard'],['chat','💬','Chat'],['tasksPage','✅','Tasks'],['agentsPage','🧩','Agents'],['settingsPage','⚙️','Settings'],['voice','🎙️','Voice'],['files','📎','Files']];
var currentAudio=null,recognition=null,isListening=false,wakeMode=false,messages=[],editingId=null;
var csrfToken=document.querySelector('meta[name="csrf-token"]').content;
function $(id){return document.getElementById(id)}function esc(t){var d=document.createElement('div');d.textContent=t||'';return d.innerHTML}
function renderNav(){['sideNav','bottomNav'].forEach(function(id){$(id).innerHTML=navItems.map(function(n,i){return '<button class="'+(i===0?'active':'')+'" data-page="'+n[0]+'"><span class="emoji">'+n[1]+'</span>'+n[2]+'</button>'}).join('')});document.querySelectorAll('[data-page]').forEach(function(b){b.onclick=function(){showPage(b.dataset.page,b)}})}renderNav();
function showPage(page,btn){document.querySelectorAll('.page').forEach(function(p){p.classList.remove('active')});$(page).classList.add('active');document.querySelectorAll('[data-page]').forEach(function(x){x.classList.toggle('active',x.dataset.page===page)});$('pageTitle').textContent={dashboard:'Dashboard',chat:'Chat NEXUS',voice:'Voice Engine',search:'Search',files:'Files',tasksPage:'Tasks',agentsPage:'Agents',settingsPage:'Settings'}[page]||'NEXUS';if(page==='tasksPage')loadTasks();if(page==='agentsPage')loadAgents();if(page==='settingsPage')loadSettings()}
function alertMsg(t,ok){$('alert').innerHTML='<div class="alert '+(ok?'ok':'err')+'">'+esc(t)+'</div>';setTimeout(function(){$('alert').innerHTML=''},4200)}
function api(url,opts){opts=opts||{};opts.headers=opts.headers||{};if(opts.method&&opts.method.toUpperCase()==='POST')opts.headers['X-CSRF-Token']=csrfToken;return fetch(url,opts).then(function(r){if(r.status===401){location.href='/login';return}return r.text().then(function(t){try{return JSON.parse(t)}catch(e){throw new Error('Сервер вернул не JSON: '+url)}})})}
function md(t){var s=esc(t);s=s.replace(/```([\\s\\S]*?)```/g,function(_,c){return '<pre><code>'+c+'</code></pre>'});s=s.replace(/`([^`]+)`/g,'<code>$1</code>');s=s.replace(/\\*\\*([^*]+)\\*\\*/g,'<strong>$1</strong>');s=s.replace(/\\*([^*]+)\\*/g,'<em>$1</em>');s=s.replace(/^[-•] (.*)$/gm,'<li>$1</li>');s=s.replace(/(<li>[\\s\\S]*?<\\/li>)/g,'<ul>$1</ul>');return s.replace(/\\n/g,'<br>')}
function updateClock(){$('clock').textContent=new Date().toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'})}setInterval(updateClock,1000);updateClock();
function loadStats(){api('/stats').then(function(d){if(!d)return;$('sMessages').textContent=d.messages||0;$('sVoice').textContent=d.voice||0;$('sFiles').textContent=d.files||0;$('sBriefings').textContent=d.briefings||0})}
function loadHistory(){api('/history').then(function(d){messages=d.messages||[];renderMessages();searchHistory()})}
function renderMessages(){var box=$('messages');box.innerHTML='';messages.slice(-40).forEach(function(m){var w=document.createElement('div');w.className='msgwrap '+(m.role==='user'?'user':'ai');w.dataset.id=m.id;w.innerHTML='<div class="speaker">'+(m.role==='user'?'Никита':'NEXUS')+'</div><div class="bubble '+(m.role==='user'?'user':'ai')+'">'+md(m.content)+'</div>'+(m.role==='user'?'<button class="edit-btn" onclick="editMessage(\\''+m.id+'\\')">Редактировать</button>':'');box.appendChild(w)});box.scrollTop=box.scrollHeight}
function appendMessage(role,text,id){messages.push({id:id||String(Date.now()),role:role,content:text});renderMessages()}
function editMessage(id){var m=messages.find(function(x){return x.id===id});if(!m)return;editingId=id;$('chatInput').value=m.content;$('chatInput').focus();alertMsg('Редактируйте текст и отправьте заново.',true)}
function sendMsg(voice){var inp=$('chatInput'),text=inp.value.trim();if(!text)return;if(currentAudio){currentAudio.pause();currentAudio=null}inp.value='';var payload={message:text,voice:voice,edit_id:editingId};editingId=null;appendMessage('user',text);var aiId='ai-'+Date.now();appendMessage('assistant','',aiId);var aiMsg=messages[messages.length-1];$('speaking').classList.add('on');fetch('/chat_stream',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify(payload)}).then(function(r){var reader=r.body.getReader(),dec=new TextDecoder();function pump(){return reader.read().then(function(x){if(x.done){$('speaking').classList.remove('on');loadStats();loadHistory();return}dec.decode(x.value).split('\\n\\n').forEach(function(line){if(line.indexOf('data: ')===0){var data=JSON.parse(line.slice(6));if(data.token){aiMsg.content+=data.token;renderMessages()}if(data.audio){playB64(data.audio)}}});return pump()})}return pump()}).catch(function(e){aiMsg.content='Ошибка: '+e.message;renderMessages();$('speaking').classList.remove('on')})}
function playB64(b64){if(currentAudio)currentAudio.pause();currentAudio=new Audio('data:audio/mp3;base64,'+b64);currentAudio.play().catch(function(){})}
function quickAsk(){var v=$('quickInput').value.trim();if(!v)return;$('quickInput').value='';$('quickResult').textContent='Думаю...';api('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:v})}).then(function(d){$('quickResult').innerHTML=md(d.reply||'');loadStats();loadHistory()})}
function searchHistory(inputId,outId){var q=($(inputId||'historyQuery')?.value||'').toLowerCase();var out=$(outId||'historyResults');if(!out)return;if(!q){out.innerHTML='<div class="item-meta">Введите текст для поиска.</div>';return}var found=messages.filter(function(m){return m.content.toLowerCase().indexOf(q)>=0}).slice(-8);out.innerHTML=found.map(function(m){return '<div class="item"><div class="item-title">'+(m.role==='user'?'Никита':'NEXUS')+'</div><div class="item-meta">'+esc(m.content).slice(0,180)+'</div></div>'}).join('')||'<div class="item-meta">Ничего не найдено</div>'}
function toggleVoice(){if(currentAudio){currentAudio.pause();currentAudio=null;$('speaking').classList.remove('on')}if(!window.SpeechRecognition&&!window.webkitSpeechRecognition){alertMsg('Голосовой ввод доступен в Chrome.',false);return}if(isListening&&recognition){recognition.stop();return}var SR=window.SpeechRecognition||window.webkitSpeechRecognition;recognition=new SR();recognition.lang='ru-RU';recognition.continuous=false;recognition.interimResults=false;recognition.onstart=function(){isListening=true;$('micBtn').classList.add('recording');$('micBtn').textContent='⏹'};recognition.onend=function(){isListening=false;$('micBtn').classList.remove('recording');$('micBtn').textContent='🎤'};recognition.onerror=recognition.onend;recognition.onresult=function(e){var t=e.results[0][0].transcript;if(t.toLowerCase().indexOf('эй nexus')>=0||t.toLowerCase().indexOf('эй нексус')>=0){t=t.replace(/эй nexus/ig,'').replace(/эй нексус/ig,'').trim();wakeMode=true}if(t){$('chatInput').value=t;sendMsg(true)}};recognition.start()}
function uploadChatFile(input){var f=input.files[0];if(!f)return;var fd=new FormData();fd.append('file',f);api('/upload_chat',{method:'POST',body:fd}).then(function(d){alertMsg(d.message||'Файл загружен',!!d.success);if($('fileResult'))$('fileResult').innerHTML='<div class="item"><div class="item-title">'+esc(f.name)+'</div><div class="item-meta">'+esc(d.message||'Файл загружен')+'</div></div>';loadStats();loadHistory()})}
function loadBriefing(){var city=($('briefCity').value||'Kyiv').trim();$('briefingResult').textContent='Собираю брифинг...';api('/morning_briefing?city='+encodeURIComponent(city)).then(function(d){$('briefingResult').innerHTML=md(d.briefing||d.error||'');loadStats()}).catch(function(e){$('briefingResult').textContent=e.message})}
function loadTasks(){api('/tasks').then(function(d){var tasks=d.tasks||[];$('tasksList').innerHTML=tasks.map(function(t){return '<div class="item"><div class="item-title">'+esc(t.title)+'</div><div class="item-meta">'+esc(t.status||'open')+' · '+esc(t.owner||'')+'</div></div>'}).join('')||'<div class="item-meta">No tasks yet.</div>'})}
function createTask(){var title=($('taskTitle').value||'').trim();if(!title)return;api('/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:title})}).then(function(d){$('taskTitle').value='';alertMsg(d.success?'Task created':(d.error||'Task error'),!!d.success);loadTasks()})}
function runCommand(){var command=($('commandInput').value||'').trim();if(!command)return;api('/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:command})}).then(function(d){$('commandResult').textContent=JSON.stringify(d,null,2);loadTasks();loadStats()})}
function loadAgents(){api('/agents').then(function(d){var agents=d.agents||[];$('agentsList').innerHTML=agents.map(function(a){return '<div class="item"><div class="item-title">'+esc(a.name)+'</div><div class="item-meta">'+esc(a.status||'')+'<br>'+esc(a.description||'')+'</div></div>'}).join('')||'<div class="item-meta">No agents yet.</div>'})}
function createAgent(){var name=($('agentName').value||'').trim();var description=($('agentDescription').value||'').trim();if(!name)return;api('/agents',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,description:description})}).then(function(d){$('agentName').value='';$('agentDescription').value='';alertMsg(d.success?'Agent created':(d.error||'Agent error'),!!d.success);loadAgents()})}
function statusClass(status){if(status==='configured'||status==='done')return 'status-ok';if(status==='planned'||String(status).indexOf('planned')>=0)return 'status-plan';return 'status-warn'}
function loadSettings(){api('/capabilities').then(function(d){var integrations=d.integrations||{};$('integrationsList').innerHTML=Object.keys(integrations).map(function(k){var s=integrations[k].status;return '<div class="item"><div class="item-title">'+esc(k)+'</div><div class="item-meta '+statusClass(s)+'">'+esc(s)+'</div></div>'}).join('');var caps=d.capabilities||{};$('capabilitiesList').innerHTML=Object.keys(caps).map(function(k){var s=caps[k];return '<div class="item"><div class="item-title">'+esc(k)+'</div><div class="item-meta '+statusClass(s)+'">'+esc(s)+'</div></div>'}).join('')});api('/users').then(function(d){var users=d.users||[];$('usersList').innerHTML=users.map(function(u){return '<div class="item"><div class="item-title">'+esc(u.username)+'</div><div class="item-meta">'+esc(u.role)+'</div></div>'}).join('')})}
function saveUser(){var username=($('newUsername').value||'').trim();var role=$('newUserRole').value;var password=$('newUserPassword').value;if(!username)return;api('/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:username,role:role,password:password})}).then(function(d){alertMsg(d.success?'User saved':(d.error||'User error'),!!d.success);loadSettings()})}
loadStats();loadHistory();setInterval(loadStats,8000);
</script>
</body>
</html>"""


def logged_in():
    return session.get("logged_in") is True


def message_id():
    return datetime.utcnow().strftime("%Y%m%d%H%M%S%f")


def public_history():
    return [{"id": m.get("id", ""), "role": m.get("role", ""), "content": m.get("content", "")} for m in history[-100:]]


def persist_history():
    save_json(MEMORY_FILE, history[-160:])


def persist_state():
    save_json(STATE_FILE, state)


def current_user():
    return {
        "username": session.get("username", "anonymous"),
        "role": session.get("role", "guest"),
    }


def has_role(*roles):
    return session.get("role") in roles


def require_roles(*roles):
    if not logged_in():
        return jsonify({"error": "Нужен вход."}), 401
    if roles and not has_role(*roles):
        return jsonify({"error": "Недостаточно прав."}), 403
    return None


def integration_status():
    result = {}
    for name, marker in state.get("integrations", {}).items():
        if marker == "planned":
            result[name] = {"status": "planned"}
        elif marker.startswith("env:"):
            env_names = marker[4:].split("+")
            configured = all(bool(get_env(env_name, "").strip()) for env_name in env_names)
            result[name] = {"status": "configured" if configured else "missing_env", "env": env_names}
        else:
            result[name] = {"status": "unknown"}
    return result


def storage_status():
    database_url = get_env("DATABASE_URL", "").strip()
    if database_url.startswith(("postgres://", "postgresql://")):
        return {
            "backend": "postgresql_ready",
            "configured": True,
            "active": False,
            "note": "DATABASE_URL is set. Migration layer is the next step before switching writes from JSON.",
        }
    if database_url:
        return {
            "backend": "database_url_unknown",
            "configured": True,
            "active": False,
            "note": "DATABASE_URL is set, but it is not PostgreSQL.",
        }
    return {
        "backend": "json_files",
        "configured": False,
        "active": True,
        "note": "Current storage uses JSON files. Set DATABASE_URL for PostgreSQL migration readiness.",
    }


def capability_map():
    return {
        "text_chat": "done",
        "context_memory": "done",
        "web_dashboard": "done",
        "password_auth": "done",
        "roles": "basic",
        "csrf": "done",
        "rate_limit": "done",
        "security_headers": "done",
        "streaming_responses": "done",
        "markdown_chat": "done",
        "chat_file_upload": "done",
        "message_editing": "done",
        "history_search": "done",
        "voice_input": "browser_basic",
        "wake_word": "browser_experimental",
        "voice_interruption": "basic",
        "weather_briefing": "env_required",
        "nova_poshta": "env_required",
        "pdf_invoices": "done",
        "business_agents": "basic_templates",
        "tasks": "basic",
        "postgresql": "planned",
        "fastapi_nextjs": "planned_rearchitecture",
        "docker": "planned",
        "2fa": "planned",
        "vector_rag": "partial_files_only",
        "monobank": "planned",
        "google_maps": "planned",
        "whatsapp": "planned",
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        username = request.form.get("username", "admin").strip() or "admin"
        password = request.form.get("password", "")
        if password == require_web_password():
            session["logged_in"] = True
            session["username"] = username
            session["role"] = "admin"
            return redirect(url_for("index"))
        for user in state.get("users", []):
            if user.get("username") == username and user.get("password") and user.get("password") == password:
                session["logged_in"] = True
                session["username"] = username
                session["role"] = user.get("role", "guest")
                return redirect(url_for("index"))
        error = "Неверный пароль."
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    if not logged_in():
        return redirect(url_for("login"))
    return render_template_string(
        HTML,
        email=get_env("GMAIL", "photobusines63@gmail.com"),
        csrf_token=get_csrf_token(),
    )


@app.route("/stats")
def get_stats():
    return jsonify(stats)


@app.route("/healthz")
def healthz():
    return jsonify(
        {
            "ok": True,
            "app": "nexus_web",
            "time": datetime.utcnow().isoformat() + "Z",
            "storage": storage_status(),
            "features": {
                "stats": True,
                "chat_stream": True,
                "csrf": True,
                "weather": bool(get_env("OPENWEATHER_API_KEY", "").strip()),
                "nova_poshta": bool(get_env("NOVA_POSHTA_API_KEY", "").strip()),
            },
        }
    )


@app.route("/capabilities")
def capabilities():
    guard = require_roles("admin", "employee")
    if guard:
        return guard
    return jsonify(
        {
            "user": current_user(),
            "capabilities": capability_map(),
            "integrations": integration_status(),
            "storage": storage_status(),
            "roadmap_next": [
                "2FA",
                "PostgreSQL migration",
                "Vector RAG over uploaded files",
                "Monobank import",
                "Google Maps reviews",
                "WhatsApp bot",
                "Docker deployment",
            ],
        }
    )


@app.route("/users", methods=["GET", "POST"])
def users():
    guard = require_roles("admin")
    if guard:
        return guard
    if request.method == "GET":
        safe_users = [{k: v for k, v in user.items() if k != "password"} for user in state.get("users", [])]
        return jsonify({"users": safe_users})

    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    role = str(data.get("role", "guest")).strip()
    password = str(data.get("password", "")).strip()
    if not username or role not in {"admin", "employee", "guest"}:
        return jsonify({"success": False, "error": "Укажите username и роль admin/employee/guest."})
    for user in state.get("users", []):
        if user.get("username") == username:
            user["role"] = role
            if password:
                user["password"] = password
            persist_state()
            return jsonify({"success": True, "updated": True})
    state.setdefault("users", []).append({"username": username, "role": role, "password": password})
    persist_state()
    return jsonify({"success": True, "created": True})


@app.route("/agents", methods=["GET", "POST"])
def agents():
    guard = require_roles("admin", "employee")
    if guard:
        return guard
    if request.method == "GET":
        return jsonify({"agents": state.get("agents", [])})

    data = request.get_json(silent=True) or {}
    agent_id = str(data.get("id", "")).strip() or f"agent_{message_id()}"
    agent = {
        "id": agent_id,
        "name": str(data.get("name", "Новый AI-агент")).strip(),
        "description": str(data.get("description", "")).strip(),
        "status": str(data.get("status", "draft")).strip(),
        "created_by": session.get("username", "admin"),
    }
    state.setdefault("agents", []).append(agent)
    persist_state()
    return jsonify({"success": True, "agent": agent})


@app.route("/tasks", methods=["GET", "POST"])
def tasks():
    guard = require_roles("admin", "employee")
    if guard:
        return guard
    if request.method == "GET":
        return jsonify({"tasks": state.get("tasks", [])})

    data = request.get_json(silent=True) or {}
    title = str(data.get("title", "")).strip()
    if not title:
        return jsonify({"success": False, "error": "Укажите задачу."})
    task = {
        "id": message_id(),
        "title": title,
        "status": "open",
        "owner": session.get("username", "admin"),
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    state.setdefault("tasks", []).append(task)
    persist_state()
    return jsonify({"success": True, "task": task})


@app.route("/command", methods=["POST"])
def command():
    guard = require_roles("admin", "employee")
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    text = str(data.get("command", "")).strip()
    if not text:
        return jsonify({"success": False, "error": "Команда пустая."})

    stats["commands"] += 1
    lower = text.lower()
    if lower.startswith("добавь задачу") or lower.startswith("создай задачу"):
        title = text.split(" ", 2)[-1].strip()
        task = {
            "id": message_id(),
            "title": title,
            "status": "open",
            "owner": session.get("username", "admin"),
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        state.setdefault("tasks", []).append(task)
        persist_state()
        return jsonify({"success": True, "action": "task_created", "task": task})

    if "брифинг" in lower:
        city = data.get("city", get_env("DEFAULT_WEATHER_CITY", "Kyiv"))
        return jsonify({"success": True, "action": "briefing_hint", "next": f"/morning_briefing?city={city}"})

    answer = handle_chat({"message": text}, stream=False)
    return jsonify({"success": True, "action": "chat", "reply": answer})


@app.route("/history")
def get_history():
    if not logged_in():
        return jsonify({"messages": []}), 401
    return jsonify({"messages": public_history()})


@app.route("/chat", methods=["POST"])
def chat():
    if not logged_in():
        return jsonify({"reply": "Нужен вход в систему."}), 401
    data = request.get_json(silent=True) or {}
    answer = handle_chat(data, stream=False)
    return jsonify({"reply": answer, "audio": make_audio(answer) if data.get("voice") else None})


@app.route("/chat_stream", methods=["POST"])
def chat_stream():
    if not logged_in():
        return jsonify({"reply": "Нужен вход в систему."}), 401
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


def handle_chat(data, stream=False):
    msg = (data.get("message") or "").strip()
    if not msg:
        return "Напишите сообщение."

    edit_id = data.get("edit_id")
    if edit_id:
        for item in history:
            if item.get("id") == edit_id and item.get("role") == "user":
                item["content"] = msg
                stats["edits"] += 1
                break
    else:
        history.append({"id": message_id(), "role": "user", "content": msg})

    stats["messages"] += 1
    if data.get("voice"):
        stats["voice"] += 1

    try:
        answer = ask_ai([{"role": "system", "content": SYSTEM}, *history[-20:]])
    except Exception as exc:
        answer = "Ошибка AI: " + str(exc)

    history.append({"id": message_id(), "role": "assistant", "content": answer})
    persist_history()
    return answer


@app.route("/upload_chat", methods=["POST"])
def upload_chat():
    if not logged_in():
        return jsonify({"success": False, "message": "Нужен вход."}), 401
    file = request.files.get("file")
    if not file:
        return jsonify({"success": False, "message": "Файл не выбран."})
    safe_name = Path(file.filename or "file").name
    target = UPLOAD_DIR / f"{message_id()}_{safe_name}"
    file.save(target)
    stats["files"] += 1
    note = f"Файл загружен в чат: {safe_name}"
    history.append({"id": message_id(), "role": "user", "content": note})
    persist_history()
    return jsonify({"success": True, "message": note})


@app.route("/weather")
def weather():
    if not logged_in():
        return jsonify({"error": "Нужен вход."}), 401
    city = request.args.get("city", get_env("DEFAULT_WEATHER_CITY", "Kyiv")).strip() or "Kyiv"
    return jsonify(load_weather(city))


@app.route("/morning_briefing")
def morning_briefing():
    if not logged_in():
        return jsonify({"error": "Нужен вход."}), 401

    city = request.args.get("city", get_env("DEFAULT_WEATHER_CITY", "Kyiv")).strip() or "Kyiv"
    weather_data = load_weather(city)
    stats["briefings"] += 1

    weather_line = weather_data.get("summary") or weather_data.get("error") or "Погода пока недоступна."
    recent = "\n".join(f"- {m.get('role')}: {m.get('content')}" for m in history[-8:])
    prompt = (
        "Собери короткий утренний брифинг для Никиты на русском языке.\n"
        f"Погода: {weather_line}\n"
        f"Последний контекст:\n{recent}\n"
        "Формат: 1) погода, 2) фокус дня, 3) 3 действия."
    )

    try:
        briefing = ask_ai([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}])
    except Exception:
        briefing = (
            f"**Погода:** {weather_line}\n"
            "- Проверь срочные сообщения.\n"
            "- Выбери 1 главный фокус на день.\n"
            "- Зафиксируй следующие 3 действия."
        )

    return jsonify({"briefing": briefing, "weather": weather_data})


def load_weather(city):
    api_key = get_env("OPENWEATHER_API_KEY", "").strip()
    if not api_key:
        return {
            "city": city,
            "error": "OPENWEATHER_API_KEY не задан. Добавьте ключ в Render Environment.",
        }

    params = urllib.parse.urlencode(
        {
            "q": city,
            "appid": api_key,
            "units": "metric",
            "lang": "ru",
        }
    )
    url = "https://api.openweathermap.org/data/2.5/weather?" + params
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
        temp = round(data.get("main", {}).get("temp", 0))
        feels = round(data.get("main", {}).get("feels_like", temp))
        wind = data.get("wind", {}).get("speed", 0)
        desc = (data.get("weather") or [{}])[0].get("description", "")
        name = data.get("name", city)
        return {
            "city": name,
            "temp": temp,
            "feels_like": feels,
            "wind": wind,
            "description": desc,
            "summary": f"{name}: {temp}°C, ощущается как {feels}°C, {desc}, ветер {wind} м/с.",
        }
    except Exception as exc:
        return {"city": city, "error": str(exc)}


@app.route("/nova_poshta/track")
def nova_poshta_track():
    if not logged_in():
        return jsonify({"error": "Нужен вход."}), 401
    tracking_number = request.args.get("number", "").strip()
    if not tracking_number:
        return jsonify({"success": False, "error": "Укажите номер ТТН."})

    api_key = get_env("NOVA_POSHTA_API_KEY", "").strip()
    if not api_key:
        return jsonify(
            {
                "success": False,
                "error": "NOVA_POSHTA_API_KEY не задан. Добавьте ключ в Render Environment.",
            }
        )

    payload = {
        "apiKey": api_key,
        "modelName": "TrackingDocument",
        "calledMethod": "getStatusDocuments",
        "methodProperties": {"Documents": [{"DocumentNumber": tracking_number}]},
    }
    request_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.novaposhta.ua/v2.0/json/",
        data=request_data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
        return jsonify(data)
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)})


@app.route("/invoice_pdf", methods=["POST"])
def invoice_pdf():
    if not logged_in():
        return jsonify({"error": "Нужен вход."}), 401
    data = request.get_json(silent=True) or {}
    client = str(data.get("client", "Client"))
    invoice_id = str(data.get("invoice_id", datetime.utcnow().strftime("%Y%m%d%H%M")))
    items = data.get("items") or []
    if not isinstance(items, list) or not items:
        items = [{"name": "Service", "qty": 1, "price": float(data.get("amount", 0) or 0)}]

    lines = [f"NEXUS invoice #{invoice_id}", f"Client: {client}", ""]
    total = 0.0
    for item in items:
        name = str(item.get("name", "Item"))
        qty = float(item.get("qty", 1) or 1)
        price = float(item.get("price", 0) or 0)
        amount = qty * price
        total += amount
        lines.append(f"{name} | {qty:g} x {price:.2f} = {amount:.2f}")
    lines.extend(["", f"Total: {total:.2f} UAH"])

    pdf = build_simple_pdf(lines)
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="invoice_{invoice_id}.pdf"'},
    )


def build_simple_pdf(lines):
    def clean(text):
        return str(text).encode("latin-1", errors="replace").decode("latin-1")

    content = ["BT", "/F1 14 Tf", "50 790 Td"]
    for idx, line in enumerate(lines):
        if idx:
            content.append("0 -22 Td")
        escaped = clean(line).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content.append(f"({escaped}) Tj")
    content.append("ET")
    stream = "\n".join(content).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for idx, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{idx} 0 obj\n".encode("ascii"))
        pdf.extend(obj)
        pdf.extend(b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        f"trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(pdf)


def make_audio(text):
    try:
        client = OpenAI(api_key=require_openai_key())
        audio_response = client.audio.speech.create(
            model=get_env("OPENAI_TTS_MODEL", "tts-1"),
            voice=get_env("OPENAI_TTS_VOICE", "onyx"),
            input=text[:4000],
        )
        return base64.b64encode(audio_response.content).decode("ascii")
    except Exception:
        return None


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    print(f"NEXUS запущен: http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
