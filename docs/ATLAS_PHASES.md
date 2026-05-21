# Atlas — Phase History

Engineering memory for Atlas evolution. For full architecture, bugs, and config detail see [ATLAS_CONTEXT.md](ATLAS_CONTEXT.md).

Last updated: 2026-05-21  
**Current phase: 13 complete**

---

## Phase index

| Phase | Name | Status | Primary artifacts |
|-------|------|--------|-------------------|
| 1 | Core loop | Complete | `orchestrator.py`, `task_manager.py` |
| 2 | Claude Code agent | Complete | `agents/claude_code.py`, `sessions/tmux_manager.py` |
| 3 | Slack integration | Complete | `slack/`, `atlas_controller.py` |
| 4 | Browser verification | Complete | `agents/browser_task.py`, Playwright |
| 5 | Adaptive recovery | Complete | `recovery_engine.py`, `failure_classifier.py` |
| 6 | Git safety | Complete | `git_safety.py`, `rollback_engine.py` |
| 7 | Operational memory | Complete | `memory/` |
| 8 | Persistent runtime | Complete | `runtime_manager.py`, `task_store.py`, `dashboard.py` |
| 9 | GitHub PR lifecycle | Complete | `pr_manager.py`, `approval_manager.py` |
| 10 | Cross-project intelligence | Complete | `lesson_extractor.py`, global `__global__` lessons |
| 11 | Lesson quality feedback | Complete | `signal_score`, `injected_lesson_ids`, retire weak lessons |
| 12 | Proactive task suggestions | Complete | `task_scanner.py`, `/atlas scan`, background loop |
| 13 | Worker pool orchestration | Complete | `worker_pool*.py`, `auth_monitor.py`, multi-pool routing |
| — | Local / OpenAI / Gemini / distributed | Planned | `LocalPool`, `future_systems.py` stubs |

> **Note:** Early docs sometimes numbered phases differently (e.g. Slack as “Phase 3” in README vs browser/recovery order in context). This table reflects **delivery order in the repo today**, not renumbered history.

---

## Phase 13 — Worker Pool Orchestration

### Why it existed

- Claude auth switching in a single worker was **brittle**
- API key **overrides** subscription auth in the same CLI session
- Atlas needed **workload routing**, not auth hacking at runtime
- Foundation for **provider-agnostic** orchestration (pools + capabilities, not one Claude session)

### What was built

**Modules:**

- `core/pool_config.py` — types, capabilities, YAML
- `core/worker_pool.py` — `SubscriptionPool`, `ApiPool`, `LocalPool` placeholder
- `core/worker_registry.py` — pool registration, active worker sync
- `core/worker_router.py` — routing policy
- `core/auth_monitor.py` — exhaustion detection, cooldowns
- `core/worker_pool_manager.py` — coordinator + per-pool tmux

**Integrations:** `orchestrator.py`, `main.py`, `agents/claude_code.py`, `dashboard.py`, `slack/bot.py`, `configs/default.yaml` (`worker_pools`)

### Pool types

| Pool | Prefix example | Auth | Role |
|------|----------------|------|------|
| SubscriptionPool | `atlas-sub-assignmint` | Subscription login | Preferred, low cost |
| ApiPool | `atlas-api-assignmint` | `ANTHROPIC_API_KEY` | Fallback / overflow |
| LocalPool | `atlas-local-*` | — | Placeholder (not implemented) |

### Routing philosophy

Tasks are routed by:

- capabilities (`metadata.capabilities`)
- pool availability (busy vs `max_workers`)
- task priority
- cost tier (subscription before API)
- cooldown state (subscription skipped when exhausted)

Default: **subscription → API → local (skip)**.

### Pool isolation

- Separate **tmux** `TmuxManager` per pool
- Separate **session prefixes** (`atlas-sub-*`, `atlas-api-*`)
- Separate **auth launch** (plain `claude` vs env-injected API key)
- Separate **task metadata** (`worker_pool`, `session_prefix`, `pool_auth_mode`, …)

### Auth monitoring

- **Centralized** markers in `auth_monitor.py` (not duplicated in agents)
- Subscription **cooldown** on exhaustion (no full Atlas restart)
- **Overflow** to API pool while subscription recovers

### Dashboard

`python main.py --dashboard` — **WORKER POOLS** section: health, busy/max, cooldown, queued hints.

### Slack

- Subscription pool exhausted → route to API
- Subscription pool restored
- API pool overloaded
- Per-task routing / overflow notifications

### Architecture after Phase 13

```
WorkerRouter
    ↓
WorkerPools
├── SubscriptionPool
├── ApiPool
└── LocalPool (future)
```

Atlas is **multi-pool runtime + workload router**, not a single-worker system.

---

## Current capabilities (post–Phase 13)

- Orchestration + runtime persistence
- Adaptive recovery + rollback safety
- Operational memory + cross-project lessons
- Worker pools + provider routing + auth-aware distribution
- Slack control + PR lifecycle + approval workflows
- Proactive scanner suggestions

---

## Current priorities

1. Approval workflows  
2. PR lifecycle reliability  
3. Deployment safety (future hooks)  
4. AssignMint operational testing (subscription + API pools)  
5. Runtime hardening  
6. Worker pool refinement + cost-aware routing prep  
7. tmux/WSL operational setup  

---

## Lessons learned (Phase 13)

- **Do not fight Claude auth in one session** — separate pools are cleaner and more reliable.
- **Cooldown a pool, not the process** — subscription exhaustion should not require restarting Atlas.
- **Keep routing out of agents** — `WorkerRouter` decides; agents execute.
