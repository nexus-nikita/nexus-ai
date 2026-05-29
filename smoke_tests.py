import sys
from nexus_common import get_env
import nexus_web


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"{name}{': ' + detail if detail else ''}")
    print(f"ok - {name}")


def main():
    client = nexus_web.app.test_client()

    # ── Login page ─────────────────────────────────────────────────────────────
    login_page = client.get("/login")
    check("login page loads", login_page.status_code == 200)

    # ── Health ─────────────────────────────────────────────────────────────────
    health = client.get("/healthz")
    check("healthz ok", health.status_code == 200 and health.json.get("ok") is True)
    check("storage in health", "storage" in health.json and "backend" in health.json["storage"])

    # ── Auth guard ─────────────────────────────────────────────────────────────
    protected = client.get("/history")
    check("history requires login", protected.status_code == 401)

    # ── Login (now requires username + password) ────────────────────────────────
    password = get_env("WEB_PASSWORD")
    login = client.post("/login", data={"username": "admin", "password": password})
    check("password login redirects", login.status_code == 302,
          f"got {login.status_code}")

    # ── CSRF ───────────────────────────────────────────────────────────────────
    client.get("/")
    with client.session_transaction() as sess:
        token = sess.get("csrf_token")
    check("csrf token created", bool(token))

    no_csrf = client.post("/chat", json={"message": ""})
    check("csrf blocks post", no_csrf.status_code == 403)

    with_csrf = client.post("/chat", json={"message": ""},
                            headers={"X-CSRF-Token": token})
    check("csrf allows post", with_csrf.status_code == 200)

    # ── Core routes ────────────────────────────────────────────────────────────
    stats = client.get("/stats")
    check("stats json", stats.status_code == 200 and "messages" in stats.json)

    weather = client.get("/weather?city=Kyiv")
    check("weather endpoint", weather.status_code == 200 and
          ("summary" in weather.json or "error" in weather.json))

    nova = client.get("/nova_poshta/track?number=20400000000000")
    check("nova poshta fallback", nova.status_code == 200 and "success" in nova.json)

    pdf = client.post("/invoice_pdf", json={"client": "Test", "amount": 100},
                      headers={"X-CSRF-Token": token})
    check("invoice pdf", pdf.status_code == 200 and pdf.data.startswith(b"%PDF-1.4"))

    caps = client.get("/capabilities")
    check("capabilities registry", caps.status_code == 200 and "capabilities" in caps.json)
    check("settings payload", "integrations" in caps.json and "storage" in caps.json)

    users = client.get("/users")
    check("users endpoint", users.status_code == 200 and "users" in users.json)

    agents = client.get("/agents")
    check("agents endpoint", agents.status_code == 200 and "agents" in agents.json)

    # ── 2FA status ─────────────────────────────────────────────────────────────
    tfa = client.get("/2fa/status")
    check("2fa status", tfa.status_code == 200 and "enabled" in tfa.json)

    # ── Calendar ───────────────────────────────────────────────────────────────
    cal = client.get("/calendar")
    check("calendar get", cal.status_code == 200 and "events" in cal.json)

    # ── Audit log ──────────────────────────────────────────────────────────────
    audit = client.get("/audit")
    check("audit log", audit.status_code == 200 and "entries" in audit.json)

    # ── Task creation ──────────────────────────────────────────────────────────
    task = client.post("/tasks", json={"title": "Smoke test task"},
                       headers={"X-CSRF-Token": token})
    check("tasks create", task.status_code == 200 and task.json.get("success") is True)

    cmd = client.post("/command", json={"command": "добавь задачу проверить NEXUS"},
                      headers={"X-CSRF-Token": token})
    check("command router", cmd.status_code == 200 and cmd.json.get("action") == "task_created")

    # ── Monobank (no token — should return token_ok=False, not crash) ──────────
    mono = client.get("/monobank")
    check("monobank no-token graceful", mono.status_code == 200 and "token_ok" in mono.json)

    print("\nAll smoke tests passed ✅")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        raise
