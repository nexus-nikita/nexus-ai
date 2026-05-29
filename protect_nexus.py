#!/usr/bin/env python3
"""
protect_nexus.py — Tail-guard for nexus_web.py
================================================
Run this after ANY edit to nexus_web.py to make sure the file wasn't
silently truncated by the Edit tool's size limit.

Usage:
    python protect_nexus.py [--fix]

Without --fix  : reports status only (exit 0 = OK, exit 1 = truncated)
With    --fix  : re-appends the canonical stable tail if truncated
"""

import sys
import ast
import pathlib
import collections

TARGET = pathlib.Path(__file__).parent / "nexus_web.py"

# ── Markers that MUST be present, in order, near the end of the file ──────────
REQUIRED_TAIL_MARKERS = [
    '@app.route("/calendar",',
    '@app.route("/calendar/<event_id>",',
    '@app.route("/audit")',
    "def rag_context(",
    '@app.route("/rag/docs")',
    'if __name__ == "__main__":',
    "app.run(",
]

# ── Canonical stable tail (appended when truncation is detected) ───────────────
STABLE_TAIL = '''

@app.route("/calendar", methods=["GET","POST"])
def calendar():
    if not logged_in(): return jsonify({"error": "Login required."}), 401
    events = load_json(CALENDAR_FILE, [])
    if request.method == "GET":
        events_sorted = sorted(events, key=lambda e: e.get("date",""))
        return jsonify({"events": events_sorted})
    data = request.get_json(silent=True) or {}
    title = str(data.get("title","")).strip()
    date  = str(data.get("date","")).strip()
    if not title or not date:
        return jsonify({"success": False, "error": "Need title and date."})
    event = {
        "id":      secrets.token_hex(6),
        "title":   title,
        "date":    date,
        "time":    str(data.get("time","")).strip(),
        "desc":    str(data.get("desc","")).strip(),
        "created": datetime.utcnow().isoformat() + "Z",
    }
    events.append(event)
    save_json(CALENDAR_FILE, events)
    audit("calendar_add", f"{date} {title}", user=current_user()["username"])
    return jsonify({"success": True, "event": event})


@app.route("/calendar/<event_id>", methods=["DELETE"])
def calendar_delete(event_id):
    if not logged_in(): return jsonify({"error": "Login required."}), 401
    events = load_json(CALENDAR_FILE, [])
    before = len(events)
    events = [e for e in events if e.get("id") != event_id]
    save_json(CALENDAR_FILE, events)
    audit("calendar_delete", f"id={event_id}", user=current_user()["username"])
    return jsonify({"success": before != len(events)})


@app.route("/audit")
def audit_log():
    if not logged_in(): return jsonify({"error": "Login required."}), 401
    entries = load_json(AUDIT_FILE, [])
    action_filter = request.args.get("action", "").strip()
    if action_filter:
        entries = [e for e in entries if e.get("action") == action_filter]
    return jsonify({"entries": list(reversed(entries[-500:]))})


def rag_context(query: str, n: int = 3) -> str:
    try:
        from nexus_rag import search_documents
        results = search_documents(query, n=n)
        if not results:
            return ""
        snippets = "\\n\\n".join(f"[doc] {r}" for r in results)
        return f"\\n\\n---\\nRelevant documents:\\n{snippets}\\n---"
    except Exception:
        return ""


@app.route("/rag/docs")
def rag_docs():
    if not logged_in(): return jsonify({"error": "Login required."}), 401
    try:
        from nexus_rag import get_document_list
        return jsonify({"documents": get_document_list()})
    except Exception as e:
        return jsonify({"documents": [], "error": str(e)})


if __name__ == "__main__":
    ensure_env_keys()
    port = int(get_env("PORT", "5001"))
    print("NEXUS Web starting...")
    app.run(host="0.0.0.0", port=port, debug=False)
'''


def check(src: str) -> tuple[bool, list[str]]:
    """Return (ok, missing_markers)."""
    missing = [m for m in REQUIRED_TAIL_MARKERS if m not in src]
    return (len(missing) == 0, missing)


def ast_check(src: str) -> tuple[bool, dict]:
    """Return (ok, duplicates_dict)."""
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return False, {"SyntaxError": str(e)}
    counter = collections.Counter(
        n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
    )
    dups = {k: v for k, v in counter.items() if v > 1}
    return len(dups) == 0, dups


def find_cut_point(src: str) -> int:
    """Find line index of first missing tail marker to cut there before re-append."""
    for marker in REQUIRED_TAIL_MARKERS:
        if marker not in src:
            # Cut just before the last app.run / if __main__ block if present
            idx = src.rfind('\nif __name__')
            if idx > 0:
                return idx
            # Otherwise cut at last occurrence of calendar route marker
            idx2 = src.rfind('@app.route("/calendar"')
            if idx2 > 0:
                return idx2
            # Fallback: cut at last function def line
            return len(src.rstrip())
    return len(src)


def main():
    fix_mode = "--fix" in sys.argv

    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found.")
        sys.exit(2)

    src = TARGET.read_bytes().decode("utf-8", errors="replace")

    print(f"nexus_web.py  —  {src.count(chr(10))} lines")

    # 1. Tail markers
    ok_tail, missing = check(src)
    if ok_tail:
        print("✅  All tail markers present")
    else:
        print(f"❌  Missing tail markers: {missing}")

    # 2. AST
    ok_ast, dups = ast_check(src)
    if ok_ast:
        print("✅  AST clean — no duplicate functions")
    else:
        print(f"❌  AST issues: {dups}")

    all_ok = ok_tail and ok_ast

    if all_ok:
        print("\n✅  File is healthy. No action needed.")
        sys.exit(0)

    if not fix_mode:
        print("\n⚠️  Run with --fix to repair automatically.")
        sys.exit(1)

    # ── FIX MODE ──────────────────────────────────────────────────────────────
    print("\n🔧  Applying fix...")

    # Step 1: strip duplicate function bodies if needed
    if not ok_ast:
        # Cut at first occurrence of `if __name__ == "__main__":` to remove dups
        cut = src.find('\nif __name__ == "__main__":')
        if cut > 0:
            src = src[:cut]
            print("   Stripped from first __main__ block downward")

    # Step 2: strip incomplete tail (from first missing marker onward)
    if not ok_tail:
        cut = find_cut_point(src)
        src = src[:cut].rstrip()
        src += "\n" + STABLE_TAIL
        print("   Re-appended stable tail")

    # Step 3: verify repair
    ok2, missing2 = check(src)
    ok_ast2, dups2 = ast_check(src)

    if ok2 and ok_ast2:
        TARGET.write_bytes(src.encode("utf-8"))
        print(f"✅  Fixed and saved  ({src.count(chr(10))} lines)")
        sys.exit(0)
    else:
        print(f"❌  Could not auto-fix. Missing={missing2}  AST={dups2}")
        print("    Manual repair required.")
        sys.exit(1)


if __name__ == "__main__":
    main()
