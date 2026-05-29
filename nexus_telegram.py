"""
NEXUS Telegram Bot
==================
Запуск: python nexus_telegram.py
Настройка: добавь в .env:
  TELEGRAM_TOKEN=...
  OPENAI_API_KEY=...
  TELEGRAM_ALLOWED_USER_IDS=928415420
"""

import base64
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from openai import OpenAI
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ── Конфиг ────────────────────────────────────────────────────────────────────

# Data dir: NEXUS_DATA_DIR > /app (Render disk) > script directory
import os as _os
_code_dir   = Path(__file__).resolve().parent
_render_disk = Path("/app")
_env_data    = _os.environ.get("NEXUS_DATA_DIR", "").strip()
if _env_data:
    BASE_DIR = Path(_env_data)
elif _render_disk.is_dir() and _code_dir != _render_disk:
    BASE_DIR = _render_disk
else:
    BASE_DIR = _code_dir


def load_env():
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()


def get_env(name, default=""):
    return os.getenv(name, default)


def get_telegram_token():
    token = get_env("TELEGRAM_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Не задан TELEGRAM_TOKEN в .env")
    return token


def get_openai_client():
    key = get_env("OPENAI_API_KEY", "").strip()
    if not key or key.startswith("your_"):
        raise RuntimeError("Не задан OPENAI_API_KEY в .env")
    return OpenAI(api_key=key)


def get_allowed_ids():
    raw = get_env("TELEGRAM_ALLOWED_USER_IDS", "")
    result = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        try:
            result.add(int(part))
        except ValueError:
            pass
    return result


# ── Файлы данных ──────────────────────────────────────────────────────────────

REMINDERS_FILE = BASE_DIR / "reminders.json"
MEMORY_FILE = BASE_DIR / "nexus_memory.json"
TASKS_FILE = BASE_DIR / "tasks.json"
CRM_FILE = BASE_DIR / "crm_data.json"
ANALYTICS_FILE = BASE_DIR / "analytics_data.json"


def read_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── История чата ──────────────────────────────────────────────────────────────

history = read_json(MEMORY_FILE, [])

SYSTEM = """Ты NEXUS — персональный AI-центр управления бизнесом.
Хозяин: Никита. Бизнес: общепит, аква бизнес, продвижение. Украина.
Отвечай на русском языке, кратко и по делу — это Telegram.
Обращайся к нему по имени."""


# ── Авторизация ───────────────────────────────────────────────────────────────

def is_allowed(update: Update) -> bool:
    allowed = get_allowed_ids()
    if not allowed:
        return True  # если список пуст — доступ открыт всем
    uid = update.effective_user.id if update.effective_user else None
    return uid in allowed


async def deny(update: Update):
    uid = update.effective_user.id if update.effective_user else "?"
    await update.message.reply_text(f"🔒 Доступ закрыт. Ваш ID: {uid}")


