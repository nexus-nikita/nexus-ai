import base64
import email
import hashlib
import imaplib
import json
import os
import smtplib
from datetime import datetime, timedelta
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template_string, request, session
from openai import OpenAI

try:
    import chromadb
except Exception:
    chromadb = None

try:
    from docx import Document
except Exception:
    Document = None

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
except Exception:
    service_account = None
    build = None

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

try:
    from nexus_common import (
        DEFAULT_PROFILE,
        build_system_prompt,
        get_env,
        get_web_session_secret,
        require_openai_key,
        require_web_password,
    )
except Exception:
    DEFAULT_PROFILE = {"name": "Никита", "business": "общепит, аква бизнес, продвижение", "location": "Украина"}

    def get_env(name, default=""):
        return os.getenv(name, default)

    def require_openai_key():
        key = get_env("OPENAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("Не задан OPENAI_API_KEY")
        return key

    def require_web_password():
        password = get_env("WEB_PASSWORD", "").strip()
        if not password:
            raise RuntimeError("Не задан WEB_PASSWORD")
        return password

    def get_web_session_secret():
        return get_env("WEB_SESSION_SECRET", "change-me")

    def build_system_prompt(profile=None):
        profile = profile or DEFAULT_PROFILE
        return (
            "Ты NEXUS - приватный AI-помощник и центр управления задачами.\n"
            f"Пользователь: {profile['name']}.\n"
            f"Бизнес: {profile['business']}.\n"
            f"Локация: {profile['location']}.\n"
            "Отвечай на русском языке, коротко, конкретно и по делу."
        )


BASE_DIR = Path(__file__).resolve().parent
PROFILE_FILE = BASE_DIR / "nexus_profile.json"
MEMORY_FILE = BASE_DIR / "nexus_memory.json"
CRM_FILE = BASE_DIR / "crm_data.json"
ANALYTICS_FILE = BASE_DIR / "analytics_data.json"
TASKS_FILE = BASE_DIR / "tasks.json"
USERS_FILE = BASE_DIR / "users.json"

app = Flask(__name__)
app.secret_key = get_web_session_secret()


# ── Helpers ──────────────────────────────────────────────────────────────────

def read_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def require_login():
    return session.get("logged_in") is True


# ── App state ─────────────────────────────────────────────────────────────────

profile = read_json(PROFILE_FILE, DEFAULT_PROFILE.copy())
history = read_json(MEMORY_FILE, [])
stats = {"messages": len([m for m in history if m.get("role") == "user"]), "voice": 0, "emails": 0, "events": 0}
SYSTEM = build_system_prompt(profile)


def require_openai():
    return OpenAI(api_key=require_openai_key())


def init_docs_collection():
    if chromadb is None:
        return None
    try:
        client = chromadb.Client()
        return client.get_or_create_collection("nexus_docs")
    except Exception:
        return None


collection = init_docs_collection()


def init_calendar():
    if service_account is None or build is None:
        return None
    service_file = get_env("SERVICE_ACCOUNT_FILE", str(BASE_DIR / "service_account.json"))
    if not Path(service_file).exists():
        return None
    try:
        creds = service_account.Credentials.from_service_account_file(
            service_file,
            scopes=["https://www.googleapis.com/auth/calendar"],
        )
        return build("calendar", "v3", credentials=creds)
    except Exception:
        return None


calendar_service = init_calendar()
CALENDAR_ID = get_env("CALENDAR_ID", "primary")


def init_sheets():
    if service_account is None or build is None:
        return None
    service_file = get_env("SERVICE_ACCOUNT_FILE", str(BASE_DIR / "service_account.json"))
    if not Path(service_file).exists():
        return None
    try:
        creds = service_account.Credentials.from_service_account_file(
            service_file,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        return build("sheets", "v4", credentials=creds)
    except Exception:
        return None


sheets_service = init_sheets()


# ── HTML ──────────────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NEXUS - вход</title>
<style>
:root{--bg:#071016;--line:#263b45;--text:#eef7f8;--muted:#8ea3aa;--cyan:#37d7e8;--green:#47e08c}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(circle at 20% 10%,#163744 0,#071016 34%,#05090d 100%);font-family:Inter,'Segoe UI',Arial,sans-serif;color:var(--text);padding:24px}
.shell{width:min(420px,100%);border:1px solid var(--line);background:linear-gradient(180deg,rgba(20,36,45,.96),rgba(9,17,23,.96));border-radius:18px;padding:28px;box-shadow:0 30px 80px rgba(0,0,0,.45)}
.brand{display:flex;align-items:center;gap:12px;margin-bottom:24px}.mark{width:42px;height:42px;border-radius:12px;background:linear-gradient(135deg,var(--cyan),var(--green));display:grid;place-items:center;color:#051014;font-weight:900;font-size:18px}.name{font-size:22px;font-weight:900;letter-spacing:5px}.sub{color:var(--muted);font-size:13px;margin-top:3px}
label{display:block;color:var(--muted);font-size:12px;margin-bottom:6px;margin-top:12px}
input{width:100%;height:46px;border-radius:12px;border:1px solid var(--line);background:#09131a;color:var(--text);padding:0 14px;font-size:15px;outline:none}
input:focus{border-color:var(--cyan);box-shadow:0 0 0 3px rgba(55,215,232,.14)}
button{width:100%;height:46px;margin-top:16px;border:0;border-radius:12px;background:linear-gradient(135deg,var(--cyan),var(--green));color:#051014;font-weight:900;cursor:pointer;font-size:15px}
.error{min-height:20px;color:#ff7b7b;font-size:13px;margin-top:10px}
.hint{color:var(--muted);font-size:12px;margin-top:10px;text-align:center}
</style>
</head>
<body>
<form class="shell" method="post">
  <div class="brand"><div class="mark">N</div><div><div class="name">NEXUS</div><div class="sub">Центр управления</div></div></div>
  <label for="login_field">Логин (имя или email)</label>
  <input id="login_field" name="login" type="text" autocomplete="username" autofocus placeholder="Никита или admin@email.com">
  <label for="password">Пароль</label>
  <input id="password" name="password" type="password" autocomplete="current-password" placeholder="••••••••">
  <button type="submit">Войти</button>
  <div class="error">{{ error }}</div>
  <div class="hint">Или оставь логин пустым и введи мастер-пароль</div>
</form>
</body>
</html>"""

HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NEXUS</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
:root{
  --bg:#070d11;--side:#0b151b;--panel:#101c23;--panel2:#14242d;--line:#263b45;
  --text:#edf7f8;--muted:#8ba1a8;--soft:#c3d1d5;--cyan:#37d7e8;--green:#47e08c;--amber:#f4b860;--red:#ff7474;
  --shadow:0 22px 60px rgba(0,0,0,.35);--r:14px
}
*{box-sizing:border-box}html,body{height:100%}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,'Segoe UI',Arial,sans-serif}
button,input,textarea,select{font:inherit}button{cursor:pointer}
.app{height:100vh;display:grid;grid-template-columns:256px 1fr;overflow:hidden;background:radial-gradient(circle at 70% -10%,rgba(55,215,232,.14),transparent 38%),var(--bg)}
.sidebar{background:linear-gradient(180deg,var(--side),#05090c);border-right:1px solid var(--line);display:flex;flex-direction:column;min-width:0;overflow-y:auto}
.brand{padding:20px 20px 16px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px;flex-shrink:0}
.mark{width:40px;height:40px;border-radius:12px;background:linear-gradient(135deg,var(--cyan),var(--green));display:grid;place-items:center;color:#041115;font-weight:900;font-size:18px}
.brand-title{font-size:19px;font-weight:900;letter-spacing:5px}.brand-sub{font-size:11px;color:var(--muted);margin-top:3px}
.nav{padding:10px 10px;display:grid;gap:2px}
.nav-section{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:1.5px;padding:10px 12px 4px;opacity:.6}
.nav button{height:40px;border:0;background:transparent;color:var(--muted);border-radius:10px;padding:0 12px;text-align:left;display:flex;align-items:center;gap:10px;font-size:13px;transition:all .15s}
.nav button:hover{background:rgba(255,255,255,.04);color:var(--text)}
.nav button.active{background:rgba(55,215,232,.12);color:var(--cyan);box-shadow:inset 3px 0 0 var(--cyan)}
.ico{width:22px;height:22px;display:grid;place-items:center;border-radius:7px;background:#12242d;color:var(--soft);font-size:11px;font-weight:900;flex-shrink:0}
.nav button.active .ico{background:rgba(55,215,232,.18);color:var(--cyan)}
.side-bottom{margin-top:auto;padding:14px 18px;border-top:1px solid var(--line);color:var(--muted);font-size:13px;flex-shrink:0}
.online{display:flex;align-items:center;gap:8px}.dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 12px var(--green)}
.main{min-width:0;display:flex;flex-direction:column;overflow:hidden}
.topbar{height:60px;border-bottom:1px solid var(--line);background:rgba(10,18,24,.82);backdrop-filter:blur(14px);display:flex;align-items:center;justify-content:space-between;padding:0 24px;flex-shrink:0}
.title{font-weight:850;font-size:17px}.meta{display:flex;align-items:center;gap:14px;color:var(--muted);font-size:13px}
.logout{color:var(--muted);text-decoration:none;border:1px solid var(--line);padding:7px 12px;border-radius:10px;transition:all .15s}
.logout:hover{border-color:var(--cyan);color:var(--cyan)}
.content{overflow:auto;padding:20px;min-height:0;flex:1}
.page{display:none}.page.active{display:block}.stack{display:grid;gap:16px}
.grid4{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}
.grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.card{background:linear-gradient(180deg,rgba(20,36,45,.92),rgba(13,23,30,.92));border:1px solid var(--line);border-radius:var(--r);padding:18px;box-shadow:var(--shadow)}
.card h3{margin:0 0 14px;font-size:12px;color:var(--cyan);text-transform:uppercase;letter-spacing:1.5px}
.stat{min-height:108px;display:flex;flex-direction:column;justify-content:space-between}
.stat .num{font-size:32px;font-weight:900;color:var(--text)}.stat .lab{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px}.stat .hint{font-size:12px;color:var(--soft)}
.field{width:100%;border:1px solid var(--line);background:#0a141b;color:var(--text);border-radius:11px;min-height:42px;padding:0 13px;outline:none;margin-bottom:10px}
.field:focus{border-color:var(--cyan);box-shadow:0 0 0 3px rgba(55,215,232,.1)}
textarea.field{padding:10px 13px;resize:vertical;line-height:1.5}select.field{height:42px}
.btn{border:0;border-radius:11px;min-height:40px;padding:0 16px;background:linear-gradient(135deg,var(--cyan),var(--green));color:#041115;font-weight:700;font-size:13px;transition:opacity .15s}
.btn:hover{opacity:.88}.btn.secondary{background:#0a141b;color:var(--cyan);border:1px solid var(--line)}.btn.danger{background:var(--red);color:#210808}
.btn.sm{min-height:32px;padding:0 12px;font-size:12px}
.row{display:flex;gap:10px;align-items:flex-start}.row .field{margin:0}
.chatbox{height:calc(100vh - 148px);display:flex;flex-direction:column}
.messages{flex:1;overflow:auto;display:flex;flex-direction:column;gap:10px;padding-right:4px}
.bubble{max-width:78%;padding:11px 14px;border-radius:14px;line-height:1.6;white-space:pre-wrap;font-size:14px}
.bubble.user{align-self:flex-end;background:#183447}.bubble.ai{align-self:flex-start;background:#0f2a24;border:1px solid rgba(71,224,140,.18)}
.speaker{font-size:10px;text-transform:uppercase;letter-spacing:1.5px;color:var(--muted);margin-bottom:3px}
.compose{border-top:1px solid var(--line);padding-top:12px;display:flex;gap:10px}.compose textarea{margin:0;min-height:44px;max-height:110px}
.list{display:grid;gap:8px}
.item{background:#0a141b;border:1px solid var(--line);border-radius:12px;padding:12px}
.item-title{font-weight:600;margin-bottom:4px;font-size:14px}.item-meta{color:var(--muted);font-size:13px;line-height:1.45}
.drop{border:1.5px dashed #35515d;border-radius:13px;padding:24px;text-align:center;color:var(--muted);background:#0a141b;cursor:pointer;transition:border-color .15s}
.drop:hover{border-color:var(--cyan)}
.alert{border-radius:12px;padding:10px 14px;margin-bottom:12px;font-size:13px}
.alert.ok{background:rgba(71,224,140,.1);color:var(--green);border:1px solid rgba(71,224,140,.25)}
.alert.err{background:rgba(255,116,116,.1);color:var(--red);border:1px solid rgba(255,116,116,.25)}
.badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:600}
.badge-active{background:rgba(71,224,140,.18);color:var(--green)}.badge-potential{background:rgba(244,184,96,.18);color:var(--amber)}.badge-inactive{background:rgba(255,116,116,.18);color:var(--red)}
.period-btns{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}
.period-btn{background:#0a141b;border:1px solid var(--line);color:var(--muted);padding:6px 14px;border-radius:8px;cursor:pointer;font-size:13px;transition:all .15s}
.period-btn.active,.period-btn:hover{background:rgba(55,215,232,.1);color:var(--cyan);border-color:var(--cyan)}
.history-entry{border-left:3px solid var(--cyan);border-radius:0 10px 10px 0;background:#0a141b;padding:10px 12px;margin-bottom:6px}
canvas{max-height:230px}
@media(max-width:900px){.app{grid-template-columns:1fr}.sidebar{display:none}.grid4,.grid3,.grid2{grid-template-columns:1fr 1fr}.content{padding:14px}.topbar{padding:0 14px}.chatbox{height:calc(100vh-116px)}.bubble{max-width:92%}}
@media(max-width:600px){.grid4,.grid3,.grid2{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="brand">
      <div class="mark">N</div>
      <div><div class="brand-title">NEXUS</div><div class="brand-sub">ЦЕНТР УПРАВЛЕНИЯ</div></div>
    </div>
    <nav class="nav">
      <div class="nav-section">Главное</div>
      <button class="active" data-page="dashboard"><span class="ico">D</span>Dashboard</button>
      <button data-page="chat"><span class="ico">AI</span>Чат NEXUS</button>
      <div class="nav-section">Бизнес</div>
      <button data-page="crm"><span class="ico">👥</span>CRM клиенты</button>
      <button data-page="analytics"><span class="ico">₴</span>Аналитика</button>
      <button data-page="tasks"><span class="ico">T</span>Задачи</button>
      <div class="nav-section">Инструменты</div>
      <button data-page="email"><span class="ico">@</span>Email</button>
      <button data-page="calendar"><span class="ico">C</span>Календарь</button>
      <button data-page="sheets"><span class="ico">GS</span>Google Sheets</button>
      <button data-page="docs"><span class="ico">F</span>Документы</button>
      <button data-page="search"><span class="ico">S</span>Поиск</button>
      <div class="nav-section">Система</div>
      <button data-page="users"><span class="ico">👤</span>Пользователи</button>
    </nav>
    <div class="side-bottom"><div class="online"><span class="dot"></span>{{ name }} онлайн</div></div>
  </aside>

  <main class="main">
    <header class="topbar">
      <div class="title" id="pageTitle">Dashboard</div>
      <div class="meta">
        <span id="clock"></span>
        <span>{{ email }}</span>
        <a class="logout" href="/logout">Выйти</a>
      </div>
    </header>

    <section class="content">
      <div id="alert"></div>

      <!-- DASHBOARD -->
      <div class="page active" id="dashboard">
        <div class="stack">
          <div class="grid4">
            <div class="card stat"><div class="num" id="sMessages">0</div><div><div class="lab">Сообщений</div><div class="hint">AI диалоги</div></div></div>
            <div class="card stat"><div class="num" id="sVoice">0</div><div><div class="lab">Голосовых</div><div class="hint">Ввод голосом</div></div></div>
            <div class="card stat"><div class="num" id="sCrmClients">–</div><div><div class="lab">Клиентов</div><div class="hint">CRM база</div></div></div>
            <div class="card stat"><div class="num" id="sDashRevenue">–</div><div><div class="lab">Выручка</div><div class="hint">Последние 7 дней</div></div></div>
          </div>
          <div class="grid2">
            <div class="card">
              <h3>Быстрый запрос</h3>
              <div class="row"><input class="field" id="quickInput" placeholder="Спроси NEXUS..." style="margin:0" onkeydown="if(event.key==='Enter')quickAsk()"><button class="btn" onclick="quickAsk()">Спросить</button></div>
              <div id="quickResult" class="item-meta" style="margin-top:12px"></div>
            </div>
            <div class="card"><h3>Ближайшие события</h3><div id="dashEvents" class="list"><div class="item-meta">Загрузка...</div></div></div>
          </div>
          <div class="grid3">
            <div class="card">
              <h3>Последние задачи</h3>
              <div id="dashTasks" class="list"><div class="item-meta">Загрузка...</div></div>
              <button class="btn secondary sm" style="margin-top:10px" onclick="showPage('tasks')">Все задачи</button>
            </div>
            <div class="card">
              <h3>Клиенты CRM</h3>
              <div id="dashClients" class="list"><div class="item-meta">Загрузка...</div></div>
              <button class="btn secondary sm" style="margin-top:10px" onclick="showPage('crm')">Перейти в CRM</button>
            </div>
            <div class="card">
              <h3>Аналитика (7 дней)</h3>
              <div id="dashAnalytics" class="list"><div class="item-meta">Загрузка...</div></div>
              <button class="btn secondary sm" style="margin-top:10px" onclick="showPage('analytics')">Подробнее</button>
            </div>
          </div>
        </div>
      </div>

      <!-- ЧАТ -->
      <div class="page" id="chat">
        <div class="card chatbox">
          <div class="messages" id="messages">
            <div class="speaker">NEXUS</div>
            <div class="bubble ai">Привет, это NEXUS. Центр управления активен. Чем могу помочь?</div>
          </div>
          <div class="compose">
            <button class="btn secondary" id="micBtn" onclick="toggleVoice()">🎤</button>
            <textarea class="field" id="chatInput" placeholder="Напишите сообщение..." onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendMsg(false)}"></textarea>
            <button class="btn" onclick="sendMsg(false)">Отправить</button>
          </div>
        </div>
      </div>

      <!-- CRM -->
      <div class="page" id="crm">
        <div class="grid2" style="margin-bottom:16px">
          <div class="card">
            <h3>База клиентов</h3>
            <div class="row" style="margin-bottom:10px">
              <input class="field" id="crmSearch" placeholder="Поиск по имени, телефону, компании..." style="margin:0" oninput="filterClients()">
              <button class="btn secondary" onclick="loadClients()">↻</button>
            </div>
            <div id="crmList" class="list"><div class="item-meta">Загрузка...</div></div>
          </div>
          <div class="card">
            <h3>Добавить клиента</h3>
            <input class="field" id="cName" placeholder="Имя клиента *">
            <input class="field" id="cPhone" placeholder="Телефон">
            <input class="field" id="cEmail2" placeholder="Email">
            <input class="field" id="cCompany" placeholder="Компания">
            <select class="field" id="cStatus">
              <option value="active">Активный</option>
              <option value="potential">Потенциальный</option>
              <option value="inactive">Неактивный</option>
            </select>
            <input class="field" type="number" id="cTotal" placeholder="Сумма сделок ₴">
            <textarea class="field" id="cNotes" rows="2" placeholder="Заметки..."></textarea>
            <button class="btn" onclick="saveClient()">Сохранить клиента</button>
          </div>
        </div>
        <div id="clientDetail" style="display:none"></div>
      </div>

      <!-- АНАЛИТИКА -->
      <div class="page" id="analytics">
        <div class="period-btns">
          <button class="period-btn active" onclick="setAnalyticsPeriod(7,this)">7 дней</button>
          <button class="period-btn" onclick="setAnalyticsPeriod(30,this)">30 дней</button>
          <button class="period-btn" onclick="setAnalyticsPeriod(90,this)">3 месяца</button>
          <button class="period-btn" onclick="setAnalyticsPeriod(365,this)">Год</button>
        </div>
        <div class="grid4" style="margin-bottom:16px">
          <div class="card stat"><div class="num" id="anRevenue">—</div><div><div class="lab">Выручка</div><div class="hint">За период</div></div></div>
          <div class="card stat"><div class="num" id="anProfit">—</div><div><div class="lab">Прибыль</div><div class="hint">Выручка − расходы</div></div></div>
          <div class="card stat"><div class="num" id="anExpenses">—</div><div><div class="lab">Расходы</div><div class="hint">За период</div></div></div>
          <div class="card stat"><div class="num" id="anClients">—</div><div><div class="lab">Клиентов</div><div class="hint">За период</div></div></div>
        </div>
        <div class="grid2" style="margin-bottom:16px">
          <div class="card"><h3>Выручка по дням</h3><canvas id="revenueChart"></canvas></div>
          <div class="card"><h3>Распределение по бизнесам</h3><canvas id="businessChart"></canvas></div>
        </div>
        <div class="grid3" style="margin-bottom:16px">
          <div class="card"><h3>🍽️ Общепит</h3><div class="num" id="anObRev" style="font-size:22px">—</div><div class="lab">Выручка</div><div class="item-meta" style="margin-top:8px">Клиентов: <span id="anObCli">0</span> | Прибыль: <span id="anObPro" style="color:var(--green)">—</span></div></div>
          <div class="card"><h3>🐟 Аква бизнес</h3><div class="num" id="anAkRev" style="font-size:22px">—</div><div class="lab">Выручка</div><div class="item-meta" style="margin-top:8px">Клиентов: <span id="anAkCli">0</span> | Прибыль: <span id="anAkPro" style="color:var(--green)">—</span></div></div>
          <div class="card"><h3>📈 Продвижение</h3><div class="num" id="anPrRev" style="font-size:22px">—</div><div class="lab">Выручка</div><div class="item-meta" style="margin-top:8px">Клиентов: <span id="anPrCli">0</span> | Прибыль: <span id="anPrPro" style="color:var(--green)">—</span></div></div>
        </div>
        <div class="card">
          <h3>Добавить запись</h3>
          <div class="row" style="flex-wrap:wrap;gap:10px">
            <select class="field" id="anBiz" style="margin:0;flex:1;min-width:140px">
              <option value="obshchepit">🍽️ Общепит</option>
              <option value="akva">🐟 Аква бизнес</option>
              <option value="prodvizhenie">📈 Продвижение</option>
            </select>
            <input class="field" type="date" id="anDate" style="margin:0;flex:1;min-width:130px">
            <input class="field" type="number" id="anRevField" placeholder="Выручка ₴" style="margin:0;flex:1;min-width:110px">
            <input class="field" type="number" id="anExpField" placeholder="Расходы ₴" style="margin:0;flex:1;min-width:110px">
            <input class="field" type="number" id="anCliField" placeholder="Клиентов" style="margin:0;flex:1;min-width:100px">
            <button class="btn" onclick="addAnalyticsRecord()">Добавить</button>
          </div>
          <input class="field" id="anComment" placeholder="Комментарий (необязательно)" style="margin-top:10px">
        </div>
      </div>

      <!-- ЗАДАЧИ -->
      <div class="page" id="tasks">
        <div class="card">
          <h3>Задачи</h3>
          <div class="row" style="margin-bottom:4px">
            <input class="field" id="taskInput" placeholder="Новая задача..." style="margin:0" onkeydown="if(event.key==='Enter')addTask()">
            <button class="btn" onclick="addTask()">Добавить</button>
          </div>
          <div id="taskList" class="list"></div>
        </div>
      </div>

      <!-- EMAIL -->
      <div class="page" id="email">
        <div class="stack">
          <div class="card">
            <div class="row" style="justify-content:space-between;margin-bottom:14px">
              <h3 style="margin:0">Email центр</h3>
              <button class="btn secondary sm" onclick="loadEmails()">Обновить</button>
            </div>
            <div id="emailList" class="list"><div class="item-meta">Нажмите «Обновить», чтобы загрузить письма.</div></div>
          </div>
          <div class="card">
            <h3>Написать письмо</h3>
            <input class="field" id="emailTo" placeholder="Кому (email)">
            <input class="field" id="emailSubject" placeholder="Тема">
            <textarea class="field" id="emailBody" rows="5" placeholder="Текст письма"></textarea>
            <div class="row">
              <button class="btn" onclick="sendEmail()">Отправить</button>
              <button class="btn secondary" onclick="aiWriteEmail()">✨ AI черновик</button>
            </div>
          </div>
        </div>
      </div>

      <!-- КАЛЕНДАРЬ -->
      <div class="page" id="calendar">
        <div class="grid2">
          <div class="card">
            <h3>Новое событие</h3>
            <input class="field" id="eventTitle" placeholder="Название">
            <div class="row"><input class="field" id="eventDate" type="date" style="margin:0"><input class="field" id="eventTime" type="time" style="margin:0"></div>
            <button class="btn" style="margin-top:10px" onclick="addEvent()">Добавить событие</button>
          </div>
          <div class="card"><h3>Предстоящие события</h3><div id="calEvents" class="list"></div></div>
        </div>
      </div>

      <!-- ДОКУМЕНТЫ -->
      <div class="page" id="docs">
        <div class="grid2">
          <div class="card">
            <h3>Загрузить документ</h3>
            <div class="drop" onclick="document.getElementById('docFile').click()">
              Нажмите для загрузки PDF, DOCX или TXT
            </div>
            <input id="docFile" type="file" accept=".pdf,.docx,.txt" style="display:none" onchange="uploadDoc(this)">
            <div id="docStatus"></div>
            <div id="docList" class="list" style="margin-top:10px"></div>
          </div>
          <div class="card">
            <h3>Вопрос по документам</h3>
            <input class="field" id="docQuestion" placeholder="Что найти или объяснить?" onkeydown="if(event.key==='Enter')askDocs()">
            <button class="btn" onclick="askDocs()">Спросить</button>
            <div id="docAnswer" class="item-meta" style="margin-top:12px;line-height:1.6"></div>
          </div>
        </div>
      </div>

      <!-- ПОИСК -->
      <div class="page" id="search">
        <div class="card">
          <h3>Поиск по системе</h3>
          <div class="row" style="margin-bottom:4px">
            <input class="field" id="searchInput" placeholder="Клиенты, аналитика, заметки..." style="margin:0" onkeydown="if(event.key==='Enter')doSearch()">
            <button class="btn" onclick="doSearch()">Найти</button>
          </div>
          <div id="searchResults" class="list"></div>
        </div>
      </div>

      <!-- GOOGLE SHEETS -->
      <div class="page" id="sheets">
        <div class="grid2" style="margin-bottom:16px">
          <div class="card">
            <h3>Подключить таблицу</h3>
            <label style="font-size:12px;color:var(--muted)">ID таблицы (из URL)</label>
            <input class="field" id="sheetId" placeholder="1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms">
            <label style="font-size:12px;color:var(--muted)">Диапазон</label>
            <input class="field" id="sheetRange" placeholder="Sheet1!A1:Z100" value="Sheet1">
            <div class="row">
              <button class="btn" onclick="readSheet()">Загрузить данные</button>
              <button class="btn secondary" onclick="saveSheetConfig()">Сохранить</button>
            </div>
            <div id="sheetStatus" style="margin-top:10px"></div>
          </div>
          <div class="card">
            <h3>Добавить строку</h3>
            <div class="item-meta" style="margin-bottom:10px">Введи значения через запятую</div>
            <textarea class="field" id="sheetNewRow" rows="3" placeholder="Иван Петров, 096-123-45-67, Клиент, 5000"></textarea>
            <button class="btn" onclick="appendSheetRow()">Добавить строку</button>
            <div style="margin-top:14px">
              <h3>Спросить AI по таблице</h3>
              <div class="row">
                <input class="field" id="sheetQuestion" placeholder="Проанализируй данные..." style="margin:0" onkeydown="if(event.key==='Enter')askSheet()">
                <button class="btn" onclick="askSheet()">Спросить</button>
              </div>
              <div id="sheetAnswer" class="item-meta" style="margin-top:10px;line-height:1.6"></div>
            </div>
          </div>
        </div>
        <div class="card">
          <h3>Данные таблицы</h3>
          <div id="sheetTable" style="overflow-x:auto;max-height:400px;overflow-y:auto"><div class="item-meta">Введи ID таблицы и нажми «Загрузить данные»</div></div>
        </div>
      </div>

      <!-- ПОЛЬЗОВАТЕЛИ -->
      <div class="page" id="users">
        <div class="grid2" style="margin-bottom:16px">
          <div class="card">
            <h3>Пользователи системы</h3>
            <div id="usersList" class="list"><div class="item-meta">Загрузка...</div></div>
          </div>
          <div class="card">
            <h3>Добавить пользователя</h3>
            <input class="field" id="uName" placeholder="Имя *">
            <input class="field" id="uEmail" placeholder="Email *">
            <input class="field" type="password" id="uPassword" placeholder="Пароль *">
            <select class="field" id="uRole">
              <option value="admin">Администратор — полный доступ</option>
              <option value="staff">Сотрудник — рабочий доступ</option>
              <option value="viewer">Просмотр — только читать</option>
            </select>
            <div style="margin-bottom:10px">
              <div style="font-size:12px;color:var(--muted);margin-bottom:8px">Доступ к разделам:</div>
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">
                <label style="display:flex;align-items:center;gap:6px;font-size:13px"><input type="checkbox" value="dashboard" checked> Dashboard</label>
                <label style="display:flex;align-items:center;gap:6px;font-size:13px"><input type="checkbox" value="chat" checked> Чат</label>
                <label style="display:flex;align-items:center;gap:6px;font-size:13px"><input type="checkbox" value="crm"> CRM</label>
                <label style="display:flex;align-items:center;gap:6px;font-size:13px"><input type="checkbox" value="analytics"> Аналитика</label>
                <label style="display:flex;align-items:center;gap:6px;font-size:13px"><input type="checkbox" value="tasks" checked> Задачи</label>
                <label style="display:flex;align-items:center;gap:6px;font-size:13px"><input type="checkbox" value="email"> Email</label>
                <label style="display:flex;align-items:center;gap:6px;font-size:13px"><input type="checkbox" value="calendar"> Календарь</label>
                <label style="display:flex;align-items:center;gap:6px;font-size:13px"><input type="checkbox" value="docs"> Документы</label>
              </div>
            </div>
            <button class="btn" onclick="addUser()">Добавить пользователя</button>
          </div>
        </div>
      </div>

    </section>
  </main>
</div>

<script>
var serverTasks = [];
var allClients = [];
var analyticsPeriod = 7;
var anCharts = {};
var docs = [];
var recognition = null;
var isListening = false;

var titles = {
  dashboard:'Dashboard', chat:'Чат NEXUS', crm:'CRM клиенты',
  analytics:'Аналитика', tasks:'Задачи', email:'Email центр',
  calendar:'Календарь', sheets:'Google Sheets', docs:'Документы',
  search:'Поиск', users:'Пользователи'
};

var pageActivateHandlers = {
  crm: function(){ loadClients(); },
  analytics: function(){ loadAnalytics(); },
  tasks: function(){ loadTasks(); },
  dashboard: function(){ loadDashboardData(); },
  users: function(){ loadUsers(); },
  sheets: function(){ loadSheetConfig(); }
};

function $(id){ return document.getElementById(id); }
function esc(t){ var d=document.createElement('div'); d.textContent=t||''; return d.innerHTML; }
function fmt(n){ return Number(n||0).toLocaleString('uk-UA')+' ₴'; }

// ── Navigation ────────────────────────────────────────────────────────────────
document.querySelectorAll('.nav button').forEach(function(b){
  b.onclick = function(){
    document.querySelectorAll('.nav button').forEach(function(x){ x.classList.remove('active'); });
    document.querySelectorAll('.page').forEach(function(p){ p.classList.remove('active'); });
    b.classList.add('active');
    $(b.dataset.page).classList.add('active');
    $('pageTitle').textContent = titles[b.dataset.page] || 'NEXUS';
    var handler = pageActivateHandlers[b.dataset.page];
    if(handler) handler();
  };
});

function showPage(name){
  var btn = document.querySelector('.nav button[data-page="'+name+'"]');
  if(btn) btn.click();
}

// ── Utilities ─────────────────────────────────────────────────────────────────
function alertMsg(text, ok){
  $('alert').innerHTML = '<div class="alert '+(ok?'ok':'err')+'">'+esc(text)+'</div>';
  setTimeout(function(){ $('alert').innerHTML=''; }, 4000);
}

function updateClock(){ $('clock').textContent = new Date().toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'}); }
setInterval(updateClock, 1000); updateClock();

function api(url, opts){
  return fetch(url, opts).then(function(r){
    return r.text().then(function(t){
      try{ return JSON.parse(t); } catch(e){ throw new Error('Ошибка сервера: '+url); }
    });
  });
}

// ── Dashboard ─────────────────────────────────────────────────────────────────
function loadDashboardData(){
  api('/stats').then(function(d){
    $('sMessages').textContent = d.messages||0;
    $('sVoice').textContent = d.voice||0;
  }).catch(function(){});

  api('/clients').then(function(d){
    var clients = d.clients||[];
    $('sCrmClients').textContent = clients.length;
    $('dashClients').innerHTML = clients.slice(0,3).map(function(c){
      var sColor = {active:'var(--green)',potential:'var(--amber)',inactive:'var(--red)'};
      return '<div class="item">'
        +'<div style="display:flex;justify-content:space-between">'
        +'<div class="item-title">'+esc(c.name)+'</div>'
        +'<span style="color:'+sColor[c.status]+';font-size:11px">●</span>'
        +'</div>'
        +(c.phone?'<div class="item-meta">'+esc(c.phone)+'</div>':'')
        +'</div>';
    }).join('') || '<div class="item-meta">Клиентов нет</div>';
  }).catch(function(){});

  api('/analytics_data?days=7').then(function(d){
    $('sDashRevenue').textContent = Number(d.totals.revenue||0).toLocaleString()+' ₴';
    $('dashAnalytics').innerHTML =
      '<div class="item"><div style="display:flex;justify-content:space-between"><span class="item-meta">Выручка</span><span style="color:var(--cyan)">'+fmt(d.totals.revenue)+'</span></div></div>'
      +'<div class="item"><div style="display:flex;justify-content:space-between"><span class="item-meta">Прибыль</span><span style="color:var(--green)">'+fmt(d.totals.profit)+'</span></div></div>'
      +'<div class="item"><div style="display:flex;justify-content:space-between"><span class="item-meta">Расходы</span><span style="color:var(--red)">'+fmt(d.totals.expenses)+'</span></div></div>';
  }).catch(function(){});

  api('/tasks').then(function(d){
    var tasks = (d.tasks||[]).filter(function(t){ return !t.done; });
    $('dashTasks').innerHTML = tasks.slice(0,4).map(function(t){
      return '<div class="item"><div class="item-title">'+esc(t.text)+'</div>'
        +(t.created?'<div class="item-meta">'+esc(t.created)+'</div>':'')+'</div>';
    }).join('') || '<div class="item-meta">Активных задач нет</div>';
  }).catch(function(){});

  loadEvents();
}

// ── Stats ─────────────────────────────────────────────────────────────────────
function loadStats(){
  api('/stats').then(function(d){
    $('sMessages').textContent = d.messages||0;
    $('sVoice').textContent = d.voice||0;
  }).catch(function(){});
}

// ── Chat ──────────────────────────────────────────────────────────────────────
function addBubble(who, text){
  var wrap = document.createElement('div');
  wrap.className = 'bubble '+(who==='user'?'user':'ai');
  wrap.textContent = text;
  var msg = $('messages');
  if(who !== 'user'){ var s=document.createElement('div'); s.className='speaker'; s.textContent='NEXUS'; msg.appendChild(s); }
  msg.appendChild(wrap);
  msg.scrollTop = msg.scrollHeight;
  return wrap;
}

function sendMsg(voice){
  var inp = $('chatInput'), text = inp.value.trim();
  if(!text) return;
  inp.value = '';
  addBubble('user', text);
  var pending = addBubble('ai','Думаю...');
  api('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text,voice:voice})})
    .then(function(d){
      pending.textContent = d.reply||'';
      if(d.audio){ var a=new Audio('data:audio/mp3;base64,'+d.audio); if(voice) a.play().catch(function(){}); }
      loadStats();
    }).catch(function(e){ pending.textContent = e.message; });
}

function quickAsk(){
  var inp = $('quickInput'), text = inp.value.trim();
  if(!text) return;
  inp.value = '';
  $('quickResult').textContent = 'Думаю...';
  api('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text,voice:false})})
    .then(function(d){ $('quickResult').textContent = d.reply||''; loadStats(); })
    .catch(function(e){ $('quickResult').textContent = e.message; });
}

function toggleVoice(){
  if(!window.SpeechRecognition && !window.webkitSpeechRecognition){ alertMsg('Голосовой ввод работает в Chrome.',false); return; }
  if(isListening && recognition){ recognition.stop(); return; }
  var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  recognition = new SR();
  recognition.lang = 'ru-RU';
  recognition.onstart = function(){ isListening=true; $('micBtn').textContent='⏹'; };
  recognition.onend = function(){ isListening=false; $('micBtn').textContent='🎤'; };
  recognition.onerror = recognition.onend;
  recognition.onresult = function(e){ $('chatInput').value=e.results[0][0].transcript; sendMsg(true); };
  recognition.start();
}

// ── Tasks (server-side) ───────────────────────────────────────────────────────
function loadTasks(){
  api('/tasks').then(function(d){
    serverTasks = d.tasks||[];
    renderServerTasks();
    var open = serverTasks.filter(function(t){ return !t.done; });
    $('dashTasks').innerHTML = open.slice(0,4).map(function(t){
      return '<div class="item"><div class="item-title">'+esc(t.text)+'</div></div>';
    }).join('') || '<div class="item-meta">Активных задач нет</div>';
  }).catch(function(){});
}

function addTask(){
  var t = $('taskInput').value.trim();
  if(!t) return;
  $('taskInput').value = '';
  api('/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})})
    .then(function(d){ if(d.success) loadTasks(); });
}

function toggleTask(id){
  api('/tasks/'+id,{method:'PATCH'}).then(function(){ loadTasks(); });
}

function deleteTask(id){
  api('/tasks/'+id,{method:'DELETE'}).then(function(){ loadTasks(); });
}

function renderServerTasks(){
  $('taskList').innerHTML = serverTasks.map(function(t){
    return '<div class="item" style="display:flex;align-items:center;gap:10px">'
      +'<div onclick="toggleTask('+t.id+')" style="flex:1;cursor:pointer">'
      +'<div class="item-title" style="'+(t.done?'text-decoration:line-through;color:var(--muted)':'')+'">'
      +esc(t.text)+'</div>'
      +(t.created?'<div class="item-meta">'+esc(t.created)+'</div>':'')
      +'</div>'
      +'<button onclick="deleteTask('+t.id+')" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:18px;padding:0 4px;line-height:1" title="Удалить">×</button>'
      +'</div>';
  }).join('') || '<div class="item-meta">Задач нет. Добавьте первую!</div>';
}

// ── CRM ───────────────────────────────────────────────────────────────────────
function loadClients(){
  api('/clients').then(function(d){
    allClients = d.clients||[];
    filterClients();
  });
}

function filterClients(){
  var q = ($('crmSearch').value||'').toLowerCase();
  var list = q ? allClients.filter(function(c){
    return (c.name||'').toLowerCase().includes(q) || (c.phone||'').includes(q) || (c.company||'').toLowerCase().includes(q);
  }) : allClients;
  renderCrmList(list);
}

function renderCrmList(list){
  var sC = {active:'var(--green)',potential:'var(--amber)',inactive:'var(--red)'};
  var sN = {active:'Активный',potential:'Потенциальный',inactive:'Неактивный'};
  $('crmList').innerHTML = list.map(function(c){
    return '<div class="item" onclick="showCrmDetail('+c.id+')" style="cursor:pointer">'
      +'<div style="display:flex;justify-content:space-between;align-items:center">'
      +'<div class="item-title">'+esc(c.name)+'</div>'
      +'<span class="badge badge-'+c.status+'">'+sN[c.status]+'</span>'
      +'</div>'
      +(c.phone?'<div class="item-meta">'+esc(c.phone)+(c.company?' · '+esc(c.company):'')+'</div>':'')
      +(c.total?'<div style="color:var(--cyan);font-size:13px;margin-top:4px">'+Number(c.total).toLocaleString()+' ₴</div>':'')
      +'</div>';
  }).join('') || '<div class="item-meta" style="padding:20px;text-align:center">Клиентов нет. Добавьте первого!</div>';
}

function showCrmDetail(id){
  var c = allClients.find(function(x){ return x.id===id; });
  if(!c) return;
  var sN = {active:'Активный',potential:'Потенциальный',inactive:'Неактивный'};
  var detail = $('clientDetail');
  detail.style.display = 'block';
  detail.innerHTML = '<div class="card">'
    +'<h3>'+esc(c.name)+'</h3>'
    +'<div class="grid2" style="gap:8px;margin-bottom:10px">'
    +(c.phone?'<div class="item-meta">📞 '+esc(c.phone)+'</div>':'')
    +(c.email?'<div class="item-meta">✉️ '+esc(c.email)+'</div>':'')
    +(c.company?'<div class="item-meta">🏢 '+esc(c.company)+'</div>':'')
    +'<div class="item-meta">Статус: <span class="badge badge-'+c.status+'">'+sN[c.status]+'</span></div>'
    +(c.total?'<div class="item-meta">Сумма сделок: <span style="color:var(--cyan)">'+Number(c.total).toLocaleString()+' ₴</span></div>':'')
    +'</div>'
    +(c.notes?'<div class="item-meta" style="margin-bottom:12px;padding:10px;background:#0a141b;border-radius:8px">'+esc(c.notes)+'</div>':'')
    +'<div class="row" style="margin-bottom:12px">'
    +'<input class="field" id="noteText" placeholder="Добавить запись в историю..." style="margin:0">'
    +'<button class="btn" onclick="addCrmNote('+id+')">Добавить</button>'
    +'</div>'
    +(c.history&&c.history.length
      ? '<div>'+c.history.slice().reverse().map(function(h){
          return '<div class="history-entry"><div style="font-size:11px;color:var(--muted);margin-bottom:3px">'+esc(h.date)+'</div><div>'+esc(h.text)+'</div></div>';
        }).join('')+'</div>'
      : '<div class="item-meta">История пуста</div>')
    +'</div>';
  detail.scrollIntoView({behavior:'smooth', block:'start'});
}

function addCrmNote(id){
  var text = $('noteText').value.trim();
  if(!text) return;
  api('/add_note',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id,text:text})})
    .then(function(d){
      if(d.success){ loadClients(); setTimeout(function(){ showCrmDetail(id); }, 300); }
    });
}

function saveClient(){
  var data = {
    name: $('cName').value.trim(),
    phone: $('cPhone').value.trim(),
    email: $('cEmail2').value.trim(),
    company: $('cCompany').value.trim(),
    status: $('cStatus').value,
    total: parseFloat($('cTotal').value)||0,
    notes: $('cNotes').value.trim()
  };
  if(!data.name){ alertMsg('Введите имя клиента.', false); return; }
  api('/add_client',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})
    .then(function(d){
      if(d.success){
        alertMsg('Клиент добавлен!', true);
        $('cName').value=''; $('cPhone').value=''; $('cEmail2').value='';
        $('cCompany').value=''; $('cTotal').value=''; $('cNotes').value='';
        loadClients();
      } else alertMsg(d.error||'Ошибка', false);
    });
}

// ── Analytics ─────────────────────────────────────────────────────────────────
function setAnalyticsPeriod(days, btn){
  analyticsPeriod = days;
  document.querySelectorAll('.period-btn').forEach(function(b){ b.classList.remove('active'); });
  if(btn) btn.classList.add('active');
  loadAnalytics();
}

function loadAnalytics(){
  api('/analytics_data?days='+analyticsPeriod).then(function(d){
    $('anRevenue').textContent = fmt(d.totals.revenue);
    $('anProfit').textContent = fmt(d.totals.profit);
    $('anExpenses').textContent = fmt(d.totals.expenses);
    $('anClients').textContent = d.totals.clients;
    $('anObRev').textContent = fmt(d.by_business.obshchepit.revenue);
    $('anObCli').textContent = d.by_business.obshchepit.clients;
    $('anObPro').textContent = fmt(d.by_business.obshchepit.profit);
    $('anAkRev').textContent = fmt(d.by_business.akva.revenue);
    $('anAkCli').textContent = d.by_business.akva.clients;
    $('anAkPro').textContent = fmt(d.by_business.akva.profit);
    $('anPrRev').textContent = fmt(d.by_business.prodvizhenie.revenue);
    $('anPrCli').textContent = d.by_business.prodvizhenie.clients;
    $('anPrPro').textContent = fmt(d.by_business.prodvizhenie.profit);
    if(window.Chart){ buildRevenueChart(d.daily); buildBusinessChart(d.by_business); }
  }).catch(function(){});
}

function buildRevenueChart(daily){
  if(anCharts.revenue) anCharts.revenue.destroy();
  var ctx = $('revenueChart').getContext('2d');
  anCharts.revenue = new Chart(ctx,{
    type:'line',
    data:{
      labels:daily.map(function(d){return d.date}),
      datasets:[
        {label:'Общепит',data:daily.map(function(d){return d.obshchepit}),borderColor:'#37d7e8',backgroundColor:'rgba(55,215,232,.08)',tension:.4,fill:true},
        {label:'Аква',data:daily.map(function(d){return d.akva}),borderColor:'#47e08c',backgroundColor:'rgba(71,224,140,.08)',tension:.4,fill:true},
        {label:'Продвижение',data:daily.map(function(d){return d.prodvizhenie}),borderColor:'#f4b860',backgroundColor:'rgba(244,184,96,.08)',tension:.4,fill:true}
      ]
    },
    options:{responsive:true,plugins:{legend:{labels:{color:'#edf7f8',font:{size:12}}}},scales:{x:{ticks:{color:'#8ba1a8'}},y:{ticks:{color:'#8ba1a8',callback:function(v){return v.toLocaleString()}}}}}
  });
}

function buildBusinessChart(bb){
  if(anCharts.business) anCharts.business.destroy();
  var ctx = $('businessChart').getContext('2d');
  anCharts.business = new Chart(ctx,{
    type:'doughnut',
    data:{
      labels:['Общепит','Аква бизнес','Продвижение'],
      datasets:[{data:[bb.obshchepit.revenue,bb.akva.revenue,bb.prodvizhenie.revenue],backgroundColor:['rgba(55,215,232,.7)','rgba(71,224,140,.7)','rgba(244,184,96,.7)'],borderColor:['#37d7e8','#47e08c','#f4b860'],borderWidth:2}]
    },
    options:{responsive:true,plugins:{legend:{labels:{color:'#edf7f8',font:{size:12}}}}}
  });
}

function addAnalyticsRecord(){
  var data = {
    business: $('anBiz').value,
    date: $('anDate').value,
    revenue: parseFloat($('anRevField').value)||0,
    expenses: parseFloat($('anExpField').value)||0,
    clients: parseInt($('anCliField').value)||0,
    comment: $('anComment').value
  };
  if(!data.date){ alertMsg('Выберите дату.', false); return; }
  api('/add_record',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})
    .then(function(d){
      if(d.success){
        alertMsg('Запись добавлена!', true);
        $('anRevField').value=''; $('anExpField').value=''; $('anCliField').value=''; $('anComment').value='';
        loadAnalytics();
      } else alertMsg(d.error||'Ошибка', false);
    });
}

// ── Email ─────────────────────────────────────────────────────────────────────
function loadEmails(){
  $('emailList').innerHTML = '<div class="item-meta">Загружаю...</div>';
  api('/emails').then(function(d){
    var emails = d.emails||[];
    $('emailList').innerHTML = emails.map(function(e,i){
      return '<div class="item" onclick="analyzeEmail('+i+')" style="cursor:pointer">'
        +'<div class="item-title">'+esc(e.subject)+'</div>'
        +'<div class="item-meta">'+esc(e.from)+'</div>'
        +'<div class="item-meta">'+esc(e.preview)+'</div>'
        +'</div>';
    }).join('') || '<div class="item-meta">'+esc(d.error||'Писем нет')+'</div>';
    window.emailCache = emails;
  });
}

function analyzeEmail(i){
  var e=(window.emailCache||[])[i];
  if(!e) return;
  api('/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({subject:e.subject,text:e.body})})
    .then(function(d){ alertMsg(d.analysis||'Готово', true); });
}

function sendEmail(){
  var to=$('emailTo').value, subject=$('emailSubject').value, body=$('emailBody').value;
  if(!to||!subject||!body){ alertMsg('Заполните все поля письма.', false); return; }
  api('/send_email',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({to,subject,body})})
    .then(function(d){ alertMsg(d.success?'Письмо отправлено!':d.error, d.success); });
}

function aiWriteEmail(){
  var p = prompt('Что нужно написать?');
  if(!p) return;
  api('/generate_email',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:p})})
    .then(function(d){ $('emailSubject').value=d.subject||''; $('emailBody').value=d.body||''; });
}

// ── Calendar ──────────────────────────────────────────────────────────────────
function loadEvents(){
  api('/events').then(function(d){
    var html = (d.events||[]).map(function(e){
      return '<div class="item"><div class="item-title">'+esc(e.title)+'</div><div class="item-meta">'+esc(e.time)+'</div></div>';
    }).join('') || '<div class="item-meta">Нет предстоящих событий</div>';
    $('calEvents').innerHTML = html;
    $('dashEvents').innerHTML = html;
  }).catch(function(e){ $('dashEvents').innerHTML='<div class="item-meta">Календарь не подключён</div>'; });
}

function addEvent(){
  var title=$('eventTitle').value, date=$('eventDate').value, time=$('eventTime').value;
  if(!title||!date||!time){ alertMsg('Заполните название, дату и время.', false); return; }
  api('/add_event',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title,date,time})})
    .then(function(d){ if(d.success){ alertMsg('Событие добавлено!', true); $('eventTitle').value=''; loadEvents(); } else alertMsg(d.error||'Ошибка', false); });
}

// ── Docs ──────────────────────────────────────────────────────────────────────
function uploadDoc(input){
  var f=input.files[0]; if(!f) return;
  var fd=new FormData(); fd.append('file',f);
  $('docStatus').innerHTML='<div class="alert ok">Загружаю...</div>';
  api('/upload_doc',{method:'POST',body:fd}).then(function(d){
    if(d.success){ docs.push(f.name); $('docStatus').innerHTML='<div class="alert ok">Документ загружен: '+esc(f.name)+'</div>'; $('docList').innerHTML=docs.map(function(x){return'<div class="item">'+esc(x)+'</div>'}).join(''); }
    else $('docStatus').innerHTML='<div class="alert err">'+esc(d.error)+'</div>';
  });
}

function askDocs(){
  var q=$('docQuestion').value.trim(); if(!q) return;
  $('docAnswer').textContent='Думаю...';
  api('/ask_docs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q})})
    .then(function(d){ $('docAnswer').textContent=d.answer||''; });
}

// ── Search ────────────────────────────────────────────────────────────────────
function doSearch(){
  var q=$('searchInput').value.trim(); if(!q) return;
  $('searchResults').innerHTML='<div class="item-meta">Ищу...</div>';
  api('/search?q='+encodeURIComponent(q)).then(function(d){
    $('searchResults').innerHTML=(d.results||[]).map(function(r){
      return '<div class="item"><div class="item-title">'+esc(r.title)+'</div>'
        +'<div class="item-meta">'+esc(r.type)+'</div>'
        +'<div class="item-meta">'+esc(r.info)+'</div></div>';
    }).join('') || '<div class="item-meta">Ничего не найдено</div>';
  });
}

// ── Google Sheets ─────────────────────────────────────────────────────────────
var sheetData = [];

function loadSheetConfig(){
  var saved = localStorage.getItem('nexusSheetId');
  if(saved){ $('sheetId').value = saved; }
  var savedRange = localStorage.getItem('nexusSheetRange');
  if(savedRange){ $('sheetRange').value = savedRange; }
}

function saveSheetConfig(){
  localStorage.setItem('nexusSheetId', $('sheetId').value);
  localStorage.setItem('nexusSheetRange', $('sheetRange').value);
  alertMsg('Настройки сохранены!', true);
}

function readSheet(){
  var id = $('sheetId').value.trim();
  var range = $('sheetRange').value.trim() || 'Sheet1';
  if(!id){ alertMsg('Введи ID таблицы.', false); return; }
  $('sheetStatus').innerHTML = '<div class="alert ok">Загружаю...</div>';
  api('/sheet_read', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({spreadsheet_id:id, range:range})})
    .then(function(d){
      if(d.error){ $('sheetStatus').innerHTML='<div class="alert err">'+esc(d.error)+'</div>'; return; }
      sheetData = d.values || [];
      $('sheetStatus').innerHTML = '<div class="alert ok">Загружено строк: '+sheetData.length+'</div>';
      renderSheetTable(sheetData);
      saveSheetConfig();
    }).catch(function(e){ $('sheetStatus').innerHTML='<div class="alert err">'+esc(e.message)+'</div>'; });
}

function renderSheetTable(data){
  if(!data || !data.length){ $('sheetTable').innerHTML='<div class="item-meta">Таблица пуста</div>'; return; }
  var headers = data[0];
  var rows = data.slice(1);
  var html = '<table style="width:100%;border-collapse:collapse;font-size:13px">';
  html += '<thead><tr>'+headers.map(function(h){
    return '<th style="padding:8px 12px;border-bottom:1px solid var(--line);color:var(--cyan);text-align:left;white-space:nowrap">'+esc(h)+'</th>';
  }).join('')+'</tr></thead><tbody>';
  rows.forEach(function(row, ri){
    html += '<tr style="'+(ri%2?'background:rgba(255,255,255,.02)':'')+'">'+headers.map(function(_, ci){
      return '<td style="padding:7px 12px;border-bottom:1px solid rgba(38,59,69,.4);white-space:nowrap">'+esc(row[ci]||'')+'</td>';
    }).join('')+'</tr>';
  });
  html += '</tbody></table>';
  $('sheetTable').innerHTML = html;
}

function appendSheetRow(){
  var id = $('sheetId').value.trim();
  var range = $('sheetRange').value.trim() || 'Sheet1';
  var rawValues = $('sheetNewRow').value.trim();
  if(!id){ alertMsg('Введи ID таблицы.', false); return; }
  if(!rawValues){ alertMsg('Введи значения для строки.', false); return; }
  var values = rawValues.split(',').map(function(v){ return v.trim(); });
  api('/sheet_append', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({spreadsheet_id:id, range:range, values:values})})
    .then(function(d){
      if(d.success){ alertMsg('Строка добавлена!', true); $('sheetNewRow').value=''; readSheet(); }
      else alertMsg(d.error||'Ошибка', false);
    });
}

function askSheet(){
  var q = $('sheetQuestion').value.trim();
  if(!q) return;
  var context = sheetData.length ? 'Данные таблицы:\n' + sheetData.slice(0,30).map(function(r){return r.join(', ')}).join('\n') : '';
  var fullQ = context ? context + '\n\nВопрос: ' + q : q;
  $('sheetAnswer').textContent = 'Анализирую...';
  api('/chat', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({message:fullQ, voice:false})})
    .then(function(d){ $('sheetAnswer').textContent = d.reply||''; });
}

// ── Users ─────────────────────────────────────────────────────────────────────
function loadUsers(){
  api('/users_list').then(function(d){
    var roleNames = {admin:'Администратор', staff:'Сотрудник', viewer:'Просмотр'};
    var roleColors = {admin:'var(--cyan)', staff:'var(--green)', viewer:'var(--amber)'};
    $('usersList').innerHTML = (d.users||[]).map(function(u){
      return '<div class="item">'
        +'<div style="display:flex;justify-content:space-between;align-items:center">'
        +'<div><div class="item-title">'+esc(u.name)+'</div>'
        +'<div class="item-meta">'+esc(u.email)+'</div>'
        +(u.access?'<div class="item-meta" style="margin-top:3px">'+u.access.join(', ')+'</div>':'')
        +'</div>'
        +'<span style="color:'+roleColors[u.role]+';font-size:12px;font-weight:600">'+roleNames[u.role]+'</span>'
        +'</div></div>';
    }).join('') || '<div class="item-meta">Нет пользователей</div>';
  }).catch(function(){});
}

function addUser(){
  var access = [];
  document.querySelectorAll('#users input[type=checkbox]:checked').forEach(function(cb){ access.push(cb.value); });
  var data = {
    name: $('uName').value.trim(),
    email: $('uEmail').value.trim(),
    password: $('uPassword').value,
    role: $('uRole').value,
    access: access
  };
  if(!data.name||!data.email||!data.password){ alertMsg('Заполни имя, email и пароль.', false); return; }
  api('/users_add', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)})
    .then(function(d){
      if(d.success){
        alertMsg('Пользователь добавлен!', true);
        $('uName').value=''; $('uEmail').value=''; $('uPassword').value='';
        loadUsers();
      } else alertMsg(d.error||'Ошибка', false);
    });
}

// ── Init ──────────────────────────────────────────────────────────────────────
document.getElementById('anDate').value = new Date().toISOString().split('T')[0];
loadDashboardData();
setInterval(loadStats, 10000);
</script>
</body>
</html>"""


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if not require_login():
        return redirect("/login")
    return render_template_string(
        HTML,
        email=get_env("GMAIL", "nexus@gmail.com"),
        name=session.get("user_name", profile.get("name", "Никита")),
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        login_val = request.form.get("login", "").strip()
        password = request.form.get("password", "").strip()
        hashed = hashlib.sha256(password.encode()).hexdigest()

        # Проверка по users.json
        users_data = read_json(USERS_FILE, {"users": []})
        for user in users_data.get("users", []):
            name_match = user.get("name", "").lower() == login_val.lower()
            email_match = user.get("email", "").lower() == login_val.lower()
            if (name_match or email_match) and user.get("password") == hashed:
                session["logged_in"] = True
                session["user_name"] = user["name"]
                session["user_role"] = user.get("role", "admin")
                return redirect("/")

        # Мастер-пароль (без логина или admin)
        try:
            master = require_web_password()
            if password == master and login_val in ("", "admin", "никита", "nikita"):
                session["logged_in"] = True
                session["user_name"] = profile.get("name", "Никита")
                session["user_role"] = "admin"
                return redirect("/")
        except Exception:
            pass

        error = "Неверный логин или пароль"
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.route("/stats")
def get_stats():
    return jsonify(stats)


# ── Chat ──────────────────────────────────────────────────────────────────────

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    msg = data.get("message", "").strip()
    voice = bool(data.get("voice", False))
    if not msg:
        return jsonify({"reply": "Напишите сообщение."})

    stats["messages"] += 1
    if voice:
        stats["voice"] += 1
    history.append({"role": "user", "content": msg})

    try:
        client = require_openai()
        response = client.chat.completions.create(
            model=get_env("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "system", "content": SYSTEM}, *history[-20:]],
        )
        answer = response.choices[0].message.content
    except Exception as exc:
        answer = "Ошибка AI: " + str(exc)

    history.append({"role": "assistant", "content": answer})
    try:
        write_json(MEMORY_FILE, history[-80:])
    except Exception:
        pass

    audio_b64 = None
    if voice:
        try:
            client = require_openai()
            audio_response = client.audio.speech.create(
                model=get_env("OPENAI_TTS_MODEL", "tts-1"),
                voice=get_env("OPENAI_TTS_VOICE", "onyx"),
                input=answer[:4000],
            )
            audio_b64 = base64.b64encode(audio_response.content).decode("ascii")
        except Exception:
            audio_b64 = None
    return jsonify({"reply": answer, "audio": audio_b64})


# ── Tasks ─────────────────────────────────────────────────────────────────────

def load_tasks_data():
    return read_json(TASKS_FILE, {"tasks": [], "next_id": 1})


def save_tasks_data(data):
    write_json(TASKS_FILE, data)


@app.route("/tasks", methods=["GET"])
def get_tasks():
    if not require_login():
        return jsonify({"error": "Unauthorized"}), 401
    data = load_tasks_data()
    return jsonify({"tasks": data["tasks"]})


@app.route("/tasks", methods=["POST"])
def add_task_route():
    if not require_login():
        return jsonify({"success": False}), 401
    body = request.get_json(silent=True) or {}
    text = body.get("text", "").strip()
    if not text:
        return jsonify({"success": False, "error": "Нет текста"})
    data = load_tasks_data()
    task = {
        "id": data["next_id"],
        "text": text,
        "done": False,
        "created": datetime.now().strftime("%d.%m.%Y %H:%M"),
    }
    data["tasks"].append(task)
    data["next_id"] += 1
    save_tasks_data(data)
    return jsonify({"success": True, "task": task})


@app.route("/tasks/<int:tid>", methods=["PATCH"])
def toggle_task(tid):
    if not require_login():
        return jsonify({"success": False}), 401
    data = load_tasks_data()
    for t in data["tasks"]:
        if t["id"] == tid:
            t["done"] = not t["done"]
            save_tasks_data(data)
            return jsonify({"success": True, "task": t})
    return jsonify({"success": False, "error": "Не найдено"})


@app.route("/tasks/<int:tid>", methods=["DELETE"])
def delete_task_route(tid):
    if not require_login():
        return jsonify({"success": False}), 401
    data = load_tasks_data()
    data["tasks"] = [t for t in data["tasks"] if t["id"] != tid]
    save_tasks_data(data)
    return jsonify({"success": True})


# ── CRM ───────────────────────────────────────────────────────────────────────

def load_crm():
    return read_json(CRM_FILE, {"clients": []})


def save_crm(data):
    write_json(CRM_FILE, data)


@app.route("/clients")
def get_clients():
    if not require_login():
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"clients": load_crm()["clients"]})


@app.route("/add_client", methods=["POST"])
def add_client():
    if not require_login():
        return jsonify({"success": False}), 401
    try:
        data = load_crm()
        c = request.get_json(silent=True) or {}
        c["id"] = len(data["clients"]) + 1
        c["created"] = datetime.now().strftime("%d.%m.%Y")
        c["history"] = []
        data["clients"].append(c)
        save_crm(data)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/add_note", methods=["POST"])
def add_note():
    if not require_login():
        return jsonify({"success": False}), 401
    try:
        req = request.get_json(silent=True) or {}
        data = load_crm()
        for c in data["clients"]:
            if c["id"] == req["id"]:
                c.setdefault("history", []).append({
                    "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
                    "text": req["text"],
                })
                break
        save_crm(data)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ── Analytics ─────────────────────────────────────────────────────────────────

def load_analytics():
    return read_json(ANALYTICS_FILE, {"obshchepit": [], "akva": [], "prodvizhenie": []})


def save_analytics(data):
    write_json(ANALYTICS_FILE, data)


@app.route("/analytics_data")
def analytics_data():
    if not require_login():
        return jsonify({"error": "Unauthorized"}), 401
    days = int(request.args.get("days", 7))
    data = load_analytics()
    cutoff = datetime.now() - timedelta(days=days)

    all_records = []
    for business, records in data.items():
        for r in records:
            all_records.append({**r, "business": business})

    try:
        filtered = [r for r in all_records if datetime.fromisoformat(r["date"]) >= cutoff]
    except Exception:
        filtered = all_records

    totals = {"revenue": 0, "expenses": 0, "profit": 0, "clients": 0}
    by_business = {
        "obshchepit": {"revenue": 0, "expenses": 0, "profit": 0, "clients": 0},
        "akva": {"revenue": 0, "expenses": 0, "profit": 0, "clients": 0},
        "prodvizhenie": {"revenue": 0, "expenses": 0, "profit": 0, "clients": 0},
    }

    for r in filtered:
        b = r["business"]
        rev = float(r.get("revenue", 0))
        exp = float(r.get("expenses", 0))
        cli = int(r.get("clients", 0))
        profit = rev - exp
        totals["revenue"] += rev
        totals["expenses"] += exp
        totals["profit"] += profit
        totals["clients"] += cli
        if b in by_business:
            by_business[b]["revenue"] += rev
            by_business[b]["expenses"] += exp
            by_business[b]["profit"] += profit
            by_business[b]["clients"] += cli

    show_days = min(days, 60)
    daily = []
    for i in range(show_days):
        day = datetime.now() - timedelta(days=show_days - 1 - i)
        day_str = day.strftime("%Y-%m-%d")
        day_short = day.strftime("%d.%m")
        day_data = {"date": day_short, "obshchepit": 0, "akva": 0, "prodvizhenie": 0}
        for r in filtered:
            if r.get("date") == day_str:
                b = r["business"]
                if b in day_data:
                    day_data[b] += float(r.get("revenue", 0))
        daily.append(day_data)

    return jsonify({"totals": totals, "by_business": by_business, "daily": daily})


@app.route("/add_record", methods=["POST"])
def add_record():
    if not require_login():
        return jsonify({"success": False}), 401
    try:
        rec = request.get_json(silent=True) or {}
        data = load_analytics()
        business = rec.get("business", "obshchepit")
        if business not in data:
            data[business] = []
        data[business].append({
            "date": rec.get("date"),
            "revenue": float(rec.get("revenue", 0)),
            "expenses": float(rec.get("expenses", 0)),
            "clients": int(rec.get("clients", 0)),
            "comment": rec.get("comment", ""),
        })
        save_analytics(data)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ── Email ─────────────────────────────────────────────────────────────────────

def decode_mime(value):
    output = ""
    for part, enc in decode_header(value or ""):
        if isinstance(part, bytes):
            output += part.decode(enc or "utf-8", errors="ignore")
        else:
            output += part
    return output


def extract_plain_body(message):
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
        return ""
    payload = message.get_payload(decode=True) or b""
    return payload.decode(message.get_content_charset() or "utf-8", errors="ignore")


@app.route("/emails")
def get_emails():
    gmail = get_env("GMAIL", "")
    app_password = get_env("APP_PASSWORD", "")
    if not gmail or not app_password:
        return jsonify({"emails": [], "error": "Email не настроен: задайте GMAIL и APP_PASSWORD в .env"})
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(gmail, app_password)
        mail.select("inbox")
        _, data = mail.search(None, "ALL")
        ids = data[0].split()[-15:]
        emails = []
        for eid in reversed(ids):
            _, msg_data = mail.fetch(eid, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            body = extract_plain_body(msg)
            emails.append({
                "from": decode_mime(msg.get("From", ""))[:80],
                "subject": decode_mime(msg.get("Subject", "Без темы"))[:120],
                "preview": body[:120],
                "body": body[:2000],
                "date": msg.get("Date", "")[:40],
            })
        mail.logout()
        stats["emails"] = len(emails)
        return jsonify({"emails": emails})
    except Exception as exc:
        return jsonify({"emails": [], "error": str(exc)})


@app.route("/send_email", methods=["POST"])
def send_email():
    gmail = get_env("GMAIL", "")
    app_password = get_env("APP_PASSWORD", "")
    if not gmail or not app_password:
        return jsonify({"success": False, "error": "Email не настроен"})
    data = request.get_json(silent=True) or {}
    try:
        msg = MIMEMultipart()
        msg["From"] = gmail
        msg["To"] = data["to"]
        msg["Subject"] = data["subject"]
        msg.attach(MIMEText(data["body"], "plain", "utf-8"))
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        server.login(gmail, app_password)
        server.send_message(msg)
        server.quit()
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)})


@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.get_json(silent=True) or {}
    try:
        client = require_openai()
        response = client.chat.completions.create(
            model=get_env("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": "Кратко проанализируй письмо на русском: смысл, срочность, нужен ли ответ, следующие действия."},
                {"role": "user", "content": f"Тема: {data.get('subject','')}\nТекст: {data.get('text','')[:1200]}"},
            ],
        )
        return jsonify({"analysis": response.choices[0].message.content})
    except Exception as exc:
        return jsonify({"analysis": str(exc)})


@app.route("/generate_email", methods=["POST"])
def generate_email():
    prompt = (request.get_json(silent=True) or {}).get("prompt", "")
    try:
        client = require_openai()
        response = client.chat.completions.create(
            model=get_env("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": 'Напиши деловое письмо. Верни только JSON: {"subject":"тема","body":"текст"}'},
                {"role": "user", "content": prompt},
            ],
        )
        text = response.choices[0].message.content.replace("```json", "").replace("```", "").strip()
        return jsonify(json.loads(text))
    except Exception as exc:
        return jsonify({"subject": "", "body": str(exc)})


# ── Calendar ──────────────────────────────────────────────────────────────────

@app.route("/events")
def get_events():
    if calendar_service is None:
        return jsonify({"events": []})
    try:
        now = datetime.utcnow().isoformat() + "Z"
        result = (
            calendar_service.events()
            .list(calendarId=CALENDAR_ID, timeMin=now, maxResults=6, singleEvents=True, orderBy="startTime")
            .execute()
        )
        events = [
            {"title": e.get("summary", "Без названия"), "time": e.get("start", {}).get("dateTime", e.get("start", {}).get("date", ""))}
            for e in result.get("items", [])
        ]
        stats["events"] = len(events)
        return jsonify({"events": events})
    except Exception as exc:
        return jsonify({"events": [], "error": str(exc)})


@app.route("/add_event", methods=["POST"])
def add_event():
    if calendar_service is None:
        return jsonify({"success": False, "error": "Календарь не настроен"})
    data = request.get_json(silent=True) or {}
    try:
        start = f"{data['date']}T{data['time']}:00"
        end = (datetime.fromisoformat(start) + timedelta(hours=1)).isoformat()
        event = {
            "summary": data["title"],
            "start": {"dateTime": start, "timeZone": "Europe/Kiev"},
            "end": {"dateTime": end, "timeZone": "Europe/Kiev"},
        }
        calendar_service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)})


