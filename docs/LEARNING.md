# Learning log

## Phase 1 — Project foundation and CRUD

### What was built

- FastAPI application structure, configuration through environment variables,
  explicit CORS, JSON request logs, and request IDs.
- Typed SQLAlchemy models and an Alembic migration for users, projects,
  environments, flags, and environment-specific flag settings.
- Registration/login with Argon2 password hashing and expiring JWTs. REST
  endpoints use bearer auth; dashboard forms use HttpOnly cookies and CSRF
  tokens.
- Project, environment, and flag CRUD API, including environment-specific
  enable/kill-switch settings, flag archiving, and ownership checks.
- Jinja dashboard pages for account creation/login, project creation, flag
  creation/details, and enabling/disabling a flag.
- Docker Compose services for PostgreSQL 16 and Redis 7, plus SQLite-backed
  API/dashboard tests.

### Key concepts

- A migration is a versioned schema change; it makes database setup repeatable
  instead of relying on tables being created implicitly at application startup.
- A foreign key represents ownership/dependency. Unique constraints enforce
  invariants even when concurrent requests try to create duplicate records.
- Passwords are stored as slow, salted Argon2 hashes, never as plaintext.
- A JWT contains a signed user identifier and expiry, not a password. Bearer
  tokens are explicit on API requests; browser cookies are ambient and thus
  require CSRF protection for state-changing forms. Parakh signs CSRF tokens
  and rotates them after login to prevent cookie-injection attacks.
- A flag is defined once per project; its enabled state is per environment so
  development and production can be managed independently.
- API tests replace the database dependency with in-memory SQLite, making
  request behavior repeatable without running local infrastructure.

### Interview questions

1. **Why hash passwords instead of encrypting them?**  
   Password verification only needs to check a candidate, not recover the
   original. A salted, deliberately expensive password hash limits damage if
   the database leaks; encryption would require protecting a decryption key.

2. **Why use database uniqueness constraints if the API checks input?**  
   Two requests can pass an application-level check at the same time. A
   database constraint is the final concurrency-safe guarantee; the API
   translates violations into a useful conflict response.

3. **What is the difference between authentication and authorization?**  
   Authentication establishes who sent the request. Authorization checks
   whether that user may act on a resource. Every project-scoped query filters
   by the authenticated owner to enforce authorization.

4. **Why are dashboard cookies protected against CSRF?**  
   Browsers automatically attach cookies to cross-site requests. A malicious
   site could otherwise cause an authenticated user's browser to submit a
   change. The dashboard checks a token supplied both as a cookie and in the
   form.

5. **Why keep a flag's definition separate from its environment state?**  
   The key, name, type, and default describe one project-level flag. Enablement
   varies by environment. Separating them avoids duplicated definitions and
   allows independent rollout controls later.

## Phase 2 — Evaluation, targeting, SDK keys, and audit

### What was built

- Pure-Python flag evaluation with shared backend/SDK MurmurHash3 bucketing,
  kill-switch precedence, targeting conditions, and percentage rollouts.
- Environment-scoped SDK keys that are shown once and stored as SHA-256
  digests; create/list/revoke APIs.
- `/v1/evaluate`, `/v1/evaluate/all`, and `/v1/config` with ETag/304 responses,
  Redis cache, invalidation, and a short-TTL database fallback.
- Audit log records for flag and environment configuration changes; public
  SDK request rate limits.

### Key concepts

- Deterministic hashing lets independent SDK processes compute the same
  rollout assignment without saving every user assignment in the database.
- Hashing a high-entropy API key is appropriate for lookup; a slow password
  hash is used for human passwords because they are low-entropy.
- ETags let a polling client avoid downloading unchanged config using HTTP
  304 responses.
- Cache invalidation is attempted immediately after mutation; TTL limits
  staleness when Redis is temporarily unreachable.
- Evaluation is pure: the engine accepts config and user data and has no DB,
  Redis, or network dependency.

### Interview questions

1. **Why use a hash for percentage rollout?**  
   It gives stable user assignments across processes and restarts, while
   avoiding a per-user database lookup.
2. **Why SHA-256 SDK keys but Argon2 passwords?**  
   SDK keys are machine-generated with high entropy, so a fast digest is
   sufficient for lookup. Human passwords are guessable and need a slow,
   memory-hard password hash.
