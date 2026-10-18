# JazariRoute — Complete Setup Guide

A self-hosted, multi-provider AI chat platform. Routes prompts across Gemini, Groq,
Mistral, Cohere, and OpenRouter with automatic failover, JWT auth, per-user token
limits, daily featured free models, and an admin dashboard.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Prerequisites](#prerequisites)
3. [Environment Variables](#environment-variables)
4. [Supabase Database Setup](#supabase-database-setup)
5. [Local Development — Flask](#local-development--flask)
6. [Local Development — Docker + Kong](#local-development--docker--kong)
7. [Vercel Deployment](#vercel-deployment)
8. [First Login & Admin Access](#first-login--admin-access)
9. [OpenRouter Featured Models](#openrouter-featured-models)
10. [Admin Panel](#admin-panel)
11. [API Reference](#api-reference)
12. [File Structure](#file-structure)
13. [Free Tier Limits](#free-tier-limits)

---

## Architecture Overview

```
Browser / CLI
     │
     ▼
Flask App (api/index.py)          ← Vercel serverless OR local Python
     │
     ├── /api/chat ──────────────► Provider auto-failover chain
     │                               Gemini → Groq → Mistral → Cohere → OpenRouter
     │
     ├── /api/cron/refresh-models ► OpenRouter catalogue → top 10 free models → Supabase
     │
     └── /api/admin/* ───────────► User management, token stats, activity log
                │
                ▼
         Supabase Postgres
         (users, messages, agents, token_usage, featured_models)
```

Kong sits in front as a production API gateway when running in Docker mode — it
handles rate limiting, JWT validation, and request logging before traffic reaches Flask.

---

## Prerequisites

| Tool | Minimum version | Notes |
|------|----------------|-------|
| Python | 3.10+ | 3.12 recommended |
| pip | any | bundled with Python |
| Docker + Docker Compose | v2 | Docker Desktop on Windows/Mac |
| Git | any | |
| A Supabase account | — | free tier is fine |
| API keys | — | see table below |

### API Keys — where to get them (all free tiers)

| Provider | Dashboard |
|----------|-----------|
| Gemini | https://aistudio.google.com/app/apikey |
| Groq | https://console.groq.com/keys |
| Mistral | https://console.mistral.ai/api-keys |
| Cohere | https://dashboard.cohere.com/api-keys |
| OpenRouter | https://openrouter.ai/keys |

---

## Environment Variables

Copy the template and fill in your values:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|----------|----------|-------------|
| `GEMINI_API_KEY` | yes | Google Gemini API key |
| `GROQ_API_KEY` | yes | Groq API key |
| `MISTRAL_API_KEY` | yes | Mistral API key |
| `COHERE_API_KEY` | yes | Cohere API key |
| `OPENROUTER_API_KEY` | optional | Enables 5th provider + daily featured models |
| `OPENROUTER_MODEL` | optional | Default OpenRouter model (must end in `:free`). Defaults to `meta-llama/llama-3.3-70b-instruct:free` |
| `JWT_SECRET` | yes | Long random string used to sign auth tokens. Generate with: `openssl rand -hex 32` |
| `RESET_SECRET` | yes | Short passphrase used in the password reset endpoint |
| `MAX_USERS` | yes | Maximum number of registered accounts (e.g. `3`) |
| `DAILY_TOKEN_LIMIT` | yes | Per-user daily token cap (e.g. `100000`) |
| `DATABASE_URL` | yes | Supabase session pooler connection string (see below) |

**Important:** `OPENROUTER_MODEL` must always end in `:free` or OpenRouter will charge
per token. Example: `meta-llama/llama-3.3-70b-instruct:free`

---

## Supabase Database Setup

The app uses Supabase Postgres. SQLite is not supported.

### 1. Create a project

1. Go to https://supabase.com and sign in
2. Click **New project**, choose a region close to you (e.g. EU West)
3. Set a strong database password and save it

### 2. Get the connection string

1. In your project, go to **Project Settings → Database**
2. Under **Connection string**, select **Session pooler** (not direct connection)
3. Copy the URI — it looks like:
   ```
   postgresql://postgres.[ref]:[password]@aws-0-[region].pooler.supabase.com:5432/postgres
   ```
4. Paste it as `DATABASE_URL` in your `.env`

> **Why session pooler?** The direct host only resolves to IPv6. WSL 2 and some
> cloud environments (including Vercel) cannot reach IPv6. The session pooler uses
> IPv4 and port 5432 — compatible everywhere.

The app creates all tables automatically on first startup (`init_db()` runs at import
time). You do not need to run any migrations manually.

### Tables created automatically

| Table | Purpose |
|-------|---------|
| `users` | Registered accounts with hashed passwords |
| `token_usage` | Per-user per-day token counts |
| `agents` | Named chat agents with system prompts |
| `messages` | Full message history per agent |
| `featured_models` | Top 10 free OpenRouter models (refreshed daily) |

---

## Local Development — Flask

### 1. Create a virtual environment

**Mac / Linux / WSL:**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**Windows PowerShell (native Python):**
```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Fill in `.env`

```bash
cp .env.example .env
# Edit .env with your keys and DATABASE_URL
```

### 3. Start the server

**Mac / Linux / WSL:**
```bash
source venv/bin/activate
python api/index.py
```

**Windows PowerShell:**
```powershell
venv\Scripts\activate
python api\index.py
```

The server starts on **http://localhost:5001**

### 4. Verify it's working

```bash
curl http://localhost:5001/api/health
```

Expected response:
```json
{
  "gemini":     {"name": "Gemini 2.5 Flash",       "configured": true},
  "groq":       {"name": "Groq Llama 3.3 70B",     "configured": true},
  "mistral":    {"name": "Mistral 7B",              "configured": true},
  "cohere":     {"name": "Cohere Command-A",        "configured": true},
  "openrouter": {"name": "OpenRouter llama-3.3-70b-instruct:free", "configured": true}
}
```

### 5. Test a chat (requires login first — see First Login section)

```bash
# 1. Register
curl -X POST http://localhost:5001/api/register \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"yourpassword"}'

# 2. Login and capture token
TOKEN=$(curl -s -X POST http://localhost:5001/api/login \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"yourpassword"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

# 3. Chat
curl -X POST http://localhost:5001/api/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"messages":[{"role":"user","content":"say hi"}]}'
```

### Stopping the server

Press `Ctrl+C` in the terminal where the server is running.

---

## Local Development — Docker + Kong

Kong runs as a production API gateway in front of the Flask app. This mode adds
rate limiting, JWT validation, and request logging via Kong plugins.

### 1. Make sure Docker Desktop is running

```bash
docker info   # should return Docker version info without errors
```

### 2. Fill in `.env` (same as Flask mode)

Docker Compose reads `.env` automatically and injects values into the Kong container.

### 3. Start Kong

```bash
docker compose up -d
```

First run pulls the `kong/kong-gateway:3.9` image (~300MB). Subsequent starts are instant.

### 4. Verify Kong is up

```bash
# Kong proxy (traffic goes through here)
curl http://localhost:8000

# Kong admin API (config management)
curl http://localhost:8001/status
```

### 5. Test the AI route through Kong

```bash
curl -s -X POST http://localhost:8000/ai/chat \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello, who are you?"}]}'
```

Kong handles JWT validation and rate limiting, then forwards to the Flask app.

### 6. View logs

```bash
# All services
docker compose logs -f

# Kong only
docker compose logs -f kong
```

### 7. Stop Kong

```bash
docker compose down
```

### Kong Admin API — useful commands

```bash
# List all routes
curl http://localhost:8001/routes

# List all plugins
curl http://localhost:8001/plugins

# List all consumers
curl http://localhost:8001/consumers

# Kong health
curl http://localhost:8001/status
```

### Adding plugins without restart (via Admin API)

```bash
# Add API key auth to the jazariroute service
curl -X POST http://localhost:8001/services/jazariroute/plugins \
  -d name=key-auth

# Create a consumer and give them a key
curl -X POST http://localhost:8001/consumers \
  -d username=myapp
curl -X POST http://localhost:8001/consumers/myapp/key-auth \
  -d key=my-secret-key-123

# Now every request needs the header: apikey: my-secret-key-123
```

### Adding plugins in `kong.yml` (declarative, requires restart)

```yaml
plugins:
  - name: proxy-cache
    service: jazariroute
    config:
      response_code: [200]
      request_method: [POST]
      cache_ttl: 300
      strategy: memory
```

Then apply:
```bash
docker compose restart
```

---

## Vercel Deployment

The app is built for Vercel Python serverless functions. One command deploys everything.

### 1. Install the Vercel CLI

```bash
npm install -g vercel
```

### 2. Link the project

```bash
vercel
# Follow the prompts to link to your Vercel account and project
```

### 3. Add environment variables to Vercel

Go to your project in the Vercel dashboard → **Settings → Environment Variables**
and add every variable from your `.env`:

```
GEMINI_API_KEY
GROQ_API_KEY
MISTRAL_API_KEY
COHERE_API_KEY
OPENROUTER_API_KEY
OPENROUTER_MODEL
JWT_SECRET
RESET_SECRET
MAX_USERS
DAILY_TOKEN_LIMIT
DATABASE_URL
```

> Do **not** add these via the CLI — paste them in the dashboard so they are
> encrypted at rest.

### 4. Deploy to production

```bash
vercel --prod
```

Vercel builds the Python serverless function, deploys `static/` as static assets,
and wires up the cron job from `vercel.json`.

### 5. Verify the deployment

```bash
curl https://your-project.vercel.app/api/health
```

### Cron job

`vercel.json` schedules the featured models refresh at **9am UTC every day**:

```json
{
  "crons": [{"path": "/api/cron/refresh-models", "schedule": "0 9 * * *"}]
}
```

Vercel triggers this automatically on the Pro plan. On the free Hobby plan, call
`/api/cron/refresh-models` manually or via an external cron service (e.g. cron-job.org).

---

## First Login & Admin Access

### Register

The first account registered automatically becomes **admin**. Subsequent accounts
are regular users.

1. Open the app in your browser
2. Click **Register**, enter email and password (min 8 characters)
3. Log in

To cap how many accounts can be created, set `MAX_USERS` in your `.env`.

### Password Reset

If a user forgets their password, use the reset endpoint with the `RESET_SECRET`
from your `.env`:

```bash
curl -X POST http://localhost:5001/api/reset-password \
  -H "Content-Type: application/json" \
  -d '{
    "email": "user@example.com",
    "new_password": "newpassword123",
    "reset_secret": "your-reset-secret-here"
  }'
```

---

## OpenRouter Featured Models

Every day at 9am UTC the cron job:

1. Fetches the full OpenRouter model catalogue
2. Filters for:
   - `context_length >= 8000` tokens
   - `price == 0` (free tier only — no paid models ever selected)
   - Chat models only (excludes audio, code-completion, fill-in-the-middle)
   - Trusted providers: Google, Meta, DeepSeek, Qwen, NVIDIA, Mistral, Anthropic, etc.
   - Blocked patterns: `fim`, `starcoder`, `codestral-mamba`, `code-gecko`, etc.
3. Scores and ranks by: free > trusted provider > context length
4. Stores the top 10 in the `featured_models` table

Users see a popup after 9am on first login each day listing the top models. Featured
models appear in the provider dropdown prefixed with `✦`.

### Manually trigger a refresh (admin only)

```bash
curl http://localhost:5001/api/cron/refresh-models
```

Or click **↻ Refresh now** in the admin panel.

---

## Admin Panel

Access at `/admin` — requires an admin account.

### Dashboard tabs

| Tab | What it shows |
|-----|--------------|
| **Stats** | Total users, messages today, token usage per provider |
| **Users** | All registered accounts, delete users |
| **Activity** | Last 50 messages across all agents with provider and token counts |
| **Featured Models** | Current top-10 free OpenRouter models, manual refresh button |

---

## API Reference

All endpoints require a `Bearer` token in the `Authorization` header except
`/api/register`, `/api/login`, `/api/reset-password`, and `/api/health`.

Get a token by calling `/api/login`.

---

### Auth

#### Register
```
POST /api/register
{"email": "user@example.com", "password": "min8chars"}
```

#### Login
```
POST /api/login
{"email": "user@example.com", "password": "yourpassword"}

→ {"token": "<jwt>"}
```

#### Reset password
```
POST /api/reset-password
{"email": "...", "new_password": "...", "reset_secret": "..."}
```

---

### Chat

```
POST /api/chat
Authorization: Bearer <token>

{
  "messages": [
    {"role": "user", "content": "Hello"}
  ],
  "provider": "auto",
  "system_prompt": "You are a helpful assistant."
}
```

**provider options:**
- `auto` — tries providers in order until one succeeds
- `gemini` / `groq` / `mistral` / `cohere` / `openrouter` — pin to one provider
- `or:<model-id>` — use a specific featured model (e.g. `or:google/gemma-3-27b-it:free`)

**Response — Server-Sent Events stream:**

```
data: {"type": "provider", "name": "Gemini 2.5 Flash"}
data: {"type": "chunk", "content": "Hello"}
data: {"type": "chunk", "content": "!"}
data: {"type": "done", "provider": "Gemini 2.5 Flash", "usage": {"input_tokens": 5, "output_tokens": 3}}
```

On error:
```
data: {"type": "error", "message": "All providers exhausted"}
```

---

### Providers

```
GET /api/providers
Authorization: Bearer <token>

→ [
    {"id": "gemini",  "name": "Gemini 2.5 Flash"},
    {"id": "groq",    "name": "Groq Llama 3.3 70B"},
    {"id": "mistral", "name": "Mistral 7B"},
    {"id": "cohere",  "name": "Cohere Command-A"},
    {"id": "or:google/gemma-3-27b-it:free", "name": "Gemma 3 27B", "context": 131072, "price": "free", "featured": true},
    ...
  ]
```

---

### Health

```
GET /api/health

→ {
    "gemini":     {"name": "Gemini 2.5 Flash", "configured": true},
    "groq":       {"name": "Groq Llama 3.3 70B", "configured": true},
    ...
  }
```

---

### Agents

```
GET    /api/agents                      # list all agents
POST   /api/agents                      # create agent
DELETE /api/agents/:id                  # delete agent
GET    /api/agents/:id/messages         # message history (last 100)
POST   /api/agents/:id/messages         # save messages
```

**Create agent body:**
```json
{
  "name": "Code Reviewer",
  "system_prompt": "You are a concise code reviewer. Flag issues only.",
  "model": "auto"
}
```

---

### Admin (admin accounts only)

```
GET    /api/admin/users                 # list all users
DELETE /api/admin/users/:id             # delete a user
GET    /api/admin/stats                 # token usage and message counts
GET    /api/admin/activity              # last 50 messages across all agents
GET    /api/cron/refresh-models         # trigger featured models refresh
```

---

## File Structure

```
jazariroute/
├── api/
│   └── index.py           # Flask app — all routes, SSE streaming, DB, auth
├── static/
│   ├── index.html         # Chat UI (single file, no build step)
│   └── admin.html         # Admin dashboard
├── client.py              # CLI streaming client (development use)
├── test_connections.py    # Tests DB + all LLM providers (development use)
├── kong.yml               # Kong declarative config (database-off mode)
├── docker-compose.yml     # Kong container setup
├── vercel.json            # Vercel routing + cron schedule
├── requirements.txt       # Python dependencies
├── .env                   # Your secrets — never commit this
├── .env.example           # Template for new installs
└── HOW_TO.md              # This file
```

---

## Free Tier Limits

### Fixed providers

| Provider | Model | RPM | Daily limit |
|----------|-------|-----|------------|
| Gemini | 2.5 Flash | 15 | 1,500 req / 1M tokens |
| Groq | Llama 3.3 70B | 30 | 14,400 req |
| Mistral | 7B | 60 | no hard daily cap |
| Cohere | Command-A | 20 | 1,000 req/month |

### OpenRouter free models

Featured models are always `price == 0`. OpenRouter free models are:
- Rate-limited per-key (typically 20 req/min)
- Shared capacity — may queue during peak hours
- No monthly cap in most cases

> The auto-failover chain means a rate limit on one provider is invisible to the
> user — the next provider picks up the request automatically.

### App-level limits

Set in `.env`:
- `DAILY_TOKEN_LIMIT` — tokens per user per day across all providers (resets at midnight UTC)
- `MAX_USERS` — maximum registered accounts

---

## Kong Gateway — Plugin Reference

Kong is built on OpenResty (NGINX + Lua). It handles tens of thousands of concurrent
connections with no thread-per-request overhead.

### Useful plugins

**Security**

| Plugin | What it does |
|--------|-------------|
| `key-auth` | Require an API key header on every request |
| `jwt` | Bearer token validation (already in `kong.yml`) |
| `ip-restriction` | Whitelist or blacklist IP ranges |
| `bot-detection` | Block known scrapers |
| `cors` | Cross-origin request control |

**Rate limiting**

| Plugin | What it does |
|--------|-------------|
| `rate-limiting` | Requests per second/minute/hour (already in `kong.yml` at 15 rpm) |
| `request-size-limiting` | Cap request payload size |

**Observability**

| Plugin | What it does |
|--------|-------------|
| `prometheus` | Exposes `/metrics` for Grafana |
| `file-log` | Log requests to stdout (already in `kong.yml`) |
| `http-log` | Forward logs to a webhook or logging service |

**Reliability**

| Plugin | What it does |
|--------|-------------|
| `proxy-cache` | Cache identical responses — saves tokens on repeated prompts |
| `circuit-breaker` | Stop sending to a failing upstream automatically |
| `health-checks` | Active health polling on upstream targets |
