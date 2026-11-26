# BUP-check Implementation Status

- **Baseline Commit:** `3e791d6dea9947ceeff514253e0fd8855f11e3fa` (`v1.0.1` preserved)
- **Working Branch:** `feat/final-platform` (release candidate committed & pushed; human live demo rehearsal pending)
- **Last Updated:** 2026-11-26

## Final Gate Status

| Gate | Status | Summary |
| --- | --- | --- |
| G0 Baseline | PASS | Isolated baseline verified (`npm ci`, Vite build, `verify.mjs e2e`, OpenAPI parity) before applying final fixes. |
| G1 Simulator | PASS | Atomic `before==after` reads, circuit breaker, real `stream_disconnect` SSE reconnect, `RUNNING` `15.0 s` grace cache, `X-Bup-*` headers, and strict `stale_data` `503` blocking verified (`17/17`). |
| G2 Data | PASS | Transactional SQLite WAL store (`SCHEMA_VERSION = 4`), immutable plan retention protection, generation isolation, causal `v1.0.0` features, and 3 simulator datasets (`normal_baseline`, `crisis_dev_mix`, `crisis_heldout_mix`). |
| G3 Intelligence | PASS | Core stack runs with zero local weights (`SEASONAL`); `--profile ml` runs pinned offline `amazon/chronos-2@29ec3766d36d` + locked `iforest-bup-v1.0.0` (`F1=0.9114`, `recall=1.0`, `FPR=0.01` held-out), SHA-256 verified with `--network=none`. |
| G4 Decision quality | PASS | OR-Tools `CBC` + validator + `SAFE_REORDER` evaluated fairly across matched budgets (`single_1_7k`, `batch_4_15k`, `unconstrained`), decomposing `outage_unmet_liters` (`2,696.711 L`) vs depletion (`320.415 L`) in [evaluation.md](evaluation.md). |
| G5 Approval | PASS | Backend-owned `PUT /api/plans/{plan_id}/approval` & single-item `PUT /api/approvals/{intent_id}`, deterministic `uuid5` intents, reload restore, simultaneous submit dedup (`35/35` `200`, `10` total allocs), restart replay, cancel refund, and `409` reset block. |
| G6 Product | PASS | 6-tab Command Center with batch plan review, live vs observed provenance, evaluation & forecast tables, `"not_yet_measured"` banner, and mobile responsiveness. |
| G7 Resilience | PASS | `17/17` pass in [run-resilience-matrix.py](../scripts/run-resilience-matrix.py) across all 6 domain events, all 5 API faults, ML stop/restart, concurrent approvals, restart replay, cancel, and reset isolation. |
| G8 Observability/load | PASS | Component health, `/api/metrics`, Prometheus `up`, and `0` errors across all 8 benchmark scenarios including `120.39 s` sustained `c=10` (`106.37 req/s`) and `running_simulator_c8` (`age_ticks_max=0`). |
| G9 Deployment | PASS | Clean-core without weights, locked `requirements.lock.txt`, `scripts/prepare-models.py`, curated [evidence/](evidence/) clean-checkout fallback, and `actionlint` clean. |
| G10 Submission | PASS | Relative-link checked docs, GitHub Mermaid architecture diagram, deterministic demo runbook, clean-source candidate verified (`115` files, curated fallback), and release checklist (`Human live demo rehearsal: Pending`). |
