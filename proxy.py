from flask import Flask, request, jsonify, Response, stream_with_context, session
import httpx
import sqlite3
import json
import time
import os
import base64
import hashlib
import secrets
import zipfile
import io
import mimetypes
from collections import defaultdict

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

API_KEY   = os.environ.get("API_KEY", "test123")
HTML_PATH = os.path.join(os.path.dirname(__file__), 'index.html')
DB_PATH   = os.path.join(os.path.dirname(__file__), 'cesarai.db')

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

# ── DATABASE BACKEND (SQLite ó PostgreSQL) ────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")

if DATABASE_URL:
    try:
        import psycopg2
        DB_TYPE = "postgres"
        PH      = "%s"
        print("[DB] ✅ PostgreSQL conectado")
    except ImportError:
        print("[DB] ⚠️  psycopg2 no instalado — usando SQLite como fallback")
        DATABASE_URL = ""
        DB_TYPE = "sqlite"
        PH      = "?"
else:
    DB_TYPE = "sqlite"
    PH      = "?"
    print(f"[DB] SQLite → {DB_PATH}")

# ── PROVIDERS ─────────────────────────────────────────────────────────────────
image_sessions = {}

IMAGE_ENGINES = {
    "flux":         {"provider": "pollinations", "model": "flux",         "label": "FLUX Pro",        "key": False},
    "flux-schnell": {"provider": "pollinations", "model": "flux-schnell", "label": "FLUX Schnell ⚡",  "key": False},
    "flux-realism": {"provider": "pollinations", "model": "flux-realism", "label": "FLUX Realism 📷", "key": False},
    "turbo":        {"provider": "pollinations", "model": "turbo",        "label": "Turbo (SD XL)",   "key": False},
    "hf-flux":      {"provider": "huggingface",  "model": "black-forest-labs/FLUX.1-schnell", "label": "HF FLUX Schnell", "key": True},
    "hf-sd35":      {"provider": "huggingface",  "model": "stabilityai/stable-diffusion-3.5-large", "label": "HF SD 3.5 Large", "key": True},
}

PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key":  os.environ.get("GROQ_API_KEY", ""),
        "models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "gemma2-9b-it"
        ],
        "type": "openai"
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key":  os.environ.get("GEMINI_API_KEY", ""),
        "models": [
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
            "gemini-2.5-flash"
        ],
        "type": "openai"
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "api_key":  os.environ.get("MISTRAL_API_KEY", ""),
        "models": [
            "mistral-small-latest",
            "mistral-nemo",
            "open-mistral-7b"
        ],
        "type": "openai"
    }
}

VISION_PROVIDERS = [
    ("groq",    "llama-3.2-11b-vision-preview"),
    ("gemini",  "gemini-2.0-flash"),
]

FALLBACK_ORDER = ["groq", "gemini", "mistral"]
rate_data      = defaultdict(list)
RATE_LIMIT     = 60

# ── HELPERS ───────────────────────────────────────────────────────────────────

def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()

def check_rate_limit(ip):
    now = time.time()
    rate_data[ip] = [t for t in rate_data[ip] if now - t < 60]
    if len(rate_data[ip]) >= RATE_LIMIT:
        return False
    rate_data[ip].append(now)
    return True

# ── DB ────────────────────────────────────────────────────────────────────────

