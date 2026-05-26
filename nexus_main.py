import base64
import email
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

app = Flask(__name__)
app.secret_key = get_web_session_secret()


def read_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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

LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NEXUS - вход</title>
<style>
:root{--bg:#071016;--panel:#101b22;--panel2:#14242d;--line:#263b45;--text:#eef7f8;--muted:#8ea3aa;--cyan:#37d7e8;--green:#47e08c}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(circle at 20% 10%,#163744 0,#071016 34%,#05090d 100%);font-family:Inter,'Segoe UI',Arial,sans-serif;color:var(--text);padding:24px}
.shell{width:min(420px,100%);border:1px solid var(--line);background:linear-gradient(180deg,rgba(20,36,45,.96),rgba(9,17,23,.96));border-radius:18px;padding:28px;box-shadow:0 30px 80px rgba(0,0,0,.45)}
.brand{display:flex;align-items:center;gap:12px;margin-bottom:24px}.mark{width:42px;height:42px;border-radius:12px;background:linear-gradient(135deg,var(--cyan),var(--green));display:grid;place-items:center;color:#051014;font-weight:900}.name{font-size:22px;font-weight:900;letter-spacing:5px}.sub{color:var(--muted);font-size:13px;margin-top:3px}
label{display:block;color:var(--muted);font-size:12px;margin-bottom:8px}input{width:100%;height:46px;border-radius:12px;border:1px solid var(--line);background:#09131a;color:var(--text);padding:0 14px;font-size:15px;outline:none}input:focus{border-color:var(--cyan);box-shadow:0 0 0 3px rgba(55,215,232,.14)}
button{width:100%;height:46px;margin-top:14px;border:0;border-radius:12px;background:linear-gradient(135deg,var(--cyan),var(--green));color:#051014;font-weight:900;cursor:pointer}.error{min-height:20px;color:#ff7b7b;font-size:13px;margin-top:12px}
</style>
</head>
<body>
<form class="shell" method="post">
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
<title>NEXUS - Центр управления</title>
<style>
:root{
  --bg:#070d11;--side:#0b151b;--panel:#101c23;--panel2:#14242d;--line:#263b45;
  --text:#edf7f8;--muted:#8ba1a8;--soft:#c3d1d5;--cyan:#37d7e8;--green:#47e08c;--amber:#f4b860;--red:#ff7474;
  --shadow:0 22px 60px rgba(0,0,0,.35);--r:14px
}
*{box-sizing:border-box}html,body{height:100%}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,'Segoe UI',Arial,sans-serif;letter-spacing:0}
button,input,textarea{font:inherit}button{cursor:pointer}
.app{height:100vh;display:grid;grid-template-columns:256px 1fr;overflow:hidden;background:radial-gradient(circle at 70% -10%,rgba(55,215,232,.16),transparent 38%),var(--bg)}
.sidebar{background:linear-gradient(180deg,var(--side),#05090c);border-right:1px solid var(--line);display:flex;flex-direction:column;min-width:0}
.brand{padding:22px 20px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px}.mark{width:40px;height:40px;border-radius:12px;background:linear-gradient(135deg,var(--cyan),var(--green));display:grid;place-items:center;color:#041115;font-weight:900}.brand-title{font-size:19px;font-weight:900;letter-spacing:5px}.brand-sub{font-size:11px;color:var(--muted);margin-top:3px}
.nav{padding:12px 10px;display:grid;gap:4px}.nav button{height:42px;border:0;background:transparent;color:var(--muted);border-radius:10px;padding:0 12px;text-align:left;display:flex;align-items:center;gap:10px}.nav button:hover{background:rgba(255,255,255,.04);color:var(--text)}.nav button.active{background:rgba(55,215,232,.12);color:var(--cyan);box-shadow:inset 3px 0 0 var(--cyan)}
.ico{width:22px;height:22px;display:grid;place-items:center;border-radius:7px;background:#12242d;color:var(--soft);font-size:12px;font-weight:900}.nav button.active .ico{background:rgba(55,215,232,.18);color:var(--cyan)}
.side-bottom{margin-top:auto;padding:16px 18px;border-top:1px solid var(--line);color:var(--muted);font-size:13px}.online{display:flex;align-items:center;gap:8px}.dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 16px var(--green)}
.main{min-width:0;display:flex;flex-direction:column;overflow:hidden}.topbar{height:64px;border-bottom:1px solid var(--line);background:rgba(10,18,24,.78);backdrop-filter:blur(14px);display:flex;align-items:center;justify-content:space-between;padding:0 24px}.title{font-weight:850;font-size:18px}.meta{display:flex;align-items:center;gap:14px;color:var(--muted);font-size:13px}.logout{color:var(--muted);text-decoration:none;border:1px solid var(--line);padding:8px 10px;border-radius:10px}
.content{overflow:auto;padding:22px;min-height:0}.page{display:none}.page.active{display:block}.stack{display:grid;gap:16px}.grid4{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.card{background:linear-gradient(180deg,rgba(20,36,45,.92),rgba(13,23,30,.92));border:1px solid var(--line);border-radius:var(--r);padding:18px;box-shadow:var(--shadow)}.card h3{margin:0 0 14px;font-size:13px;color:var(--cyan);text-transform:uppercase;letter-spacing:1px}
.stat{min-height:116px;display:flex;flex-direction:column;justify-content:space-between}.stat .num{font-size:34px;font-weight:900;color:var(--text)}.stat .lab{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:1px}.stat .hint{font-size:12px;color:var(--soft)}
.field{width:100%;border:1px solid var(--line);background:#0a141b;color:var(--text);border-radius:11px;min-height:44px;padding:0 13px;outline:none;margin-bottom:10px}.field:focus{border-color:var(--cyan);box-shadow:0 0 0 3px rgba(55,215,232,.12)}textarea.field{padding:12px 13px;resize:vertical;line-height:1.5}
.btn{border:0;border-radius:11px;min-height:42px;padding:0 15px;background:linear-gradient(135deg,var(--cyan),var(--green));color:#041115;font-weight:900}.btn.secondary{background:#0a141b;color:var(--cyan);border:1px solid var(--line)}.btn.danger{background:var(--red);color:#210808}.row{display:flex;gap:10px;align-items:center}.row .field{margin:0}
.chatbox{height:calc(100vh - 150px);display:flex;flex-direction:column}.messages{flex:1;overflow:auto;display:flex;flex-direction:column;gap:12px;padding-right:4px}.bubble{max-width:78%;padding:12px 14px;border-radius:14px;line-height:1.55;white-space:pre-wrap;font-size:14px}.bubble.user{align-self:flex-end;background:#183447}.bubble.ai{align-self:flex-start;background:#0f2a24;border:1px solid rgba(71,224,140,.18)}.speaker{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin-bottom:4px}.compose{border-top:1px solid var(--line);padding-top:12px;display:flex;gap:10px}.compose textarea{margin:0;min-height:46px;max-height:120px}
.list{display:grid;gap:10px}.item{background:#0a141b;border:1px solid var(--line);border-radius:12px;padding:12px}.item-title{font-weight:750;margin-bottom:5px}.item-meta{color:var(--muted);font-size:13px;line-height:1.45}.drop{border:1px dashed #35515d;border-radius:13px;padding:26px;text-align:center;color:var(--muted);background:#0a141b}
.alert{border-radius:12px;padding:11px 13px;margin-bottom:12px;font-size:13px}.alert.ok{background:rgba(71,224,140,.12);color:var(--green);border:1px solid rgba(71,224,140,.28)}.alert.err{background:rgba(255,116,116,.12);color:var(--red);border:1px solid rgba(255,116,116,.28)}
@media(max-width:900px){.app{grid-template-columns:1fr}.sidebar{display:none}.grid4,.grid2{grid-template-columns:1fr}.content{padding:14px}.topbar{padding:0 14px}.chatbox{height:calc(100vh - 120px)}.bubble{max-width:92%}}
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="brand"><div class="mark">N</div><div><div class="brand-title">NEXUS</div><div class="brand-sub">ЦЕНТР УПРАВЛЕНИЯ</div></div></div>
    <nav class="nav">
      <button class="active" data-page="dashboard"><span class="ico">D</span>Dashboard</button>
      <button data-page="chat"><span class="ico">AI</span>Чат NEXUS</button>
      <button data-page="email"><span class="ico">@</span>Email</button>
      <button data-page="calendar"><span class="ico">C</span>Календарь</button>
      <button data-page="docs"><span class="ico">F</span>Документы</button>
      <button data-page="tasks"><span class="ico">T</span>Задачи</button>
      <button data-page="search"><span class="ico">S</span>Поиск</button>
    </nav>
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
            <div class="card stat"><div class="num" id="sVoice">0</div><div><div class="lab">Голосовых</div><div class="hint">Ввод голосом</div></div></div>
            <div class="card stat"><div class="num" id="sEmails">0</div><div><div class="lab">Писем</div><div class="hint">Последняя загрузка</div></div></div>
            <div class="card stat"><div class="num" id="sEvents">0</div><div><div class="lab">Событий</div><div class="hint">Ближайший календарь</div></div></div>
          </div>
          <div class="grid2">
            <div class="card"><h3>Быстрый запрос</h3><div class="row"><input class="field" id="quickInput" placeholder="Спроси NEXUS..." onkeydown="if(event.key==='Enter')quickAsk()"><button class="btn" onclick="quickAsk()">Спросить</button></div><div id="quickResult" class="item-meta"></div></div>
            <div class="card"><h3>Ближайшие события</h3><div id="dashEvents" class="list"><div class="item-meta">Загрузка...</div></div></div>
          </div>
        </div>
      </div>
      <div class="page" id="chat">
        <div class="card chatbox">
          <div class="messages" id="messages"><div class="speaker">NEXUS</div><div class="bubble ai">Привет, Никита. Центр управления активен. Что делаем?</div></div>
          <div class="compose"><button class="btn secondary" id="micBtn" onclick="toggleVoice()">Микрофон</button><textarea class="field" id="chatInput" placeholder="Напишите сообщение..." onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendMsg(false)}"></textarea><button class="btn" onclick="sendMsg(false)">Отправить</button></div>
        </div>
      </div>
      <div class="page" id="email">
        <div class="stack"><div class="card"><div class="row" style="justify-content:space-between"><h3>Email центр</h3><button class="btn secondary" onclick="loadEmails()">Обновить</button></div><div id="emailList" class="list"><div class="item-meta">Нажмите обновить, чтобы загрузить письма.</div></div></div><div class="card"><h3>Новое письмо</h3><input class="field" id="emailTo" placeholder="Кому"><input class="field" id="emailSubject" placeholder="Тема"><textarea class="field" id="emailBody" rows="5" placeholder="Текст письма"></textarea><div class="row"><button class="btn" onclick="sendEmail()">Отправить</button><button class="btn secondary" onclick="aiWriteEmail()">AI черновик</button></div></div></div>
      </div>
      <div class="page" id="calendar">
        <div class="grid2"><div class="card"><h3>Новое событие</h3><input class="field" id="eventTitle" placeholder="Название"><div class="row"><input class="field" id="eventDate" type="date"><input class="field" id="eventTime" type="time"></div><button class="btn" onclick="addEvent()">Добавить</button></div><div class="card"><h3>Календарь</h3><div id="calEvents" class="list"></div></div></div>
      </div>
      <div class="page" id="docs">
        <div class="grid2"><div class="card"><h3>Документы</h3><div class="drop" onclick="document.getElementById('docFile').click()">Загрузить PDF, DOCX или TXT</div><input id="docFile" type="file" accept=".pdf,.docx,.txt" style="display:none" onchange="uploadDoc(this)"><div id="docStatus"></div><div id="docList" class="list"></div></div><div class="card"><h3>Вопрос по документам</h3><input class="field" id="docQuestion" placeholder="Что найти или объяснить?" onkeydown="if(event.key==='Enter')askDocs()"><button class="btn" onclick="askDocs()">Спросить</button><div id="docAnswer" class="item-meta" style="margin-top:12px"></div></div></div>
      </div>
      <div class="page" id="tasks">
        <div class="card"><h3>Задачи</h3><div class="row"><input class="field" id="taskInput" placeholder="Новая задача" onkeydown="if(event.key==='Enter')addTask()"><button class="btn" onclick="addTask()">Добавить</button></div><div id="taskList" class="list"></div></div>
      </div>
      <div class="page" id="search">
        <div class="card"><h3>Поиск по системе</h3><div class="row"><input class="field" id="searchInput" placeholder="Клиенты, заметки, аналитика..." onkeydown="if(event.key==='Enter')doSearch()"><button class="btn" onclick="doSearch()">Найти</button></div><div id="searchResults" class="list"></div></div>
      </div>
    </section>
  </main>
</div>
<script>
var tasks=JSON.parse(localStorage.getItem('nexusTasks')||'[]'),docs=[],recognition=null,isListening=false,audioStore={},audioIndex=0,currentAudio=null;
var titles={dashboard:'Dashboard',chat:'Чат NEXUS',email:'Email центр',calendar:'Календарь',docs:'Документы',tasks:'Задачи',search:'Поиск'};
function $(id){return document.getElementById(id)}function esc(t){var d=document.createElement('div');d.textContent=t||'';return d.innerHTML}
document.querySelectorAll('.nav button').forEach(function(b){b.onclick=function(){document.querySelectorAll('.nav button').forEach(function(x){x.classList.remove('active')});document.querySelectorAll('.page').forEach(function(p){p.classList.remove('active')});b.classList.add('active');$(b.dataset.page).classList.add('active');$('pageTitle').textContent=titles[b.dataset.page]||'NEXUS'}});
function alertMsg(text,ok){$('alert').innerHTML='<div class="alert '+(ok?'ok':'err')+'">'+esc(text)+'</div>';setTimeout(function(){$('alert').innerHTML=''},4200)}
function updateClock(){$('clock').textContent=new Date().toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'})}setInterval(updateClock,1000);updateClock();
function api(url,opts){return fetch(url,opts).then(function(r){return r.text().then(function(t){try{return JSON.parse(t)}catch(e){throw new Error('Сервер вернул не JSON для '+url)}})})}
function loadStats(){api('/stats').then(function(d){$('sMessages').textContent=d.messages||0;$('sVoice').textContent=d.voice||0;$('sEmails').textContent=d.emails||0;$('sEvents').textContent=d.events||0}).catch(function(){})}
function addBubble(who,text){var wrap=document.createElement('div');wrap.className='bubble '+(who==='user'?'user':'ai');wrap.textContent=text;var msg=$('messages');if(who!=='user'){var s=document.createElement('div');s.className='speaker';s.textContent='NEXUS';msg.appendChild(s)}msg.appendChild(wrap);msg.scrollTop=msg.scrollHeight;return wrap}
function sendMsg(voice){var inp=$('chatInput'),text=inp.value.trim();if(!text)return;inp.value='';addBubble('user',text);var pending=addBubble('ai','Думаю...');api('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text,voice:voice})}).then(function(d){pending.textContent=d.reply||'';if(d.audio){var a=new Audio('data:audio/mp3;base64,'+d.audio);if(voice)a.play().catch(function(){})}loadStats()}).catch(function(e){pending.textContent=e.message})}
function quickAsk(){var inp=$('quickInput'),text=inp.value.trim();if(!text)return;inp.value='';$('quickResult').textContent='Думаю...';api('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text,voice:false})}).then(function(d){$('quickResult').textContent=d.reply||'';loadStats()}).catch(function(e){$('quickResult').textContent=e.message})}
function toggleVoice(){if(!window.SpeechRecognition&&!window.webkitSpeechRecognition){alertMsg('Голосовой ввод работает в Chrome.',false);return}if(isListening&&recognition){recognition.stop();return}var SR=window.SpeechRecognition||window.webkitSpeechRecognition;recognition=new SR();recognition.lang='ru-RU';recognition.onstart=function(){isListening=true;$('micBtn').textContent='Стоп'};recognition.onend=function(){isListening=false;$('micBtn').textContent='Микрофон'};recognition.onerror=recognition.onend;recognition.onresult=function(e){$('chatInput').value=e.results[0][0].transcript;sendMsg(true)};recognition.start()}
function loadEvents(){api('/events').then(function(d){var html=(d.events||[]).map(function(e){return '<div class="item"><div class="item-title">'+esc(e.title)+'</div><div class="item-meta">'+esc(e.time)+'</div></div>'}).join('')||'<div class="item-meta">Нет событий</div>';$('calEvents').innerHTML=html;$('dashEvents').innerHTML=html;loadStats()}).catch(function(e){$('dashEvents').innerHTML='<div class="item-meta">'+esc(e.message)+'</div>'})}
function addEvent(){var title=$('eventTitle').value,date=$('eventDate').value,time=$('eventTime').value;if(!title||!date||!time){alertMsg('Заполните название, дату и время.',false);return}api('/add_event',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:title,date:date,time:time})}).then(function(d){if(d.success){alertMsg('Событие добавлено.',true);$('eventTitle').value='';loadEvents()}else alertMsg(d.error||'Ошибка календаря',false)})}
function loadEmails(){$('emailList').innerHTML='<div class="item-meta">Загружаю...</div>';api('/emails').then(function(d){var emails=d.emails||[];$('emailList').innerHTML=emails.map(function(e,i){return '<div class="item" onclick="analyzeEmail('+i+')"><div class="item-title">'+esc(e.subject)+'</div><div class="item-meta">'+esc(e.from)+'<br>'+esc(e.preview)+'</div></div>'}).join('')||'<div class="item-meta">'+esc(d.error||'Писем нет')+'</div>';window.emailCache=emails;loadStats()})}
function analyzeEmail(i){var e=(window.emailCache||[])[i];if(!e)return;api('/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({subject:e.subject,text:e.body})}).then(function(d){alertMsg(d.analysis||'Готово',true)})}
function sendEmail(){var to=$('emailTo').value,subject=$('emailSubject').value,body=$('emailBody').value;if(!to||!subject||!body){alertMsg('Заполните все поля письма.',false);return}api('/send_email',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({to:to,subject:subject,body:body})}).then(function(d){alertMsg(d.success?'Письмо отправлено.':d.error,false)})}
function aiWriteEmail(){var p=prompt('Что нужно написать?');if(!p)return;api('/generate_email',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:p})}).then(function(d){$('emailSubject').value=d.subject||'';$('emailBody').value=d.body||''})}
function uploadDoc(input){var f=input.files[0];if(!f)return;var fd=new FormData();fd.append('file',f);$('docStatus').innerHTML='<div class="alert ok">Загружаю...</div>';api('/upload_doc',{method:'POST',body:fd}).then(function(d){if(d.success){docs.push(f.name);$('docStatus').innerHTML='<div class="alert ok">Документ загружен.</div>';$('docList').innerHTML=docs.map(function(x){return'<div class="item">'+esc(x)+'</div>'}).join('')}else $('docStatus').innerHTML='<div class="alert err">'+esc(d.error)+'</div>'})}
function askDocs(){var q=$('docQuestion').value.trim();if(!q)return;$('docAnswer').textContent='Думаю...';api('/ask_docs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q})}).then(function(d){$('docAnswer').textContent=d.answer||''})}
function addTask(){var t=$('taskInput').value.trim();if(!t)return;$('taskInput').value='';tasks.push({text:t,done:false});renderTasks()}function toggleTask(i){tasks[i].done=!tasks[i].done;renderTasks()}function renderTasks(){localStorage.setItem('nexusTasks',JSON.stringify(tasks));$('taskList').innerHTML=tasks.map(function(t,i){return'<div class="item" onclick="toggleTask('+i+')"><div class="item-title" style="'+(t.done?'text-decoration:line-through;color:var(--muted)':'')+'">'+esc(t.text)+'</div></div>'}).join('')||'<div class="item-meta">Задач нет</div>'}renderTasks();
function doSearch(){var q=$('searchInput').value.trim();if(!q)return;$('searchResults').innerHTML='<div class="item-meta">Ищу...</div>';api('/search?q='+encodeURIComponent(q)).then(function(d){$('searchResults').innerHTML=(d.results||[]).map(function(r){return'<div class="item"><div class="item-title">'+esc(r.title)+'</div><div class="item-meta">'+esc(r.type)+'<br>'+esc(r.info)+'</div></div>'}).join('')||'<div class="item-meta">Ничего не найдено</div>'})}
loadStats();loadEvents();setInterval(loadStats,8000);
</script>
</body>
</html>"""


def require_login():
    return session.get("logged_in") is True


@app.route("/")
def index():
    if not require_login():
        return redirect("/login")
    return render_template_string(HTML, email=get_env("GMAIL", "photobusines63@gmail.com"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        if request.form.get("password", "") == require_web_password():
            session["logged_in"] = True
            return redirect("/")
        error = "Неверный пароль"
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/stats")
def get_stats():
    return jsonify(stats)


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
        return jsonify({"emails": [], "error": "Email не настроен: задайте GMAIL и APP_PASSWORD"})
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
            emails.append(
                {
                    "from": decode_mime(msg.get("From", ""))[:80],
                    "subject": decode_mime(msg.get("Subject", "Без темы"))[:120],
                    "preview": body[:120],
                    "body": body[:2000],
                    "date": msg.get("Date", "")[:40],
                }
            )
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


@app.route("/upload_doc", methods=["POST"])
def upload_doc():
    uploaded = request.files.get("file")
    if not uploaded:
        return jsonify({"success": False, "error": "Нет файла"})
    if collection is None:
        return jsonify({"success": False, "error": "Хранилище документов не настроено"})
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
            chunk = " ".join(words[i : i + 500])
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


@app.route("/search")
def search():
    q = request.args.get("q", "").lower().strip()
    if not q:
        return jsonify({"results": []})
    results = []
    crm = read_json(CRM_FILE, {})
    for client in crm.get("clients", []):
        text = f"{client.get('name', '')} {client.get('phone', '')} {client.get('company', '')}".lower()
        if q in text:
            results.append(
                {
                    "type": "CRM - клиент",
                    "title": client.get("name", ""),
                    "info": f"{client.get('phone', '')} | {client.get('company', '')}",
                }
            )
    analytics = read_json(ANALYTICS_FILE, {})
    for business, records in analytics.items():
        for record in records:
            if q in str(record.get("comment", "")).lower() or q in str(record.get("date", "")).lower():
                results.append(
                    {
                        "type": "Аналитика - " + business,
                        "title": f"{record.get('revenue', 0)} грн | {record.get('date', '')}",
                        "info": str(record.get("comment", "")),
                    }
                )
    return jsonify({"results": results})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5001"))
    print(f"NEXUS Main: http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", debug=False, port=port)
