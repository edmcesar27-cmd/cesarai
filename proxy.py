from flask import Flask, request, jsonify, Response, stream_with_context, session
import httpx
import sqlite3
import json
import time
import os
import base64
import hashlib
import secrets
from collections import defaultdict

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

API_KEY   = os.environ.get("API_KEY", "test123")
HTML_PATH = os.path.join(os.path.dirname(__file__), 'index.html')
DB_PATH   = os.path.join(os.path.dirname(__file__), 'cesarai.db')

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

PROVIDERS = {
    # ✅ GROQ — Ultra rápido, 100% gratis, sin tarjeta
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
    # ✅ GEMINI — Google AI Studio, 500 req/día gratis, sin tarjeta
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
    # ✅ MISTRAL — Free tier (solo necesita número de teléfono, sin tarjeta)
    # Registrate en: console.mistral.ai -> Experiment plan
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
    """Crea las tablas si no existen. Se llama a nivel módulo (funciona con gunicorn)."""
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

# ── LLAMAR init_db() A NIVEL MÓDULO ──────────────────────────────────────────
# CRÍTICO: esto asegura que las tablas existen tanto con gunicorn como con python proxy.py
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
        "Recuerdas el contexto completo de la conversación incluyendo imágenes enviadas."
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
        user_id = c.fetchone()[0]
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"error": str(e)}), 500
    conn.close()

    session["user_id"] = user_id
    return jsonify({"ok": True, "user": {"id": user_id, "username": username, "display_name": display}})

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

# ── MODELS ────────────────────────────────────────────────────────────────────

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
        if not any(m["role"] == "system" for m in messages):
            body["messages"] = [{"role": "system", "content": system_prompt}] + messages

    model         = body.get("model", "llama-3.3-70b-versatile")
    provider_name, provider = get_provider(model)

    print(f"[CHAT] modelo={model} proveedor={provider_name} stream={stream}")

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

# ── IMAGE ─────────────────────────────────────────────────────────────────────

@app.route("/api/image/proxy", methods=["GET"])
def image_proxy():
    url = request.args.get("url", "")
    if not url.startswith("https://image.pollinations.ai"):
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
    aspect      = body.get("aspect", "1:1")   # 1:1 | 16:9 | 9:16 | 4:3
    quality     = body.get("quality", "normal") # normal | hd

    if not prompt:
        return jsonify({"error": "Prompt vacío"}), 400

    # Enriquecer prompt con estilo si se especificó
    full_prompt = f"{prompt}, {style}" if style else prompt
    if quality == "hd":
        full_prompt += ", ultra detailed, high quality, 8k"

    # ── 1) Intentar Gemini imagen (nativo, 500/día gratis) ─────────────────
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if gemini_key:
        try:
            gemini_url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-2.0-flash-exp:generateContent"
            )
            gem_body = {
                "contents": [{"parts": [{"text": f"Generate an image: {full_prompt}"}]}],
                "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]}
            }
            gem_resp = httpx.post(
                gemini_url,
                json=gem_body,
                headers={"Content-Type": "application/json"},
                params={"key": gemini_key},
                timeout=60
            )
            if gem_resp.status_code == 200:
                gdata = gem_resp.json()
                parts = gdata.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                for part in parts:
                    if part.get("inlineData", {}).get("mimeType", "").startswith("image/"):
                        img_b64  = part["inlineData"]["data"]
                        mime     = part["inlineData"]["mimeType"]
                        data_url = f"data:{mime};base64,{img_b64}"
                        print(f"[GEMINI IMG] ✅ imagen generada con gemini-2.0-flash-exp")
                        return jsonify({
                            "data":     [{"url": data_url, "b64": img_b64}],
                            "prompt":   full_prompt,
                            "provider": "gemini",
                            "model":    "gemini-2.0-flash-exp"
                        })
            else:
                print(f"[GEMINI IMG] ❌ HTTP {gem_resp.status_code} — {gem_resp.text[:200]}")
        except Exception as e:
            print(f"[GEMINI IMG] ❌ excepción: {e}")

    # ── 2) Fallback: Pollinations (sin key, siempre disponible) ───────────
    import urllib.parse
    aspect_map  = {"1:1": (1024,1024), "16:9": (1280,720), "9:16": (720,1280), "4:3": (1024,768)}
    width, height = aspect_map.get(aspect, (1024, 1024))
    seed        = int(time.time()) % 99999
    model_param = "flux" if quality == "hd" else "turbo"
    img_url     = (
        f"https://image.pollinations.ai/prompt/{urllib.parse.quote(full_prompt)}"
        f"?width={width}&height={height}&seed={seed}&nologo=true&model={model_param}"
    )
    print(f"[POLLINATIONS] fallback → {img_url}")
    return jsonify({
        "data":     [{"url": img_url}],
        "prompt":   full_prompt,
        "provider": "pollinations",
        "model":    model_param
    })

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
