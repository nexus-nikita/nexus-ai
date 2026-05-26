import sys

from nexus_common import get_env
import nexus_web


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print(f"ok - {name}")


def main():
    client = nexus_web.app.test_client()

    login_page = client.get("/login")
    check("login page", login_page.status_code == 200)

    health = client.get("/healthz")
    check("healthz", health.status_code == 200 and health.json.get("ok") is True)

    protected = client.get("/history")
    check("history requires login", protected.status_code == 401)

    password = get_env("WEB_PASSWORD")
    login = client.post("/login", data={"password": password})
    check("password login redirects", login.status_code == 302)

    client.get("/")
    ctx = client.session_transaction()
    sess = ctx.__enter__()
    token = sess.get("csrf_token")
    ctx.__exit__(None, None, None)
    check("csrf token created", bool(token))

    no_csrf = client.post("/chat", json={"message": ""})
    check("csrf blocks post", no_csrf.status_code == 403)

    with_csrf = client.post("/chat", json={"message": ""}, headers={"X-CSRF-Token": token})
    check("csrf allows post", with_csrf.status_code == 200)

    stats = client.get("/stats")
    check("stats json", stats.status_code == 200 and "messages" in stats.json)

    weather = client.get("/weather?city=Kyiv")
    check("weather endpoint", weather.status_code == 200 and ("summary" in weather.json or "error" in weather.json))

    nova = client.get("/nova_poshta/track?number=20400000000000")
    check("nova poshta fallback", nova.status_code == 200 and "success" in nova.json)

    pdf = client.post(
        "/invoice_pdf",
        json={"client": "Test", "amount": 100},
        headers={"X-CSRF-Token": token},
    )
    check("invoice pdf", pdf.status_code == 200 and pdf.data.startswith(b"%PDF-1.4"))

    print("All smoke tests passed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        raise
