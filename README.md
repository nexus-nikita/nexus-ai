# ⚡ NEXUS — AI Business OS

Персональный центр управления бизнесом на базе AI. Веб-панель + Telegram бот.

## Возможности

| Раздел | Что умеет |
|--------|-----------|
| 💬 Чат | AI-диалог, потоковый ответ, голос, файлы (PDF/DOCX/TXT), экспорт |
| ✅ Задачи | Добавление, приоритет, toggle выполнено/открыть, фильтры |
| 👥 CRM | База клиентов, телефоны, бизнес, история заметок |
| 📈 Аналитика | Выручка/расходы по бизнесам, график за 7 дней |
| ✉️ Email | Gmail входящие (IMAP), отправка (SMTP), AI-черновик |
| 🔔 Напоминания | Добавление с датой/временем, повтор, удаление |
| 📦 Нова Пошта | Трекинг ТТН по API |
| 🌅 Брифинг | Утренний AI-брифинг с погодой и задачами |
| 🧩 Агенты | Шаблоны AI-агентов для каждого бизнеса |
| ⌘K Палитра | Командный поиск по всем разделам |
| 📲 PWA | Устанавливается на телефон/компьютер |

### Telegram бот
`/tasks` `/addtask` `/done` `/status` `/analytics` `/crm` `/brief` `/weather` `/remind`  
+ голосовые сообщения (Whisper) + анализ фото (GPT-4o) + авто-напоминания

---

## Быстрый старт

### 1. Клонируй репозиторий
```bash
git clone https://github.com/nexus-nikita/nexus-ai.git
cd nexus-ai
```

### 2. Создай .env
```bash
cp .env.example .env
```
Заполни `.env` своими ключами (минимум: `OPENAI_API_KEY` и `WEB_PASSWORD`).

### 3. Запуск через Docker (рекомендуется)
```bash
docker-compose up -d
```
Открой: **http://localhost:5001**

### 4. Запуск локально (без Docker)
```bash
pip install -r requirements.txt
python nexus_web.py          # веб-панель
python nexus_telegram.py     # Telegram бот (отдельно)
```

---

## Деплой на Render.com (бесплатно)

1. Форкни репозиторий на GitHub
2. Зайди на [render.com](https://render.com) → New → Blueprint
3. Укажи репозиторий — Render сам прочитает `render.yaml`
4. Добавь секреты в Environment Variables
5. Deploy!

---

## Структура файлов

```
nexus_web.py        — главное Flask приложение (1600+ строк)
nexus_telegram.py   — Telegram бот
nexus_common.py     — общие утилиты (AI, env, профиль)
smoke_tests.py      — тесты (18 штук)
Dockerfile          — Docker образ
docker-compose.yml  — оба сервиса (web + telegram)
render.yaml         — деплой на Render.com
requirements.txt    — Python зависимости
.env.example        — шаблон переменных окружения
```

### JSON хранилища (данные)
```
nexus_memory.json   — история чата
nexus_profile.json  — профиль пользователя
nexus_state.json    — задачи, агенты, пользователи, настройки
crm_data.json       — CRM клиенты
analytics_data.json — аналитика выручки
reminders.json      — напоминания
```

---

## .env переменные

| Переменная | Описание |
|------------|----------|
| `OPENAI_API_KEY` | Ключ OpenAI |
| `WEB_PASSWORD` | Пароль входа на сайт |
| `WEB_SESSION_SECRET` | Секрет сессии Flask |
| `TELEGRAM_TOKEN` | Токен Telegram бота |
| `TELEGRAM_ALLOWED_USER_IDS` | ID пользователей (через запятую) |
| `GMAIL` | Gmail адрес |
| `APP_PASSWORD` | Google App Password |
| `OPENWEATHER_API_KEY` | Ключ OpenWeatherMap |
| `NOVA_POSHTA_API_KEY` | Ключ Нова Пошта |
| `AI_PROVIDER` | `openai` или `ollama` |

---

## Горячие клавиши

| Сочетание | Действие |
|-----------|----------|
| `Ctrl+K` | Командная палитра |
| `Ctrl+E` | Экспорт чата |
| `Enter` | Отправить сообщение |
| `Shift+Enter` | Новая строка |
| `Esc` | Закрыть палитру |

---

## Требования

- Python 3.11+
- OpenAI API key (или Ollama локально)
- Опционально: Gmail App Password, OpenWeatherMap, Nova Poshta API

---

## Деплой на Render (Free tier)

### Важливо — keep-alive пінг

Render free tier засипає через 15 хвилин бездіяльності. Щоб сайт завжди відповідав миттєво:

1. Зареєструйся на [UptimeRobot](https://uptimerobot.com) (безкоштовно)
2. Додай монітор: **HTTP(s)** → `https://nexus-ai-48sm.onrender.com/healthz`
3. Інтервал перевірки: **14 хвилин**

### ENV змінні (заповнити в Render Dashboard → Environment)

| Змінна | Опис |
|--------|------|
| `OPENAI_API_KEY` | OpenAI ключ (обов'язково) |
| `MONOBANK_TOKEN` | Monobank personal token |
| `TELEGRAM_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Твій Telegram chat ID |
| `GMAIL` | Gmail адреса для auto-reply |
| `APP_PASSWORD` | Gmail App Password |
| `GOOGLE_SPREADSHEET_ID` | ID Google Sheets таблиці |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Шлях до service account JSON |

### Деплой

```bash
# Одна команда — і Render сам передеплоїть:
git push origin deploy14
```
