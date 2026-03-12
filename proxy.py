from flask import Flask, request, jsonify, Response, stream_with_context, send_file
import httpx
import sqlite3
import json
import time
import os
import base64
import io
from collections import defaultdict

app = Flask(__name__)

API_KEY = "test123"
DB_PATH = "/data/data/com.termux/files/home/miproxy/cesarai.db"
HTML_PATH = "/data/data/com.termux/files/home/miproxy/index.html"

image_sessions = {}

PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": os.environ.get("GROQ_API_KEY", ""),
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"],
        "type": "openai"
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": os.environ.get("OPENROUTER_API_KEY", ""),
        "models": ["meta-llama/llama-3.3-70b-instruct:free", "mistralai/mistral-7b-instruct:free", "google/gemma-3-27b-it:free"],
        "type": "openai"
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key": os.environ.get("GEMINI_API_KEY", ""),
        "models": ["gemini-2.5-flash", "gemini-2.0-flash"],
        "type": "openai"
    }
}

VISION_PROVIDERS = [
    ("groq", "llama-3.2-11b-vision-preview"),
    ("gemini", "gemini-2.5-flash"),
    ("openrouter", "google/gemini-2.0-flash-exp:free"),
]

FALLBACK_ORDER = ["groq", "gemini", "openrouter"]

rate_data = defaultdict(list)
RATE_LIMIT = 60

def check_rate_limit(ip):
    now = time.time()
    rate_data[ip] = [t for t in rate_data[ip] if now - t < 60]
    if len(rate_data[ip]) >= RATE_LIMIT:
        return False
    rate_data[ip].append(now)
    return True

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL UNIQUE,
        title TEXT DEFAULT 'Nueva conversacion',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        model TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_profile (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    defaults = [
        ("name", "Cesar"),
        ("assistant_name", "CesarIA"),
        ("personality", "Eres CesarIA, el asistente personal inteligente de Cesar. Eres directo, util, amigable y sarcastico cuando corresponde. Recuerdas el contexto completo de la conversacion incluyendo imagenes enviadas."),
        ("theme", "dark")
    ]
    for key, val in defaults:
        c.execute("INSERT OR IGNORE INTO user_profile VALUES (?,?)", (key, val))
    conn.commit()
    conn.close()

def get_db():
    return sqlite3.connect(DB_PATH)

def save_message(session_id, role, content, model=None):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO chats (session_id) VALUES (?)", (session_id,))
    c.execute("INSERT INTO messages (session_id, role, content, model) VALUES (?,?,?,?)",
              (session_id, role, content, model))
    c.execute("UPDATE chats SET updated_at=CURRENT_TIMESTAMP WHERE session_id=?", (session_id,))
    c.execute("SELECT title FROM chats WHERE session_id=?", (session_id,))
    row = c.fetchone()
    if row and row[0] == 'Nueva conversacion' and role == 'user':
        title = content[:45] + ("..." if len(content) > 45 else "")
        c.execute("UPDATE chats SET title=? WHERE session_id=?", (title, session_id))
    conn.commit()
    conn.close()

def get_history(session_id, limit=30):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT role, content FROM messages WHERE session_id=? ORDER BY timestamp DESC LIMIT ?",
              (session_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": r, "content": ct} for r, ct in reversed(rows)]

def get_profile():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT key, value FROM user_profile")
    profile = dict(c.fetchall())
    conn.close()
    return profile

def check_auth():
    return request.headers.get("Authorization") == f"Bearer {API_KEY}"

def get_provider(model):
    for name, data in PROVIDERS.items():
        if model in data["models"]:
            return name, data
    return "groq", PROVIDERS["groq"]

def build_headers(provider_name, provider):
    headers = {
        "Authorization": f"Bearer {provider['api_key']}",
        "Content-Type": "application/json"
    }
    if provider_name == "openrouter":
        headers["HTTP-Referer"] = "https://cesarai.local"
        headers["X-Title"] = "CesarIA"
    return headers

def call_provider_sync(provider_name, provider, body):
    headers = build_headers(provider_name, provider)
    url = f"{provider['base_url']}/chat/completions"
    resp = httpx.post(url, json=body, headers=headers, timeout=60)
    return resp

def call_with_fallback(body):
    model = body.get("model", "llama-3.3-70b-versatile")
    provider_name, provider = get_provider(model)
    try:
        resp = call_provider_sync(provider_name, provider, body)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("choices") and data["choices"][0].get("message", {}).get("content"):
                return resp, provider_name
    except Exception:
        pass
    for fallback_name in FALLBACK_ORDER:
        if fallback_name == provider_name:
            continue
        fallback = PROVIDERS[fallback_name]
        fallback_body = dict(body)
        fallback_body["model"] = fallback["models"][0]
        try:
            resp = call_provider_sync(fallback_name, fallback, fallback_body)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("choices") and data["choices"][0].get("message", {}).get("content"):
                    return resp, fallback_name
        except Exception:
            continue
    return None, None

def build_messages_with_image(session_id, new_text, system_prompt):
    history = get_history(session_id, limit=20)
    messages = [{"role": "system", "content": system_prompt}]
    img_data = image_sessions.get(session_id)
    if img_data and history:
        first_user_with_image = True
        for msg in history:
            if msg["role"] == "user" and first_user_with_image and img_data:
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{img_data['mime']};base64,{img_data['b64']}"}},
                        {"type": "text", "text": msg["content"]}
                    ]
                })
                first_user_with_image = False
            else:
                messages.append(msg)
        messages.append({
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{img_data['mime']};base64,{img_data['b64']}"}},
                {"type": "text", "text": new_text}
            ]
        })
    else:
        for msg in history:
            messages.append(msg)
        messages.append({"role": "user", "content": new_text})
    return messages