def get_db():
    if DB_TYPE == "postgres":
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        return conn
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = get_db()
    c    = conn.cursor()
    try:
        if DB_TYPE == "postgres":
            c.execute('''CREATE TABLE IF NOT EXISTS users (
                id           SERIAL PRIMARY KEY,
                username     TEXT NOT NULL UNIQUE,
                pin_hash     TEXT NOT NULL,
                display_name TEXT,
                personality  TEXT DEFAULT '',
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS chats (
                id         SERIAL PRIMARY KEY,
                session_id TEXT NOT NULL UNIQUE,
                user_id    INTEGER NOT NULL DEFAULT 0,
                title      TEXT DEFAULT 'Nueva conversacion',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS messages (
                id         SERIAL PRIMARY KEY,
                session_id TEXT NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                model      TEXT,
                timestamp  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
        else:
            c.execute('''CREATE TABLE IF NOT EXISTS users (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                username     TEXT NOT NULL UNIQUE COLLATE NOCASE,
                pin_hash     TEXT NOT NULL,
                display_name TEXT,
                personality  TEXT DEFAULT '',
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS chats (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL UNIQUE,
                user_id    INTEGER NOT NULL DEFAULT 0,
                title      TEXT DEFAULT 'Nueva conversacion',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                model      TEXT,
                timestamp  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
        conn.commit()
        print("[DB] ✅ Tablas listas")
    except Exception as e:
        print(f"[DB] ❌ Error init_db: {e}")
        conn.rollback()
    finally:
        conn.close()

init_db()

# ── USER HELPERS ──────────────────────────────────────────────────────────────

def get_user_by_username(username):
    conn = get_db()
    c    = conn.cursor()
    if DB_TYPE == "postgres":
        c.execute("SELECT id, username, pin_hash, display_name, personality FROM users WHERE LOWER(username)=LOWER(%s)", (username,))
    else:
        c.execute("SELECT id, username, pin_hash, display_name, personality FROM users WHERE username=? COLLATE NOCASE", (username,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "username": row[1], "pin_hash": row[2],
                "display_name": row[3] or row[1], "personality": row[4] or ""}
    return None

def get_user_by_id(user_id):
    conn = get_db()
    c    = conn.cursor()
    c.execute(f"SELECT id, username, pin_hash, display_name, personality FROM users WHERE id={PH}", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "username": row[1], "pin_hash": row[2],
                "display_name": row[3] or row[1], "personality": row[4] or ""}
    return None

def current_user():
    uid = session.get("user_id")
    if uid:
        return get_user_by_id(uid)
    return None

# ── CHAT HELPERS ──────────────────────────────────────────────────────────────

def save_message(session_id, role, content, model=None, user_id=0):
    conn = get_db()
    c    = conn.cursor()
    try:
        if DB_TYPE == "postgres":
            c.execute(f"INSERT INTO chats (session_id, user_id) VALUES ({PH},{PH}) ON CONFLICT (session_id) DO NOTHING",
                      (session_id, user_id))
        else:
            c.execute("INSERT OR IGNORE INTO chats (session_id, user_id) VALUES (?,?)", (session_id, user_id))

        c.execute(f"INSERT INTO messages (session_id, role, content, model) VALUES ({PH},{PH},{PH},{PH})",
                  (session_id, role, content, model))
        c.execute(f"UPDATE chats SET updated_at=CURRENT_TIMESTAMP WHERE session_id={PH}", (session_id,))
        c.execute(f"SELECT title FROM chats WHERE session_id={PH}", (session_id,))
        row = c.fetchone()
        if row and row[0] == 'Nueva conversacion' and role == 'user':
            title = content[:45] + ("..." if len(content) > 45 else "")
            c.execute(f"UPDATE chats SET title={PH} WHERE session_id={PH}", (title, session_id))
        conn.commit()
    except Exception as e:
        print(f"[DB] ❌ save_message error: {e}")
        conn.rollback()
    finally:
        conn.close()

def get_history(session_id, limit=30):
    conn = get_db()
    c    = conn.cursor()
    c.execute(f"SELECT role, content FROM messages WHERE session_id={PH} ORDER BY timestamp DESC LIMIT {PH}",
              (session_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": r, "content": ct} for r, ct in reversed(rows)]

def build_system_prompt(user):
    name   = user["display_name"] if user else "usuario"
    custom = user["personality"].strip() if user and user["personality"] else ""
    base   = (
        f"Eres CesarIA, el asistente personal inteligente de {name}. "
        "Eres directo, útil, amigable y sarcástico cuando corresponde. "
        "Recuerdas el contexto completo de la conversación incluyendo imágenes y archivos enviados. "
        "Cuando recibes resultados de búsqueda web, los usas para dar respuestas actualizadas y precisas. "
        "Cuando recibes contenido de archivos, lo analizas en detalle y respondes sobre él."
    )
    return f"{base}\n\n{custom}" if custom else base

# ── PROVIDER HELPERS ──────────────────────────────────────────────────────────

def get_provider(model):
    for name, data in PROVIDERS.items():
        if model in data["models"]:
            return name, data
    return "groq", PROVIDERS["groq"]

def build_headers(provider_name, provider):
    h = {"Authorization": f"Bearer {provider['api_key']}", "Content-Type": "application/json"}
    return h

def call_provider_sync(provider_name, provider, body):
    headers = build_headers(provider_name, provider)
    url     = f"{provider['base_url']}/chat/completions"
    resp    = httpx.post(url, json=body, headers=headers, timeout=60)
    if resp.status_code != 200:
        print(f"[{provider_name.upper()}] ❌ HTTP {resp.status_code} — {resp.text[:300]}")
    return resp

def call_with_fallback(body):
    model         = body.get("model", "llama-3.3-70b-versatile")
    provider_name, provider = get_provider(model)
    try:
        resp = call_provider_sync(provider_name, provider, body)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("choices") and data["choices"][0].get("message", {}).get("content"):
                return resp, provider_name
    except Exception as e:
        print(f"[{provider_name.upper()}] ❌ excepción: {e}")

    for fb_name in FALLBACK_ORDER:
        if fb_name == provider_name:
            continue
        fb      = PROVIDERS[fb_name]
        fb_body = {**body, "model": fb["models"][0]}
        try:
            resp = call_provider_sync(fb_name, fb, fb_body)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("choices") and data["choices"][0].get("message", {}).get("content"):
                    print(f"[FALLBACK] ✅ usando {fb_name}")
                    return resp, fb_name
        except Exception as e:
            print(f"[FALLBACK {fb_name.upper()}] ❌ excepción: {e}")
            continue
    return None, None

def build_messages_with_image(session_id, new_text, system_prompt):
    history  = get_history(session_id, limit=20)
    messages = [{"role": "system", "content": system_prompt}]
    img_data = image_sessions.get(session_id)
    if img_data and history:
        first = True
        for msg in history:
            if msg["role"] == "user" and first and img_data:
                messages.append({"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{img_data['mime']};base64,{img_data['b64']}"}},
                    {"type": "text", "text": msg["content"]}
                ]})
                first = False
            else:
                messages.append(msg)
        messages.append({"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{img_data['mime']};base64,{img_data['b64']}"}},
            {"type": "text", "text": new_text}
        ]})
    else:
        messages += history
        messages.append({"role": "user", "content": new_text})
    return messages