# ── Docs / RAG ────────────────────────────────────────────────────────────────

@app.route("/upload_doc", methods=["POST"])
def upload_doc():
    uploaded = request.files.get("file")
    if not uploaded:
        return jsonify({"success": False, "error": "Нет файла"})
    if collection is None:
        return jsonify({"success": False, "error": "Хранилище документов не настроено (нет ChromaDB)"})
    try:
        filename = uploaded.filename or "document"
        lower = filename.lower()
        if lower.endswith(".pdf"):
            if PdfReader is None:
                return jsonify({"success": False, "error": "PDF модуль недоступен"})
            reader = PdfReader(uploaded)
            text = " ".join(page.extract_text() or "" for page in reader.pages)
        elif lower.endswith(".docx"):
            if Document is None:
                return jsonify({"success": False, "error": "DOCX модуль недоступен"})
            doc = Document(uploaded)
            text = " ".join(p.text for p in doc.paragraphs)
        else:
            text = uploaded.read().decode("utf-8", errors="ignore")

        words = text.split()
        if not words:
            return jsonify({"success": False, "error": "Документ пустой или текст не распознан"})

        client = require_openai()
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        for i in range(0, len(words), 500):
            chunk = " ".join(words[i: i + 500])
            emb = client.embeddings.create(model="text-embedding-3-small", input=chunk).data[0].embedding
            collection.add(embeddings=[emb], documents=[chunk], ids=[f"{filename}_{stamp}_{i}"])
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)})


