"""
NEXUS Telegram Bot v2
Команды:
  /start   — приветствие
  /brief   — утренний брифинг
  /weather — погода Одессы
  /tasks   — задачи из Notion
  /done N  — отметить задачу выполненной
  /add ... — добавить задачу в Notion
  /email   — отправить брифинг на почту
  /ask ... — спросить AI
"""
import os
import json
import urllib.request
import urllib.parse
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from openai import OpenAI

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENWEATHER_KEY = os.getenv("OPENWEATHER_API_KEY", "")
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_TASKS_DB = os.getenv("NOTION_TASKS_DB", "eedc1201-290f-4bf6-bf21-62f0c7408c2b")
GMAIL = os.getenv("GMAIL", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

client = OpenAI(api_key=OPENAI_API_KEY)
API = f"https://api.telegram.org/bot{TOKEN}"

SYSTEM = ("Ты NEXUS — персональный AI помощник Никиты. "
          "Бизнес: общепит, аква бизнес, продвижение. Украина. "
          "Отвечай кратко, по-русски. Используй эмодзи.")

_notion_cache = []

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def tg(method, **params):
    url = f"{API}/{method}"
    data = json.dumps(params).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def send(chat_id, text, parse_mode="Markdown"):
    if len(text) > 4096:
        text = text[:4090] + "..."
    try:
        tg("sendMessage", chat_id=chat_id, text=text, parse_mode=parse_mode)
    except Exception:
        try:
            tg("sendMessage", chat_id=chat_id, text=text)
        except Exception as e:
            print(f"Send error: {e}")

def notion_req(method, path, body=None):
    url = "https://api.notion.com/v1" + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method,
        headers={"Authorization": "Bearer " + NOTION_TOKEN,
                 "Notion-Version": "2022-06-28",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

# ── Features ──────────────────────────────────────────────────────────────────

def get_weather():
    if not OPENWEATHER_KEY:
        return "⚠️ OPENWEATHER_API_KEY не настроен"
    try:
        url = ("https://api.openweathermap.org/data/2.5/weather"
               "?q=Odessa,UA&appid=" + OPENWEATHER_KEY + "&units=metric&lang=ru")
        with urllib.request.urlopen(url, timeout=5) as r:
            d = json.loads(r.read())
        icons = {"Clear":"☀️","Clouds":"☁️","Rain":"🌧️","Drizzle":"🌦️","Thunderstorm":"⛈️","Snow":"❄️"}
        icon = icons.get(d["weather"][0]["main"], "🌤️")
        temp = round(d["main"]["temp"])
        feels = round(d["main"]["feels_like"])
        hum = d["main"]["humidity"]
        wind = round(d["wind"]["speed"])
        desc = d["weather"][0]["description"].capitalize()
        return (f"{icon} *Одесса, {datetime.now().strftime('%d.%m.%Y')}*\n"
                f"🌡 *{temp}°C* (ощущается {feels}°C)\n"
                f"💧 Влажность: {hum}%\n"
                f"💨 Ветер: {wind} м/с\n"
                f"📝 {desc}")
    except Exception as e:
        return f"❌ Ошибка погоды: {e}"

def get_briefing():
    weather = get_weather()
    today = datetime.now().strftime("%A, %d %B %Y")
    prompt = (f"Создай краткий утренний брифинг для Никиты на {today}.\n"
              f"Погода: {weather}\n"
              "Включи: приветствие, мотивацию, топ-3 задачи бизнеса. "
              "Кратко, по делу. Markdown для Telegram.")
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}])
        return resp.choices[0].message.content
    except Exception as e:
        return f"❌ Ошибка: {e}"

def get_notion_tasks():
    global _notion_cache
    if not NOTION_TOKEN:
        return None, "⚠️ NOTION_TOKEN не настроен"
    try:
        res = notion_req("POST", f"/databases/{NOTION_TASKS_DB}/query", {
            "filter": {"property": "Статус", "select": {"does_not_equal": "done"}},
            "sorts": [{"property": "Приоритет", "direction": "ascending"}],
            "page_size": 20
        })
        tasks = []
        for page in res.get("results", []):
            props = page.get("properties", {})
            title_arr = props.get("Задача", {}).get("title", [])
            title = title_arr[0]["plain_text"] if title_arr else "—"
            status = (props.get("Статус", {}).get("select") or {}).get("name", "open")
            priority = (props.get("Приоритет", {}).get("select") or {}).get("name", "normal")
            tasks.append({"id": page["id"], "title": title, "status": status, "priority": priority})
        _notion_cache = tasks
        return tasks, None
    except Exception as e:
        return None, str(e)

def format_tasks(tasks):
    if not tasks:
        return "✅ Открытых задач нет!"
    icons = {"high": "🔴", "normal": "⚪", "low": "🔵"}
    status_icons = {"open": "📋", "in_progress": "⚡"}
    lines = [f"*📋 Задачи NEXUS ({len(tasks)} открытых):*\n"]
    for i, t in enumerate(tasks):
        p = icons.get(t["priority"], "⚪")
        s = status_icons.get(t["status"], "📋")
        lines.append(f"{i+1}. {p} {s} {t['title']}")
    lines.append("\n`/done N` — выполнить задачу N")
    lines.append("`/add текст` — добавить задачу")
    return "\n".join(lines)