@app.route("/")
def index():
    with open(HTML_PATH, encoding="utf-8") as f:
        return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/v1/models", methods=["GET"])
def list_models():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    models = []
    for provider, data in PROVIDERS.items():
        for model in data["models"]:
            models.append({"id": model, "object": "model", "owned_by": provider})
    return jsonify({"object": "list", "data": models})

@app.route("/v1/chat/completions", methods=["POST"])
def chat():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    ip = request.remote_addr
    if not check_rate_limit(ip):
        return jsonify({"error": "Rate limit excedido"}), 429
    body = request.json
    stream = body.get("stream", False)
    session_id = body.pop("session_id", "default")
    user_text = body.pop("user_text", None)

    profile = get_profile()
    system_prompt = profile.get("personality", "Eres CesarIA, un asistente personal inteligente.")

    if user_text and session_id in image_sessions:
        save_message(session_id, "user", user_text, body.get("model"))
        body["messages"] = build_messages_with_image(session_id, user_text, system_prompt)
        body["model"] = "gemini-2.5-flash"
        provider_name, provider = get_provider("gemini-2.5-flash")
        body["model"] = "gemini-2.5-flash"
        provider_name, provider = get_provider("gemini-2.5-flash")
    else:
        messages = body.get("messages", [])
        user_msgs = [m for m in messages if m["role"] == "user"]
        if user_msgs:
            last = user_msgs[-1]["content"]
            if isinstance(last, list):
                last = " ".join(p["text"] for p in last if p.get("type") == "text")
            save_message(session_id, "user", last, body.get("model"))
        if not any(m["role"] == "system" for m in messages):
            body["messages"] = [{"role": "system", "content": system_prompt}] + messages

    model = body.get("model", "llama-3.3-70b-versatile")
    provider_name, provider = get_provider(model)

    if stream:
        def generate():
            headers = build_headers(provider_name, provider)
            full_response = ""
            try:
                with httpx.stream("POST", f"{provider['base_url']}/chat/completions",
                                  json=body, headers=headers, timeout=90) as resp:
                    if resp.status_code != 200:
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
            except Exception:
                if not full_response:
                    for fb_name in FALLBACK_ORDER:
                        if fb_name == provider_name:
                            continue
                        fb = PROVIDERS[fb_name]
                        fb_body = dict(body)
                        fb_body["model"] = fb["models"][0]
                        fb_headers = build_headers(fb_name, fb)
                        try:
                            fb_resp = httpx.post(f"{fb['base_url']}/chat/completions",
                                                 json=fb_body, headers=fb_headers, timeout=60)
                            if fb_resp.status_code == 200:
                                content = fb_resp.json()["choices"][0]["message"]["content"]
                                if content:
                                    full_response = content
                                    fake = {"choices": [{"delta": {"content": content}, "finish_reason": None}]}
                                    yield f"data: {json.dumps(fake)}\n\n"
                                    yield "data: [DONE]\n\n"
                                    break
                        except Exception:
                            continue
            finally:
                if full_response:
                    save_message(session_id, "assistant", full_response, model)
        return Response(stream_with_context(generate()), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    else:
        resp, _ = call_with_fallback(body)
        if resp is None:
            return jsonify({"error": "Todos los proveedores fallaron"}), 503
        data = resp.json()
        try:
            save_message(session_id, "assistant", data["choices"][0]["message"]["content"], model)
        except Exception:
            pass
        return jsonify(data), resp.status_code

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
@app.route("/api/image/generate", methods=["POST"])
def generate_image():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    body = request.json
    prompt = body.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "Prompt vacio"}), 400
    size = body.get("size", "1024x1024")
    width, height = 1024, 1024
    if "x" in size:
        try:
            w, h = size.split("x")
            width, height = int(w), int(h)
        except Exception:
            pass
    import urllib.parse
    prompt_enc = urllib.parse.quote(prompt)
    seed = int(time.time()) % 99999
    img_url = f"https://image.pollinations.ai/prompt/{prompt_enc}?width={width}&height={height}&seed={seed}&nologo=true"
    return jsonify({"data": [{"url": img_url}], "prompt": prompt})

