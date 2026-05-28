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
        "AI_PROVIDER": "ollama",
        "OLLAMA_MODEL": "qwen2.5:3b",
        "OLLAMA_URL": "http://127.0.0.1:11434/api/chat",
        "OPENAI_API_KEY": "",
        "OPENAI_MODEL": "gpt-4o-mini",
        "TELEGRAM_TOKEN": "",
        "WEB_PASSWORD": secrets.token_urlsafe(18),
        "WEB_SESSION_SECRET": secrets.token_urlsafe(48),
        "TELEGRAM_ALLOWED_USER_IDS": "",
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
        raise RuntimeError("\u041d\u0435 \u0437\u0430\u0434\u0430\u043d OPENAI_API_KEY. \u041b\u0438\u0431\u043e \u0432\u0441\u0442\u0430\u0432\u044c \u043d\u043e\u0432\u044b\u0439 \u043a\u043b\u044e\u0447 \u0432 .env, \u043b\u0438\u0431\u043e \u043e\u0441\u0442\u0430\u0432\u044c AI_PROVIDER=ollama.")
    return key


def require_web_password():
    password = get_env("WEB_PASSWORD", "").strip()
    if not password:
        raise RuntimeError("\u041d\u0435 \u0437\u0430\u0434\u0430\u043d WEB_PASSWORD \u0432 .env")
    return password


def get_web_session_secret():
    secret = get_env("WEB_SESSION_SECRET", "").strip()
    if not secret:
        secret = secrets.token_urlsafe(48)
        os.environ["WEB_SESSION_SECRET"] = secret
    return secret


def require_telegram_token():
    token = get_env("TELEGRAM_TOKEN", "").strip()
    if not token:
        raise RuntimeError("\u041d\u0435 \u0437\u0430\u0434\u0430\u043d TELEGRAM_TOKEN \u0432 .env")
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
    raise RuntimeError("\u041d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u044b\u0439 AI_PROVIDER. \u0418\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439 ollama \u0438\u043b\u0438 openai.")


def ask_ollama(messages):
    model = get_env("OLLAMA_MODEL", "qwen2.5:3b")
    url = get_env("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
    payload = json.dumps({"model": model, "messages": messages, "stream": False}).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError("Ollama \u043d\u0435 \u043e\u0442\u0432\u0435\u0447\u0430\u0435\u0442. \u0417\u0430\u043f\u0443\u0441\u0442\u0438 Ollama \u0438 \u0432\u044b\u043f\u043e\u043b\u043d\u0438: ollama pull " + model) from exc

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
    "name": "\u041d\u0438\u043a\u0438\u0442\u0430",
    "business": "\u043e\u0431\u0449\u0435\u043f\u0438\u0442, \u0430\u043a\u0432\u0430 \u0431\u0438\u0437\u043d\u0435\u0441, \u043a\u043e\u043c\u043f\u0430\u043d\u0438\u044f \u043f\u043e \u043f\u0440\u043e\u0434\u0432\u0438\u0436\u0435\u043d\u0438\u044e \u0431\u0438\u0437\u043d\u0435\u0441\u0430",
    "location": "\u0423\u043a\u0440\u0430\u0438\u043d\u0430",
}


def build_system_prompt(profile=None):
    profile = profile or DEFAULT_PROFILE
    return f"""\u0422\u044b NEXUS - \u043f\u0440\u0438\u0432\u0430\u0442\u043d\u044b\u0439 AI-\u043f\u043e\u043c\u043e\u0449\u043d\u0438\u043a.
\u0418\u043d\u0444\u043e\u0440\u043c\u0430\u0446\u0438\u044f \u043e \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u0435:
- \u0418\u043c\u044f: {profile['name']}
- \u0411\u0438\u0437\u043d\u0435\u0441: {profile['business']}
- \u041b\u043e\u043a\u0430\u0446\u0438\u044f: {profile['location']}
\u041e\u0442\u0432\u0435\u0447\u0430\u0439 \u043d\u0430 \u0440\u0443\u0441\u0441\u043a\u043e\u043c \u044f\u0437\u044b\u043a\u0435. \u041e\u0431\u0440\u0430\u0449\u0430\u0439\u0441\u044f \u043a \u043d\u0435\u043c\u0443 \u043f\u043e \u0438\u043c\u0435\u043d\u0438."""