def mark_done(idx):
    global _notion_cache
    if not _notion_cache or idx < 0 or idx >= len(_notion_cache):
        return "❌ Задача не найдена. Сначала /tasks"
    task = _notion_cache[idx]
    try:
        notion_req("PATCH", f"/pages/{task['id']}", {"properties": {"Статус": {"select": {"name": "done"}}}})
        title = task["title"]
        _notion_cache.pop(idx)
        return f"✅ Выполнено: *{title}*"
    except Exception as e:
        return f"❌ Ошибка: {e}"

def add_notion_task(title):
    if not NOTION_TOKEN:
        return "⚠️ NOTION_TOKEN не настроен"
    try:
        notion_req("POST", "/pages", {
            "parent": {"database_id": NOTION_TASKS_DB},
            "properties": {
                "Задача": {"title": [{"text": {"content": title}}]},
                "Статус": {"select": {"name": "open"}},
                "Приоритет": {"select": {"name": "normal"}},
                "Владелец": {"rich_text": [{"text": {"content": "Никита"}}]},
                "Создано": {"date": {"start": datetime.now().strftime("%Y-%m-%d")}}
            }
        })
        return f"✅ Задача добавлена в Notion:\n*{title}*"
    except Exception as e:
        return f"❌ Ошибка: {e}"

def send_email_brief():
    if not GMAIL or not APP_PASSWORD:
        return "⚠️ GMAIL / APP_PASSWORD не настроены"
    try:
        brief = get_briefing()
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"⚡ NEXUS Брифинг · {datetime.now().strftime('%d.%m.%Y')}"
        msg["From"] = GMAIL
        msg["To"] = GMAIL
        html = f"<pre style='font-family:Arial;white-space:pre-wrap'>{brief}</pre>"
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL, APP_PASSWORD)
            smtp.sendmail(GMAIL, GMAIL, msg.as_string())
        return f"📧 Брифинг отправлен на *{GMAIL}*"
    except Exception as e:
        return f"❌ Ошибка email: {e}"

def ask_ai(question):
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}])
        return resp.choices[0].message.content
    except Exception as e:
        return f"❌ Ошибка: {e}"

# ── Handler ───────────────────────────────────────────────────────────────────

def handle(msg):
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()
    name = msg.get("from", {}).get("first_name", "Никита")

    if text.startswith("/start"):
        send(chat_id, f"⚡ *NEXUS активирован*\n\nПривет, {name}! 👋\n\n"
             "*/brief* — утренний брифинг\n"
             "*/weather* — погода Одессы\n"
             "*/tasks* — задачи из Notion\n"
             "*/done N* — выполнить задачу N\n"
             "*/add текст* — добавить задачу\n"
             "*/email* — брифинг на почту\n"
             "*/ask вопрос* — спросить AI\n\n"
             "Или просто напиши — отвечу! 🤖")

    elif text.startswith("/help"):
        send(chat_id, "⚡ *Команды NEXUS*\n\n"
             "*/brief* — утренний брифинг с погодой\n"
             "*/weather* — погода в Одессе\n"
             "*/tasks* — открытые задачи Notion\n"
             "*/done 1* — отметить задачу 1 выполненной\n"
             "*/add купить кофе* — добавить задачу\n"
             "*/email* — брифинг на почту\n"
             "*/ask вопрос* — задать вопрос AI")

    elif text.startswith("/brief"):
        send(chat_id, "⏳ Генерирую брифинг...")
        send(chat_id, get_briefing())

    elif text.startswith("/weather"):
        send(chat_id, get_weather())

    elif text.startswith("/tasks"):
        send(chat_id, "⏳ Загружаю задачи из Notion...")
        tasks, err = get_notion_tasks()
        if err:
            send(chat_id, f"❌ {err}")
        else:
            send(chat_id, format_tasks(tasks))

    elif text.startswith("/done"):
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            send(chat_id, "❓ Используй: */done 1* (номер из /tasks)")
        else:
            idx = int(parts[1]) - 1
            send(chat_id, mark_done(idx))

    elif text.startswith("/add "):
        title = text[5:].strip()
        if title:
            send(chat_id, "⏳ Добавляю...")
            send(chat_id, add_notion_task(title))
        else:
            send(chat_id, "❓ Напиши: */add название задачи*")

    elif text.startswith("/email"):
        send(chat_id, "⏳ Отправляю брифинг на почту...")
        send(chat_id, send_email_brief())

    elif text.startswith("/ask "):
        q = text[5:].strip()
        if q:
            send(chat_id, "⏳ Думаю...")
            send(chat_id, ask_ai(q))
        else:
            send(chat_id, "❓ Напиши: */ask твой вопрос*")

    elif text and not text.startswith("/"):
        send(chat_id, ask_ai(text))

# ── Main loop ─────────────────────────────────────────────────────────────────

def run():
    print("NEXUS Telegram Bot v2 запущен...")
    print(f"Token: {'OK' if TOKEN else 'MISSING'}")
    print(f"OpenAI: {'OK' if OPENAI_API_KEY else 'MISSING'}")
    print(f"Notion: {'OK' if NOTION_TOKEN else 'MISSING'}")
    offset = 0
    while True:
        try:
            result = tg("getUpdates", offset=offset, timeout=30)
            for upd in result.get("result", []):
                offset = upd["update_id"] + 1
                if "message" in upd:
                    try:
                        handle(upd["message"])
                    except Exception as e:
                        print(f"Handler error: {e}")
        except Exception as e:
            print(f"Poll error: {e}")
            import time; time.sleep(5)

if __name__ == "__main__":
    if not TOKEN:
        print("Ошибка: TELEGRAM_TOKEN не задан в .env")
    else:
        # Load .env if running locally
        env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())
        run()