3. **What does an ETag do?**  
   It identifies a representation version. A client sends `If-None-Match`;
   if config is unchanged, the server replies 304 without resending the body.
4. **How does a kill switch interact with targeting?**  
   It is checked first and forces the default, overriding targeting and
   rollout so an operator can stop a feature immediately.
5. **What happens when Redis is unavailable?**  
   Cache reads fall back to Postgres with a structured warning, while public
   requests requiring the shared rate limiter fail with 503 rather than
   silently bypassing the limit.

## Phase 3 — Python SDK and demo app

### What was built

- Installable `parakh-sdk` package with shared bucketing/evaluation behavior.
- Background conditional config polling, in-memory local checks, last-known
  config and caller-default fallbacks, and bounded asynchronous event batching.
- Chai Corner FastAPI demo app and a deterministic 5,000-user traffic
  simulation script.

### Key concepts

- Network I/O is moved off the feature-check hot path; the SDK evaluates
  cached configuration locally.
- A daemon thread refreshes config, while a bounded queue batches tracking
  events without blocking the host app's request thread.
- `close()` signals the SDK to flush pending events and stop its workers.
- Context-manager support makes cleanup predictable with `with`.
- A config fallback preserves availability during a control-plane outage.

### Interview questions

1. **Why evaluate locally in the SDK?**  
   Flag checks are frequent and latency-sensitive. Local evaluation removes
   one network round trip per check.
2. **How does the SDK learn about changes?**  
   It polls config with an ETag; unchanged responses are 304 and retain the
   current in-memory ruleset.
3. **What happens if the server is down?**  
   The last successful config remains active. Before any config has loaded,
   the API caller's supplied default is returned.
4. **Why is the event queue bounded?**  
   An unavailable server must not cause unlimited memory growth in the host
   process. Queue saturation is logged instead of blocking application code.
5. **Why expose `close()` and context-manager support?**  
   They provide a clear lifecycle boundary for flushing events and stopping
   background threads during application shutdown.

## Phase 4 — Redis Streams event pipeline

### What was built

- Validated batched event ingestion, keyed per environment, into Redis Streams.
- Consumer-group worker that recovers abandoned pending entries, batch-inserts
  transactionally, acknowledges after commit, and routes poison entries to a
  dead-letter stream.
- Unique event IDs plus conflict-safe insertions for idempotent retries.
- Direct-to-Postgres publisher interface for comparing a queue-less baseline.

### Key concepts

- Streams decouple API response time from event database writes.
- A consumer group tracks pending messages; acknowledging before commit could
  lose events, so the worker commits first.
- At-least-once delivery is made safe with a unique event ID and idempotent
  database insertion.
- Batches reduce database round trips while keeping memory and latency bounded.
- Dead-letter records preserve malformed or repeatedly failing entries for
  later inspection instead of retrying poison data forever.

### Interview questions

1. **Why use a stream instead of writing every event synchronously?**  
   The API can acknowledge queued work quickly, while the worker amortizes
   database cost with batches and absorbs temporary database slowness.
2. **Why acknowledge only after the database commit?**  
   If a message were acknowledged first and the insert then failed, Redis
   would no longer retain the event for retry.
3. **How are duplicate deliveries handled?**  
   `event_id` has a unique constraint and inserts use conflict-do-nothing, so
   retries do not duplicate records.
4. **Why batch inserts?**  
   Batching reduces round trips and transaction overhead; batch size bounds
   memory and the amount of work held per transaction.
5. **When would Kafka be a better choice?**  
   At much higher sustained throughput or when partitioned replay and
   large-scale independent consumer groups justify its operational cost.

## Phase 5 — Experiments and statistics

### What was built

- Experiment creation with traffic-split variants, primary metric, start/stop,
  exposure attribution, and results API/dashboard.
- Pure-Python two-proportion z-test, confidence interval, sample-size
  calculator in both the API and dashboard, and chi-square SRM check; tests
  compare the z-test to SciPy. Multi-variant experiments compare every
  treatment against the first (control) variant.
- Results charts and an explicit peeking warning.

### Key concepts

