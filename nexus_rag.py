"""
nexus_rag.py — RAG (Retrieval-Augmented Generation) module for NEXUS.

Provides:
  index_document(text, doc_id)   — chunk and embed a document into ChromaDB
  search_documents(query, n=3)   — return n most relevant text snippets
  get_document_list()             — list indexed document IDs

Can also run as a standalone Flask app on port 5005 for testing.
"""
import os
from pathlib import Path

# ── Lazy imports so nexus_web doesn't fail if chromadb is missing ─────────────

def _get_openai_client():
    from openai import OpenAI
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
    return OpenAI(api_key=api_key)

def _get_collection():
    import chromadb
    import os as _os
    _render_disk = Path("/app")
    _env_data    = _os.environ.get("NEXUS_DATA_DIR", "").strip()
    if _env_data:
        _base = Path(_env_data)
    elif _render_disk.is_dir() and str(Path(__file__).parent) != str(_render_disk):
        _base = _render_disk
    else:
        _base = Path(__file__).parent
    db_path = str(_base / "chroma_db")
    chroma = chromadb.PersistentClient(path=db_path)
    return chroma.get_or_create_collection("nexus_docs")


def chunk_text(text: str, size: int = 500) -> list[str]:
    words = text.split()
    return [" ".join(words[i:i+size]) for i in range(0, len(words), size)]


def index_document(text: str, doc_id: str) -> int:
    """Chunk text, embed each chunk, store in ChromaDB. Returns number of chunks."""
    client = _get_openai_client()
    collection = _get_collection()
    chunks = chunk_text(text)
    for i, chunk in enumerate(chunks):
        emb = client.embeddings.create(
            model="text-embedding-3-small",
            input=chunk,
        ).data[0].embedding
        collection.upsert(
            embeddings=[emb],
            documents=[chunk],
            ids=[f"{doc_id}__chunk{i}"],
            metadatas=[{"doc_id": doc_id, "chunk": i}],
        )
    return len(chunks)


def search_documents(query: str, n: int = 3) -> list[str]:
    """Return up to n relevant text snippets for the given query."""
    try:
        client = _get_openai_client()
        collection = _get_collection()
        if collection.count() == 0:
            return []
        q_emb = client.embeddings.create(
            model="text-embedding-3-small",
            input=query,
        ).data[0].embedding
        results = collection.query(query_embeddings=[q_emb], n_results=min(n, collection.count()))
        return results["documents"][0] if results.get("documents") else []
    except Exception:
        return []


def get_document_list() -> list[str]:
    """Return deduplicated list of indexed document IDs."""
    try:
        collection = _get_collection()
        metas = collection.get(include=["metadatas"])["metadatas"] or []
        seen = {}
        for m in metas:
            doc_id = m.get("doc_id", "?")
            seen[doc_id] = seen.get(doc_id, 0) + 1
        return [f"{k} ({v} chunks)" for k, v in sorted(seen.items())]
    except Exception:
        return []


def extract_text(file_obj, filename: str) -> str:
    """Extract plain text from PDF, DOCX, or TXT file objects."""
    try:
        if filename.lower().endswith(".pdf"):
            from pypdf import PdfReader
            reader = PdfReader(file_obj)
            return " ".join(page.extract_text() or "" for page in reader.pages)
        elif filename.lower().endswith(".docx"):
            from docx import Document
            doc = Document(file_obj)
            return " ".join(p.text for p in doc.paragraphs)
        else:
            return file_obj.read().decode("utf-8", errors="replace")
    except Exception as e:
        return ""


# ── Standalone Flask server (optional) ───────────────────────────────────────

if __name__ == "__main__":
    from flask import Flask, request, jsonify, render_template_string

    _standalone_app = Flask(__name__)

    STANDALONE_HTML = """<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8"><title>NEXUS Knowledge Base</title>
<style>
body{background:#07141a;color:#eef8f9;font-family:Inter,Arial,sans-serif;margin:0;padding:24px}
.card{background:#101d25;border:1px solid #25414b;border-radius:14px;padding:18px;margin-bottom:16px;max-width:700px}
h2{color:#35d7e9;font-size:14px;letter-spacing:1px;text-transform:uppercase;margin-bottom:12px}
input,button{font:inherit}
.btn{background:linear-gradient(135deg,#35d7e9,#48e08c);color:#041014;font-weight:900;border:0;border-radius:10px;padding:8px 18px;cursor:pointer}
.field{width:100%;border:1px solid #25414b;background:#071218;color:#eef8f9;border-radius:10px;padding:8px 12px;outline:none;margin-bottom:8px}
.result{margin-top:12px;color:#c5d5d9;font-size:14px;line-height:1.6;white-space:pre-wrap}
</style></head><body>
<div class="card"><h2>📁 Загрузить документ</h2>
  <input type="file" id="f" accept=".pdf,.docx,.txt" class="field">
  <button class="btn" onclick="upload()">Загрузить</button>
  <div class="result" id="upRes"></div>
</div>
<div class="card"><h2>🔍 Поиск</h2>
  <input class="field" id="q" placeholder="Вопрос..." onkeydown="if(event.key==='Enter')search()">
  <button class="btn" onclick="search()">Найти</button>
  <div class="result" id="sRes"></div>
</div>
<script>
function upload(){
  var f=document.getElementById('f').files[0];if(!f)return;
  var fd=new FormData();fd.append('file',f);
  document.getElementById('upRes').textContent='Загружаю...';
  fetch('/upload',{method:'POST',body:fd}).then(r=>r.json()).then(d=>{
    document.getElementById('upRes').textContent=d.success?'✅ '+d.chunks+' чанків':(d.error||'Помилка');
  });
}
function search(){
  var q=document.getElementById('q').value.trim();if(!q)return;
  fetch('/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:q,n:3})})
  .then(r=>r.json()).then(d=>{
    document.getElementById('sRes').textContent=(d.results||[]).join('\\n\\n---\\n')||'Нічого не знайдено';
  });
}
</script></body></html>"""

    @_standalone_app.route("/")
    def _index():
        return render_template_string(STANDALONE_HTML)

    @_standalone_app.route("/upload", methods=["POST"])
    def _upload():
        f = request.files.get("file")
        if not f:
            return jsonify({"success": False, "error": "No file"})
        try:
            text = extract_text(f, f.filename)
            if not text.strip():
                return jsonify({"success": False, "error": "Could not extract text"})
            n = index_document(text, f.filename)
            return jsonify({"success": True, "chunks": n})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    @_standalone_app.route("/search", methods=["POST"])
    def _search():
        data = request.get_json(silent=True) or {}
        query = data.get("query", "")
        n = int(data.get("n", 3))
        results = search_documents(query, n=n)
        return jsonify({"results": results})

    @_standalone_app.route("/docs")
    def _docs():
        return jsonify({"documents": get_document_list()})

    print("NEXUS Knowledge Base: http://127.0.0.1:5005")
    _standalone_app.run(host="127.0.0.1", debug=False, port=5005)