@app.route("/ask_docs", methods=["POST"])
def ask_docs():
    question = (request.get_json(silent=True) or {}).get("question", "").strip()
    if not question:
        return jsonify({"answer": "Задайте вопрос."})
    if collection is None:
        return jsonify({"answer": "Хранилище документов не настроено"})
    try:
        client = require_openai()
        emb = client.embeddings.create(model="text-embedding-3-small", input=question).data[0].embedding
        results = collection.query(query_embeddings=[emb], n_results=3)
        context = "\n\n".join(results["documents"][0]) if results.get("documents") else ""
        response = client.chat.completions.create(
            model=get_env("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": SYSTEM + (f"\n\nКонтекст документов:\n{context}" if context else "")},
                {"role": "user", "content": question},
            ],
        )
        return jsonify({"answer": response.choices[0].message.content})
    except Exception as exc:
        return jsonify({"answer": str(exc)})


# ── Search ────────────────────────────────────────────────────────────────────

@app.route("/search")
def search():
    q = request.args.get("q", "").lower().strip()
    if not q:
        return jsonify({"results": []})
    results = []
    crm = load_crm()
    for c in crm.get("clients", []):
        text = f"{c.get('name','')} {c.get('phone','')} {c.get('company','')}".lower()
        if q in text:
            results.append({
                "type": "CRM — клиент",
                "title": c.get("name", ""),
                "info": f"{c.get('phone','')} | {c.get('company','')} | {c.get('status','')}",
            })
    analytics = load_analytics()
    for business, records in analytics.items():
        names = {"obshchepit": "Общепит", "akva": "Аква бизнес", "prodvizhenie": "Продвижение"}
        for record in records:
            if q in str(record.get("comment", "")).lower() or q in str(record.get("date", "")).lower():
                results.append({
                    "type": "Аналитика — " + names.get(business, business),
                    "title": f"{record.get('revenue', 0)} ₴ | {record.get('date', '')}",
                    "info": str(record.get("comment", "")),
                })
    tasks_data = load_tasks_data()
    for t in tasks_data.get("tasks", []):
        if q in t.get("text", "").lower():
            results.append({
                "type": "Задача",
                "title": t["text"],
                "info": ("Выполнена" if t.get("done") else "Активная") + " · " + t.get("created", ""),
            })
    return jsonify({"results": results})


