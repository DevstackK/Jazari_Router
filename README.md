# JazariRoute

A self-hosted, free-first multi-provider AI gateway for small teams. It combines direct provider free tiers with OpenRouter's live free-model catalog, automatic failover, private agents, per-user limits, and an OpenAI-compatible API.

Free and open source under the [MIT License](LICENSE). Upstream providers control their own availability, quotas, eligibility, and terms.

[Live app](https://jazariroute.vercel.app) · [Complete feature inventory](FEATURES.md) · [Comparison with OmniRoute](COMPARISON.md) · [Roadmap](ROADMAP.md)

**Free-first AI routing for everyone.** JazariRoute is named for the 13th-century engineer Ismail al-Jazari. Its elephant-clock mark celebrates engineering that is useful, inventive, and open to the world.

---

## Features

- **Multi-provider routing** — Gemini, Groq, Mistral, Cohere, and OpenRouter in one interface
- **Auto failover** — if one provider is rate-limited or down, the next picks up instantly
- **Free models only** — OpenRouter models require both input and output prices to equal zero
- **OpenAI-compatible** — use `/v1/models` and `/v1/chat/completions` with existing clients
- **Daily top-10 featured models** — cron job fetches the best free models from OpenRouter every day at 9am UTC
- **JWT authentication** — protected endpoints and bootstrap-token-controlled first administrator
- **Per-user token limits** — configurable daily cap, resets at midnight UTC
- **Private agent system** — each user's assistants and history are tenant-isolated
- **Admin dashboard** — user management, token usage stats, provider breakdown, activity log
- **Vercel ready** — deploys as a Python serverless function in one command
- **Supabase Postgres** — fully stateless server, all data in the cloud

See [FEATURES.md](FEATURES.md) for the full shipped inventory and explicit limitations. For a current, evidence-based positioning against OmniRoute, see [COMPARISON.md](COMPARISON.md).

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python + Flask |
| Database | Supabase Postgres (psycopg2) |
| Auth | JWT (PyJWT) |
| Streaming | Server-Sent Events (SSE) |
| API Gateway | Kong (Docker, optional) |
| Hosting | Vercel (serverless) |
| Frontend | Vanilla JS + HTML (no build step) |

---

## Providers

| Provider | Model | Free Limit |
|----------|-------|-----------|
| Gemini | 2.5 Flash | 15 req/min, 1M tokens/day |
| Groq | Llama 3.3 70B | 30 req/min, 14,400 req/day |
| Mistral | Mistral 7B | 60 req/min |
| Cohere | Command-A | 20 req/min |
| OpenRouter | Top 10 free models (daily) | Varies per model |

---

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/DevstackK/JazariRoute.git
cd JazariRoute
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Set up environment

```bash
cp .env.example .env
```

Edit `.env` with your API keys and Supabase connection string. See [HOW_TO.md](HOW_TO.md) for where to get each key.

### 3. Set up Supabase

1. Create a free project at https://supabase.com
2. Go to **Project Settings → Database → Session pooler** and copy the connection URI
3. Paste it as `DATABASE_URL` in your `.env`

Tables are created automatically on first run.

### 4. Run

```bash
python api/index.py
```

Open **http://localhost:5001**. The first registration must send the configured `ADMIN_BOOTSTRAP_TOKEN`; subsequent registrations do not become administrators.

---

## Deployment

### Vercel (recommended)

```bash
npm install -g vercel
vercel
```

Add all `.env` variables to your Vercel project dashboard, then:

```bash
vercel --prod
```

The cron job (`0 9 * * *`) runs automatically where scheduled cron is available. Every refresh request must include `Authorization: Bearer $CRON_SECRET`.

### Docker + Kong

```bash
docker compose up -d
```

Kong proxy on **:8000**, admin API on **:8001**. See [HOW_TO.md](HOW_TO.md) for plugin configuration.

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `GEMINI_API_KEY` | Google Gemini API key |
| `GROQ_API_KEY` | Groq API key |
| `MISTRAL_API_KEY` | Mistral API key |
| `COHERE_API_KEY` | Cohere API key |
| `OPENROUTER_API_KEY` | OpenRouter API key (enables 5th provider + featured models) |
| `OPENROUTER_MODEL` | Default model — must end in `:free` |
| `JWT_SECRET` | Random secret of at least 32 characters; required in production |
| `GATEWAY_API_KEY` | Stable bearer key for OpenAI-compatible clients |
| `ADMIN_BOOTSTRAP_TOKEN` | Required to create the first administrator |
| `RESET_SECRET` | Passphrase for the password reset endpoint |
| `CRON_SECRET` | Bearer secret protecting the catalog refresh endpoint |
| `APP_ORIGINS` | Comma-separated browser origins allowed by CORS |
| `MAX_USERS` | Maximum registered accounts |
| `DAILY_TOKEN_LIMIT` | Per-user token cap per day |
| `DATABASE_URL` | Supabase session pooler connection string |

---

## API

All endpoints require `Authorization: Bearer <token>` except register, login, and health.

```
POST /api/register          Register a new account
POST /api/login             Login and receive a JWT
GET  /api/health            Provider configuration status
GET  /api/providers         List available providers and featured models
POST /api/chat              Stream a chat response (SSE)
GET  /v1/models             OpenAI-compatible model catalog
POST /v1/chat/completions   OpenAI-compatible chat (streaming or JSON)
GET  /api/agents            List agents
POST /api/agents            Create an agent
DELETE /api/agents/:id      Delete an agent
GET  /api/agents/:id/messages   Message history
POST /api/agents/:id/messages   Save messages
GET  /api/cron/refresh-models   Trigger featured models refresh
```

Full request/response examples in [HOW_TO.md](HOW_TO.md).

---

## File Structure

```
├── api/
│   └── index.py           # Flask app — all routes, SSE, auth, DB
├── static/
│   ├── index.html         # Chat UI
│   └── admin.html         # Admin dashboard
├── vercel.json            # Vercel routing + cron
├── docker-compose.yml     # Kong container
├── kong.yml               # Kong declarative config
├── requirements.txt       # Python dependencies
├── .env.example           # Environment variable template
└── HOW_TO.md              # Full setup and deployment guide
```

---

## Full Documentation

See [HOW_TO.md](HOW_TO.md) for complete setup instructions including Supabase configuration, Docker + Kong plugin reference, Vercel deployment steps, and the full API reference.
