import os
import json
import base64
import urllib.request
from datetime import datetime
from flask import Flask, request, jsonify, Response, stream_with_context
from openai import OpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
client = OpenAI(api_key=OPENAI_API_KEY)
app = Flask(__name__)

history = []
stats = {"messages_today": 0, "voice_used": 0, "total_messages": 0, "last_active": "", "topics": []}

SYSTEM = (
    "Ты NEXUS — центр управления всем. Мощный персональный AI помощник.\n"
    "Пользователь: Никита. Бизнес: общепит, аква бизнес, продвижение. Украина.\n"
    "Всегда отвечай на русском языке. Обращайся по имени Никита.\n"
    "Форматируй ответы с Markdown: **жирный**, *курсив*, списки, заголовки где уместно."
)

_HTML = None

def get_html():
    global _HTML
    if _HTML is None:
        tpl = os.path.join(os.path.dirname(__file__), "nexus_template.html")
        with open(tpl, encoding="utf-8") as f:
            _HTML = f.read()
    return _HTML


@app.route("/")
def index():
    return get_html()


@app.route("/stats")
def get_stats():
    return jsonify(stats)


@app.route("/chat/stream")
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
            yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/tts", methods=["POST"])
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
        url = (
            "https://api.openweathermap.org/data/2.5/weather"
            "?q=Odessa,UA&appid=" + api_key + "&units=metric&lang=ru"
        )
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
        icons = {
            "Clear": "☀️", "Clouds": "☁️", "Rain": "🌧️", "Drizzle": "🌦️",
            "Thunderstorm": "⛈️", "Snow": "❄️", "Mist": "🌫️", "Fog": "🌫️",
        }
        main = data["weather"][0]["main"]
        return {
            "temp": round(data["main"]["temp"]),
            "feels_like": round(data["main"]["feels_like"]),
            "humidity": data["main"]["humidity"],
            "wind": round(data["wind"]["speed"]),
            "description": data["weather"][0]["description"].capitalize(),
            "icon": icons.get(main, "🌤️"),
        }, None
    except Exception as e:
        return None, str(e)


@app.route("/weather")
def weather():
    w, err = get_odessa_weather()
    if err:
        return jsonify({"error": err})
    return jsonify(w)


@app.route("/briefing")
def briefing():
    w, _ = get_odessa_weather()
    weather_text = ""
    if w:
        weather_text = (
            f"Погода в Одессе: {w['temp']}°C, {w['description']}, "
            f"влажность {w['humidity']}%, ветер {w['wind']} м/с."
        )
    today = datetime.now().strftime("%A, %d %B %Y")
    prompt = (
        f"Создай утренний брифинг для Никиты на {today}.\n"
        f"{weather_text}\n"
        "Включи:\n1. Приветствие с датой и погодой\n"
        "2. Мотивационную мысль дня\n"
        "3. Топ-3 фокуса для бизнеса (общепит, аква, продвижение)\n"
        "4. Совет дня\nФорматируй красиво с Markdown."
    )
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        )
        return jsonify({"briefing": response.choices[0].message.content})
    except Exception as e:
        return jsonify({"error": str(e)})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5003))
    print(f"NEXUS Dashboard: http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", debug=False, port=port)