# ── Google Sheets ─────────────────────────────────────────────────────────────

@app.route("/sheet_read", methods=["POST"])
def sheet_read():
    if not require_login():
        return jsonify({"error": "Unauthorized"}), 401
    if sheets_service is None:
        return jsonify({"error": "Google Sheets не настроен. Добавь service_account.json в папку проекта."})
    data = request.get_json(silent=True) or {}
    spreadsheet_id = data.get("spreadsheet_id", "").strip()
    range_name = data.get("range", "Sheet1").strip()
    if not spreadsheet_id:
        return jsonify({"error": "Не указан ID таблицы"})
    try:
        result = (
            sheets_service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=range_name)
            .execute()
        )
        return jsonify({"values": result.get("values", [])})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/sheet_append", methods=["POST"])
def sheet_append():
    if not require_login():
        return jsonify({"success": False}), 401
    if sheets_service is None:
        return jsonify({"success": False, "error": "Google Sheets не настроен"})
    data = request.get_json(silent=True) or {}
    spreadsheet_id = data.get("spreadsheet_id", "").strip()
    range_name = data.get("range", "Sheet1").strip()
    values = data.get("values", [])
    if not spreadsheet_id:
        return jsonify({"success": False, "error": "Не указан ID таблицы"})
    try:
        sheets_service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueInputOption="USER_ENTERED",
            body={"values": [values]},
        ).execute()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ── Users ─────────────────────────────────────────────────────────────────────