# ── Команды ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    await update.message.reply_text(
        "⚡ *NEXUS активен!*\n\n"
        "📋 *Задачи:*\n"
        "/tasks — список задач\n"
        "/addtask [текст] — добавить\n"
        "/done [часть названия] — отметить выполненной\n\n"
        "📊 *Бизнес:*\n"
        "/status — статус системы\n"
        "/analytics — выручка за 7 дней\n"
        "/crm — последние клиенты\n"
        "/brief — утренний брифинг\n"
        "/weather [город] — погода\n\n"
        "🔔 *Напоминания:*\n"
        "/remind [мин] [текст] — напомнить через N минут\n\n"
        "💬 Или просто пиши — отвечу\n"
        "🎤 Голосовое — распознаю и отвечу\n"
        "📸 Фото — проанализирую",
        parse_mode="Markdown"
    )


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    data = read_json(TASKS_FILE, {"tasks": []})
    tasks = data.get("tasks", [])
    active = [t for t in tasks if not t.get("done")]
    done = [t for t in tasks if t.get("done")]

    if not tasks:
        return await update.message.reply_text("📋 Задач пока нет.")

    lines = ["📋 *Задачи NEXUS*\n"]
    if active:
        lines.append("*Активные:*")
        for t in active[:10]:
            lines.append(f"• {t['text']}")
    if done:
        lines.append(f"\n*Выполнено:* {len(done)} шт.")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_addtask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    text = " ".join(context.args).strip()
    if not text:
        return await update.message.reply_text("Укажи текст задачи: /addtask Позвонить клиенту")
    data = read_json(TASKS_FILE, {"tasks": [], "next_id": 1})
    task = {
        "id": data.get("next_id", len(data["tasks"]) + 1),
        "text": text,
        "done": False,
        "created": datetime.now().strftime("%d.%m.%Y %H:%M"),
    }
    data["tasks"].append(task)
    data["next_id"] = task["id"] + 1
    write_json(TASKS_FILE, data)
    await update.message.reply_text(f"✅ Задача добавлена:\n*{text}*", parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    tasks = read_json(TASKS_FILE, {"tasks": []}).get("tasks", [])
    crm = read_json(CRM_FILE, {"clients": []}).get("clients", [])
    history_len = len([m for m in read_json(MEMORY_FILE, []) if m.get("role") == "user"])
    active_tasks = len([t for t in tasks if not t.get("done")])

    lines = [
        "⚡ *NEXUS — Статус системы*\n",
        f"💬 Сообщений в памяти: {history_len}",
        f"📋 Активных задач: {active_tasks} / {len(tasks)}",
        f"👥 Клиентов в CRM: {len(crm)}",
        f"🕐 Время: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        "\n✅ Система работает нормально",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    raw = read_json(ANALYTICS_FILE, {})
    # Support both new {"records":[...]} and old {business:[...]} formats
    if isinstance(raw, dict) and "records" in raw:
        all_records = raw["records"]
    elif isinstance(raw, dict):
        all_records = []
        for biz, entries in raw.items():
            if isinstance(entries, list):
                for e in entries:
                    e = dict(e); e.setdefault("business", biz); all_records.append(e)
    else:
        all_records = []

    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    recent = [r for r in all_records if r.get("date", "") >= cutoff]

    names = {"obshchepit": "🍽️ Общепит", "akva": "🐟 Аква", "prodvizhenie": "📈 Продвижение"}
    by_biz = {}
    for r in recent:
        biz = r.get("business", "other")
        if biz not in by_biz: by_biz[biz] = {"rev": 0, "exp": 0}
        by_biz[biz]["rev"] += float(r.get("revenue", 0))
        by_biz[biz]["exp"] += float(r.get("expenses", 0))

    total_rev = sum(v["rev"] for v in by_biz.values())
    total_exp = sum(v["exp"] for v in by_biz.values())
    lines = ["📊 *Аналитика за 7 дней*\n"]
    for biz, vals in by_biz.items():
        profit = vals["rev"] - vals["exp"]
        lines.append(f"{names.get(biz, biz)}:")
        lines.append(f"  Выручка: {vals['rev']:,.0f} ₴  |  Прибыль: {profit:,.0f} ₴")
    lines.append(f"\n💰 *Итого:* {total_rev:,.0f} ₴ / прибыль {total_rev - total_exp:,.0f} ₴")
    if total_rev == 0:
        lines.append("\n_(Данных нет — добавь записи в Аналитику)_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_crm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    data = read_json(CRM_FILE, {"clients": []})
    clients = data.get("clients", [])

    if not clients:
        return await update.message.reply_text("👥 CRM пуст. Добавь клиентов в веб-панели.")

    status_emoji = {"active": "🟢", "potential": "🟡", "inactive": "🔴"}
    lines = [f"👥 *CRM — {len(clients)} клиентов*\n"]

    for c in clients[-8:]:  # последние 8
        emoji = status_emoji.get(c.get("status", ""), "⚪")
        line = f"{emoji} *{c.get('name', '?')}*"
        if c.get("phone"):
            line += f" · {c['phone']}"
        if c.get("total"):
            line += f" · {int(c['total']):,} ₴"
        lines.append(line)

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    text = " ".join(context.args).strip().lower()
    # Try to read tasks from nexus_state.json (new format) or tasks.json (old)
    state_file = BASE_DIR / "nexus_state.json"
    if state_file.exists():
        state = read_json(state_file, {})
        tasks = state.get("tasks", [])
        matched = [t for t in tasks if t.get("status") != "done" and text in t.get("title","").lower()]
        if matched:
            matched[0]["status"] = "done"
            state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            await update.message.reply_text(f"✅ Задача выполнена:\n*{matched[0]['title']}*", parse_mode="Markdown")
            return
    await update.message.reply_text("Задача не найдена. Используй /tasks чтобы посмотреть список.")


async def cmd_weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    import urllib.request, urllib.parse
    city = " ".join(context.args).strip() or "Kyiv"
    api_key = get_env("OPENWEATHER_API_KEY", "").strip()
    if not api_key:
        return await update.message.reply_text("⚠️ OPENWEATHER_API_KEY не настроен.")
    params = urllib.parse.urlencode({"q": city, "appid": api_key, "units": "metric", "lang": "ru"})
    try:
        with urllib.request.urlopen("https://api.openweathermap.org/data/2.5/weather?" + params, timeout=10) as r:
            d = json.loads(r.read().decode())
        temp = round(d["main"]["temp"]); feels = round(d["main"]["feels_like"])
        desc = (d.get("weather") or [{}])[0].get("description", "")
        wind = d.get("wind", {}).get("speed", 0)
        await update.message.reply_text(
            f"🌤 *{d.get('name', city)}*\n{temp}°C, ощущается {feels}°C\n{desc}, ветер {wind} м/с",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка погоды: {e}")


async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    await update.message.reply_text("☀️ Собираю брифинг...")
    state_file = BASE_DIR / "nexus_state.json"
    tasks = read_json(state_file, {}).get("tasks", [])
    open_tasks = [t for t in tasks if t.get("status") != "done"][:5]
    tasks_str = "\n".join(f"- {t['title']}" for t in open_tasks) or "нет задач"
    prompt = (
        f"Короткий утренний брифинг для Никиты. Telegram формат.\n"
        f"Открытых задач: {len(open_tasks)}\n{tasks_str}\n"
        "Формат: 1) фокус дня, 2) топ-3 действия. Кратко!"
    )
    try:
        ai = get_openai_client()
        resp = ai.chat.completions.create(
            model=get_env("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
        )
        await update.message.reply_text(f"☀️ *Утренний брифинг*\n\n{resp.choices[0].message.content}", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Периодически проверяет напоминания и отправляет уведомления."""
    allowed = get_allowed_ids()
    if not allowed:
        return
    db_file = BASE_DIR / "reminders.json"
    raw = read_json(db_file, {})
    reminders = raw.get("reminders", raw) if isinstance(raw, dict) else raw
    if not isinstance(reminders, list):
        return
    now = datetime.now()
    changed = False
    for rem in reminders:
        if rem.get("sent"):
            continue
        try:
            t = datetime.fromisoformat(rem.get("time", ""))
        except Exception:
            continue
        if t <= now:
            for uid in allowed:
                try:
                    await context.bot.send_message(chat_id=uid, text=f"🔔 *Напоминание:*\n{rem['text']}", parse_mode="Markdown")
                except Exception:
                    pass
            rem["sent"] = True
            changed = True
    if changed:
        if isinstance(raw, dict):
            raw["reminders"] = reminders
            write_json(db_file, raw)
        else:
            write_json(db_file, reminders)


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    args = context.args
    if len(args) < 2:
        return await update.message.reply_text("Формат: /remind 30 Позвонить клиенту\n(первое число — минут)")
    try:
        minutes = int(args[0])
    except ValueError:
        return await update.message.reply_text("Первый аргумент — количество минут (число)")
    text = " ".join(args[1:])
    remind_time = datetime.now() + timedelta(minutes=minutes)
    db = read_json(REMINDERS_FILE, {"reminders": []})
    if isinstance(db, list): db = {"reminders": db}
    db.setdefault("reminders", []).append({
        "id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
        "text": text, "time": remind_time.isoformat(),
        "repeat": "once", "sent": False,
    })
    write_json(REMINDERS_FILE, db)
    await update.message.reply_text(
        f"🔔 Напоминание установлено!\n*{text}*\nВремя: {remind_time.strftime('%H:%M')}",
        parse_mode="Markdown"
    )


# ── Голосовые сообщения ───────────────────────────────────────────────────────

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    await update.message.reply_text("🎤 Распознаю...")
    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    file_bytes = await file.download_as_bytearray()
    voice_path = BASE_DIR / "voice_temp.ogg"
    voice_path.write_bytes(file_bytes)
    try:
        ai = get_openai_client()
        with open(voice_path, "rb") as audio:
            transcript = ai.audio.transcriptions.create(model="whisper-1", file=audio, language="ru")
        text = transcript.text
        await update.message.reply_text(f"💬 *Ты сказал:* {text}", parse_mode="Markdown")
        history.append({"role": "user", "content": text})
        response = ai.chat.completions.create(
            model=get_env("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "system", "content": SYSTEM}, *history[-20:]],
        )
        answer = response.choices[0].message.content
        history.append({"role": "assistant", "content": answer})
        write_json(MEMORY_FILE, history[-80:])
        audio_resp = ai.audio.speech.create(model="tts-1", voice="onyx", input=answer[:4000])
        resp_path = BASE_DIR / "response.ogg"
        resp_path.write_bytes(audio_resp.content)
        await update.message.reply_text(answer)
        with open(resp_path, "rb") as af:
            await update.message.reply_voice(voice=af)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
    finally:
        for p in [BASE_DIR / "voice_temp.ogg", BASE_DIR / "response.ogg"]:
            if p.exists():
                p.unlink()


# ── Фото ──────────────────────────────────────────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    await update.message.reply_text("📸 Анализирую...")
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()
    image_b64 = base64.b64encode(file_bytes).decode()
    caption = update.message.caption or "Что на этом фото? Дай детальный анализ на русском."
    try:
        ai = get_openai_client()
        response = ai.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Ты NEXUS — помощник Никиты. {caption}"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                ],
            }],
        )
        answer = response.choices[0].message.content
    except Exception as e:
        answer = f"Ошибка анализа: {e}"
    await update.message.reply_text(f"🔍 *Анализ NEXUS:*\n\n{answer}", parse_mode="Markdown")


# ── Текстовые сообщения ───────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    msg = update.message.text.strip()

    # Быстрое распознавание напоминания
    if re.search(r'напомни', msg, re.IGNORECASE):
        minutes = 60
        nums = re.findall(r'\d+', msg)
        if 'минут' in msg.lower() and nums:
            minutes = int(nums[0])
        elif 'час' in msg.lower() and nums:
            minutes = int(nums[0]) * 60
        remind_time = datetime.now() + timedelta(minutes=minutes)
        reminders = read_json(REMINDERS_FILE, [])
        reminders.append({"text": msg, "time": remind_time.isoformat(), "sent": False})
        write_json(REMINDERS_FILE, reminders)
        await update.message.reply_text(f"🔔 Напоминание на {remind_time.strftime('%H:%M')}!")
        return

    history.append({"role": "user", "content": msg})
    try:
        ai = get_openai_client()
        response = ai.chat.completions.create(
            model=get_env("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "system", "content": SYSTEM}, *history[-20:]],
        )
        answer = response.choices[0].message.content
    except Exception as exc:
        answer = f"❌ Ошибка AI: {exc}"

    history.append({"role": "assistant", "content": answer})
    write_json(MEMORY_FILE, history[-80:])
    await update.message.reply_text(answer)


# ── Запуск ────────────────────────────────────────────────────────────────────

async def cmd_mono(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Monobank balance via /mono."""
    if not is_allowed(update): return await deny(update)
    import urllib.request
    token = get_env("MONOBANK_TOKEN", "").strip()
    if not token:
        await update.message.reply_text("💳 MONOBANK_TOKEN не задан в .env")
        return
    try:
        req = urllib.request.Request(
            "https://api.monobank.ua/personal/client-info",
            headers={"X-Token": token}
        )
        import json as _json
        with urllib.request.urlopen(req, timeout=10) as r:
            info = _json.loads(r.read().decode("utf-8"))
        accounts = info.get("accounts", [])
        uah = next((a for a in accounts if a.get("currencyCode") == 980), accounts[0] if accounts else {})
        balance = uah.get("balance", 0) / 100
        credit = uah.get("creditLimit", 0) / 100
        lines = [f"💳 *Monobank*", f"Баланс: `{balance:,.2f} грн`"]
        if credit:
            lines.append(f"Кредитный лимит: `{credit:,.2f} грн`")
        lines.append(f"Карт: {len(accounts)}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка Monobank: {e}")


async def weekly_digest(context):
    """Send weekly AI-generated business summary every Monday at 09:00."""
    import urllib.request, urllib.parse
    chat_id = get_env("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        # fallback: try TELEGRAM_ALLOWED_USER_IDS
        allowed = get_env("TELEGRAM_ALLOWED_USER_IDS", "").strip()
        chat_id = allowed.split(",")[0].strip() if allowed else ""
    if not chat_id:
        return

    tasks = read_json(BASE_DIR / "nexus_state.json", {}).get("tasks", [])
    open_t = [t for t in tasks if t.get("status") != "done"]
    analytics = read_json(ANALYTICS_FILE, {})
    records = analytics.get("records", []) if isinstance(analytics.get("records"), list) else []
    week_ago = (datetime.utcnow() - timedelta(days=7)).date().isoformat()
    week_records = [r for r in records if r.get("date", "") >= week_ago]
    revenue = sum(r.get("revenue", r.get("amount", 0)) for r in week_records)

    crm = read_json(CRM_FILE, {})
    clients = crm.get("clients", []) if isinstance(crm, dict) else []

    prompt = (
        f"Еженедельный дайджест NEXUS. Никита, вот сводка за неделю:\n"
        f"- Открытых задач: {len(open_t)}\n"
        f"- Выручка (7 дней): {revenue:.0f} UAH\n"
        f"- Записей аналитики за неделю: {len(week_records)}\n"
        f"- Клиентов в CRM: {len(clients)}\n\n"
        "Напиши краткий мотивирующий дайджест с ключевыми метриками и советом на неделю. "
        "Telegram-формат, до 300 символов."
    )
    try:
        ai = get_openai_client()
        resp = ai.chat.completions.create(
            model=get_env("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            max_tokens=400,
        )
        text = resp.choices[0].message.content
    except Exception:
        text = (
            f"📊 Итоги недели:\n"
            f"• Задач открыто: {len(open_t)}\n"
            f"• Выручка: {revenue:.0f} UAH\n"
            f"• Клиентов: {len(clients)}"
        )

    try:
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=f"📅 *Еженедельный дайджест NEXUS*\n\n{text}",
            parse_mode="Markdown",
        )
    except Exception:
        pass


async def cmd_mono(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Monobank balance via /mono."""
    if not is_allowed(update): return await deny(update)
    import urllib.request as _req
    token = get_env("MONOBANK_TOKEN", "").strip()
    if not token:
        await update.message.reply_text("💳 MONOBANK_TOKEN не задан в .env")
        return
    try:
        req = _req.Request(
            "https://api.monobank.ua/personal/client-info",
            headers={"X-Token": token}
        )
        with _req.urlopen(req, timeout=10) as r:
            info = json.loads(r.read().decode("utf-8"))
        accounts = info.get("accounts", [])
        uah = next((a for a in accounts if a.get("currencyCode") == 980), accounts[0] if accounts else {})
        balance = uah.get("balance", 0) / 100
        credit = uah.get("creditLimit", 0) / 100
        lines = ["💳 *Monobank*", f"Баланс: `{balance:,.2f} грн`"]
        if credit:
            lines.append(f"Кредитный лимит: `{credit:,.2f} грн`")
        lines.append(f"Карт: {len(accounts)}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка Monobank: {e}")


async def cmd_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/calendar — show today's and upcoming events."""
    if not is_allowed(update): return await deny(update)
    from pathlib import Path as _P
    cal_file = BASE_DIR / "calendar_data.json"
    events = read_json(cal_file, [])
    today = datetime.utcnow().date().isoformat()
    today_evs = [e for e in events if e.get("date","") == today]
    upcoming  = sorted([e for e in events if e.get("date","") > today],
                       key=lambda e: e.get("date",""))[:5]
    lines = [f"📅 *Календар NEXUS*\n"]
    if today_evs:
        lines.append("*Сьогодні:*")
        for e in today_evs:
            t = f" {e['time']}" if e.get("time") else ""
            lines.append(f"• {e['title']}{t}" + (f"\n  _{e['desc']}_" if e.get("desc") else ""))
    else:
        lines.append("Сьогодні подій немає.")
    if upcoming:
        lines.append("\n*Найближчі:*")
        for e in upcoming:
            lines.append(f"• {e['date']} — {e['title']}" + (f" {e.get('time','')}" if e.get("time") else ""))
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_pipeline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pipeline — CRM pipeline summary by stage."""
    if not is_allowed(update): return await deny(update)
    clients = read_json(CRM_FILE, [])
    if isinstance(clients, dict):
        clients = clients.get("clients", [])
    stages = {"lead": "🔵 Ліди", "active": "🟡 Активні", "done": "🟢 Завершені", "lost": "🔴 Втрачені"}
    lines = ["🗂 *CRM Pipeline*\n"]
    for key, label in stages.items():
        grp = [c for c in clients if c.get("status","lead") == key]
        lines.append(f"{label}: *{len(grp)}*")
        for c in grp[:3]:
            lines.append(f"  • {c.get('name','?')} — {c.get('phone','')}")
        if len(grp) > 3:
            lines.append(f"  _…ще {len(grp)-3}_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start",     "Запустить NEXUS"),
        BotCommand("tasks",     "Список задач"),
        BotCommand("addtask",   "Добавить задачу"),
        BotCommand("done",      "Отметить задачу выполненной"),
        BotCommand("status",    "Статус системы"),
        BotCommand("analytics", "Аналитика за 7 дней"),
        BotCommand("crm",       "Последние клиенты"),
        BotCommand("pipeline",  "CRM по стадіях"),
        BotCommand("calendar",  "Події на сьогодні"),
        BotCommand("brief",     "Утренний брифинг"),
        BotCommand("weather",   "Погода"),
        BotCommand("remind",    "Установить напоминание"),
        BotCommand("mono",      "Баланс Monobank"),
    ])
    application.job_queue.run_repeating(check_reminders, interval=60, first=10)
    from datetime import time as _time
    application.job_queue.run_daily(
        weekly_digest,
        time=_time(hour=9, minute=0),
        days=(0,),  # 0 = Monday
    )


def main():
    token = get_telegram_token()
    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("tasks",     cmd_tasks))
    app.add_handler(CommandHandler("addtask",   cmd_addtask))
    app.add_handler(CommandHandler("done",      cmd_done))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("analytics", cmd_analytics))
    app.add_handler(CommandHandler("crm",       cmd_crm))
    app.add_handler(CommandHandler("brief",     cmd_brief))
    app.add_handler(CommandHandler("weather",   cmd_weather))
    app.add_handler(CommandHandler("remind",    cmd_remind))
    app.add_handler(CommandHandler("mono",      cmd_mono))
    app.add_handler(CommandHandler("calendar",  cmd_calendar))
    app.add_handler(CommandHandler("pipeline",  cmd_pipeline))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("⚡ NEXUS Telegram запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
