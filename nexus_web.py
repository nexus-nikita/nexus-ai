import base64
import json
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template_string, request, session, url_for
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

app = Flask(__name__)
app.secret_key = get_web_session_secret()


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


profile = load_json(PROFILE_FILE, DEFAULT_PROFILE.copy())
history = load_json(MEMORY_FILE, [])
SYSTEM = build_system_prompt(profile)

LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NEXUS вход</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{min-height:100vh;background:#080c14;color:#e8f4f8;font-family:'Segoe UI',Arial,sans-serif;display:grid;place-items:center;padding:20px}
.login{width:100%;max-width:360px;background:#0d1520;border:1px solid rgba(0,212,255,.18);border-radius:16px;padding:24px}
.logo{font-size:24px;font-weight:900;color:#00d4ff;letter-spacing:5px;margin-bottom:6px}
.hint{color:#8aa8b8;font-size:13px;margin-bottom:18px}
input{width:100%;background:#111d2e;border:1px solid rgba(0,212,255,.18);color:#e8f4f8;padding:12px 14px;border-radius:10px;font-size:15px;outline:none;margin-bottom:12px}
input:focus{border-color:#00d4ff}
button{width:100%;background:#00d4ff;color:#001018;border:none;border-radius:10px;padding:12px;font-weight:800;cursor:pointer}
.error{color:#ff6b6b;font-size:13px;margin-bottom:12px}
</style>
</head>
<body>
<form class="login" method="post">
  <div class="logo">NEXUS</div>
  <div class="hint">Центр управления. Введите пароль.</div>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <input type="password" name="password" placeholder="Пароль" autofocus>
  <button type="submit">Войти</button>
</form>
</body>
</html>"""

HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NEXUS</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;overflow:hidden}
body{background:#080c14;color:#e8f4f8;font-family:'Segoe UI',Arial,sans-serif;display:flex;flex-direction:column;height:100vh}
.header{background:#0d1520;border-bottom:1px solid rgba(0,212,255,0.15);padding:12px 20px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.logo{font-size:22px;font-weight:900;color:#00d4ff;letter-spacing:6px}
.status{display:flex;align-items:center;gap:6px;font-size:12px;color:#8aa8b8}
.dot{width:8px;height:8px;border-radius:50%;background:#00ff88;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
.chat{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:16px}
.chat::-webkit-scrollbar{width:4px}
.chat::-webkit-scrollbar-thumb{background:rgba(0,212,255,0.2);border-radius:4px}
.mw{display:flex;flex-direction:column}
.mw.user{align-self:flex-end;align-items:flex-end;max-width:80%}
.mw.nexus{align-self:flex-start;align-items:flex-start;max-width:80%}
.mn{font-size:11px;color:#8aa8b8;margin-bottom:4px;font-weight:600;letter-spacing:1px}
.mn.nx{color:#00d4ff}
.msg{padding:12px 16px;border-radius:16px;line-height:1.6;font-size:15px;word-break:break-word;white-space:pre-wrap}
.msg.user{background:#1a3a5c;border-radius:16px 16px 4px 16px}
.msg.nexus{background:#0a1f1a;border:1px solid rgba(0,212,255,0.15);border-radius:16px 16px 16px 4px}
.play-btn{margin-top:6px;background:transparent;border:1px solid #00d4ff;color:#00d4ff;padding:4px 12px;border-radius:20px;font-size:12px;cursor:pointer}
.typing{display:flex;gap:4px;padding:8px}
.typing span{width:8px;height:8px;background:#00d4ff;border-radius:50%;animation:bounce 1.2s infinite}
.typing span:nth-child(2){animation-delay:0.2s}
.typing span:nth-child(3){animation-delay:0.4s}
@keyframes bounce{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-8px)}}
.input-area{background:#0d1520;border-top:1px solid rgba(0,212,255,0.15);padding:16px 20px;display:flex;gap:10px;align-items:flex-end;flex-shrink:0}
textarea{flex:1;background:#111d2e;border:1px solid rgba(0,212,255,0.15);color:#e8f4f8;padding:12px 16px;border-radius:12px;font-size:15px;outline:none;resize:none;font-family:inherit;line-height:1.5;max-height:120px}
textarea:focus{border-color:#00d4ff}
textarea::placeholder{color:#8aa8b8}
.btn{background:#00d4ff;color:#000;border:none;width:44px;height:44px;border-radius:12px;font-size:18px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.mic-btn{background:#111d2e;border:1px solid rgba(0,212,255,0.15);color:#00d4ff}
.mic-btn.active{background:#ff4444;border-color:#ff4444;color:#fff}
.vbar{display:none;align-items:center;gap:8px;padding:8px 16px;background:rgba(255,68,68,0.1);border-top:1px solid rgba(255,68,68,0.3);font-size:13px;color:#ff4444;flex-shrink:0}
.vbar.on{display:flex}
.wave{display:flex;gap:3px;align-items:center}
.wave span{width:3px;background:#ff4444;border-radius:3px;animation:wv 0.8s infinite}
.wave span:nth-child(1){height:8px}
.wave span:nth-child(2){height:16px;animation-delay:0.1s}
.wave span:nth-child(3){height:12px;animation-delay:0.2s}
.wave span:nth-child(4){height:20px;animation-delay:0.3s}
.wave span:nth-child(5){height:10px;animation-delay:0.4s}
@keyframes wv{0%,100%{transform:scaleY(1)}50%{transform:scaleY(0.3)}}
@media(max-width:600px){.mw.user,.mw.nexus{max-width:95%}.chat{padding:12px}.input-area{padding:10px 12px}.logo{font-size:18px;letter-spacing:4px}}
</style>
</head>
<body>
<div class="header">
  <div style="display:flex;align-items:center;gap:12px">
    <div class="logo">NEXUS</div>
    <div class="status"><div class="dot"></div><span>ОНЛАЙН</span></div>
  </div>
  <div style="font-size:11px;color:#8aa8b8">Центр управления</div>
</div>
<div class="vbar" id="vbar"><div class="wave"><span></span><span></span><span></span><span></span><span></span></div>Слушаю...</div>
<div class="chat" id="chat">
  <div class="mw nexus">
    <div class="mn nx">NEXUS</div>
    <div class="msg nexus">Привет, Никита! Центр управления активен. Чем могу помочь?</div>
  </div>
</div>
<div class="input-area">
  <button class="btn mic-btn" id="micBtn" onclick="toggleVoice()">🎤</button>
  <textarea id="inp" placeholder="Напишите сообщение..." rows="1"
    onkeydown="handleKey(event)"
    oninput="this.style.height='auto';this.style.height=this.scrollHeight+'px'"></textarea>
  <button class="btn" onclick="sendMsg(false)">➤</button>
</div>
<script>
var isListening = false;
var recognition = null;
var curAudio = null;
var audioStore = {};
var audioIdx = 0;

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMsg(false);
  }
}

function setVoiceUi(active) {
  isListening = active;
  document.getElementById('micBtn').classList.toggle('active', active);
  document.getElementById('micBtn').textContent = active ? '⏹' : '🎤';
  document.getElementById('vbar').classList.toggle('on', active);
}

function toggleVoice() {
  if (!window.SpeechRecognition && !window.webkitSpeechRecognition) {
    alert('Используйте Chrome для голосового ввода.');
    return;
  }
  if (isListening && recognition) {
    recognition.stop();
    return;
  }
  var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  recognition = new SR();
  recognition.lang = 'ru-RU';
  recognition.continuous = false;
  recognition.interimResults = false;
  recognition.onstart = function() { setVoiceUi(true); };
  recognition.onresult = function(e) {
    document.getElementById('inp').value = e.results[0][0].transcript;
    sendMsg(true);
  };
  recognition.onend = function() { setVoiceUi(false); };
  recognition.onerror = function() { setVoiceUi(false); };
  recognition.start();
}

function escHtml(t) {
  var d = document.createElement('div');
  d.textContent = t || '';
  return d.innerHTML;
}

function scrollBottom() {
  var c = document.getElementById('chat');
  c.scrollTop = c.scrollHeight;
}

function addTyping() {
  var d = document.createElement('div');
  d.className = 'mw nexus';
  d.id = 'typing';
  d.innerHTML = '<div class="mn nx">NEXUS</div><div class="msg nexus"><div class="typing"><span></span><span></span><span></span></div></div>';
  document.getElementById('chat').appendChild(d);
  scrollBottom();
}

function removeTyping() {
  var t = document.getElementById('typing');
  if (t) t.remove();
}

function playAudio(idx) {
  if (curAudio) curAudio.pause();
  var b64 = audioStore[idx];
  if (!b64) return;
  curAudio = new Audio('data:audio/mp3;base64,' + b64);
  curAudio.play().catch(function(){});
}

function sendMsg(voice) {
  var inp = document.getElementById('inp');
  var chat = document.getElementById('chat');
  var text = inp.value.trim();
  if (!text) return;
  inp.value = '';
  inp.style.height = 'auto';

  var ud = document.createElement('div');
  ud.className = 'mw user';
  ud.innerHTML = '<div class="mn">Никита</div><div class="msg user">' + escHtml(text) + '</div>';
  chat.appendChild(ud);
  scrollBottom();
  addTyping();

  fetch('/chat', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({message: text, voice: voice})
  })
  .then(function(r) {
    if (r.status === 401) window.location.href = '/login';
    return r.json();
  })
  .then(function(d) {
    removeTyping();
    var nd = document.createElement('div');
    nd.className = 'mw nexus';
    var html = '<div class="mn nx">NEXUS</div><div class="msg nexus">' + escHtml(d.reply) + '</div>';
    if (d.audio) {
      var idx = audioIdx++;
      audioStore[idx] = d.audio;
      if (voice) {
        curAudio = new Audio('data:audio/mp3;base64,' + d.audio);
        curAudio.play().catch(function(){});
      } else {
        html += '<button class="play-btn" onclick="playAudio(' + idx + ')">🔊 Слушать</button>';
      }
    }
    nd.innerHTML = html;
    chat.appendChild(nd);
    scrollBottom();
  })
  .catch(function() {
    removeTyping();
    var ed = document.createElement('div');
    ed.className = 'mw nexus';
    ed.innerHTML = '<div class="msg nexus" style="color:#ff4444">Ошибка соединения</div>';
    chat.appendChild(ed);
    scrollBottom();
  });
}
</script>
</body>
</html>"""


def is_logged_in():
    return session.get("logged_in") is True


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == require_web_password():
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Неверный пароль."
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    if not is_logged_in():
        return redirect(url_for("login"))
    return render_template_string(HTML)


@app.route("/chat", methods=["POST"])
def chat():
    if not is_logged_in():
        return jsonify({"reply": "Нужен вход в систему."}), 401

    data = request.get_json(silent=True) or {}
    msg = data.get("message", "").strip()
    voice = bool(data.get("voice", False))
    if not msg:
        return jsonify({"reply": "Напишите сообщение."})

    history.append({"role": "user", "content": msg})

    try:
        answer = ask_ai([{"role": "system", "content": SYSTEM}, *history[-20:]])
    except Exception as exc:
        return jsonify({"reply": "Ошибка AI: " + str(exc)})

    history.append({"role": "assistant", "content": answer})
    save_json(MEMORY_FILE, history)

    audio_b64 = make_audio(answer) if voice or get_env("ENABLE_TTS_BUTTONS", "1") == "1" else None
    return jsonify({"reply": answer, "audio": audio_b64})


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
    print("NEXUS запущен: http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
