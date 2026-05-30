"""
NEXUS Scheduler — автоматические задачи
Запуск: python nexus_scheduler.py
Работает параллельно с nexus_dashboard.py

Расписание:
  08:00 — утренний брифинг на email + Telegram
  21:00 — вечерний отчёт в Telegram
"""
import os
import time
import json
import urllib.request
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from openai import OpenAI

# Load .env
env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENWEATHER_KEY = os.getenv("OPENWEATHER_API_KEY", "")
GMAIL = os.getenv("GMAIL", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_TASKS_DB = os.getenv("NOTION_TASKS_DB", "eedc1201-290f-4bf6-bf21-62f0c7408c2b")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # set after first /start

client = OpenAI(api_key=OPENAI_API_KEY)
TG_API = f"https://api.telegram.org/bot{TOKEN}"

SYSTEM = ("Ты NEXUS — персональный AI помощник Никиты. "
          "Бизнес: общепит, аква, продвижение. Украина. Отвечай кратко, по-русски.")


def tg_send(text, chat_id=None):
    cid = chat_id or CHAT_ID
    if not cid or not TOKEN:
        return
    if len(text) > 4096:
        text = text[:4090] + "..."
    try:
        url = f"{TG_API}/sendMessage"
        data = json.dumps({"chat_id": cid, "text": text, "parse_mode": "Markdown"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"TG error: {e}")


def get_weather():
    if not OPENWEATHER_KEY:
        return "Погода: ключ не настроен"
    try:
        url = ("https://api.openweathermap.org/data/2.5/weather"
               "?q=Odessa,UA&appid=" + OPENWEATHER_KEY + "&units=metric&lang=ru")
        with urllib.request.urlopen(url, timeout=5) as r:
            d = json.loads(r.read())
        icons = {"Clear":"☀️","Clouds":"☁️","Rain":"🌧️","Drizzle":"🌦️","Thunderstorm":"⛈️","Snow":"❄️"}
        icon = icons.get(d["weather"][0]["main"], "🌤️")
        temp = round(d["main"]["temp"])
        desc = d["weather"][0]["description"].capitalize()
        wind = round(d["wind"]["speed"])
        return f"{icon} {temp}°C, {desc}, ветер {wind} м/с"
    except Exception as e:
        return f"Погода: ошибка ({e})"


def get_open_tasks():
    if not NOTION_TOKEN:
        return []
    try:
        url = "https://api.notion.com/v1/databases/" + NOTION_TASKS_DB + "/query"
        body = json.dumps({
            "filter": {"property": "Статус", "select": {"does_not_equal": "done"}},
            "sorts": [{"property": "Приоритет", "direction": "ascending"}],
            "page_size": 10
        }).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": "Bearer " + NOTION_TOKEN,
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.loads(r.read())
        tasks = []
        for page in res.get("results", []):
            props = page.get("properties", {})
            title_arr = props.get("Задача", {}).get("title", [])
            title = title_arr[0]["plain_text"] if title_arr else "—"
            priority = (props.get("Приоритет", {}).get("select") or {}).get("name", "normal")
            tasks.append({"title": title, "priority": priority})
        return tasks
    except Exception:
        return []


def generate_briefing(weather, tasks):
    today = datetime.now().strftime("%A, %d %B %Y")
    tasks_text = "\n".join(f"- {t['title']}" for t in tasks[:5]) if tasks else "Нет открытых задач"
    prompt = (f"Создай краткий утренний брифинг для Никиты на {today}.\n"
              f"Погода Одесса: {weather}\n"
              f"Открытые задачи:\n{tasks_text}\n\n"
              "Включи: приветствие, мотивацию, топ-3 фокуса бизнеса (общепит, аква, продвижение), "
              "совет дня. Кратко, Markdown для Telegram.")
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}])
        return resp.choices[0].message.content
    except Exception as e:
        return f"Ошибка генерации брифинга: {e}"


def generate_evening_report(tasks):
    today = datetime.now().strftime("%d.%m.%Y")
    tasks_text = "\n".join(f"- {t['title']}" for t in tasks[:5]) if tasks else "Задач нет"
    prompt = (f"Создай краткий вечерний отчёт для Никиты за {today}.\n"
              f"Открытые задачи на завтра:\n{tasks_text}\n\n"
              "Включи: подведение итогов дня, что важно сделать завтра, мотивацию на ночь. "
              "Кратко, Markdown.")
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}])
        return resp.choices[0].message.content
    except Exception as e:
        return f"Ошибка: {e}"


def send_email(subject, text):
    if not GMAIL or not APP_PASSWORD:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = GMAIL
        msg["To"] = GMAIL
        html = f"""<div style="background:#080c14;color:#e8f4f8;font-family:Arial,sans-serif;padding:24px;max-width:600px;margin:0 auto">
        <div style="font-size:22px;font-weight:900;color:#00d4ff;letter-spacing:4px;margin-bottom:16px">⚡ NEXUS</div>
        <div style="background:#0d1520;border-radius:12px;padding:20px;white-space:pre-wrap;line-height:1.7">{text}</div>
        <div style="margin-top:16px;font-size:12px;color:#8aa8b8;text-align:center">
          <a href="https://nexus-ai-48sm.onrender.com" style="color:#00d4ff">Открыть NEXUS</a>
        </div></div>"""
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL, APP_PASSWORD)
            smtp.sendmail(GMAIL, GMAIL, msg.as_string())
        print(f"Email sent: {subject}")
    except Exception as e:
        print(f"Email error: {e}")


def morning_routine():
    print(f"[{datetime.now().strftime('%H:%M')}] 🌅 Morning routine...")
    weather = get_weather()
    tasks = get_open_tasks()
    briefing = generate_briefing(weather, tasks)
    # Telegram
    tg_send(f"🌅 *Доброе утро, Никита!*\n\n{briefing}")
    # Email
    today = datetime.now().strftime("%d.%m.%Y")
    send_email(f"⚡ NEXUS Брифинг · {today}", briefing)
    print("Morning routine done")


def evening_routine():
    print(f"[{datetime.now().strftime('%H:%M')}] 🌙 Evening routine...")
    tasks = get_open_tasks()
    report = generate_evening_report(tasks)
    tg_send(f"🌙 *Вечерний отчёт*\n\n{report}")
    print("Evening routine done")


def run():
    print("NEXUS Scheduler запущен")
    print(f"Gmail: {'OK' if GMAIL else 'не настроен'}")
    print(f"Telegram: {'OK' if TOKEN else 'не настроен'}")
    print(f"Notion: {'OK' if NOTION_TOKEN else 'не настроен'}")
    print("Расписание: 08:00 брифинг, 21:00 отчёт")

    ran_morning = False
    ran_evening = False

    while True:
        now = datetime.now()
        hour = now.hour
        minute = now.minute
        date_str = now.strftime("%Y-%m-%d")

        # Reset flags at midnight
        if hour == 0 and minute < 2:
            ran_morning = False
            ran_evening = False

        # Morning briefing at 08:00
        if hour == 8 and minute < 5 and not ran_morning:
            ran_morning = True
            try:
                morning_routine()
            except Exception as e:
                print(f"Morning error: {e}")

        # Evening report at 21:00
        if hour == 21 and minute < 5 and not ran_evening:
            ran_evening = True
            try:
                evening_routine()
            except Exception as e:
                print(f"Evening error: {e}")

        time.sleep(60)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        print("Тест утреннего брифинга...")
        morning_routine()
    else:
        run()
