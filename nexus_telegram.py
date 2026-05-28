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

BASE_DIR = Path(__file__).resolve().parent


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
        "📋 Команды:\n"
        "/tasks — список задач\n"
        "/addtask [текст] — добавить задачу\n"
        "/status — статус системы\n"
        "/analytics — выручка за 7 дней\n"
        "/crm — последние клиенты\n"
        "/remind [мин] [текст] — напомнить\n\n"
        "💬 Или просто пиши — отвечу текстом\n"
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
    data = read_json(ANALYTICS_FILE, {"obshchepit": [], "akva": [], "prodvizhenie": []})
    cutoff = datetime.now() - timedelta(days=7)

    names = {"obshchepit": "🍽️ Общепит", "akva": "🐟 Аква", "prodvizhenie": "📈 Продвижение"}
    total_rev = 0
    total_exp = 0
    lines = ["📊 *Аналитика за 7 дней*\n"]

    for biz, records in data.items():
        rev = 0
        exp = 0
        for r in records:
            try:
                if datetime.fromisoformat(r["date"]) >= cutoff:
                    rev += float(r.get("revenue", 0))
                    exp += float(r.get("expenses", 0))
            except Exception:
                pass
        if rev > 0 or exp > 0:
            profit = rev - exp
            lines.append(f"{names.get(biz, biz)}:")
            lines.append(f"  Выручка: {rev:,.0f} ₴")
            lines.append(f"  Прибыль: {profit:,.0f} ₴")
        total_rev += rev
        total_exp += exp

    lines.append(f"\n💰 *Итого:*")
    lines.append(f"  Выручка: {total_rev:,.0f} ₴")
    lines.append(f"  Прибыль: {total_rev - total_exp:,.0f} ₴")

    if total_rev == 0:
        lines.append("\n_(Данных пока нет — добавь записи в Аналитику)_")

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


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    args = context.args
    if len(args) < 2:
        return await update.message.reply_text("Формат: /remind 30 Позвонить клиенту")
    try:
        minutes = int(args[0])
    except ValueError:
        return await update.message.reply_text("Первый аргумент — количество минут (число)")
    text = " ".join(args[1:])
    remind_time = datetime.now() + timedelta(minutes=minutes)
    reminders = read_json(REMINDERS_FILE, [])
    reminders.append({"text": text, "time": remind_time.isoformat(), "sent": False})
    write_json(REMINDERS_FILE, reminders)
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

async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Запустить NEXUS"),
        BotCommand("tasks", "Список задач"),
        BotCommand("addtask", "Добавить задачу"),
        BotCommand("status", "Статус системы"),
        BotCommand("analytics", "Аналитика за 7 дней"),
        BotCommand("crm", "Последние клиенты"),
        BotCommand("remind", "Установить напоминание"),
    ])


def main():
    token = get_telegram_token()
    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("addtask", cmd_addtask))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("analytics", cmd_analytics))
    app.add_handler(CommandHandler("crm", cmd_crm))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("⚡ NEXUS Telegram запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
