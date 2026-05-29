import json
import os
import secrets
import urllib.error
import urllib.request
from pathlib import Path

from openai import OpenAI

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


def load_env_file(filename=".env"):
    path = BASE_DIR / filename
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        # Do NOT overwrite vars already set by the environment (Render/Docker)
        if key.strip() not in os.environ:
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


def ensure_env_file():
    if ENV_FILE.exists():
        return
    ENV_FILE.write_text(
        "AI_PROVIDER=ollama\n"
        "OLLAMA_MODEL=qwen2.5:3b\n"
        "OLLAMA_URL=http://127.0.0.1:11434/api/chat\n"
        "OPENAI_API_KEY=\n"
        "OPENAI_MODEL=gpt-4o-mini\n"
        "TELEGRAM_TOKEN=\n"
        f"WEB_PASSWORD={secrets.token_urlsafe(18)}\n"
        f"WEB_SESSION_SECRET={secrets.token_urlsafe(48)}\n"
        "TELEGRAM_ALLOWED_USER_IDS=\n",
        encoding="utf-8",
    )


def ensure_env_keys():
    ensure_env_file()
    existing = {}
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if "=" in raw_line and not raw_line.strip().startswith("#"):
            key, value = raw_line.split("=", 1)
            existing[key.strip()] = value.strip()

    defaults = {
        "AI_PROVIDER": "openai",
        "OPENAI_API_KEY": "",
        "OPENAI_MODEL": "gpt-4o-mini",
        "OPENAI_TTS_MODEL": "tts-1",
        "OPENAI_TTS_VOICE": "onyx",
        "OLLAMA_MODEL": "qwen2.5:3b",
        "OLLAMA_URL": "http://127.0.0.1:11434/api/chat",
        "WEB_PASSWORD": secrets.token_urlsafe(18),
        "WEB_SESSION_SECRET": secrets.token_urlsafe(48),
        "PORT": "5001",
        "TELEGRAM_TOKEN": "",
        "TELEGRAM_ALLOWED_USER_IDS": "",
        "GMAIL": "",
        "APP_PASSWORD": "",
        "OPENWEATHER_API_KEY": "",
        "DEFAULT_WEATHER_CITY": "Kyiv",
        "NOVA_POSHTA_API_KEY": "",
        "MONOBANK_TOKEN": "",
        "TELEGRAM_CHAT_ID": "",
    }

    changed = False
    for key, value in defaults.items():
        if key not in existing:
            existing[key] = value
            changed = True

    if changed:
        ENV_FILE.write_text("\n".join(f"{k}={v}" for k, v in existing.items()) + "\n", encoding="utf-8")

    load_env_file()


def get_env(name, default=""):
    ensure_env_keys()
    return os.getenv(name, default)


def get_ai_provider():
    return get_env("AI_PROVIDER", "ollama").strip().lower()


def require_openai_key():
    key = get_env("OPENAI_API_KEY", "").strip()
    if not key or key.startswith("your_"):
        raise RuntimeError("Не задан OPENAI_API_KEY. Либо вставь новый ключ в .env, либо оставь AI_PROVIDER=ollama.")
    return key


def require_web_password():
    # Prioritize OS env var (Render/Docker) over .env file
    password = os.environ.get("WEB_PASSWORD", "").strip()
    if not password:
        password = get_env("WEB_PASSWORD", "").strip()
    if not password:
        password = "nexus2026"
    return password


def get_web_session_secret():
    secret = os.environ.get("WEB_SESSION_SECRET", "").strip()
    if not secret:
        secret = get_env("WEB_SESSION_SECRET", "").strip()
    if not secret:
        secret = secrets.token_urlsafe(48)
        os.environ["WEB_SESSION_SECRET"] = secret
    return secret


def require_telegram_token():
    token = get_env("TELEGRAM_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Не задан TELEGRAM_TOKEN в .env")
    return token


def get_allowed_telegram_user_ids():
    raw = get_env("TELEGRAM_ALLOWED_USER_IDS", "")
    allowed = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            allowed.add(int(part))
        except ValueError:
            continue
    return allowed


def ask_ai(messages):
    provider = get_ai_provider()
    if provider == "ollama":
        return ask_ollama(messages)
    if provider == "openai":
        return ask_openai(messages)
    raise RuntimeError("Неизвестный AI_PROVIDER. Используй ollama или openai.")


def ask_ollama(messages):
    model = get_env("OLLAMA_MODEL", "qwen2.5:3b")
    url = get_env("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
    payload = json.dumps({"model": model, "messages": messages, "stream": False}).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError("Ollama не отвечает. Запусти Ollama и выполни: ollama pull " + model) from exc
    return data.get("message", {}).get("content", "")


def ask_openai(messages):
    key = require_openai_key()
    client = OpenAI(api_key=key)
    response = client.chat.completions.create(
        model=get_env("OPENAI_MODEL", "gpt-4o-mini"),
        messages=messages,
    )
    return response.choices[0].message.content


DEFAULT_PROFILE = {
    "name": "Никита",
    "business": "общепит, аква бизнес, компания по продвижению бизнеса",
    "location": "Украина",
}


def build_system_prompt(profile=None):
    profile = profile or DEFAULT_PROFILE
    name     = profile.get("name", "Никита")
    business = profile.get("business", "бизнес")
    location = profile.get("location", "Украина")
    city     = profile.get("city", "")
    city_str = f", город {city}" if city else ""
    return (
        f"Ты NEXUS — персональный AI-центр управления бизнесом.\n"
        f"Хозяин: {name}. Бизнес: {business}. Локация: {location}{city_str}.\n"
        f"Отвечай на русском языке. Обращайся по имени {name}.\n"
        "Ты умеешь: управлять задачами, CRM, аналитикой, напоминаниями, email, погодой, "
        "трекингом Новой Почты, выставлением счётов и многим другим.\n"
        "Будь кратким, конкретным и деловым."
    )
