import os
import base64
import imaplib
import smtplib
import email
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string, session, redirect
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build
import chromadb
from pypdf import PdfReader
from docx import Document
import json


client = OpenAI(api_key=OPENAI_API_KEY)
app = Flask(__name__)
app.secret_key = "nexus_secret_2026_nikita"

LOGIN_HTML = """<!DOCTYPE html>
<html><head><title>NEXUS Login</title><meta charset="utf-8">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#080c14;color:#e8f4f8;font-family:Arial;height:100vh;display:flex;align-items:center;justify-content:center}
.box{background:#0d1520;border:1px solid rgba(0,212,255,0.15);border-radius:16px;padding:32px;width:320px}
h1{color:#00d4ff;text-align:center;margin-bottom:24px;letter-spacing:6px}
input{width:100%;background:#111d2e;border:1px solid rgba(0,212,255,0.15);color:#e8f4f8;padding:12px;border-radius:8px;font-size:15px;outline:none;margin-bottom:12px}
button{width:100%;background:#00d4ff;color:#000;border:none;padding:12px;border-radius:8px;font-weight:bold;cursor:pointer}
.err{color:#ff4444;font-size:13px;text-align:center;margin-top:8px}
</style></head>
<body><div class="box">
<h1>NEXUS</h1>
<form method="post">
<input type="password" name="password" placeholder="Пароль" autofocus>
<button type="submit">ВОЙТИ</button>
</form>
<div class="err">{{ error }}</div>
</div></body></html>"""
history = []
stats = {"messages": 0, "voice": 0, "emails": 0, "events": 0}

chroma = chromadb.Client()
collection = chroma.get_or_create_collection("nexus_docs")

try:
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=['https://www.googleapis.com/auth/calendar']
    )
    calendar_service = build('calendar', 'v3', credentials=creds)
    CALENDAR_OK = True
except:
    CALENDAR_OK = False

SYSTEM = """Ты NEXUS — центр управления всем. Мощный персональный AI помощник уровня Jarvis.
Пользователь: Никита.
Бизнес: общепит, аква бизнес, компания по продвижению бизнеса.
Локация: Украина.
Возможности: чат, голос, email, календарь, документы, задачи.
Всегда отвечай на русском языке. Обращайся по имени Никита.
Будь конкретным, полезным и мощным помощником."""

HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NEXUS — Центр управления</title>
<style>
:root{
  --bg:#060a12;
  --bg2:#0a1628;
  --bg3:#0f1f35;
  --accent:#00d4ff;
  --accent2:#0099bb;
  --green:#00ff88;
  --red:#ff4444;
  --orange:#ff9500;
  --text:#e8f4f8;
  --text2:#7a9ab8;
  --border:rgba(0,212,255,0.12);
  --shadow:0 4px 24px rgba(0,0,0,0.4);
  --glow:0 0 20px rgba(0,212,255,0.15);
}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;overflow:hidden}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',Arial,sans-serif;display:flex;height:100vh}