- A conversion rate is conversions divided by exposed unique users, not event
  count divided by exposure event count.
- A two-proportion test uses a pooled estimate for the null-hypothesis
  standard error; a confidence interval for the difference uses an unpooled
  standard error.
- SRM compares observed assignments with configured allocation shares.
- Sample size is computed before an experiment to reduce underpowered tests.
- Repeated peeking inflates false-positive probability unless the analysis
  accounts for sequential looks.

### Interview questions

1. **Why compare unique users rather than raw conversion events?**  
   A user can trigger the metric repeatedly. Counting unique converters
   prevents heavy users from dominating a binary conversion rate.
2. **What does a p-value mean here?**  
   Under the null of equal conversion rates and test assumptions, it is the
   probability of a result at least this extreme; it is not the probability
   that the treatment is false.
3. **Why calculate an interval as well as a p-value?**  
   The interval communicates the plausible range and magnitude of the
   treatment-control difference, not only a significance threshold.
4. **What does SRM indicate?**  
   A significant allocation mismatch can reveal broken bucketing, filtering,
   or exposure logging before interpreting conversion results.
5. **Why warn about peeking?**  
   Repeated unplanned significance checks increase the chance of a false
   positive; a planned sample size and stopping rule make interpretation more
   reliable.

## Phase 6 — Quality, CI, and load tests

### What was built

- Ruff lint/format and mypy configuration, pre-commit hooks, and GitHub Actions
  test jobs across supported Python versions.
- Locust config/evaluate scenario and a measurement guide.
- Coverage instrumentation targeting the pure evaluation and statistics
  modules.

### Key concepts

- CI makes correctness checks reproducible on each proposed change.
- A matrix catches Python-version-specific regressions.
- Coverage is useful for identifying untested branches but does not prove
  correctness by itself.
- Load tests need recorded request rate, error rate, p50, and p95 under a
  documented workload.
- Performance claims require measurements from a running, representative
  deployment; this repository intentionally records no fabricated latency.

### Interview questions

1. **Why use a CI version matrix?**  
   It verifies the supported interpreter range rather than assuming a local
   Python version represents all users.
2. **What does coverage tell you?**  
   Which lines/branches ran in tests. It does not show whether assertions
   validate the right behavior.
3. **Why record p95 instead of only average latency?**  
   Tail latency reflects the slower requests users still experience and can
   expose queueing or dependency stalls hidden by averages.
4. **Why keep lint and format separate?**  
   Lint checks common correctness/style issues; format makes layout
   deterministic. They provide distinct, fast feedback.
5. **Why is there no claimed 50 ms result?**  
   Docker and the actual production dependencies were unavailable during
   authoring; a target is not a measurement.

## Phase 7 — Deployment and presentation

### What was built

- Docker Compose API, Postgres, Redis, worker, and demo services; separate
  production Docker entrypoints for API and worker.
- Render Blueprint, provider deployment notes, project README, architecture
  diagram, five-minute demo walkthrough, and learning reflection template.

### Key concepts

- Container boundaries keep API, worker, database, and cache responsibilities
  separate while sharing a reproducible image/build context.
- Secrets are injected through environment configuration and are not stored
  in source control.
- Readiness probes should check required dependencies; liveness only checks
  that the process is responding.
- The worker is an independently scalable process and must use the same
  database and Redis as the API.
- Deployment provider free-tier capabilities and prices change; verify them
  before presenting a live deployment.

### Interview questions

1. **Why separate the worker from the API?**  
   Event ingestion and user-facing requests scale and fail independently;
   slow database batches do not consume API request workers.
2. **Why use environment variables for secrets?**  
   They allow deployments to inject credentials without committing or baking
   them into an image. A managed secret store is preferable in production.
3. **What is the difference between liveness and readiness?**  
   Liveness asks whether the process should be restarted; readiness asks
   whether it can currently serve traffic, including dependency checks.
4. **How would you scale the event pipeline?**  
   Increase consumers within the consumer group, tune batch size, monitor
   pending age and DB capacity, then shard streams or move to Kafka if
   measured volume warrants it.
5. **What would you improve next?**  
   Add organization/project memberships and roles, advanced staged
   migrations, robust scheduled experiment analysis, and measured profiling
   before optimizing the hot paths.
