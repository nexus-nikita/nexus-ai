import os
import json
import base64
import urllib.request
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify, Response, stream_with_context, session, redirect, render_template_string
from openai import OpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "nexus2026")
SECRET_KEY = os.getenv("SECRET_KEY", "nexus-secret-2026-xk9")

client = OpenAI(api_key=OPENAI_API_KEY)
app = Flask(__name__)
app.secret_key = SECRET_KEY

HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_history.json")
STATS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nexus_state.json")

stats = {"messages_today": 0, "voice_used": 0, "total_messages": 0, "last_active": ""}
history = []

def load_data():
    global stats, history
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            for k in stats:
                if k in saved:
                    stats[k] = saved[k]
            if saved.get("last_date") != datetime.now().strftime("%Y-%m-%d"):
                stats["messages_today"] = 0
        except Exception:
            pass
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            pass

def save_data():
    try:
        state = dict(stats)
        state["last_date"] = datetime.now().strftime("%Y-%m-%d")
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history[-100:], f, ensure_ascii=False)
    except Exception:
        pass

load_data()

SYSTEM = (
    "Ты NEXUS — центр управления всем. Мощный персональный AI помощник.\n"
    "Пользователь: Никита. Бизнес: общепит, аква бизнес, продвижение. Украина.\n"
    "Всегда отвечай на русском языке. Обращайся по имени Никита.\n"
    "Форматируй ответы с Markdown: **жирный**, *курсив*, списки, заголовки где уместно."
)

LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NEXUS — Вход</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#080c14;color:#e8f4f8;font-family:"Segoe UI",Arial,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center}
.box{background:#0d1520;border:1px solid rgba(0,212,255,0.2);border-radius:20px;padding:40px;width:320px;text-align:center}
.logo{font-size:28px;font-weight:900;color:#00d4ff;letter-spacing:8px;margin-bottom:8px}
.sub{color:#8aa8b8;font-size:13px;margin-bottom:32px}
.dot{width:8px;height:8px;background:#00ff88;border-radius:50%;display:inline-block;margin-right:6px;animation:p 2s infinite}
@keyframes p{0%,100%{opacity:1}50%{opacity:.4}}
input{width:100%;background:#111d2e;border:1px solid rgba(0,212,255,0.2);color:#e8f4f8;padding:12px 16px;border-radius:10px;font-size:15px;outline:none;margin-bottom:16px}
input:focus{border-color:#00d4ff}
button{width:100%;background:#00d4ff;color:#000;border:none;padding:12px;border-radius:10px;font-size:16px;font-weight:700;cursor:pointer}
.err{color:#ff4444;font-size:13px;margin-top:12px}
</style></head><body>
<div class="box">
  <div class="logo">&#9889; NEXUS</div>
  <div class="sub"><span class="dot"></span>Центр управления онлайн</div>
  <form method="post">
    <input type="password" name="password" placeholder="Введите пароль..." autofocus>
    <button type="submit">Войти</button>
    {% if error %}<div class="err">&#10060; Неверный пароль</div>{% endif %}
  </form>
</div>
</body></html>"""

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("auth"):
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated

@app.route("/login", methods=["GET", "POST"])
def login():
    error = False
    if request.method == "POST":
        if request.form.get("password", "") == WEB_PASSWORD:
            session["auth"] = True
            return redirect("/")
        error = True
    return render_template_string(LOGIN_HTML, error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/")
@login_required
def index():
    tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nexus_template.html")
    with open(tpl, encoding="utf-8") as f:
        return f.read()

@app.route("/stats")
@login_required
def get_stats():
    return jsonify(stats)

@app.route("/history")
@login_required
def get_history():
    msgs = [m for m in history if m.get("role") in ("user", "assistant")]
    return jsonify(msgs[-50:])

@app.route("/chat/stream")
@login_required
def chat_stream():
    msg = request.args.get("message", "").strip()
    voice = request.args.get("voice", "0") == "1"
    if not msg:
        return Response("data: [DONE]\n\n", mimetype="text/event-stream")
    stats["messages_today"] += 1
    stats["total_messages"] += 1
    stats["last_active"] = datetime.now().strftime("%H:%M:%S")
    if voice:
        stats["voice_used"] += 1
    history.append({"role": "user", "content": msg})

    def generate():
        collected = []
        try:
            stream = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "system", "content": SYSTEM}, *history[-20:]],
                stream=True,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    collected.append(delta)
                    yield "data: " + json.dumps(delta) + "\n\n"
        except Exception as exc:
            yield "data: " + json.dumps("Ошибка: " + str(exc)) + "\n\n"
        finally:
            full = "".join(collected)
            if full:
                history.append({"role": "assistant", "content": full})
            save_data()
            yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.route("/tts", methods=["POST"])
@login_required
def tts():
    text = request.json.get("text", "")
    if not text:
        return jsonify({"audio": None})
    try:
        r = client.audio.speech.create(model="tts-1", voice="onyx", input=text[:500])
        return jsonify({"audio": base64.b64encode(r.content).decode()})
    except Exception:
        return jsonify({"audio": None})

def get_odessa_weather():
    api_key = os.getenv("OPENWEATHER_API_KEY", "")
    if not api_key:
        return None, "Добавьте OPENWEATHER_API_KEY в .env"
    try:
        url = ("https://api.openweathermap.org/data/2.5/weather"
               "?q=Odessa,UA&appid=" + api_key + "&units=metric&lang=ru")
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
        icons = {"Clear":"☀️","Clouds":"☁️","Rain":"🌧️","Drizzle":"🌦️",
                 "Thunderstorm":"⛈️","Snow":"❄️","Mist":"🌫️","Fog":"🌫️"}
        main = data["weather"][0]["main"]
        return {"temp": round(data["main"]["temp"]),
                "feels_like": round(data["main"]["feels_like"]),
                "humidity": data["main"]["humidity"],
                "wind": round(data["wind"]["speed"]),
                "description": data["weather"][0]["description"].capitalize(),
                "icon": icons.get(main, "🌤️")}, None
    except Exception as e:
        return None, str(e)

@app.route("/weather")
@login_required
def weather():
    w, err = get_odessa_weather()
    if err:
        return jsonify({"error": err})
    return jsonify(w)

@app.route("/briefing")
@login_required
def briefing():
    w, _ = get_odessa_weather()
    weather_text = ""
    if w:
        weather_text = (f"Погода в Одессе: {w['temp']}°C, {w['description']}, "
                        f"влажность {w['humidity']}%, ветер {w['wind']} м/с.")
    today = datetime.now().strftime("%A, %d %B %Y")
    prompt = (f"Создай утренний брифинг для Никиты на {today}.\n{weather_text}\n"
              "Включи:\n1. Приветствие с датой и погодой\n2. Мотивационную мысль дня\n"
              "3. Топ-3 фокуса для бизнеса (общепит, аква, продвижение)\n4. Совет дня\n"
              "Форматируй красиво с Markdown.")
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}])
        return jsonify({"briefing": resp.choices[0].message.content})
    except Exception as e:
        return jsonify({"error": str(e)})

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5003))
    print(f"NEXUS Dashboard: http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", debug=False, port=port)


# ── NOTION SYNC ──────────────────────────────────────────────────────────────

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_TASKS_DB = os.getenv("NOTION_TASKS_DB", "eedc1201-290f-4bf6-bf21-62f0c7408c2b")

def notion_req(method, path, body=None):
    url = "https://api.notion.com/v1" + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": "Bearer " + NOTION_TOKEN,
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

@app.route("/notion/tasks", methods=["GET"])
@login_required
def notion_get_tasks():
    if not NOTION_TOKEN:
        return jsonify({"error": "NOTION_TOKEN не задан"})
    try:
        res = notion_req("POST", f"/databases/{NOTION_TASKS_DB}/query", {
            "sorts": [{"property": "Создано", "direction": "descending"}],
            "page_size": 50
        })
        tasks_out = []
        for page in res.get("results", []):
            props = page.get("properties", {})
            title_arr = props.get("Задача", {}).get("title", [])
            title = title_arr[0]["plain_text"] if title_arr else ""
            status = props.get("Статус", {}).get("select", {})
            priority = props.get("Приоритет", {}).get("select", {})
            tasks_out.append({
                "id": page["id"],
                "title": title,
                "status": status.get("name", "open") if status else "open",
                "priority": priority.get("name", "normal") if priority else "normal",
                "url": page.get("url", "")
            })
        return jsonify({"tasks": tasks_out})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/notion/tasks", methods=["POST"])
@login_required
def notion_create_task():
    if not NOTION_TOKEN:
        return jsonify({"error": "NOTION_TOKEN не задан"})
    data = request.json or {}
    title = data.get("title", "").strip()
    priority = data.get("priority", "normal")
    if not title:
        return jsonify({"error": "Нет заголовка"})
    try:
        res = notion_req("POST", "/pages", {
            "parent": {"database_id": NOTION_TASKS_DB},
            "properties": {
                "Задача": {"title": [{"text": {"content": title}}]},
                "Статус": {"select": {"name": "open"}},
                "Приоритет": {"select": {"name": priority}},
                "Владелец": {"rich_text": [{"text": {"content": "Никита"}}]},
                "Создано": {"date": {"start": datetime.now().strftime("%Y-%m-%d")}}
            }
        })
        return jsonify({"id": res["id"], "url": res.get("url", "")})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/notion/tasks/<task_id>", methods=["PATCH"])
@login_required
def notion_update_task(task_id):
    if not NOTION_TOKEN:
        return jsonify({"error": "NOTION_TOKEN не задан"})
    data = request.json or {}
    props = {}
    if "status" in data:
        props["Статус"] = {"select": {"name": data["status"]}}
    if not props:
        return jsonify({"error": "Нечего обновлять"})
    try:
        notion_req("PATCH", f"/pages/{task_id}", {"properties": props})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)})


# ── EMAIL DIGEST ──────────────────────────────────────────────────────────────

GMAIL = os.getenv("GMAIL", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

def send_email(to, subject, html_body):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL
    msg["To"] = to
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL, APP_PASSWORD)
        smtp.sendmail(GMAIL, to, msg.as_string())

def briefing_to_html(md_text, weather=None):
    """Convert markdown briefing to nice HTML email."""
    import re
    lines = md_text.split("\n")
    html_lines = []
    for line in lines:
        line = re.sub(r'\*\*(.+?)\*\*', r'<strong style="color:#00d4ff">\1</strong>', line)
        line = re.sub(r'\*(.+?)\*', r'<em>\1</em>', line)
        if line.startswith("# "):
            html_lines.append(f'<h1 style="color:#00d4ff;font-size:22px;margin:16px 0 8px">{line[2:]}</h1>')
        elif line.startswith("## "):
            html_lines.append(f'<h2 style="color:#00d4ff;font-size:18px;margin:14px 0 6px">{line[3:]}</h2>')
        elif line.startswith("### "):
            html_lines.append(f'<h3 style="color:#00d4ff;font-size:15px;margin:12px 0 4px">{line[4:]}</h3>')
        elif line.startswith("- ") or line.startswith("* "):
            html_lines.append(f'<li style="margin-bottom:4px;line-height:1.6">{line[2:]}</li>')
        elif line.strip() == "":
            html_lines.append("<br>")
        else:
            html_lines.append(f'<p style="margin-bottom:8px;line-height:1.7">{line}</p>')

    weather_block = ""
    if weather:
        weather_block = f"""
        <div style="background:#0a1f2e;border-radius:12px;padding:16px;margin-bottom:20px;display:flex;align-items:center;gap:16px">
          <span style="font-size:40px">{weather.get('icon','🌤️')}</span>
          <div>
            <div style="font-size:28px;font-weight:900;color:#00d4ff">{weather.get('temp','?')}°C</div>
            <div style="color:#8aa8b8;font-size:14px">{weather.get('description','')}</div>
            <div style="color:#8aa8b8;font-size:12px;margin-top:4px">
              Влажность {weather.get('humidity','?')}% · Ветер {weather.get('wind','?')} м/с
            </div>
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html><body style="background:#080c14;color:#e8f4f8;font-family:'Segoe UI',Arial,sans-serif;margin:0;padding:0">
<div style="max-width:600px;margin:0 auto;padding:24px">
  <div style="text-align:center;margin-bottom:24px">
    <div style="font-size:24px;font-weight:900;color:#00d4ff;letter-spacing:6px">⚡ NEXUS</div>
    <div style="color:#8aa8b8;font-size:13px;margin-top:4px">Утренний брифинг · {datetime.now().strftime('%d.%m.%Y')}</div>
  </div>
  {weather_block}
  <div style="background:#0d1520;border-radius:16px;padding:24px;border:1px solid rgba(0,212,255,0.15)">
    {''.join(html_lines)}
  </div>
  <div style="text-align:center;margin-top:20px;color:#8aa8b8;font-size:12px">
    NEXUS · Центр управления Никиты · <a href="https://nexus-ai-48sm.onrender.com" style="color:#00d4ff">Открыть</a>
  </div>
</div>
</body></html>"""

@app.route("/email/brief", methods=["POST"])
@login_required
def email_brief():
    if not GMAIL or not APP_PASSWORD:
        return jsonify({"error": "GMAIL / APP_PASSWORD не заданы в .env"})
    to = request.json.get("to", GMAIL) if request.json else GMAIL
    w, _ = get_odessa_weather()
    weather_text = ""
    if w:
        weather_text = f"Погода в Одессе: {w['temp']}°C, {w['description']}, влажность {w['humidity']}%, ветер {w['wind']} м/с."
    today = datetime.now().strftime("%A, %d %B %Y")
    prompt = (f"Создай утренний брифинг для Никиты на {today}.\n{weather_text}\n"
              "Включи:\n1. Приветствие с датой и погодой\n2. Мотивационную мысль дня\n"
              "3. Топ-3 фокуса для бизнеса (общепит, аква, продвижение)\n4. Совет дня\n"
              "Форматируй красиво с Markdown.")
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}])
        md = resp.choices[0].message.content
        html = briefing_to_html(md, w)
        subject = f"⚡ NEXUS Брифинг · {datetime.now().strftime('%d.%m.%Y')}"
        send_email(to, subject, html)
        return jsonify({"ok": True, "to": to})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/email/brief", methods=["GET"])
@login_required
def email_brief_get():
    """Quick send via GET for scheduled tasks."""
    return email_brief()


# ── CRM ───────────────────────────────────────────────────────────────────────

NOTION_CRM_DB = os.getenv("NOTION_CRM_DB", "876f85ed-a4b7-4c33-a766-dc8bdf537b24")
NOTION_ANALYTICS_DB = os.getenv("NOTION_ANALYTICS_DB", "faa5bff6-2fb9-4a30-941f-d95084c80786")

@app.route("/crm/clients", methods=["GET"])
@login_required
def crm_get_clients():
    if not NOTION_TOKEN:
        return jsonify({"error": "NOTION_TOKEN не задан"})
    try:
        res = notion_req("POST", f"/databases/{NOTION_CRM_DB}/query", {
            "sorts": [{"timestamp": "created_time", "direction": "descending"}],
            "page_size": 50
        })
        clients = []
        for page in res.get("results", []):
            props = page.get("properties", {})
            name_arr = props.get("Имя", {}).get("title", [])
            name = name_arr[0]["plain_text"] if name_arr else "—"
            status = (props.get("Статус", {}).get("select") or {}).get("name", "lead")
            business = (props.get("Бизнес", {}).get("select") or {}).get("name", "other")
            phone = props.get("Телефон", {}).get("phone_number") or ""
            email = props.get("Email", {}).get("email") or ""
            note = "".join(r["plain_text"] for r in props.get("Заметка", {}).get("rich_text", []))
            amount = props.get("Сумма", {}).get("number") or 0
            clients.append({
                "id": page["id"], "name": name, "status": status,
                "business": business, "phone": phone, "email": email,
                "note": note, "amount": amount, "url": page.get("url", "")
            })
        return jsonify({"clients": clients})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/crm/clients", methods=["POST"])
@login_required
def crm_add_client():
    if not NOTION_TOKEN:
        return jsonify({"error": "NOTION_TOKEN не задан"})
    d = request.json or {}
    name = d.get("name", "").strip()
    if not name:
        return jsonify({"error": "Нет имени"})
    props = {
        "Имя": {"title": [{"text": {"content": name}}]},
        "Статус": {"select": {"name": d.get("status", "lead")}},
        "Создан": {"date": {"start": datetime.now().strftime("%Y-%m-%d")}}
    }
    if d.get("phone"):
        props["Телефон"] = {"phone_number": d["phone"]}
    if d.get("email"):
        props["Email"] = {"email": d["email"]}
    if d.get("business"):
        props["Бизнес"] = {"select": {"name": d["business"]}}
    if d.get("note"):
        props["Заметка"] = {"rich_text": [{"text": {"content": d["note"]}}]}
    if d.get("amount"):
        props["Сумма"] = {"number": float(d["amount"])}
    try:
        res = notion_req("POST", "/pages", {"parent": {"database_id": NOTION_CRM_DB}, "properties": props})
        return jsonify({"id": res["id"], "url": res.get("url", "")})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/crm/clients/<client_id>", methods=["PATCH"])
@login_required
def crm_update_client(client_id):
    if not NOTION_TOKEN:
        return jsonify({"error": "NOTION_TOKEN не задан"})
    d = request.json or {}
    props = {}
    if "status" in d:
        props["Статус"] = {"select": {"name": d["status"]}}
    if "note" in d:
        props["Заметка"] = {"rich_text": [{"text": {"content": d["note"]}}]}
    if "amount" in d:
        props["Сумма"] = {"number": float(d["amount"])}
    if not props:
        return jsonify({"error": "Нечего обновлять"})
    try:
        notion_req("PATCH", f"/pages/{client_id}", {"properties": props})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)})


# ── ANALYTICS ─────────────────────────────────────────────────────────────────

@app.route("/analytics", methods=["GET"])
@login_required
def get_analytics():
    if not NOTION_TOKEN:
        return jsonify({"error": "NOTION_TOKEN не задан"})
    try:
        res = notion_req("POST", f"/databases/{NOTION_ANALYTICS_DB}/query", {
            "sorts": [{"property": "Период", "direction": "descending"}],
            "page_size": 30
        })
        rows = []
        totals = {"obshchepit": 0, "akva": 0, "prodvizhenie": 0}
        for page in res.get("results", []):
            props = page.get("properties", {})
            date_arr = props.get("Дата", {}).get("title", [])
            label = date_arr[0]["plain_text"] if date_arr else "—"
            business = (props.get("Бизнес", {}).get("select") or {}).get("name", "")
            revenue = props.get("Выручка", {}).get("number") or 0
            expenses = props.get("Расходы", {}).get("number") or 0
            profit = props.get("Прибыль", {}).get("number") or (revenue - expenses)
            clients = props.get("Клиентов", {}).get("number") or 0
            comment = "".join(r["plain_text"] for r in props.get("Комментарий", {}).get("rich_text", []))
            period_start = (props.get("Период", {}).get("date") or {}).get("start", "")
            rows.append({
                "id": page["id"], "label": label, "business": business,
                "revenue": revenue, "expenses": expenses, "profit": profit,
                "clients": clients, "comment": comment, "period": period_start
            })
            if business in totals:
                totals[business] += revenue
        return jsonify({"rows": rows, "totals": totals})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/analytics", methods=["POST"])
@login_required
def add_analytics():
    if not NOTION_TOKEN:
        return jsonify({"error": "NOTION_TOKEN не задан"})
    d = request.json or {}
    label = d.get("label", datetime.now().strftime("%B %Y"))
    business = d.get("business", "obshchepit")
    revenue = float(d.get("revenue", 0))
    expenses = float(d.get("expenses", 0))
    profit = revenue - expenses
    clients = int(d.get("clients", 0))
    props = {
        "Дата": {"title": [{"text": {"content": label}}]},
        "Бизнес": {"select": {"name": business}},
        "Выручка": {"number": revenue},
        "Расходы": {"number": expenses},
        "Прибыль": {"number": profit},
        "Клиентов": {"number": clients},
    }
    if d.get("comment"):
        props["Комментарий"] = {"rich_text": [{"text": {"content": d["comment"]}}]}
    if d.get("period"):
        props["Период"] = {"date": {"start": d["period"]}}
    try:
        res = notion_req("POST", "/pages", {"parent": {"database_id": NOTION_ANALYTICS_DB}, "properties": props})
        return jsonify({"id": res["id"]})
    except Exception as e:
        return jsonify({"error": str(e)})