/* SIDEBAR */
.sidebar{width:240px;background:linear-gradient(180deg,#0a1628 0%,#060a12 100%);border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0;box-shadow:4px 0 20px rgba(0,0,0,0.3)}
.sidebar-logo{padding:20px;border-bottom:1px solid var(--border)}
.logo{font-size:20px;font-weight:900;color:var(--accent);letter-spacing:6px}
.logo-sub{font-size:10px;color:var(--text2);margin-top:2px;letter-spacing:2px}
.sidebar-nav{flex:1;padding:12px 0}
.nav-item{display:flex;align-items:center;gap:12px;padding:11px 20px;cursor:pointer;color:var(--text2);font-size:14px;transition:all 0.25s;border-left:3px solid transparent;border-radius:0 8px 8px 0;margin:2px 8px 2px 0}
.nav-item:hover{background:rgba(0,212,255,0.08);color:var(--text);transform:translateX(2px)}
.nav-item.active{background:linear-gradient(90deg,rgba(0,212,255,0.15),rgba(0,212,255,0.05));color:var(--accent);border-left-color:var(--accent);box-shadow:var(--glow)}
.nav-icon{font-size:16px;width:20px;text-align:center}
.sidebar-bottom{padding:16px;border-top:1px solid var(--border)}
.status-pill{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text2)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}

/* MAIN */
.main{flex:1;display:flex;flex-direction:column;overflow:hidden}
.topbar{background:var(--bg2);border-bottom:1px solid var(--border);padding:12px 20px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.page-title{font-size:16px;font-weight:700;color:var(--text)}
.topbar-right{display:flex;align-items:center;gap:12px;font-size:13px;color:var(--text2)}
.content{flex:1;overflow-y:auto;padding:20px}
.content::-webkit-scrollbar{width:4px}
.content::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}

/* PAGES */
.page{display:none}
.page.active{display:block}

/* DASHBOARD */
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:20px}
.stat-card{background:linear-gradient(135deg,var(--bg2),var(--bg3));border:1px solid var(--border);border-radius:20px;padding:20px;transition:all 0.3s;box-shadow:var(--shadow)}
.stat-card:hover{transform:translateY(-2px);box-shadow:var(--glow),var(--shadow);border-color:rgba(0,212,255,0.3)}
.stat-val{font-size:28px;font-weight:900;color:var(--accent);margin-bottom:4px}
.stat-lab{font-size:12px;color:var(--text2);text-transform:uppercase;letter-spacing:1px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.card{background:linear-gradient(135deg,var(--bg2),var(--bg3));border:1px solid var(--border);border-radius:20px;padding:20px;box-shadow:var(--shadow)}
.card h3{font-size:13px;color:var(--accent);margin-bottom:14px;text-transform:uppercase;letter-spacing:1px}

/* CHAT */
.chat-layout{display:flex;flex-direction:column;height:calc(100vh - 120px)}
.chat-messages{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:12px;padding-bottom:10px}
.chat-messages::-webkit-scrollbar{width:4px}
.chat-messages::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}
.mw{display:flex;flex-direction:column}
.mw.user{align-self:flex-end;align-items:flex-end;max-width:75%}
.mw.nexus{align-self:flex-start;align-items:flex-start;max-width:75%}
.mn{font-size:11px;color:var(--text2);margin-bottom:3px;font-weight:600}
.mn.nx{color:var(--accent)}
.msg{padding:10px 14px;border-radius:14px;line-height:1.6;font-size:14px;word-break:break-word}
.msg.user{background:#1a3a5c;border-radius:14px 14px 4px 14px}
.msg.nexus{background:#0a1f1a;border:1px solid var(--border);border-radius:14px 14px 14px 4px}
.play-btn{margin-top:5px;background:transparent;border:1px solid var(--accent);color:var(--accent);padding:3px 10px;border-radius:20px;font-size:11px;cursor:pointer}
.typing span{width:6px;height:6px;background:var(--accent);border-radius:50%;display:inline-block;margin:0 2px;animation:bounce 1.2s infinite}
.typing span:nth-child(2){animation-delay:0.2s}
.typing span:nth-child(3){animation-delay:0.4s}
@keyframes bounce{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-6px)}}
.chat-input{padding-top:12px;border-top:1px solid var(--border);display:flex;gap:8px;align-items:flex-end;flex-shrink:0}
.chat-input textarea{flex:1;background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:10px 14px;border-radius:10px;font-size:14px;outline:none;resize:none;font-family:inherit;max-height:100px}
.chat-input textarea:focus{border-color:var(--accent)}
.btn{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#000;border:none;padding:10px 18px;border-radius:12px;font-weight:bold;cursor:pointer;font-size:14px;display:flex;align-items:center;justify-content:center;transition:all 0.2s;box-shadow:0 4px 12px rgba(0,212,255,0.3)}
.btn:hover{transform:translateY(-1px);box-shadow:0 6px 20px rgba(0,212,255,0.4)}
.btn:active{transform:translateY(0)}
.btn.mic{background:var(--bg3);border:1px solid var(--border);color:var(--accent);width:40px;height:40px;border-radius:10px;font-size:18px;flex-shrink:0}
.btn.mic.active{background:var(--red);border-color:var(--red);color:#fff}
.vbar{display:none;align-items:center;gap:8px;padding:6px 12px;background:rgba(255,68,68,0.1);border-radius:8px;font-size:12px;color:var(--red);margin-bottom:8px}
.vbar.on{display:flex}
.wave{display:flex;gap:2px;align-items:center}
.wave span{width:2px;background:var(--red);border-radius:2px;animation:wv 0.8s infinite}
.wave span:nth-child(1){height:6px}.wave span:nth-child(2){height:12px;animation-delay:0.1s}.wave span:nth-child(3){height:9px;animation-delay:0.2s}.wave span:nth-child(4){height:15px;animation-delay:0.3s}.wave span:nth-child(5){height:8px;animation-delay:0.4s}
@keyframes wv{0%,100%{transform:scaleY(1)}50%{transform:scaleY(0.3)}}

/* FORMS */
input,textarea,select{width:100%;background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:10px 14px;border-radius:8px;margin-bottom:10px;font-size:14px;outline:none;font-family:inherit}
input:focus,textarea:focus{border-color:var(--accent)}
label{font-size:12px;color:var(--text2);margin-bottom:4px;display:block}
.btn-full{width:100%}

/* TASKS */
.task-item{background:var(--bg3);border:1px solid var(--border);border-radius:10px;padding:12px 16px;margin-bottom:8px;display:flex;align-items:center;gap:12px}
.task-check{width:18px;height:18px;border-radius:5px;border:2px solid var(--border);cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:11px}
.task-check.done{background:var(--green);border-color:var(--green);color:#000}
.task-text.done{text-decoration:line-through;color:var(--text2)}

/* EMAIL */
.email-item{background:var(--bg3);border:1px solid var(--border);border-radius:10px;padding:12px;margin-bottom:8px;cursor:pointer;transition:border 0.2s}
.email-item:hover{border-color:var(--accent)}
.email-from{font-size:12px;color:var(--accent);margin-bottom:3px}
.email-subject{font-size:13px;margin-bottom:3px}
.email-preview{font-size:11px;color:var(--text2)}

/* CALENDAR */
.event-item{background:var(--bg3);border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:10px;padding:12px;margin-bottom:8px}
.event-title{font-size:14px;font-weight:600;margin-bottom:4px}
.event-time{font-size:12px;color:var(--text2)}

/* STATUS */
.alert{padding:8px 14px;border-radius:8px;font-size:13px;margin-bottom:12px}
.alert.ok{background:rgba(0,255,136,0.1);border:1px solid rgba(0,255,136,0.3);color:var(--green)}
.alert.err{background:rgba(255,68,68,0.1);border:1px solid rgba(255,68,68,0.3);color:var(--red)}

@media(max-width:768px){
  .sidebar{display:none}
  .grid4{grid-template-columns:repeat(2,1fr)}
  .grid2{grid-template-columns:1fr}
}
</style>
</head>
<body>

<div class="sidebar">
  <div class="sidebar-logo">
    <div class="logo">⚡ NEXUS</div>
    <div class="logo-sub">ЦЕНТР УПРАВЛЕНИЯ</div>
  </div>
  <div class="sidebar-nav">
    <div class="nav-item active" onclick="showPage('dashboard',this)"><span class="nav-icon">📊</span>Dashboard</div>
    <div class="nav-item" onclick="showPage('chat',this)"><span class="nav-icon">💬</span>Чат с NEXUS</div>
    <div class="nav-item" onclick="showPage('email',this)"><span class="nav-icon">📧</span>Email</div>
    <div class="nav-item" onclick="showPage('calendar',this)"><span class="nav-icon">📅</span>Календарь</div>
    <div class="nav-item" onclick="showPage('docs',this)"><span class="nav-icon">📁</span>Документы</div>
    <div class="nav-item" onclick="showPage('tasks',this)"><span class="nav-icon">✅</span>Задачи</div>
    <div class="nav-item" onclick="showPage('analytics_page',this)"><span class="nav-icon">📊</span>Analytics</div>
    <div class="nav-item" onclick="showPage('crm_page',this)"><span class="nav-icon">👥</span>CRM</div>
    <div class="nav-item" onclick="showPage('search',this)"><span class="nav-icon">🔍</span>Search</div>
    <div class="nav-item" onclick="window.open('http://127.0.0.1:5010','_blank')"><span class="nav-icon">👤</span>Users</div>












  </div>
  <div class="sidebar-bottom">
    <div class="status-pill"><div class="dot"></div>Никита — онлайн</div>
  </div>
</div>

<div class="main">
  <div class="topbar">
    <div class="page-title" id="page-title">📊 Dashboard</div>
    <div class="topbar-right">
      <span id="current-time"></span>
      <span>photobusines63@gmail.com</span>
    </div>
  </div>

  <div class="content">
    <div id="alert-bar"></div>

    <!-- DASHBOARD -->
    <div class="page active" id="page-dashboard">
      <div class="grid4">
        <div class="stat-card"><div class="stat-val" id="s-msg">0</div><div class="stat-lab">Сообщений</div></div>
        <div class="stat-card"><div class="stat-val" id="s-voice">0</div><div class="stat-lab">Голосовых</div></div>
        <div class="stat-card"><div class="stat-val" id="s-email">0</div><div class="stat-lab">Писем</div></div>
        <div class="stat-card"><div class="stat-val" id="s-events">0</div><div class="stat-lab">Событий</div></div>
      </div>
      <div class="grid2">
        <div class="card">
          <h3>🤖 Быстрый чат</h3>
          <input id="quick-inp" placeholder="Спроси NEXUS..." onkeydown="if(event.key==='Enter')quickAsk()">
          <button class="btn btn-full" onclick="quickAsk()">Спросить</button>
          <div id="quick-result" style="margin-top:10px;font-size:13px;color:var(--text2)"></div>
        </div>
        <div class="card">
          <h3>📅 Ближайшие события</h3>
          <div id="dash-events"><div style="color:var(--text2);font-size:13px">Загрузка...</div></div>
        </div>
      </div>
    </div>

    <!-- CHAT -->
    <div class="page" id="page-chat">
      <div class="chat-layout">
        <div class="vbar" id="vbar"><div class="wave"><span></span><span></span><span></span><span></span><span></span></div>Слушаю...</div>
        <div class="chat-messages" id="chat">
          <div class="mw nexus"><div class="mn nx">NEXUS</div><div class="msg nexus">Привет, Никита! Центр управления активен. Чем могу помочь?</div></div>
        </div>
        <div class="chat-input">
          <button class="btn mic" id="micBtn" onclick="toggleVoice()">🎤</button>
          <textarea id="inp" placeholder="Напишите сообщение..." rows="1"
            onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendMsg(false)}"
            oninput="this.style.height='auto';this.style.height=this.scrollHeight+'px'"></textarea>
          <button class="btn" onclick="sendMsg(false)">➤</button>
        </div>
      </div>
    </div>

    <!-- EMAIL -->
    <div class="page" id="page-email">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
        <div style="font-size:16px;font-weight:700">📧 Входящие письма</div>
        <button class="btn" onclick="loadEmails()">🔄 Обновить</button>
      </div>
      <div id="email-list"><div style="color:var(--text2);font-size:13px">Нажмите обновить для загрузки писем</div></div>
      <div id="email-detail"></div>
      <div class="card" style="margin-top:16px">
        <h3>✏️ Написать письмо</h3>
        <input id="email-to" placeholder="Кому">
        <input id="email-subject" placeholder="Тема">
        <textarea id="email-body" placeholder="Текст письма..." rows="4"></textarea>
        <div style="display:flex;gap:8px">
          <button class="btn" onclick="sendEmail()">📤 Отправить</button>
          <button class="btn" style="background:var(--bg3);color:var(--accent);border:1px solid var(--accent)" onclick="aiWriteEmail()">🤖 AI письмо</button>
        </div>
      </div>
    </div>

    <!-- CALENDAR -->
    <div class="page" id="page-calendar">
      <div class="card" style="margin-bottom:16px">
        <h3>➕ Добавить событие</h3>
        <input id="cal-title" placeholder="Название события">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
          <input id="cal-date" type="date">
          <input id="cal-time" type="time">
        </div>
        <button class="btn btn-full" onclick="addEvent()">+ Добавить</button>
      </div>
      <div class="card">
        <h3>📋 Ближайшие события</h3>
        <div id="cal-events"><div style="color:var(--text2);font-size:13px">Загрузка...</div></div>
      </div>
    </div>

    <!-- DOCS -->
    <div class="page" id="page-docs">
      <div class="card" style="margin-bottom:16px">
        <h3>📁 Загрузить документ</h3>
        <div style="border:2px dashed var(--border);border-radius:10px;padding:24px;text-align:center;cursor:pointer" onclick="document.getElementById('docFile').click()">
          <div style="font-size:32px">📄</div>
          <div style="font-size:13px;color:var(--text2);margin-top:8px">PDF, Word, TXT</div>
        </div>
        <input type="file" id="docFile" accept=".pdf,.docx,.txt" style="display:none" onchange="uploadDoc(this)">
        <div id="doc-status" style="margin-top:10px"></div>
        <div id="doc-list" style="margin-top:12px"></div>
      </div>
      <div class="card">
        <h3>🔍 Спроси по документам</h3>
        <input id="doc-q" placeholder="Задай вопрос по загруженным документам..." onkeydown="if(event.key==='Enter')askDocs()">
        <button class="btn btn-full" onclick="askDocs()">Спросить</button>
        <div id="doc-answer" style="margin-top:12px;font-size:14px;line-height:1.6"></div>
      </div>
    </div>

    <!-- TASKS -->
    <div class="page" id="page-tasks">
      <div class="card" style="margin-bottom:16px">
        <h3>➕ Новая задача</h3>
        <div style="display:flex;gap:8px">
          <input id="task-inp" placeholder="Введите задачу..." onkeydown="if(event.key==='Enter')addTask()" style="margin:0">
          <button class="btn" onclick="addTask()">+</button>
        </div>
      </div>
      <div id="task-list"></div>
    </div>


    <!-- SEARCH -->
    <div class="page" id="page-search">
      <div class="card">
        <h3>Поиск по системе</h3>
        <div style="display:flex;gap:8px;margin-bottom:16px">
          <input id="sq" placeholder="Поиск клиентов, записей..." onkeydown="if(event.key==='Enter')doSearch()" style="margin:0">
          <button class="btn" onclick="doSearch()">Найти</button>
        </div>
        <div id="sr"></div>
      </div>
    </div>

  </div>
</div>

<script>
var isListening=false,recognition=null,curAudio=null,audioStore={},audioIdx=0,tasks=[],docs=[];

function showPage(name,el){
  document.querySelectorAll('.page').forEach(function(p){p.classList.remove('active')});
  document.querySelectorAll('.nav-item').forEach(function(n){n.classList.remove('active')});
  document.getElementById('page-'+name).classList.add('active');
  el.classList.add('active');
  var titles={'dashboard':'📊 Dashboard','chat':'💬 Чат с NEXUS','email':'📧 Email','calendar':'📅 Календарь','docs':'📁 Документы','tasks':'✅ Задачи'};
  document.getElementById('page-title').textContent=titles[name]||name;
  if(name==='email')loadEmails();
  if(name==='calendar')loadEvents();
}

function showAlert(msg,ok){
  var b=document.getElementById('alert-bar');
  b.className='alert '+(ok?'ok':'err');
  b.textContent=msg;
  setTimeout(function(){b.textContent='';b.className=''},4000);
}

function updateTime(){
  document.getElementById('current-time').textContent=new Date().toLocaleTimeString('ru-RU');
}
setInterval(updateTime,1000);
updateTime();

function updateStats(){
  fetch('/stats').then(function(r){return r.json()}).then(function(d){
    document.getElementById('s-msg').textContent=d.messages||0;
    document.getElementById('s-voice').textContent=d.voice||0;
    document.getElementById('s-email').textContent=d.emails||0;
    document.getElementById('s-events').textContent=d.events||0;
  });
}
updateStats();
,30000);

function escHtml(t){var d=document.createElement('div');d.textContent=t;return d.innerHTML}
function scrollBottom(){var c=document.getElementById('chat');c.scrollTop=c.scrollHeight}

function toggleVoice(){
  if(!window.SpeechRecognition&&!window.webkitSpeechRecognition){alert('Используйте Chrome!');return}
  if(isListening){recognition.stop();return}
  var SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  recognition=new SR();recognition.lang='ru-RU';
  recognition.onstart=function(){isListening=true;document.getElementById('micBtn').classList.add('active');document.getElementById('micBtn').textContent='⏹';document.getElementById('vbar').classList.add('on')};
  recognitisetInterval(updateStatson.onresult=function(e){document.getElementById('inp').value=e.results[0][0].transcript;sendMsg(true)};
  recognition.onend=function(){isListening=false;document.getElementById('micBtn').classList.remove('active');document.getElementById('micBtn').textContent='🎤';document.getElementById('vbar').classList.remove('on')};
  recognition.start();
}

function sendMsg(voice){
  var inp=document.getElementById('inp');
  var text=inp.value.trim();if(!text)return;
  inp.value='';inp.style.height='auto';
  var chat=document.getElementById('chat');
  var ud=document.createElement('div');ud.className='mw user';
  ud.innerHTML='<div class="mn">Никита</div><div class="msg user">'+escHtml(text)+'</div>';
  chat.appendChild(ud);scrollBottom();
  var td=document.createElement('div');td.className='mw nexus';td.id='typing';
  td.innerHTML='<div class="mn nx">NEXUS</div><div class="msg nexus"><span class="typing"><span></span><span></span><span></span></span></div>';
  chat.appendChild(td);scrollBottom();
  fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text,voice:voice})})
  .then(function(r){return r.json()})
  .then(function(d){
    var t=document.getElementById('typing');if(t)t.remove();
    var nd=document.createElement('div');nd.className='mw nexus';
    var html='<div class="mn nx">NEXUS</div><div class="msg nexus">'+escHtml(d.reply)+'</div>';
    if(d.audio){var idx=audioIdx++;audioStore[idx]=d.audio;
      if(voice){curAudio=new Audio('data:audio/mp3;base64,'+d.audio);curAudio.play().catch(function(){})}
      else{html+='<button class="play-btn" onclick="playAudio('+idx+')">🔊 Слушать</button>'}}
    nd.innerHTML=html;chat.appendChild(nd);scrollBottom();updateStats();
  }).catch(function(){var t=document.getElementById('typing');if(t)t.remove()});
}

