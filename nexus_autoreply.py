"""
nexus_autoreply.py — Auto-reply daemon for incoming emails.
Checks inbox every 5 min; replies via AI to business enquiries.
Run standalone: python nexus_autoreply.py
"""
import asyncio, imaplib, smtplib, email, json, os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from datetime import datetime
from pathlib import Path

_os_env = os.environ.get("NEXUS_DATA_DIR", "").strip()
_render_disk = Path("/app")
_code_dir = Path(__file__).resolve().parent
if _os_env:
    BASE_DIR = Path(_os_env)
elif _render_disk.is_dir() and _code_dir != _render_disk:
    BASE_DIR = _render_disk
else:
    BASE_DIR = _code_dir

try:
    from nexus_common import load_env_file, get_env
    load_env_file()
except ImportError:
    def get_env(k, d=""):
        return os.getenv(k, d)

GMAIL          = get_env("GMAIL", "")
APP_PASSWORD   = get_env("APP_PASSWORD", "")
TELEGRAM_TOKEN = get_env("TELEGRAM_TOKEN", "")
_cid           = get_env("TELEGRAM_CHAT_ID", "")
CHAT_ID        = int(_cid) if _cid.strip().isdigit() else 0
REPLIED_FILE   = BASE_DIR / "replied_emails.json"

KEYWORDS = [
    'цена','стоимость','прайс','сколько стоит','услуги',
    'информация','предложение','сотрудничество','вопрос',
    'ціна','вартість','послуги','співпраця','питання',
    'price','cost','services','cooperation','inquiry',
]

def load_replied():
    if REPLIED_FILE.exists():
        try: return json.loads(REPLIED_FILE.read_text(encoding="utf-8"))
        except: pass
    return []

def save_replied(ids):
    REPLIED_FILE.write_text(json.dumps(ids), encoding="utf-8")

def decode_str(s):
    result = ""
    for part, enc in decode_header(s or ""):
        result += part.decode(enc or "utf-8", errors="ignore") if isinstance(part, bytes) else str(part)
    return result

def should_autoreply(subject, body):
    text = (subject + " " + body).lower()
    return any(kw in text for kw in KEYWORDS)

def generate_reply(subject, body, sender):
    key = get_env("OPENAI_API_KEY", "")
    if not key:
        return "Thank you! We will get back to you shortly.\n\nNEXUS Assistant"
    from openai import OpenAI
    resp = OpenAI(api_key=key).chat.completions.create(
        model=get_env("OPENAI_MODEL", "gpt-4o-mini"),
        messages=[{"role": "user", "content":
            f"Answer this business email professionally (match language of sender).\n"
            f"From: {sender}\nSubject: {subject}\nBody: {body[:600]}\n"
            "Say the owner will follow up soon. Sign as: NEXUS Assistant"}],
    )
    return resp.choices[0].message.content

def send_reply(to, subject, body):
    msg = MIMEMultipart()
    msg["From"] = GMAIL; msg["To"] = to; msg["Subject"] = f"Re: {subject}"
    msg.attach(MIMEText(body, "plain", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL, APP_PASSWORD); s.send_message(msg)

async def check_and_reply():
    if not GMAIL or not APP_PASSWORD:
        print("GMAIL/APP_PASSWORD not set — skipping."); return
    replied = load_replied()
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL, APP_PASSWORD); mail.select("inbox")
        today = datetime.now().strftime("%d-%b-%Y")
        _, data = mail.search(None, f"UNSEEN SINCE {today}")
        for eid in data[0].split():
            eid_str = eid.decode()
            if eid_str in replied: continue
            _, md = mail.fetch(eid, "(RFC822)")
            msg = email.message_from_bytes(md[0][1])
            subject = decode_str(msg.get("Subject",""))
            sender  = decode_str(msg.get("From",""))
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        try: body = part.get_payload(decode=True).decode("utf-8","ignore"); break
                        except: pass
            else:
                try: body = msg.get_payload(decode=True).decode("utf-8","ignore")
                except: pass
            if not should_autoreply(subject, body): continue
            reply = generate_reply(subject, body, sender)
            send_reply(sender, subject, reply)
            replied.append(eid_str); save_replied(replied)
            print(f"AutoReply sent → {sender}")
            if CHAT_ID and TELEGRAM_TOKEN:
                try:
                    from telegram import Bot
                    await Bot(token=TELEGRAM_TOKEN).send_message(
                        chat_id=CHAT_ID,
                        text=f"🤖 AutoReply!\nTo: {sender}\nSubj: {subject}\n\n{reply[:300]}...")
                except Exception as e: print(f"TG: {e}")
        mail.logout()
    except Exception as e: print(f"Error: {e}")

async def scheduler():
    print("NEXUS AutoReply started — checking every 5 min")
    while True:
        await check_and_reply()
        await asyncio.sleep(300)

if __name__ == "__main__":
    asyncio.run(scheduler())