# ── WEB SEARCH (TAVILY) ───────────────────────────────────────────────────────

def web_search(query: str, max_results: int = 5) -> dict:
    """Busca en la web usando Tavily y retorna resultados estructurados."""
    if not TAVILY_API_KEY:
        return {"error": "TAVILY_API_KEY no configurada", "results": []}
    try:
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "search_depth": "basic",
                "max_results": max_results,
                "include_answer": True,
                "include_raw_content": False
            },
            timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            results = []
            for r in data.get("results", []):
                results.append({
                    "title":   r.get("title", ""),
                    "url":     r.get("url", ""),
                    "snippet": r.get("content", "")[:400]
                })
            return {
                "answer":  data.get("answer", ""),
                "results": results,
                "query":   query
            }
        else:
            print(f"[SEARCH] ❌ Tavily HTTP {resp.status_code} — {resp.text[:200]}")
            return {"error": f"Error {resp.status_code}", "results": []}
    except Exception as e:
        print(f"[SEARCH] ❌ excepción: {e}")
        return {"error": str(e), "results": []}

def format_search_context(search_data: dict) -> str:
    """Convierte resultados de búsqueda en texto para el contexto del modelo."""
    if search_data.get("error"):
        return f"[Búsqueda web falló: {search_data['error']}]"

    lines = [f"🔍 Resultados de búsqueda para: \"{search_data.get('query', '')}\""]

    if search_data.get("answer"):
        lines.append(f"\n📌 Respuesta directa: {search_data['answer']}")

    for i, r in enumerate(search_data.get("results", []), 1):
        lines.append(f"\n[{i}] {r['title']}")
        lines.append(f"URL: {r['url']}")
        lines.append(f"{r['snippet']}")

    lines.append(f"\n[Fecha de búsqueda: {time.strftime('%Y-%m-%d %H:%M UTC')}]")
    return "\n".join(lines)

# ── FILE PROCESSING ───────────────────────────────────────────────────────────

TEXT_EXTENSIONS = {
    '.txt', '.md', '.py', '.js', '.ts', '.jsx', '.tsx', '.html', '.htm',
    '.css', '.json', '.xml', '.yaml', '.yml', '.csv', '.env', '.sh',
    '.bash', '.sql', '.rs', '.go', '.java', '.c', '.cpp', '.h', '.hpp',
    '.php', '.rb', '.swift', '.kt', '.dart', '.vue', '.svelte', '.toml',
    '.ini', '.cfg', '.conf', '.log', '.gitignore', '.dockerfile', '.tf'
}

def extract_text_from_file(file_bytes: bytes, filename: str) -> str:
    """Extrae texto de un archivo según su extensión."""
    ext = os.path.splitext(filename.lower())[1]

    # PDF
    if ext == '.pdf':
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(file_bytes))
            pages = []
            for i, page in enumerate(reader.pages[:30]):  # máx 30 páginas
                text = page.extract_text()
                if text.strip():
                    pages.append(f"--- Página {i+1} ---\n{text}")
            return "\n\n".join(pages) if pages else "[PDF sin texto extraíble]"
        except ImportError:
            return "[PDF: instala pypdf para leer PDFs — pip install pypdf]"
        except Exception as e:
            return f"[Error leyendo PDF: {e}]"

    # Texto plano / código
    if ext in TEXT_EXTENSIONS or ext == '':
        for encoding in ['utf-8', 'latin-1', 'cp1252']:
            try:
                return file_bytes.decode(encoding)
            except UnicodeDecodeError:
                continue
        return "[Archivo binario — no se puede leer como texto]"

    # Fallback
    try:
        return file_bytes.decode('utf-8')
    except Exception:
        return f"[Archivo {ext} no soportado para lectura de texto]"