function playAudio(idx){if(curAudio)curAudio.pause();curAudio=new Audio('data:audio/mp3;base64,'+audioStore[idx]);curAudio.play()}

function quickAsk(){
  var inp=document.getElementById('quick-inp');
  var text=inp.value.trim();if(!text)return;
  inp.value='';
  document.getElementById('quick-result').textContent='⏳ Думаю...';
  fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text,voice:false})})
  .then(function(r){return r.json()})
  .then(function(d){document.getElementById('quick-result').textContent=d.reply});
}

function loadEmails(){
  document.getElementById('email-list').innerHTML='<div style="color:var(--text2);font-size:13px">⏳ Загружаю...</div>';
  fetch('/emails').then(function(r){return r.json()}).then(function(d){
    stats_emails=d.emails?d.emails.length:0;
    if(!d.emails||d.emails.length===0){document.getElementById('email-list').innerHTML='<div style="color:var(--text2)">Нет писем</div>';return}
    var html='';
    d.emails.forEach(function(e,i){
      html+='<div class="email-item" onclick="showEmail('+i+','+JSON.stringify(d.emails).replace(/"/g,'&quot;')+')">';
      html+='<div class="email-from">✉️ '+escHtml(e.from)+'</div>';
      html+='<div class="email-subject">'+escHtml(e.subject)+'</div>';
      html+='<div class="email-preview">'+escHtml(e.preview)+'</div>';
      html+='</div>';
    });
    document.getElementById('email-list').innerHTML=html;
  });
}

var emailsCache=[];
function showEmail(idx,emails){
  emailsCache=emails||emailsCache;
  var e=emailsCache[idx];if(!e)return;
  var html='<div class="card" style="margin-top:12px">';
  html+='<h3>'+escHtml(e.subject)+'</h3>';
  html+='<p style="color:var(--text2);font-size:12px;margin-bottom:10px">От: '+escHtml(e.from)+'</p>';
  html+='<div style="background:var(--bg3);border-radius:8px;padding:12px;font-size:13px;line-height:1.6;white-space:pre-wrap">'+escHtml(e.body)+'</div>';
  html+='<div style="margin-top:10px;background:#0a1f1a;border:1px solid var(--border);border-radius:8px;padding:10px"><div style="font-size:11px;color:var(--accent);margin-bottom:6px">🤖 AI АНАЛИЗ</div><div id="ai-res">Анализирую...</div></div>';
  html+='</div>';
  document.getElementById('email-detail').innerHTML=html;
  fetch('/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:e.body,subject:e.subject})})
  .then(function(r){return r.json()})
  .then(function(d){var el=document.getElementById('ai-res');if(el)el.textContent=d.analysis});
}

function sendEmail(){
  var to=document.getElementById('email-to').value;
  var subject=document.getElementById('email-subject').value;
  var body=document.getElementById('email-body').value;
  if(!to||!subject||!body){showAlert('Заполните все поля!',false);return}
  fetch('/send_email',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({to:to,subject:subject,body:body})})
  .then(function(r){return r.json()})
  .then(function(d){
    if(d.success){showAlert('Письмо отправлено!',true);document.getElementById('email-to').value='';document.getElementById('email-subject').value='';document.getElementById('email-body').value=''}
    else showAlert('Ошибка: '+d.error,false)
  });
}

function aiWriteEmail(){
  var prompt=prompt('Опиши что написать:');
  if(!prompt)return;
  fetch('/generate_email',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:prompt})})
  .then(function(r){return r.json()})
  .then(function(d){
    document.getElementById('email-subject').value=d.subject||'';
    document.getElementById('email-body').value=d.body||'';
    showAlert('Письмо сгенерировано!',true);
  });
}

