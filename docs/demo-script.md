# Five-minute demo script

1. **0:00–0:40 — Problem and value.**  
   “Parakh helps teams ship features safely and decide with data. A flag can
   be scoped by environment, rolled out gradually, and turned off quickly.”
2. **0:40–1:30 — Create and configure.**  
   Register, create a Chai Corner project, add `order-button-color`, choose
   the string type, and set a default value. In Development set the enabled
   value, rollout, and (optionally) a `plan == pro` rule. Toggle the kill
   switch and show the default wins.
3. **1:30–2:20 — SDK and fast evaluation.**  
   Create an environment SDK key, copy it once, start the Chai Corner demo,
   and show that the feature decision is served from the SDK's local config.
4. **2:20–3:20 — Experiment.**  
   Create a 50/50 experiment with `green` and `orange` variants and
   `order_clicked` as the primary metric. Show that the same user gets a
   stable variant and that exposure tracking is automatic.
5. **3:20–4:20 — Events and results.**  
   Run `demo_app/simulate_users.py`, show worker ingestion and the results
   page's variant counts, conversion rates, confidence interval, SRM, and
   peeking warning.
6. **4:20–5:00 — Engineering choices.**  
   Explain deterministic hashing, Redis Streams/commit-before-ack,
   idempotency, ETag caching, and the test/CI setup. Avoid claiming a latency
   threshold unless you have recorded a Locust run.