def process_zip(zip_bytes: bytes) -> dict:
    """Descomprime un ZIP y retorna la lista de archivos con su contenido."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return {"error": "El archivo no es un ZIP válido", "files": []}

    all_names = zf.namelist()
    # Filtrar directorios y archivos del sistema
    file_names = [n for n in all_names if not n.endswith('/') and
                  not os.path.basename(n).startswith('.') and
                  '__MACOSX' not in n and 'node_modules' not in n]

    files_info = []
    total_text_size = 0
    MAX_TEXT_TOTAL = 80_000  # caracteres

    for name in file_names[:50]:  # máx 50 archivos
        ext = os.path.splitext(name.lower())[1]
        info = zf.getinfo(name)
        file_entry = {
            "name": name,
            "size": info.file_size,
            "ext":  ext,
            "text": None
        }

        # Leer texto si es archivo de texto y no supera el límite
        if ext in TEXT_EXTENSIONS and info.file_size < 200_000 and total_text_size < MAX_TEXT_TOTAL:
            try:
                raw = zf.read(name)
                text = extract_text_from_file(raw, name)
                file_entry["text"] = text
                total_text_size += len(text)
            except Exception as e:
                file_entry["text"] = f"[Error leyendo: {e}]"

        files_info.append(file_entry)

    zf.close()
    return {
        "total_files":   len(all_names),
        "readable_files": len(file_names),
        "files": files_info
    }

def format_zip_context(zip_data: dict, selected_files: list = None) -> str:
    """Formatea el contenido del ZIP para el contexto del modelo."""
    if zip_data.get("error"):
        return f"[Error en ZIP: {zip_data['error']}]"

    files = zip_data["files"]
    if selected_files:
        files = [f for f in files if f["name"] in selected_files]

    lines = [f"📦 Contenido del ZIP ({zip_data['readable_files']} archivos):"]
    for f in files:
        size_kb = f["size"] / 1024
        lines.append(f"\n📄 {f['name']} ({size_kb:.1f} KB)")
        if f.get("text"):
            preview = f["text"][:3000]
            if len(f["text"]) > 3000:
                preview += f"\n... [truncado, {len(f['text'])} chars total]"
            lines.append(f"```{f['ext'].lstrip('.')}\n{preview}\n```")
        else:
            lines.append("[archivo binario]")

    return "\n".join(lines)

# ── AUTH ROUTES ───────────────────────────────────────────────────────────────

@app.route("/api/auth/register", methods=["POST"])
def register():
    data     = request.json or {}
    username = (data.get("username") or "").strip()
    pin      = (data.get("pin") or "").strip()
    display  = (data.get("display_name") or username).strip()

    if not username or not pin:
        return jsonify({"error": "Usuario y PIN son requeridos"}), 400
    if len(pin) < 4:
        return jsonify({"error": "El PIN debe tener al menos 4 dígitos"}), 400
    if len(username) < 3:
        return jsonify({"error": "Usuario debe tener al menos 3 caracteres"}), 400
    if get_user_by_username(username):
        return jsonify({"error": "Ese usuario ya existe"}), 409

    conn = get_db()
    c    = conn.cursor()
    try:
        c.execute(f"INSERT INTO users (username, pin_hash, display_name) VALUES ({PH},{PH},{PH})",
                  (username, hash_pin(pin), display))
        conn.commit()
        if DB_TYPE == "postgres":
            c.execute("SELECT lastval()")
        else:
            c.execute("SELECT last_insert_rowid()")
        new_id = c.fetchone()[0]
        session["user_id"] = new_id
        return jsonify({"ok": True, "user": {"id": new_id, "username": username, "display_name": display}})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route("/api/auth/login", methods=["POST"])
def login():
    data     = request.json or {}
    username = (data.get("username") or "").strip()
    pin      = (data.get("pin") or "").strip()

    user = get_user_by_username(username)
    if not user or user["pin_hash"] != hash_pin(pin):
        return jsonify({"error": "Usuario o PIN incorrecto"}), 401

    session["user_id"] = user["id"]
    return jsonify({"ok": True, "user": {
        "id": user["id"], "username": user["username"], "display_name": user["display_name"]
    }})

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/auth/me", methods=["GET"])
def me():
    user = current_user()
    if not user:
        return jsonify({"error": "No autenticado"}), 401
    return jsonify({"id": user["id"], "username": user["username"], "display_name": user["display_name"]})

@app.route("/v1/models", methods=["GET"])
def list_models():
    if not request.headers.get("Authorization") == f"Bearer {API_KEY}":
        return jsonify({"error": "Unauthorized"}), 401
    models = []
    for provider, data in PROVIDERS.items():
        for model in data["models"]:
            models.append({"id": model, "object": "model", "owned_by": provider})
    return jsonify({"object": "list", "data": models})

# ── CHAT ──────────────────────────────────────────────────────────────────────

@app.route("/v1/chat/completions", methods=["POST"])
def chat():
    if not request.headers.get("Authorization") == f"Bearer {API_KEY}":
        return jsonify({"error": "Unauthorized"}), 401

    user    = current_user()
    user_id = user["id"] if user else 0

    ip = request.remote_addr
    if not check_rate_limit(ip):
        return jsonify({"error": "Rate limit excedido"}), 429

    body       = request.json
    stream     = body.get("stream", False)
    session_id = body.pop("session_id", f"anon_{user_id}")
    user_text  = body.pop("user_text", None)
    # Soporte para búsqueda web inyectada desde el frontend
    web_context = body.pop("web_context", None)

    system_prompt = build_system_prompt(user)

    if user_text and session_id in image_sessions:
        save_message(session_id, "user", user_text, body.get("model"), user_id)
        body["messages"] = build_messages_with_image(session_id, user_text, system_prompt)
        body["model"]    = "gemini-2.0-flash"
    else:
        messages  = body.get("messages", [])
        user_msgs = [m for m in messages if m["role"] == "user"]
        if user_msgs:
            last = user_msgs[-1]["content"]
            if isinstance(last, list):
                last = " ".join(p["text"] for p in last if p.get("type") == "text")
            save_message(session_id, "user", last, body.get("model"), user_id)

        # Inyectar contexto web si existe
        if web_context:
            sys_with_web = system_prompt + f"\n\n{web_context}"
        else:
            sys_with_web = system_prompt

        if not any(m["role"] == "system" for m in messages):
            body["messages"] = [{"role": "system", "content": sys_with_web}] + messages
        else:
            # Actualizar system prompt existente con contexto web
            for m in body["messages"]:
                if m["role"] == "system":
                    m["content"] = sys_with_web
                    break

    model         = body.get("model", "llama-3.3-70b-versatile")
    provider_name, provider = get_provider(model)

    print(f"[CHAT] modelo={model} proveedor={provider_name} stream={stream} web={'si' if web_context else 'no'}")

    if stream:
        def generate():
            headers       = build_headers(provider_name, provider)
            full_response = ""
            try:
                with httpx.stream("POST", f"{provider['base_url']}/chat/completions",
                                  json=body, headers=headers, timeout=90) as resp:
                    if resp.status_code != 200:
                        err_body = resp.read().decode()
                        print(f"[{provider_name.upper()}] ❌ stream HTTP {resp.status_code} — {err_body[:300]}")
                        raise Exception(f"Status {resp.status_code}")
                    for line in resp.iter_lines():
                        if line.startswith("data: "):
                            data = line[6:]
                            if data == "[DONE]":
                                yield "data: [DONE]\n\n"
                                break
                            try:
                                chunk = json.loads(data)
                                delta = chunk["choices"][0]["delta"].get("content", "")
                                if delta:
                                    full_response += delta
                            except Exception:
                                pass
                            yield f"{line}\n\n"
            except Exception as e:
                print(f"[{provider_name.upper()}] ❌ stream error: {e} — intentando fallback")
                if not full_response:
                    for fb_name in FALLBACK_ORDER:
                        if fb_name == provider_name:
                            continue
                        fb         = PROVIDERS[fb_name]
                        fb_body    = {**body, "model": fb["models"][0], "stream": False}
                        fb_headers = build_headers(fb_name, fb)
                        try:
                            fb_resp = httpx.post(f"{fb['base_url']}/chat/completions",
                                                 json=fb_body, headers=fb_headers, timeout=60)
                            if fb_resp.status_code == 200:
                                content = fb_resp.json()["choices"][0]["message"]["content"]
                                if content:
                                    full_response = content
                                    print(f"[FALLBACK] ✅ stream fallback a {fb_name}")
                                    fake = {"choices": [{"delta": {"content": content}, "finish_reason": None}]}
                                    yield f"data: {json.dumps(fake)}\n\n"
                                    yield "data: [DONE]\n\n"
                                    break
                            else:
                                print(f"[FALLBACK {fb_name.upper()}] ❌ HTTP {fb_resp.status_code} — {fb_resp.text[:200]}")
                        except Exception as fe:
                            print(f"[FALLBACK {fb_name.upper()}] ❌ excepción: {fe}")
                            continue
            finally:
                if full_response:
                    save_message(session_id, "assistant", full_response, model, user_id)

        return Response(stream_with_context(generate()), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    else:
        resp, used_provider = call_with_fallback(body)
        if resp is None:
            return jsonify({"error": "Todos los proveedores fallaron"}), 503
        data = resp.json()
        try:
            save_message(session_id, "assistant",
                         data["choices"][0]["message"]["content"], model, user_id)
        except Exception:
            pass
        return jsonify(data), resp.status_code

# ── WEB SEARCH ENDPOINT ───────────────────────────────────────────────────────

@app.route("/api/search", methods=["POST"])
def search_web():
    """Endpoint para búsqueda web en tiempo real usando Tavily."""
    if not request.headers.get("Authorization") == f"Bearer {API_KEY}":
        return jsonify({"error": "Unauthorized"}), 401

    data  = request.json or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Query vacío"}), 400

    if not TAVILY_API_KEY:
        return jsonify({"error": "Búsqueda web no configurada. Agrega TAVILY_API_KEY en Railway."}), 400

    print(f"[SEARCH] 🔍 Buscando: {query}")
    results = web_search(query, max_results=data.get("max_results", 5))
    return jsonify(results)

# ── FILE UPLOAD ENDPOINT ──────────────────────────────────────────────────────

@app.route("/api/files/upload", methods=["POST"])
def upload_file():
    """
    Sube un archivo (txt, pdf, código, zip) y extrae su contenido.
    Para ZIPs, retorna la lista de archivos y espera que el frontend
    envíe la acción seleccionada.
    """
    if not request.headers.get("Authorization") == f"Bearer {API_KEY}":
        return jsonify({"error": "Unauthorized"}), 401

    uploaded = request.files.get("file")
    if not uploaded:
        return jsonify({"error": "No se envió ningún archivo"}), 400

    filename  = uploaded.filename or "archivo"
    ext       = os.path.splitext(filename.lower())[1]
    file_bytes = uploaded.read()
    size_kb    = len(file_bytes) / 1024

    print(f"[FILE] 📁 Recibido: {filename} ({size_kb:.1f} KB)")

    # ── ZIP ────────────────────────────────────────────────────────────────────
    if ext == '.zip':
        zip_data = process_zip(file_bytes)
        if zip_data.get("error"):
            return jsonify({"error": zip_data["error"]}), 400

        # Construir lista de archivos para mostrar al usuario
        file_list = []
        for f in zip_data["files"]:
            file_list.append({
                "name":     f["name"],
                "size_kb":  round(f["size"] / 1024, 1),
                "ext":      f["ext"],
                "readable": f.get("text") is not None
            })

        return jsonify({
            "type":          "zip",
            "filename":      filename,
            "total_files":   zip_data["total_files"],
            "readable_files": zip_data["readable_files"],
            "files":         file_list,
            # Guardar el contenido serializado para uso posterior
            "zip_content":   json.dumps([
                {"name": f["name"], "size": f["size"], "ext": f["ext"], "text": f.get("text")}
                for f in zip_data["files"]
            ])
        })

    # ── PDF ────────────────────────────────────────────────────────────────────
    elif ext == '.pdf':
        text = extract_text_from_file(file_bytes, filename)
        char_count = len(text)
        return jsonify({
            "type":       "pdf",
            "filename":   filename,
            "size_kb":    round(size_kb, 1),
            "char_count": char_count,
            "content":    text[:100_000],  # límite de seguridad
            "truncated":  char_count > 100_000
        })

    # ── TEXTO / CÓDIGO ────────────────────────────────────────────────────────
    elif ext in TEXT_EXTENSIONS or size_kb < 500:
        text = extract_text_from_file(file_bytes, filename)
        char_count = len(text)
        return jsonify({
            "type":       "text",
            "filename":   filename,
            "size_kb":    round(size_kb, 1),
            "ext":        ext,
            "char_count": char_count,
            "content":    text[:100_000],
            "truncated":  char_count > 100_000
        })

    else:
        return jsonify({"error": f"Tipo de archivo '{ext}' no soportado"}), 415

# ── IMAGE ─────────────────────────────────────────────────────────────────────

@app.route("/api/image/engines", methods=["GET"])
def list_engines():
    if not request.headers.get("Authorization") == f"Bearer {API_KEY}":
        return jsonify({"error": "Unauthorized"}), 401
    hf_key = bool(os.environ.get("HF_API_KEY", ""))
    engines = []
    for key, eng in IMAGE_ENGINES.items():
        if eng["key"] and not hf_key:
            continue
        engines.append({"id": key, "label": eng["label"], "provider": eng["provider"]})
    return jsonify(engines)

@app.route("/api/image/proxy", methods=["GET"])
def image_proxy():
    url = request.args.get("url", "")
    if not (url.startswith("https://image.pollinations.ai") or url.startswith("https://gen.pollinations.ai")):
        return jsonify({"error": "URL no permitida"}), 403
    try:
        resp = httpx.get(url, timeout=60, follow_redirects=True)
        return Response(resp.content, content_type=resp.headers.get("content-type", "image/jpeg"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/image/generate", methods=["POST"])
def generate_image():
    if not request.headers.get("Authorization") == f"Bearer {API_KEY}":
        return jsonify({"error": "Unauthorized"}), 401

    body        = request.json
    prompt      = (body.get("prompt") or "").strip()
    style       = body.get("style", "")
    aspect      = body.get("aspect", "1:1")
    quality     = body.get("quality", "normal")
    engine_key  = body.get("engine", "flux-schnell")

    if not prompt:
        return jsonify({"error": "Prompt vacío"}), 400

    full_prompt = f"{prompt}, {style}" if style else prompt
    if quality == "hd":
        full_prompt += ", ultra detailed, high quality, 8k, sharp"

    engine = IMAGE_ENGINES.get(engine_key, IMAGE_ENGINES["flux-schnell"])
    aspect_map = {"1:1":(1024,1024), "16:9":(1280,720), "9:16":(720,1280), "4:3":(1024,768)}
    width, height = aspect_map.get(aspect, (1024, 1024))

    if engine["provider"] == "pollinations":
        import urllib.parse
        seed     = int(time.time()) % 99999
        poll_url = (
            f"https://image.pollinations.ai/prompt/{urllib.parse.quote(full_prompt)}"
            f"?width={width}&height={height}&seed={seed}&nologo=true&model={engine['model']}"
        )
        print(f"[IMG] Pollinations/{engine['model']} → descargando...")
        try:
            img_resp = httpx.get(poll_url, timeout=90, follow_redirects=True)
            if img_resp.status_code == 200:
                mime     = img_resp.headers.get("content-type", "image/jpeg")
                img_b64  = base64.b64encode(img_resp.content).decode()
                print(f"[IMG] ✅ Pollinations {engine['model']} — {len(img_resp.content)//1024}KB")
                return jsonify({
                    "data":     [{"url": f"data:{mime};base64,{img_b64}", "b64": img_b64}],
                    "prompt":   full_prompt,
                    "provider": "pollinations",
                    "label":    engine["label"],
                    "model":    engine["model"]
                })
            print(f"[IMG] ❌ Pollinations HTTP {img_resp.status_code}")
        except Exception as e:
            print(f"[IMG] ❌ Pollinations excepción: {e}")

    elif engine["provider"] == "huggingface":
        hf_key = os.environ.get("HF_API_KEY", "")
        if not hf_key:
            return jsonify({"error": "HF_API_KEY no configurada en Railway"}), 400
        hf_url = f"https://api-inference.huggingface.co/models/{engine['model']}"
        hf_body = {"inputs": full_prompt, "parameters": {"width": width, "height": height}}
        print(f"[IMG] HuggingFace/{engine['model']} → generando...")
        try:
            resp = httpx.post(hf_url,
                json=hf_body,
                headers={"Authorization": f"Bearer {hf_key}", "Content-Type": "application/json"},
                timeout=120)
            if resp.status_code == 200 and resp.headers.get("content-type","").startswith("image/"):
                mime    = resp.headers.get("content-type", "image/jpeg")
                img_b64 = base64.b64encode(resp.content).decode()
                print(f"[IMG] ✅ HuggingFace {engine['model']} — {len(resp.content)//1024}KB")
                return jsonify({
                    "data":     [{"url": f"data:{mime};base64,{img_b64}", "b64": img_b64}],
                    "prompt":   full_prompt,
                    "provider": "huggingface",
                    "label":    engine["label"],
                    "model":    engine["model"]
                })
            err_text = resp.text[:200]
            print(f"[IMG] ❌ HuggingFace HTTP {resp.status_code} — {err_text}")
            return jsonify({"error": f"HuggingFace: {err_text}"}), 502
        except Exception as e:
            print(f"[IMG] ❌ HuggingFace excepción: {e}")
            return jsonify({"error": str(e)}), 500

    return jsonify({"error": "Motor no disponible"}), 500

@app.route("/api/image/analyze", methods=["POST"])
def analyze_image():
    if not request.headers.get("Authorization") == f"Bearer {API_KEY}":
        return jsonify({"error": "Unauthorized"}), 401
    user       = current_user()
    user_id    = user["id"] if user else 0
    session_id = request.form.get("session_id", f"anon_{user_id}")
    question   = request.form.get("question", "Describe esta imagen en detalle")
    image_file = request.files.get("image")
    if not image_file:
        return jsonify({"error": "No se envió imagen"}), 400

    img_bytes  = image_file.read()
    mime_type  = image_file.content_type or "image/jpeg"
    image_b64  = base64.b64encode(img_bytes).decode("utf-8")
    image_sessions[session_id] = {"b64": image_b64, "mime": mime_type, "timestamp": time.time()}

    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}},
        {"type": "text", "text": question}
    ]}]

    for pname, vmodel in VISION_PROVIDERS:
        try:
            provider = PROVIDERS[pname]
            resp = httpx.post(f"{provider['base_url']}/chat/completions",
                              json={"model": vmodel, "messages": messages, "max_tokens": 1500},
                              headers=build_headers(pname, provider), timeout=60)
            if resp.status_code == 200:
                result = resp.json()["choices"][0]["message"]["content"]
                if result:
                    save_message(session_id, "user",      f"[Imagen enviada] {question}", user_id=user_id)
                    save_message(session_id, "assistant", result, vmodel, user_id)
                    return jsonify({"result": result, "model": vmodel})
            else:
                print(f"[VISION {pname.upper()}] ❌ HTTP {resp.status_code} — {resp.text[:200]}")
        except Exception as e:
            print(f"[VISION {pname.upper()}] ❌ excepción: {e}")
            continue
    return jsonify({"error": "No se pudo analizar la imagen"}), 500

# ── CHATS ─────────────────────────────────────────────────────────────────────

@app.route("/api/chats", methods=["GET"])
def get_chats():
    if not request.headers.get("Authorization") == f"Bearer {API_KEY}":
        return jsonify({"error": "Unauthorized"}), 401
    user    = current_user()
    user_id = user["id"] if user else 0
    conn = get_db()
    c    = conn.cursor()
    c.execute(f"""SELECT session_id, title, updated_at FROM chats
                 WHERE user_id={PH} AND session_id IN (SELECT DISTINCT session_id FROM messages)
                 ORDER BY updated_at DESC LIMIT 50""", (user_id,))
    chats = [{"session_id": r[0], "title": r[1], "updated_at": r[2]} for r in c.fetchall()]
    conn.close()
    return jsonify(chats)

@app.route("/api/chats/<session_id>", methods=["GET"])
def get_chat_messages(session_id):
    if not request.headers.get("Authorization") == f"Bearer {API_KEY}":
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(get_history(session_id, limit=100))

@app.route("/api/chats/<session_id>", methods=["DELETE"])
def delete_chat(session_id):
    if not request.headers.get("Authorization") == f"Bearer {API_KEY}":
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    c    = conn.cursor()
    c.execute(f"DELETE FROM messages WHERE session_id={PH}", (session_id,))
    c.execute(f"DELETE FROM chats WHERE session_id={PH}", (session_id,))
    conn.commit()
    conn.close()
    image_sessions.pop(session_id, None)
    return jsonify({"ok": True})

@app.route("/api/chats/new", methods=["POST"])
def new_chat():
    if not request.headers.get("Authorization") == f"Bearer {API_KEY}":
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"session_id": f"chat_{int(time.time() * 1000)}"})

# ── PROFILE ───────────────────────────────────────────────────────────────────

@app.route("/api/profile", methods=["GET"])
def get_profile_route():
    if not request.headers.get("Authorization") == f"Bearer {API_KEY}":
        return jsonify({"error": "Unauthorized"}), 401
    user = current_user()
    if not user:
        return jsonify({"name": "Usuario", "assistant_name": "CesarIA", "personality": ""})
    return jsonify({"name": user["display_name"], "assistant_name": "CesarIA",
                    "personality": user["personality"]})

@app.route("/api/profile", methods=["POST"])
def update_profile():
    if not request.headers.get("Authorization") == f"Bearer {API_KEY}":
        return jsonify({"error": "Unauthorized"}), 401
    user = current_user()
    if not user:
        return jsonify({"error": "No autenticado"}), 401
    data = request.json or {}
    conn = get_db()
    c    = conn.cursor()
    if "name" in data:
        c.execute(f"UPDATE users SET display_name={PH} WHERE id={PH}", (data["name"], user["id"]))
    if "personality" in data:
        c.execute(f"UPDATE users SET personality={PH} WHERE id={PH}", (data["personality"], user["id"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ── STATS ─────────────────────────────────────────────────────────────────────

@app.route("/api/stats", methods=["GET"])
def get_stats():
    if not request.headers.get("Authorization") == f"Bearer {API_KEY}":
        return jsonify({"error": "Unauthorized"}), 401
    user    = current_user()
    user_id = user["id"] if user else 0
    conn = get_db()
    c    = conn.cursor()
    c.execute(f"""SELECT COUNT(*) FROM messages WHERE session_id IN
                 (SELECT session_id FROM chats WHERE user_id={PH})""", (user_id,))
    total_msgs = c.fetchone()[0]
    c.execute(f"""SELECT COUNT(*) FROM chats WHERE user_id={PH}
                 AND session_id IN (SELECT DISTINCT session_id FROM messages)""", (user_id,))
    total_chats = c.fetchone()[0]
    conn.close()
    return jsonify({"total_messages": total_msgs, "total_chats": total_chats})

# ── SERVE HTML ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    with open(HTML_PATH, encoding="utf-8") as f:
        return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8317))
    print(f"[CesarIA] corriendo en http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