function loadEvents(){
  fetch('/events').then(function(r){return r.json()}).then(function(d){
    var html='';
    if(!d.events||d.events.length===0){html='<div style="color:var(--text2);font-size:13px">Нет событий</div>'}
    else{d.events.forEach(function(e){html+='<div class="event-item"><div class="event-title">'+escHtml(e.title)+'</div><div class="event-time">'+escHtml(e.time)+'</div></div>'})}
    document.getElementById('cal-events').innerHTML=html;
    document.getElementById('dash-events').innerHTML=html;
  });
}
loadEvents();

function addEvent(){
  var title=document.getElementById('cal-title').value;
  var date=document.getElementById('cal-date').value;
  var time=document.getElementById('cal-time').value;
  if(!title||!date||!time){showAlert('Заполните все поля!',false);return}
  fetch('/add_event',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:title,date:date,time:time})})
  .then(function(r){return r.json()})
  .then(function(d){
    if(d.success){showAlert('Событие добавлено!',true);document.getElementById('cal-title').value='';loadEvents()}
    else showAlert('Ошибка: '+d.error,false)
  });
}

function uploadDoc(input){
  var file=input.files[0];if(!file)return;
  var fd=new FormData();fd.append('file',file);
  document.getElementById('doc-status').innerHTML='<div class="alert ok">⏳ Загружаю...</div>';
  fetch('/upload_doc',{method:'POST',body:fd})
  .then(function(r){return r.json()})
  .then(function(d){
    if(d.success){docs.push(file.name);document.getElementById('doc-status').innerHTML='<div class="alert ok">✅ Загружено: '+file.name+'</div>';
      document.getElementById('doc-list').innerHTML=docs.map(function(n){return'<div style="background:var(--bg3);border-radius:8px;padding:8px 12px;margin-bottom:6px;font-size:13px">📄 '+n+'</div>'}).join('')}
    else document.getElementById('doc-status').innerHTML='<div class="alert err">Ошибка: '+d.error+'</div>'
  });
}

