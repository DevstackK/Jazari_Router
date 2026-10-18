import os
import json
import time
import uuid
import logging
import psycopg2
import psycopg2.extras
import httpx
import jwt as pyjwt
from datetime import datetime, timedelta, timezone
from functools import wraps
from contextlib import contextmanager
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

JWT_SECRET        = os.getenv("JWT_SECRET", "")
JWT_ISS           = os.getenv("JWT_ISS", "jazariroute")
JWT_EXP_HOURS     = 24
MAX_USERS         = int(os.getenv("MAX_USERS", "3"))
DAILY_TOKEN_LIMIT = int(os.getenv("DAILY_TOKEN_LIMIT", "100000"))
DATABASE_URL      = os.getenv("DATABASE_URL", "")
OR_MODEL          = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
GATEWAY_API_KEY   = os.getenv("GATEWAY_API_KEY", "")
ADMIN_BOOTSTRAP_TOKEN = os.getenv("ADMIN_BOOTSTRAP_TOKEN", "")
APP_ORIGINS       = [x.strip() for x in os.getenv("APP_ORIGINS", "http://localhost:5001").split(",") if x.strip()]

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static")

app = Flask(__name__, static_folder=STATIC_DIR)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_REQUEST_BYTES", "1048576"))
CORS(app, origins=APP_ORIGINS)

if os.getenv("VERCEL_ENV") == "production" and (len(JWT_SECRET) < 32 or not GATEWAY_API_KEY):
    raise RuntimeError("Production requires JWT_SECRET (32+ characters) and GATEWAY_API_KEY")

AUTO_ORDER     = ["gemini", "groq", "mistral", "cohere"]
if os.getenv("OPENROUTER_API_KEY"):
    AUTO_ORDER.append("openrouter")

RETRY_STATUSES    = {401, 403, 429, 503}
LLM_CONTEXT_LIMIT = 50

PROVIDER_META = {
    "gemini":     {"name": "Gemini 2.5 Flash"},
    "groq":       {"name": "Groq Llama 3.3 70B",  "model": "llama-3.3-70b-versatile"},
    "mistral":    {"name": "Mistral 7B",            "model": "open-mistral-7b"},
    "cohere":     {"name": "Cohere Command-A",      "model": "command-a-03-2025"},
    "openrouter": {"name": f"OpenRouter {OR_MODEL.split('/')[-1]}", "model": OR_MODEL},
}


# ── Database ──────────────────────────────────────────────────────────────────

