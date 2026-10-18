# Feature inventory

This document describes capabilities present in the current `main` branch. It does not count planned work as shipped.

## Gateway and models

- One gateway for Gemini, Groq, Mistral, Cohere, and OpenRouter.
- Ordered automatic fallback across configured direct providers.
- OpenRouter catalog refresh that admits a model only when both its input and output price are exactly zero.
- Daily top-ten free-model selection, with text/chat suitability and minimum-context filtering.
- Server-sent-event streaming for every supported provider.
- OpenAI-compatible `GET /v1/models` and `POST /v1/chat/completions` endpoints.
- Native JSON responses and OpenAI-compatible streaming responses.
- Direct provider selection or the virtual `auto` and `auto/free` model names.

## Multi-user application

- Email/password accounts with hashed passwords and expiring JWT sessions.
- Bootstrap-token protection for creation of the first administrator.
- Configurable maximum users and daily token allowance.
- Private, user-owned agents with custom system prompts.
- Persistent, tenant-isolated message history.
- Administrator dashboard for users, usage, providers, activity, and free-model catalog state.

## Security and operations

- Stable gateway API key for non-browser OpenAI clients.
- Configurable CORS allowlist and maximum request size.
- Protected scheduled catalog refresh.
- Production startup checks for required secrets.
- Database failures are reported as unavailable without taking down the public interface.
- Vercel, local Python, and optional Kong deployment assets.
- Reproducible Python 3.12 environment with a committed `uv.lock`.
- MIT license.

## Important limits

- Kongllmrouter does not provide unlimited inference. Every upstream provider controls eligibility, quotas, availability, and terms.
- `auto` currently uses ordered fallback; it is not latency-, cost-, or health-scored routing.
- Chat completions are supported. Responses API, embeddings, images, audio, tool-call normalization, MCP, and A2A are not yet implemented.
- The hosted service requires a working PostgreSQL-compatible `DATABASE_URL` for accounts, quotas, agents, and the model catalog.