def load_users_data():
    data = read_json(USERS_FILE, {"users": []})
    # Создаём дефолтного admin если файла нет
    if not data.get("users"):
        default_user = {
            "id": 1,
            "name": profile.get("name", "Никита"),
            "email": get_env("GMAIL", "admin@nexus.ai"),
            "password": hashlib.sha256("nexus2026".encode()).hexdigest(),
            "role": "admin",
            "access": ["dashboard", "chat", "email", "calendar", "docs", "tasks", "analytics", "crm", "sheets"],
            "created": datetime.now().strftime("%d.%m.%Y"),
        }
        data = {"users": [default_user]}
        write_json(USERS_FILE, data)
    return data


@app.route("/users_list")
def users_list():
    if not require_login():
        return jsonify({"error": "Unauthorized"}), 401
    data = load_users_data()
    safe = [
        {
            "id": u["id"],
            "name": u["name"],
            "email": u["email"],
            "role": u.get("role", "staff"),
            "access": u.get("access", []),
            "created": u.get("created", ""),
        }
        for u in data["users"]
    ]
    return jsonify({"users": safe})


@app.route("/users_add", methods=["POST"])
def users_add():
    if not require_login():
        return jsonify({"success": False}), 401
    if session.get("user_role") != "admin":
        return jsonify({"success": False, "error": "Только администратор может добавлять пользователей"})
    try:
        req = request.get_json(silent=True) or {}
        data = load_users_data()
        # Проверка дубликата
        for u in data["users"]:
            if u.get("email", "").lower() == req.get("email", "").lower():
                return jsonify({"success": False, "error": "Пользователь с таким email уже существует"})
        new_user = {
            "id": max((u["id"] for u in data["users"]), default=0) + 1,
            "name": req["name"],
            "email": req["email"],
            "password": hashlib.sha256(req["password"].encode()).hexdigest(),
            "role": req.get("role", "staff"),
            "access": req.get("access", ["dashboard", "chat", "tasks"]),
            "created": datetime.now().strftime("%d.%m.%Y"),
        }
        data["users"].append(new_user)
        write_json(USERS_FILE, data)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5001"))
    print(f"NEXUS запущен: http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", debug=False, port=port)