@contextmanager
def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            SERIAL PRIMARY KEY,
                email         TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin      INTEGER DEFAULT 0,
                created_at    INTEGER DEFAULT EXTRACT(EPOCH FROM NOW())::INTEGER
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS token_usage (
                user_id       INTEGER NOT NULL,
                date          TEXT NOT NULL,
                input_tokens  INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, date),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                id            TEXT PRIMARY KEY,
                owner_user_id INTEGER,
                name          TEXT NOT NULL,
                system_prompt TEXT DEFAULT '',
                model         TEXT DEFAULT 'auto',
                created_at    INTEGER DEFAULT EXTRACT(EPOCH FROM NOW())::INTEGER,
                FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        db.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS owner_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
        db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id            SERIAL PRIMARY KEY,
                agent_id      TEXT NOT NULL,
                role          TEXT NOT NULL,
                content       TEXT NOT NULL,
                provider      TEXT,
                usage_input   INTEGER DEFAULT 0,
                usage_output  INTEGER DEFAULT 0,
                created_at    INTEGER DEFAULT EXTRACT(EPOCH FROM NOW())::INTEGER,
                FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_agents_owner ON agents(owner_user_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_messages_agent_created ON messages(agent_id, created_at)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS featured_models (
                model_id       TEXT PRIMARY KEY,
                name           TEXT NOT NULL,
                context_length INTEGER DEFAULT 0,
                prompt_price   TEXT DEFAULT '0',
                refreshed_at   INTEGER DEFAULT EXTRACT(EPOCH FROM NOW())::INTEGER
            )
        """)


DB_READY = False
DB_ERROR = None


@app.before_request
def ensure_database_ready():
    """Initialize schema lazily so a stale database cannot take down static/health routes."""
    global DB_READY, DB_ERROR
    if request.endpoint in {"index", "health", "static"} or request.path in {"/", "/api/health", "/favicon.ico", "/favicon.png"}:
        return None
    if DB_READY:
        return None
    try:
        init_db()
        DB_READY = True
        DB_ERROR = None
    except Exception as exc:
        DB_ERROR = str(exc).splitlines()[0]
        log.error("database unavailable: %s", DB_ERROR)
        return jsonify({"error": "Database temporarily unavailable", "code": "database_unavailable"}), 503
    return None


# ── Auth helpers ──────────────────────────────────────────────────────────────

def get_daily_token_usage(user_id):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_db() as db:
        db.execute(
            "SELECT COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) AS total "
            "FROM token_usage WHERE user_id = %s AND date = %s",
            (user_id, today),
        )
        row = db.fetchone()
    return row["total"] if row else 0


def add_token_usage(user_id, input_tokens, output_tokens):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_db() as db:
        db.execute(
            """INSERT INTO token_usage (user_id, date, input_tokens, output_tokens)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (user_id, date) DO UPDATE SET
                 input_tokens  = token_usage.input_tokens  + EXCLUDED.input_tokens,
                 output_tokens = token_usage.output_tokens + EXCLUDED.output_tokens""",
            (user_id, today, input_tokens, output_tokens),
        )


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            log.warning("require_auth: missing Bearer token on %s", request.path)
            return jsonify({"error": "Missing or invalid token"}), 401
        try:
            payload = pyjwt.decode(auth[7:], JWT_SECRET, algorithms=["HS256"])
            request.user_id    = int(payload["sub"])
            request.user_email = payload["email"]
            request.is_admin   = bool(payload.get("is_admin", False))
        except pyjwt.ExpiredSignatureError:
            log.warning("require_auth: expired token on %s", request.path)
            return jsonify({"error": "Token expired, please log in again"}), 401
        except pyjwt.InvalidTokenError as e:
            log.warning("require_auth: invalid token on %s — %s", request.path, e)
            return jsonify({"error": "Invalid token"}), 401
        return f(*args, **kwargs)
    return decorated


def require_api_auth(f):
    """Accept the stable gateway key or a normal user JWT for OpenAI clients."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if GATEWAY_API_KEY and token == GATEWAY_API_KEY:
            request.user_id = None
            request.user_email = "gateway"
            request.is_admin = True
            return f(*args, **kwargs)
        return require_auth(f)(*args, **kwargs)
    return decorated


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Missing token"}), 401
        try:
            payload = pyjwt.decode(auth[7:], JWT_SECRET, algorithms=["HS256"])
            if not payload.get("is_admin"):
                return jsonify({"error": "Admin access required"}), 403
            request.user_id    = int(payload["sub"])
            request.user_email = payload["email"]
        except pyjwt.ExpiredSignatureError:
            return jsonify({"error": "Token expired"}), 401
        except pyjwt.InvalidTokenError:
            return jsonify({"error": "Invalid token"}), 401
        return f(*args, **kwargs)
    return decorated


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/api/register", methods=["POST"])
def register():
    data     = request.json or {}
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password", "")
    if not email or not password:
        return jsonify({"error": "email and password are required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    with get_db() as db:
        db.execute("SELECT COUNT(*) AS cnt FROM users")
        user_count = db.fetchone()["cnt"]
        if user_count >= MAX_USERS:
            return jsonify({"error": f"Registration is closed — maximum {MAX_USERS} users allowed"}), 403
        if user_count == 0:
            supplied = request.headers.get("X-Admin-Bootstrap-Token", "") or data.get("bootstrap_token", "")
            if not ADMIN_BOOTSTRAP_TOKEN or supplied != ADMIN_BOOTSTRAP_TOKEN:
                return jsonify({"error": "Initial admin registration requires X-Admin-Bootstrap-Token"}), 403
        db.execute("SELECT 1 FROM users WHERE email = %s", (email,))
        if db.fetchone():
            return jsonify({"error": "Email already registered"}), 409
        is_admin = 0 if user_count > 0 else 1
        db.execute(
            "INSERT INTO users (email, password_hash, is_admin) VALUES (%s, %s, %s)",
            (email, generate_password_hash(password), is_admin),
        )
    log.info("user registered email=%s is_admin=%s", email, bool(is_admin))
    return jsonify({"ok": True}), 201


@app.route("/api/login", methods=["POST"])
def login():
    data     = request.json or {}
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password", "")
    with get_db() as db:
        db.execute("SELECT id, password_hash, is_admin FROM users WHERE email = %s", (email,))
        row = db.fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "Invalid email or password"}), 401
    token = pyjwt.encode(
        {
            "sub":      str(row["id"]),
            "email":    email,
            "is_admin": bool(row["is_admin"]),
            "iss":      JWT_ISS,
            "exp":      datetime.now(timezone.utc) + timedelta(hours=JWT_EXP_HOURS),
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    log.info("user login email=%s", email)
    return jsonify({"token": token})


@app.route("/api/reset-password", methods=["POST"])
def reset_password():
    data         = request.json or {}
    email        = (data.get("email") or "").strip().lower()
    new_password = data.get("new_password", "")
    reset_secret = data.get("reset_secret", "")
    if not email or not new_password or not reset_secret:
        return jsonify({"error": "email, new_password and reset_secret are required"}), 400
    if reset_secret != os.getenv("RESET_SECRET", ""):
        log.warning("reset_password: wrong reset secret for email=%s", email)
        return jsonify({"error": "Invalid recovery code"}), 403
    if len(new_password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    with get_db() as db:
        db.execute("SELECT id FROM users WHERE email = %s", (email,))
        if not db.fetchone():
            return jsonify({"error": "Email not found"}), 404
        db.execute(
            "UPDATE users SET password_hash = %s WHERE email = %s",
            (generate_password_hash(new_password), email),
        )
    log.info("password reset for email=%s", email)
    return jsonify({"ok": True})


# ── Admin routes ──────────────────────────────────────────────────────────────

@app.route("/admin")
def admin_page():
    return send_from_directory(STATIC_DIR, "admin.html")


@app.route("/api/admin/users", methods=["GET"])
@require_admin
def admin_list_users():
    with get_db() as db:
        db.execute("SELECT id, email, is_admin, created_at FROM users ORDER BY created_at")
        rows = db.fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@require_admin
def admin_delete_user(user_id):
    if user_id == request.user_id:
        return jsonify({"error": "Cannot delete your own account"}), 400
    with get_db() as db:
        db.execute("DELETE FROM users WHERE id = %s", (user_id,))
    log.info("admin deleted user id=%s by=%s", user_id, request.user_email)
    return jsonify({"ok": True})


@app.route("/api/admin/stats", methods=["GET"])
@require_admin
def admin_stats():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_db() as db:
        db.execute("SELECT COUNT(*) AS cnt FROM users")
        total_users = db.fetchone()["cnt"]

        db.execute("SELECT COUNT(*) AS cnt FROM messages")
        total_messages = db.fetchone()["cnt"]

        db.execute(
            "SELECT COUNT(*) AS cnt FROM messages "
            "WHERE created_at >= EXTRACT(EPOCH FROM DATE_TRUNC('day', NOW() AT TIME ZONE 'UTC'))::INTEGER"
        )
        today_messages = db.fetchone()["cnt"]

        db.execute("""
            SELECT provider,
                   COUNT(*) AS requests,
                   SUM(usage_input) AS input_tokens,
                   SUM(usage_output) AS output_tokens
            FROM messages WHERE provider IS NOT NULL
            GROUP BY provider ORDER BY requests DESC
        """)
        provider_rows = db.fetchall()

        db.execute("""
            SELECT u.email, u.is_admin,
                   COALESCE(t.input_tokens, 0) + COALESCE(t.output_tokens, 0) AS tokens_today
            FROM users u
            LEFT JOIN token_usage t ON t.user_id = u.id AND t.date = %s
            ORDER BY tokens_today DESC
        """, (today,))
        user_usage_rows = db.fetchall()

    return jsonify({
        "total_users":       total_users,
        "total_messages":    total_messages,
        "today_messages":    today_messages,
        "daily_token_limit": DAILY_TOKEN_LIMIT,
        "providers":         [dict(r) for r in provider_rows],
        "user_usage":        [dict(r) for r in user_usage_rows],
    })


@app.route("/api/admin/activity", methods=["GET"])
@require_admin
def admin_activity():
    with get_db() as db:
        db.execute("""
            SELECT m.role, m.content, m.provider,
                   m.usage_input, m.usage_output, m.created_at,
                   a.name AS agent_name
            FROM messages m
            LEFT JOIN agents a ON m.agent_id = a.id
            ORDER BY m.created_at DESC LIMIT 50
        """)
        rows = db.fetchall()
    return jsonify([dict(r) for r in rows])


# ── SSE streaming helpers ─────────────────────────────────────────────────────

def _sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


def _stream_gemini(messages):
    key = os.getenv("GEMINI_API_KEY", "")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:streamGenerateContent?alt=sse&key={key}"
    )
    system_msgs = [m for m in messages if m["role"] == "system"]
    contents = [
        {"parts": [{"text": m["content"]}], "role": "user" if m["role"] == "user" else "model"}
        for m in messages if m["role"] != "system"
    ]
    body = {"contents": contents, "generationConfig": {"maxOutputTokens": 1024}}
    if system_msgs:
        body["systemInstruction"] = {"parts": [{"text": system_msgs[0]["content"]}]}

    with httpx.Client() as client:
        with client.stream("POST", url, json=body, timeout=60) as r:
            yield ("status", r.status_code)
            if r.status_code != 200:
                return
            usage = {}
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    obj = json.loads(line[6:])
                    text = obj["candidates"][0]["content"]["parts"][0].get("text", "")
                    if text:
                        yield ("chunk", text)
                    meta = obj.get("usageMetadata", {})
                    if meta:
                        usage = {
                            "input_tokens":  meta.get("promptTokenCount", 0),
                            "output_tokens": meta.get("candidatesTokenCount", 0),
                        }
                except (KeyError, IndexError, json.JSONDecodeError):
                    pass
            yield ("usage", usage)


def _stream_openai(key, messages):
    urls     = {
        "groq":    "https://api.groq.com/openai/v1/chat/completions",
        "mistral": "https://api.mistral.ai/v1/chat/completions",
    }
    env_vars = {"groq": "GROQ_API_KEY", "mistral": "MISTRAL_API_KEY"}
    headers  = {"Authorization": f"Bearer {os.getenv(env_vars[key], '')}"}
    body     = {
        "model":      PROVIDER_META[key]["model"],
        "messages":   messages,
        "max_tokens": 1024,
        "stream":     True,
    }

    with httpx.Client() as client:
        with client.stream("POST", urls[key], json=body, headers=headers, timeout=60) as r:
            yield ("status", r.status_code)
            if r.status_code != 200:
                return
            usage = {}
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    obj   = json.loads(data_str)
                    delta = obj["choices"][0]["delta"]
                    if delta.get("content"):
                        yield ("chunk", delta["content"])
                    if obj.get("usage"):
                        usage = {
                            "input_tokens":  obj["usage"].get("prompt_tokens", 0),
                            "output_tokens": obj["usage"].get("completion_tokens", 0),
                        }
                except (KeyError, IndexError, json.JSONDecodeError):
                    pass
            yield ("usage", usage)


def _stream_cohere(messages):
    headers = {"Authorization": f"Bearer {os.getenv('COHERE_API_KEY', '')}"}
    body    = {"model": PROVIDER_META["cohere"]["model"], "messages": messages, "stream": True}

    with httpx.Client() as client:
        with client.stream("POST", "https://api.cohere.com/v2/chat",
                           json=body, headers=headers, timeout=60) as r:
            yield ("status", r.status_code)
            if r.status_code != 200:
                return
            usage = {}
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    obj = json.loads(data_str)
                    if obj.get("type") == "content-delta":
                        text = obj["delta"].get("text", "")
                        if text:
                            yield ("chunk", text)
                    elif obj.get("type") == "message-end":
                        u = obj.get("delta", {}).get("usage", {}).get("billed_units", {})
                        usage = {
                            "input_tokens":  u.get("input_tokens", 0),
                            "output_tokens": u.get("output_tokens", 0),
                        }
                except (KeyError, json.JSONDecodeError):
                    pass
            yield ("usage", usage)


def _or_name(key):
    """Display name for a provider key, including dynamic or: models."""
    if key in PROVIDER_META:
        return PROVIDER_META[key]["name"]
    if key.startswith("or:"):
        return key[3:].split("/")[-1]
    return key


def _stream_openrouter(messages):
    key     = os.getenv("OPENROUTER_API_KEY", "")
    headers = {
        "Authorization": f"Bearer {key}",
        "HTTP-Referer":  os.getenv("PUBLIC_APP_URL", "https://jazariroute.vercel.app"),
        "X-Title":       "JazariRoute",
    }
    body = {"model": OR_MODEL, "messages": messages, "max_tokens": 1024, "stream": True}

    with httpx.Client() as client:
        with client.stream("POST", "https://openrouter.ai/api/v1/chat/completions",
                           json=body, headers=headers, timeout=60) as r:
            yield ("status", r.status_code)
            if r.status_code != 200:
                return
            usage = {}
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    obj   = json.loads(data_str)
                    delta = obj["choices"][0]["delta"]
                    if delta.get("content"):
                        yield ("chunk", delta["content"])
                    if obj.get("usage"):
                        usage = {
                            "input_tokens":  obj["usage"].get("prompt_tokens", 0),
                            "output_tokens": obj["usage"].get("completion_tokens", 0),
                        }
                except (KeyError, IndexError, json.JSONDecodeError):
                    pass
            yield ("usage", usage)


def _stream_openrouter_model(model_id, messages):
    """Stream any OpenRouter model by ID (used for featured/dynamic models)."""
    key     = os.getenv("OPENROUTER_API_KEY", "")
    headers = {
        "Authorization": f"Bearer {key}",
        "HTTP-Referer":  os.getenv("PUBLIC_APP_URL", "https://jazariroute.vercel.app"),
        "X-Title":       "JazariRoute",
    }
    body = {"model": model_id, "messages": messages, "max_tokens": 1024, "stream": True}

    with httpx.Client() as client:
        with client.stream("POST", "https://openrouter.ai/api/v1/chat/completions",
                           json=body, headers=headers, timeout=60) as r:
            yield ("status", r.status_code)
            if r.status_code != 200:
                return
            usage = {}
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    obj   = json.loads(data_str)
                    delta = obj["choices"][0]["delta"]
                    if delta.get("content"):
                        yield ("chunk", delta["content"])
                    if obj.get("usage"):
                        usage = {
                            "input_tokens":  obj["usage"].get("prompt_tokens", 0),
                            "output_tokens": obj["usage"].get("completion_tokens", 0),
                        }
                except (KeyError, IndexError, json.JSONDecodeError):
                    pass
            yield ("usage", usage)


def _get_provider_stream(key, messages):
    if key == "gemini":
        return _stream_gemini(messages)
    if key in ("groq", "mistral"):
        return _stream_openai(key, messages)
    if key == "cohere":
        return _stream_cohere(messages)
    if key == "openrouter":
        return _stream_openrouter(messages)
    if key.startswith("or:"):
        return _stream_openrouter_model(key[3:], messages)


def generate_sse(messages, provider_key, system_prompt, user_id=None, is_admin=False):
    # Strip to role+content only — providers reject unknown fields (usage, provider, etc.)
    messages = [{"role": m["role"], "content": m["content"]} for m in messages]
    if system_prompt:
        messages = [{"role": "system", "content": system_prompt}] + messages

    providers_to_try = [provider_key] if provider_key != "auto" else AUTO_ORDER

    for key in providers_to_try:
        pname = _or_name(key)
        try:
            gen = _get_provider_stream(key, messages)
            _, status = next(gen)

            if status in RETRY_STATUSES and provider_key == "auto":
                log.warning("skip provider=%s status=%s", key, status)
                continue

            if status != 200:
                log.error("provider=%s returned status=%s", key, status)
                yield _sse({"type": "error", "message": f"{pname} returned {status}"})
                return

            log.info("selected provider=%s", key)
            yield _sse({"type": "provider", "name": pname})

            for event_type, value in gen:
                if event_type == "chunk":
                    yield _sse({"type": "chunk", "content": value})
                elif event_type == "usage":
                    inp = value.get("input_tokens", 0)
                    out = value.get("output_tokens", 0)
                    log.info("done provider=%s input_tokens=%s output_tokens=%s", key, inp, out)
                    if user_id and not is_admin:
                        add_token_usage(user_id, inp, out)
                    yield _sse({
                        "type":     "done",
                        "provider": pname,
                        "usage":    value,
                    })
            return

        except Exception as e:
            log.error("provider=%s error=%s", key, e, exc_info=True)
            if provider_key != "auto":
                yield _sse({"type": "error", "message": str(e)})
                return

    log.error("all providers exhausted")
    yield _sse({"type": "error", "message": "All providers exhausted"})


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/providers", methods=["GET"])
@require_auth
def get_providers():
    providers = [{"id": k, "name": v["name"]} for k, v in PROVIDER_META.items()]
    with get_db() as db:
        db.execute("SELECT model_id, name, context_length, prompt_price FROM featured_models ORDER BY prompt_price::NUMERIC, name")
        rows = db.fetchall()
    for r in rows:
        price = float(r["prompt_price"] or 0)
        label = "free" if price == 0 else f"${price * 1e6:.2f}/M"
        providers.append({
            "id":       f"or:{r['model_id']}",
            "name":     r["name"],
            "context":  r["context_length"],
            "price":    label,
            "featured": True,
        })
    return jsonify(providers)


@app.route("/api/cron/refresh-models", methods=["GET"])
def cron_refresh_models():
    cron_secret = os.getenv("CRON_SECRET", "")
    auth = request.headers.get("Authorization", "")
    if not cron_secret or auth != f"Bearer {cron_secret}":
        return jsonify({"error": "Unauthorized"}), 401

    or_key = os.getenv("OPENROUTER_API_KEY", "")
    if not or_key:
        return jsonify({"error": "OPENROUTER_API_KEY not set"}), 400

    try:
        resp = httpx.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {or_key}"},
            timeout=15,
        )
        models = resp.json().get("data", [])
    except Exception as e:
        log.error("cron_refresh_models: fetch failed — %s", e)
        return jsonify({"error": str(e)}), 500

    TRUSTED_PROVIDERS = {
        "google", "meta-llama", "deepseek", "qwen", "nvidia",
        "mistralai", "anthropic", "openai", "cohere", "microsoft",
        "amazon", "01-ai", "x-ai", "perplexity", "together",
    }

    # Code-completion models (fill-in-the-middle, not instruction-following)
    BLOCKED_PATTERNS = {
        "fim", "fill-in", "fill_in",
        "north-mini-code",   # Cohere code completion
        "starcoder",         # StarCoder base (no instruct)
        "santacoder",        # SantaCoder completion
        "codestral-mamba",   # Mistral completion variant
        "code-gecko",        # Google code completion
    }

    def _prices(m):
        pricing = m.get("pricing", {})
        return (
            float(pricing.get("prompt") if pricing.get("prompt") is not None else 999),
            float(pricing.get("completion") if pricing.get("completion") is not None else 999),
        )

    def _is_chat_model(m):
        arch        = m.get("architecture", {})
        output_mods = arch.get("output_modalities")
        if output_mods is not None:
            return "text" in output_mods and "audio" not in output_mods
        # fallback: parse "input->output" modality string
        modality = arch.get("modality", "text->text")
        output   = modality.split("->")[-1] if "->" in modality else modality
        return "text" in output and "audio" not in output

    def _trusted(m):
        provider = m.get("id", "").split("/")[0]
        return provider in TRUSTED_PROVIDERS

    def _score(m):
        prompt_price, completion_price = _prices(m)
        price    = prompt_price + completion_price
        is_free  = prompt_price == 0 and completion_price == 0
        trusted  = _trusted(m)
        ctx      = m.get("context_length", 0)
        # Lower score = better: free trusted > free unknown > paid trusted > paid unknown
        return (not is_free, not trusted, price, -ctx)

    def _not_blocked(m):
        model_id = m.get("id", "").lower()
        return not any(p in model_id for p in BLOCKED_PATTERNS)

    valid = [
        m for m in models
        if m.get("context_length", 0) >= 8000
        and "/" in m.get("id", "")
        and _prices(m) == (0, 0)
        and _is_chat_model(m)
        and _not_blocked(m)
    ]
    valid.sort(key=_score)
    top10 = valid[:10]

    now = int(time.time())
    with get_db() as db:
        db.execute("DELETE FROM featured_models")
        for m in top10:
            db.execute(
                "INSERT INTO featured_models (model_id, name, context_length, prompt_price, refreshed_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (m["id"], m.get("name", m["id"]),
                 m.get("context_length", 0),
                 str(m["pricing"].get("prompt", "0")),
                 now),
            )

    names = [m.get("name", m["id"]) for m in top10]
    log.info("cron_refresh_models: stored %d models: %s", len(top10), names)
    return jsonify({"ok": True, "count": len(top10), "models": names})


@app.route("/api/chat", methods=["POST"])
@require_auth
def chat():
    data     = request.json or {}
    messages = data.get("messages")

    if not messages:
        return jsonify({"error": "messages must be a non-empty array"}), 400

    provider_key = data.get("provider", "auto")
    if provider_key != "auto" and provider_key not in PROVIDER_META:
        if provider_key.startswith("or:"):
            model_id = provider_key[3:]
            with get_db() as db:
                db.execute("SELECT 1 FROM featured_models WHERE model_id = %s", (model_id,))
                if not db.fetchone():
                    return jsonify({"error": f"Model not in free featured list: {model_id}"}), 400
        else:
            return jsonify({"error": f"Unknown provider: {provider_key}"}), 400

    messages = messages[-LLM_CONTEXT_LIMIT:]
    log.info("chat provider=%s messages=%d system_prompt=%s",
             provider_key, len(messages), bool(data.get("system_prompt")))

    if not request.is_admin:
        used = get_daily_token_usage(request.user_id)
        if used >= DAILY_TOKEN_LIMIT:
            log.warning("token limit reached user_id=%s used=%s limit=%s",
                        request.user_id, used, DAILY_TOKEN_LIMIT)
            def _limit_hit():
                yield _sse({"type": "error", "message": f"Daily token limit reached ({DAILY_TOKEN_LIMIT:,} tokens). Resets at midnight UTC."})
            return Response(_limit_hit(), mimetype="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return Response(
        generate_sse(messages, provider_key, data.get("system_prompt"),
                     request.user_id, request.is_admin),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/health", methods=["GET"])
def health():
    env_keys = {
        "gemini":     "GEMINI_API_KEY",
        "groq":       "GROQ_API_KEY",
        "mistral":    "MISTRAL_API_KEY",
        "cohere":     "COHERE_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }
    database_ok = False
    try:
        with get_db() as db:
            db.execute("SELECT 1")
            database_ok = True
    except Exception as exc:
        log.warning("health database check failed: %s", str(exc).splitlines()[0])
    status = {
        k: {"name": v["name"], "configured": bool(os.getenv(env_keys.get(k, "")))}
        for k, v in PROVIDER_META.items()
    }
    status["database"] = {"configured": bool(DATABASE_URL), "available": database_ok}
    return jsonify(status)


# ── OpenAI-compatible API ───────────────────────────────────────────────────────────────────────

def _openai_model_to_provider(model):
    if model in ("auto", "auto/free"):
        return "auto"
    if model in PROVIDER_META:
        return model
    if model.startswith("openrouter/"):
        return f"or:{model[len('openrouter/') :]}"
    return model


@app.route("/v1/models", methods=["GET"])
@require_api_auth
def openai_models():
    now = int(time.time())
    data = [
        {"id": "auto", "object": "model", "created": now, "owned_by": "jazariroute"},
        {"id": "auto/free", "object": "model", "created": now, "owned_by": "jazariroute"},
    ]
    data.extend(
        {"id": key, "object": "model", "created": now, "owned_by": key}
        for key in PROVIDER_META
    )
    with get_db() as db:
        db.execute("SELECT model_id FROM featured_models ORDER BY name")
        data.extend(
            {"id": f"openrouter/{row['model_id']}", "object": "model", "created": now, "owned_by": "openrouter"}
            for row in db.fetchall()
        )
    return jsonify({"object": "list", "data": data})


@app.route("/v1/chat/completions", methods=["POST"])
@require_api_auth
def openai_chat_completions():
    data = request.get_json(silent=True) or {}
    messages = data.get("messages")
    model = data.get("model", "auto")
    if not isinstance(messages, list) or not messages:
        return jsonify({"error": {"message": "messages must be a non-empty array", "type": "invalid_request_error"}}), 400
    if any(not isinstance(m, dict) or m.get("role") not in {"system", "user", "assistant"} or not isinstance(m.get("content"), str) for m in messages):
        return jsonify({"error": {"message": "invalid message role or content", "type": "invalid_request_error"}}), 400

    provider = _openai_model_to_provider(model)
    if provider != "auto" and provider not in PROVIDER_META:
        if not provider.startswith("or:"):
            return jsonify({"error": {"message": f"unknown model: {model}", "type": "invalid_request_error"}}), 400
        with get_db() as db:
            db.execute("SELECT 1 FROM featured_models WHERE model_id = %s", (provider[3:],))
            if not db.fetchone():
                return jsonify({"error": {"message": "model is not in the current free catalog", "type": "invalid_request_error"}}), 400

    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    events = generate_sse(messages[-LLM_CONTEXT_LIMIT:], provider, "", request.user_id, request.is_admin)

    if data.get("stream", False):
        def stream_openai():
            for event in events:
                payload = json.loads(event[6:])
                if payload["type"] == "chunk":
                    yield _sse({
                        "id": completion_id, "object": "chat.completion.chunk", "created": created,
                        "model": model, "choices": [{"index": 0, "delta": {"content": payload["content"]}, "finish_reason": None}],
                    })
                elif payload["type"] == "error":
                    yield _sse({"error": {"message": payload["message"], "type": "upstream_error"}})
            yield "data: [DONE]\n\n"
        return Response(stream_openai(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    content, served_by, usage, error = [], None, {}, None
    for event in events:
        payload = json.loads(event[6:])
        if payload["type"] == "chunk":
            content.append(payload["content"])
        elif payload["type"] == "provider":
            served_by = payload["name"]
        elif payload["type"] == "done":
            usage = payload.get("usage", {})
        elif payload["type"] == "error":
            error = payload["message"]
    if error:
        return jsonify({"error": {"message": error, "type": "upstream_error"}}), 502
    return jsonify({
        "id": completion_id, "object": "chat.completion", "created": created, "model": model,
        "provider": served_by,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(content)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0),
                  "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)},
    })


# ── Agent CRUD ────────────────────────────────────────────────────────────────

@app.route("/api/agents", methods=["GET"])
@require_auth
def list_agents():
    with get_db() as db:
        db.execute(
            "SELECT id, name, system_prompt, model FROM agents "
            "WHERE owner_user_id = %s ORDER BY created_at", (request.user_id,)
        )
        rows = db.fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/agents", methods=["POST"])
@require_auth
def create_agent():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    agent_id = name.lower().replace(" ", "-") + "-" + str(int(time.time() * 1000))
    agent    = {
        "id":            agent_id,
        "name":          name,
        "system_prompt": data.get("system_prompt", ""),
        "model":         data.get("model", "auto"),
    }
    with get_db() as db:
        db.execute(
            "INSERT INTO agents (id, owner_user_id, name, system_prompt, model) VALUES (%s, %s, %s, %s, %s)",
            (agent["id"], request.user_id, agent["name"], agent["system_prompt"], agent["model"]),
        )
    log.info("agent created id=%s name=%s", agent_id, name)
    return jsonify(agent), 201


@app.route("/api/agents/<agent_id>", methods=["DELETE"])
@require_auth
def delete_agent(agent_id):
    with get_db() as db:
        db.execute("DELETE FROM agents WHERE id = %s AND owner_user_id = %s", (agent_id, request.user_id))
        if db.rowcount == 0:
            return jsonify({"error": "Agent not found"}), 404
    log.info("agent deleted id=%s", agent_id)
    return jsonify({"ok": True})


# ── Message history ───────────────────────────────────────────────────────────

@app.route("/api/agents/<agent_id>/messages", methods=["GET"])
@require_auth
def get_messages(agent_id):
    with get_db() as db:
        db.execute(
            "SELECT role, content, provider, usage_input, usage_output "
            "FROM messages WHERE agent_id = %s AND EXISTS "
            "(SELECT 1 FROM agents WHERE id = %s AND owner_user_id = %s) "
            "ORDER BY created_at DESC LIMIT 100",
            (agent_id, agent_id, request.user_id),
        )
        rows = db.fetchall()
    result = []
    for r in reversed(rows):
        d  = dict(r)
        ui = d.pop("usage_input",  0) or 0
        uo = d.pop("usage_output", 0) or 0
        d["usage"] = {"input_tokens": ui, "output_tokens": uo} if (ui or uo) else None
        result.append(d)
    return jsonify(result)


@app.route("/api/agents/<agent_id>/messages", methods=["POST"])
@require_auth
def save_messages(agent_id):
    data = request.json or {}
    msgs = data.get("messages", [])
    if not msgs:
        return jsonify({"error": "messages is required"}), 400
    with get_db() as db:
        db.execute("SELECT 1 FROM agents WHERE id = %s AND owner_user_id = %s", (agent_id, request.user_id))
        if not db.fetchone():
            return jsonify({"error": "Agent not found"}), 404
        for m in msgs:
            if "role" not in m or "content" not in m:
                return jsonify({"error": "Each message must have role and content"}), 400
            usage = m.get("usage") or {}
            db.execute(
                "INSERT INTO messages (agent_id, role, content, provider, usage_input, usage_output) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (agent_id, m["role"], m["content"], m.get("provider"),
                 usage.get("input_tokens", 0), usage.get("output_tokens", 0)),
            )
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