@app.route("/api/image/analyze", methods=["POST"])
def analyze_image():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    session_id = request.form.get("session_id", "default")
    question = request.form.get("question", "Describe esta imagen en detalle")
    image_file = request.files.get("image")
    if not image_file:
        return jsonify({"error": "No se envio imagen"}), 400

    img_bytes = image_file.read()
    mime_type = image_file.content_type or "image/jpeg"
    image_b64 = base64.b64encode(img_bytes).decode("utf-8")

    image_sessions[session_id] = {
        "b64": image_b64,
        "mime": mime_type,
        "timestamp": time.time()
    }

    messages = [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}},
            {"type": "text", "text": question}
        ]
    }]

    for pname, vmodel in VISION_PROVIDERS:
        try:
            provider = PROVIDERS[pname]
            headers = build_headers(pname, provider)
            payload = {"model": vmodel, "messages": messages, "max_tokens": 1500}
            resp = httpx.post(f"{provider['base_url']}/chat/completions",
                              json=payload, headers=headers, timeout=60)
            if resp.status_code == 200:
                result = resp.json()["choices"][0]["message"]["content"]
                if result:
                    save_message(session_id, "user", f"[Imagen enviada] {question}")
                    save_message(session_id, "assistant", result, vmodel)
                    return jsonify({"result": result, "model": vmodel})
        except Exception:
            continue
    return jsonify({"error": "No se pudo analizar la imagen"}), 500

@app.route("/api/chats", methods=["GET"])
def get_chats():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT session_id, title, updated_at FROM chats
                 WHERE session_id IN (SELECT DISTINCT session_id FROM messages)
                 ORDER BY updated_at DESC LIMIT 50""")
    chats = [{"session_id": r[0], "title": r[1], "updated_at": r[2]} for r in c.fetchall()]
    conn.close()
    return jsonify(chats)

@app.route("/api/chats/<session_id>", methods=["GET"])
def get_chat_messages(session_id):
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(get_history(session_id, limit=100))

@app.route("/api/chats/<session_id>", methods=["DELETE"])
def delete_chat(session_id):
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    c.execute("DELETE FROM chats WHERE session_id=?", (session_id,))
    conn.commit()
    conn.close()
    image_sessions.pop(session_id, None)
    return jsonify({"ok": True})

@app.route("/api/chats/new", methods=["POST"])
def new_chat():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    session_id = f"chat_{int(time.time() * 1000)}"
    return jsonify({"session_id": session_id})

@app.route("/api/profile", methods=["GET"])
def get_profile_route():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(get_profile())

@app.route("/api/profile", methods=["POST"])
def update_profile():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json
    conn = get_db()
    c = conn.cursor()
    for key, val in data.items():
        c.execute("INSERT OR REPLACE INTO user_profile VALUES (?,?)", (key, str(val)))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/stats", methods=["GET"])
def get_stats():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM messages")
    total_msgs = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM chats WHERE session_id IN (SELECT DISTINCT session_id FROM messages)")
    total_chats = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM messages WHERE role='user'")
    user_msgs = c.fetchone()[0]
    conn.close()
    return jsonify({"total_messages": total_msgs, "total_chats": total_chats, "user_messages": user_msgs})

if __name__ == "__main__":
    init_db()
    print("CesarIA corriendo en http://127.0.0.1:8317")
    app.run(host="0.0.0.0", port=8317, threaded=True)
