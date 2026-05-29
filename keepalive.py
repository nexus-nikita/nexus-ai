#!/usr/bin/env python3
"""
keepalive.py — пінгує NEXUS кожні 13 хвилин щоб Render free tier не засипав.
Запускай локально або в будь-якому cloud cron.

Використання:
    python keepalive.py                  # пінгує нескінченно
    python keepalive.py --once           # один пінг і вийти
"""
import sys, time, urllib.request, urllib.error, datetime

URL = "https://nexus-ai-48sm.onrender.com/healthz"
INTERVAL = 13 * 60  # 13 хвилин

def ping():
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": "NEXUS-keepalive/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            status = r.status
            body = r.read(64).decode("utf-8", errors="replace")
            print(f"[{_now()}] ✅  {status}  {body.strip()}")
            return True
    except urllib.error.URLError as e:
        print(f"[{_now()}] ❌  {e}")
        return False
    except Exception as e:
        print(f"[{_now()}] ⚠️  {e}")
        return False

def _now():
    return datetime.datetime.now().strftime("%H:%M:%S")

if __name__ == "__main__":
    once = "--once" in sys.argv
    print(f"NEXUS keepalive → {URL}")
    if once:
        ping()
        sys.exit(0)
    print(f"Пінг кожні {INTERVAL//60} хв. Ctrl+C щоб зупинити.\n")
    while True:
        ping()
        time.sleep(INTERVAL)
