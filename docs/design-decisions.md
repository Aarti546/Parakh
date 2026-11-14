# Design decisions

## Phase 1

### Environment-specific flag settings

Flag identity and its default value live on the project-level `flags` row;
`environment_flags` stores whether the flag is enabled and its kill-switch
state in each environment. Alternatives were duplicating the flag definition
per environment (which makes editing keys/descriptions inconsistent) or
storing environment configuration as an unvalidated JSON document.

### Synchronous SQLAlchemy

The initial API uses SQLAlchemy's synchronous typed ORM because CRUD requests
are database-bound and the sync session lifecycle is easy to understand and
test. Async DB access can be considered after profiling; it would add driver
and session complexity without a demonstrated Phase 1 benefit.

### Dashboard cookie and API bearer authentication

The API uses explicit bearer tokens. The browser dashboard uses a short-lived,
HttpOnly JWT cookie with SameSite=Lax and a double-submit CSRF token for form
posts. The CSRF nonce is HMAC-signed and rotated after login so an attacker
cannot plant a matching token through a sibling subdomain. This keeps bearer
tokens out of browser script access while allowing simple server-rendered
forms. Dashboard templates use Jinja autoescaping; user text is rendered as
text, not marked safe HTML.

### Starter environments and database constraints

Creating a project creates `dev`, `staging`, and `prod` environments so the
first flag can be tried in a realistic setup. Unique constraints protect
project slugs, per-project flag keys, per-project environment keys, and
environment/flag pairs even if concurrent requests race.

Index justifications:

- `users.email` unique index: find an account during login and prevent
  duplicate normalized emails.
- `projects.slug` unique index: enforce unique public project identifiers.
- `projects(owner_user_id, created_at)`: serve each owner's project list in
  creation order.
- `environments(project_id, key)` unique index: enforce unique environment
  names within a project and support project environment lookup.
- `flags(project_id, key)` unique index: enforce unique project flag keys and
  support key lookup.
- `environment_flags.environment_id` and `.flag_id`: support environment
  configuration reads and flag/environment cascade operations.
- `environment_flags(environment_id, flag_id)` unique index: prevent
  duplicate per-environment settings for a flag.
- `sdk_keys.key_hash` unique index: authenticate SDK keys with one indexed
  digest lookup; `(environment_id, revoked_at)` supports key management.
- `audit_logs(project_id)` and `(project_id, created_at)`: scope and order the
  audit history page.
- `events(project_id, event_name, created_at)`: find metric events by project,
  name, and time; `(flag_key, user_id)` supports exposure/user analysis.
- `events.event_id` unique index: make worker retries idempotent.
- `experiments(project_id, key)` unique index: avoid duplicate experiment
  identifiers; `(project_id, status)` supports active experiment lookup.

### Migrations rather than application-startup table creation

Alembic migrations are the source of truth for schema changes. The API does
not silently create or mutate tables at startup; the development container
applies migrations before serving traffic.

### Explicit CORS and CSRF protections

CORS origins come from configuration and wildcard origins are rejected.
Cookie-authenticated dashboard forms require a matching CSRF cookie and form
token. API mutations require bearer authentication rather than falling back
to ambient cookies.

### Authentication rate limiting

Registration and login are limited to 20 requests per client IP per minute in
the current single-process development API. This small in-memory limiter
protects public credential endpoints without trusting spoofable forwarding
headers. It is not shared across workers or replicas; the SDK-facing API will
use a shared Redis-backed limiter when it is added.

### Redis and SDK rate limiting

Redis 7 is included in local Compose now for later phases. Phase 1 has no
unauthenticated evaluation/config endpoints; distributed rate limiting for
those endpoints will be added with them.

### Phase 2 — Evaluation, key storage, and cache

**Deterministic hashing versus database assignment.** Bucketing uses unsigned
MurmurHash3 with a stable flag salt and user ID so the API and SDK can assign
the same user locally without a database read or stored assignment row.
Random per-process hashing and database lookups were rejected because they
would change assignments across restarts or add a read to every flag check.

**Store SDK key digests, not recoverable secrets.** SDK keys have high entropy,
are shown once, and are looked up by SHA-256 digest. Password KDFs are intended
for human-chosen low-entropy secrets; they would add unnecessary cost to SDK
request authentication.

**Redis cache plus database fallback.** Config documents are cached by
environment and carry an ETag. Flag mutations invalidate the affected
environment caches. A short TTL bounds staleness if Redis invalidation fails;
when Redis itself is unavailable the API logs the cache failure and reads
current configuration from Postgres. Config is polled rather than streamed
because periodic refresh and ETag/304 responses are simpler for a small SDK
and bound propagation delay without maintaining persistent client sockets.

### Phase 3 — SDK refresh and local evaluation

The SDK uses a daemon thread for ETag polling and local, synchronous
evaluation. Streaming/SSE would reduce update latency but requires persistent
connections, reconnect logic, and more server resources. Periodic polling is
the simpler tradeoff for an SDK with a zero-network flag-check hot path.
Fallbacks retain the last valid ruleset; before the first successful poll,
the caller's default value is returned. Events use a bounded in-memory queue
so a slow server cannot grow client memory without limit; overload is logged.

### Phase 4 — Event pipeline

**Redis Streams rather than Kafka.** Streams provide durable append, consumer
groups, pending-message recovery, and acknowledgement without introducing a
separate broker cluster. Kafka would be appropriate at substantially higher
throughput or when multi-consumer replay and partition scaling become
requirements. The stream interface also includes a direct-to-Postgres
implementation for understanding and comparing the simpler baseline.

**Batching and idempotency.** The SDK batches events by count/time; the worker
batch-inserts within a transaction and acknowledges only after commit. Event
IDs have a database uniqueness constraint and inserts use `ON CONFLICT DO
NOTHING`, so a crash after a commit but before acknowledgement cannot double
count. Malformed or repeatedly failing entries go to a dead-letter stream.

### Phase 5 — Statistics and experiments

The two-proportion test, confidence interval, sample-size estimate, and
chi-square SRM check are implemented in small pure-Python functions.
NumPy vectorizes the SRM count calculation. SciPy is used only in tests as an
independent numerical oracle for the z-test, not as the production algorithm.
Results warn about repeated peeking because nominal p-values no longer hold
under unplanned repeated significance checks.