function askDocs(){
  var q=document.getElementById('doc-q').value.trim();if(!q)return;
  document.getElementById('doc-answer').textContent='⏳ Думаю...';
  fetch('/ask_docs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q})})
  .then(function(r){return r.json()})
  .then(function(d){document.getElementById('doc-answer').textContent=d.answer});
}

function addTask(){
  var inp=document.getElementById('task-inp');
  var text=inp.value.trim();if(!text)return;
  inp.value='';
  tasks.push({text:text,done:false});
  renderTasks();
}

function toggleTask(idx){tasks[idx].done=!tasks[idx].done;renderTasks()}

function renderTasks(){
  var html='';
  tasks.forEach(function(t,i){
    html+='<div class="task-item">';
    html+='<div class="task-check '+(t.done?'done':'')+'" onclick="toggleTask('+i+')">'+(t.done?'✓':'')+'</div>';
    html+='<div class="task-text '+(t.done?'done':'')+'">'+escHtml(t.text)+'</div>';
    html+='</div>';
  });
  document.getElementById('task-list').innerHTML=html||'<div style="color:var(--text2);font-size:13px;text-align:center;padding:20px">Нет задач</div>';
}
renderTasks();

function doSearch(){
  var q=document.getElementById('sq').value.trim();
  if(!q)return;
  document.getElementById('sr').innerHTML='Ищу...';
  fetch('/search?q='+encodeURIComponent(q))
  .then(function(r){return r.json()})
  .then(function(d){
    if(!d.results.length){document.getElementById('sr').innerHTML='<p style="color:var(--text2)">Ничего не найдено</p>';return}
    var html='<p style="color:var(--text2);margin-bottom:12px">Найдено: '+d.results.length+'</p>';
    d.results.forEach(function(r){html+='<div class="card" style="margin-bottom:8px"><div style="font-size:11px;color:var(--accent)">'+r.type+'</div><div style="font-weight:bold">'+r.title+'</div><div style="font-size:13px;color:var(--text2)">'+r.info+'</div></div>'});
    document.getElementById('sr').innerHTML=html;
  });
}

</script>
</body>
</html>"""

@app.route('/')
def index():
    if not session.get('logged_in'):
        return redirect('/login')
    return render_template_string(HTML)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == 'nexus2026':
            session['logged_in'] = True
            return redirect('/')
        return render_template_string(LOGIN_HTML, error='Неверный пароль')
    return render_template_string(LOGIN_HTML, error='')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')
def get_stats():
    return jsonify(stats)

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    msg = data.get('message', '').strip()
    voice = data.get('voice', False)
    if not msg:
        return jsonify({"reply": "Напишите сообщение."})
    stats["messages"] += 1
    if voice:
        stats["voice"] += 1
    history.append({"role": "user", "content": msg})
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": SYSTEM}, *history[-20:]]
        )
        answer = response.choices[0].message.content
    except Exception as exc:
        return jsonify({"reply": "Ошибка: " + str(exc)})
    history.append({"role": "assistant", "content": answer})
    audio_b64 = None
    try:
        audio_response = client.audio.speech.create(model="tts-1", voice="onyx", input=answer)
        audio_b64 = base64.b64encode(audio_response.content).decode()
    except:
        pass
    return jsonify({"reply": answer, "audio": audio_b64})

@app.route('/emails')
def get_emails():
    try:
        mail = imaplib.IMAP4_SSL('imap.gmail.com')
        mail.login(GMAIL, APP_PASSWORD)
        mail.select('inbox')
        _, data = mail.search(None, 'ALL')
        ids = data[0].split()[-15:]
        emails = []
        for eid in reversed(ids):
            _, msg_data = mail.fetch(eid, '(RFC822)')
            msg = email.message_from_bytes(msg_data[0][1])
            def dec(s):
                parts = decode_header(s or '')
                r = ''
                for p, enc in parts:
                    r += p.decode(enc or 'utf-8', errors='ignore') if isinstance(p, bytes) else p
                return r
            subject = dec(msg.get('Subject', 'Без темы'))
            from_addr = dec(msg.get('From', ''))
            body = ''
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == 'text/plain':
                        try:
                            body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                            break
                        except:
                            pass
            else:
                try:
                    body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                except:
                    pass
            emails.append({'from': from_addr[:50], 'subject': subject[:80], 'preview': body[:80], 'body': body[:1500], 'date': msg.get('Date', '')[:30]})
        stats["emails"] = len(emails)
        mail.logout()
        return jsonify({'emails': emails})
    except Exception as e:
        return jsonify({'emails': [], 'error': str(e)})

@app.route('/send_email', methods=['POST'])
def send_email():
    data = request.json
    try:
        msg = MIMEMultipart()
        msg['From'] = GMAIL
        msg['To'] = data['to']
        msg['Subject'] = data['subject']
        msg.attach(MIMEText(data['body'], 'plain', 'utf-8'))
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(GMAIL, APP_PASSWORD)
        server.send_message(msg)
        server.quit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/analyze', methods=['POST'])
def analyze():
    data = request.json
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": "Анализируй письмо кратко на русском. Тип, требуется ответ, срочность, действия."},
                      {"role": "user", "content": f"Тема: {data.get('subject','')}\nТекст: {data.get('text','')[:800]}"}]
        )
        return jsonify({'analysis': response.choices[0].message.content})
    except Exception as e:
        return jsonify({'analysis': str(e)})

@app.route('/generate_email', methods=['POST'])
def generate_email():
    prompt = request.json.get('prompt', '')
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": 'Напиши деловое письмо. Верни JSON: {"subject":"тема","body":"текст"}'},
                      {"role": "user", "content": prompt}]
        )
        text = response.choices[0].message.content.replace('```json','').replace('```','').strip()
        return jsonify(json.loads(text))
    except Exception as e:
        return jsonify({'subject': '', 'body': str(e)})

@app.route('/events')
def get_events():
    if not CALENDAR_OK:
        return jsonify({'events': []})
    try:
        now = datetime.utcnow().isoformat() + 'Z'
        result = calendar_service.events().list(calendarId=CALENDAR_ID, timeMin=now, maxResults=5, singleEvents=True, orderBy='startTime').execute()
        events = [{'title': e.get('summary', 'Без названия'), 'time': e['start'].get('dateTime', e['start'].get('date', ''))} for e in result.get('items', [])]
        stats["events"] = len(events)
        return jsonify({'events': events})
    except Exception as e:
        return jsonify({'events': [], 'error': str(e)})

@app.route('/add_event', methods=['POST'])
def add_event():
    data = request.json
    if not CALENDAR_OK:
        return jsonify({'success': False, 'error': 'Calendar not configured'})
    try:
        start = f"{data['date']}T{data['time']}:00"
        end = (datetime.fromisoformat(start) + timedelta(hours=1)).isoformat()
        event = {'summary': data['title'], 'start': {'dateTime': start, 'timeZone': 'Europe/Kiev'}, 'end': {'dateTime': end, 'timeZone': 'Europe/Kiev'}}
        calendar_service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/upload_doc', methods=['POST'])
def upload_doc():
    file = request.files.get('file')
    if not file:
        return jsonify({'success': False, 'error': 'Нет файла'})
    try:
        if file.filename.endswith('.pdf'):
            reader = PdfReader(file)
            text = ' '.join(p.extract_text() or '' for p in reader.pages)
        elif file.filename.endswith('.docx'):
            doc = Document(file)
            text = ' '.join(p.text for p in doc.paragraphs)
        else:
            text = file.read().decode('utf-8', errors='ignore')
        words = text.split()
        for i in range(0, len(words), 500):
            chunk = ' '.join(words[i:i+500])
            emb = client.embeddings.create(model="text-embedding-3-small", input=chunk).data[0].embedding
            collection.add(embeddings=[emb], documents=[chunk], ids=[f"{file.filename}_{i}"])
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/ask_docs', methods=['POST'])
def ask_docs():
    q = request.json.get('question', '')
    try:
        emb = client.embeddings.create(model="text-embedding-3-small", input=q).data[0].embedding
        results = collection.query(query_embeddings=[emb], n_results=3)
        context = '\n\n'.join(results['documents'][0]) if results['documents'] else ''
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": SYSTEM + (f"\n\nКонтекст:\n{context}" if context else "")},
                      {"role": "user", "content": q}]
        )
        return jsonify({'answer': response.choices[0].message.content})
    except Exception as e:
        return jsonify({'answer': str(e)})


@app.route('/search')
def search():
    import json,os
    q = request.args.get('q','').lower().strip()
    if not q:
        return jsonify({'results':[]})
    results = []
    try:
        with open('crm_data.json','r',encoding='utf-8') as f:
            crm = json.load(f)
        for c in crm.get('clients',[]):
            text = (str(c.get('name',''))+' '+str(c.get('phone',''))+' '+str(c.get('company',''))).lower()
            if q in text:
                results.append({'type':'CRM — Клиент','title':c.get('name',''),'info':str(c.get('phone',''))+' | '+str(c.get('company',''))})
    except: pass
    try:
        with open('analytics_data.json','r',encoding='utf-8') as f:
            analytics = json.load(f)
        for biz,records in analytics.items():
            for r in records:
                if q in str(r.get('comment','')).lower() or q in str(r.get('date','')):
                    results.append({'type':'Аналитика — '+biz,'title':str(r.get('revenue',0))+' грн | '+str(r.get('date','')),'info':str(r.get('comment',''))})
    except: pass
    return jsonify({'results':results})

if __name__ == '__main__':
    print("NEXUS Main: http://127.0.0.1:5000")
    app.run(host="0.0.0.0", debug=False, port=5000)