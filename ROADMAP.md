# Roadmap

The roadmap is ordered by user risk and product value. Planned items are not advertised as shipped features.

## Now: reliable shared access

- [ ] Replace the inactive production database and verify migrations.
- [ ] Make token reservation and reconciliation atomic under concurrent requests.
- [ ] Add request-per-minute and concurrent-request allowances per user.
- [ ] Add provider health records, cooldowns, and circuit breakers.
- [ ] Add mocked integration tests for every provider stream and failure class.
- [ ] Replace the shared password-reset secret with expiring single-use email tokens.

## Next: small-team governance

- [ ] Invitation links and configurable registration policy.
- [ ] Workspaces, roles, and shared/private agent visibility.
- [ ] Revocable API keys scoped to a user or workspace.
- [ ] Provider and model allowlists per workspace.
- [ ] Conversation export, deletion, and retention periods.
- [ ] Usage alerts and quota-reset visibility.

## Later: smarter routing

- [ ] `auto/fast`, `auto/quota`, `auto/quality`, and round-robin policies.
- [ ] Context-window and task-capability filtering.
- [ ] Retry classification with exponential backoff and `Retry-After` support.
- [ ] OpenAI tool-call normalization and Responses API compatibility.
- [ ] Optional local-model targets.
- [ ] Independent security review and expanded CI checks.

