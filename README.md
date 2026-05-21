# Atlas

Persistent autonomous AI orchestration system — not a chatbot.

Atlas controls coding agents, terminal sessions, browser automation, memory, Slack, and **multi-pool workload routing**. Phase 13 added subscription/API worker pools with isolated tmux namespaces — see [docs/ATLAS_PHASES.md](docs/ATLAS_PHASES.md).

## Phase 1–2 scope

- Core Python orchestrator with asyncio task loop
- Config loader (YAML + environment variables)
- Logging to console and `logs/atlas.log`
- Task manager (in-memory) + structured `TaskResult`
- SQLite memory store skeleton
- **tmux session control** (create, send-keys, capture, kill; WSL fallback on Windows)
- **Claude Code agent** — launch `claude` in tmux, inject prompts, monitor output
- Slack bot skeleton

## Requirements

- Python 3.11+
- [tmux](https://github.com/tmux/tmux/wiki) (optional for Phase 1; required for terminal agents)

## Quick start

```bash
cd Atlas
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

pip install -r requirements.txt
copy .env.example .env          # edit as needed

python main.py --demo           # orchestrator tick (no tmux)
python main.py --run-claude     # one Claude Code task via tmux (requires tmux)
python main.py --run-claude --prompt "Fix the README typo"
python main.py                  # run orchestrator loop (Ctrl+C to stop)
```

## Project layout

```
Atlas/
├── core/           # Orchestrator, config, logging, tasks
├── agents/         # Agent implementations (Claude Code, etc.)
├── memory/         # SQLite project memory
├── sessions/       # tmux terminal control
├── slack/          # Slack bot
├── tools/          # Playwright, APIs (future)
├── logs/           # Runtime logs
├── prompts/        # System / agent prompts
├── configs/        # Non-secret YAML defaults
├── projects/       # Per-project workspaces
└── main.py         # Entry point
```

## Configuration

| Source | Purpose |
|--------|---------|
| `configs/default.yaml` | Defaults (poll interval, paths) |
| `.env` | Secrets and overrides (`SLACK_*`, `ATLAS_*`) |

Never commit `.env` or tokens.

## Slack remote control (Phase 3)

| Command | Action |
|---------|--------|
| `/atlas start <prompt>` | Queue Claude Code task |
| `/atlas stop [id] [--kill]` | Cancel task; optional tmux kill |
| `/atlas status` | Task IDs, statuses, durations |
| `/atlas sessions` | tmux sessions + task mapping |
| `/atlas logs [id]` | Log tail or task output preview |

See [prompts/slack-setup.md](prompts/slack-setup.md) for app configuration.

## Browser verification (Phase 4)

```bash
pip install playwright
playwright install chromium
python main.py --run-browser --url https://example.com
```

Set `metadata.agent: browser` with `steps`, `workflow`, or `url`. Examples in [configs/browser-examples.yaml](configs/browser-examples.yaml).

| Workflow | Purpose |
|----------|---------|
| `verify_login` | Login form + dashboard visible |
| `verify_button` | Click + result element |
| `verify_notification_badge` | Badge visible/text |
| `verify_mobile_layout` | Mobile viewport + nav |

Screenshots: `logs/screenshots/`. Failures upload to Slack when enabled.

## Adaptive recovery (Phase 5)

Not a dumb retry loop. On failure Atlas:

1. Records attempt history (strategy, category, diagnostics)
2. Classifies failure (frontend, backend, auth, network, …)
3. Detects contradictions (same symptom, different strategies)
4. Enters **INVESTIGATING** — gather logs, no patching
5. Selects a **new** recovery strategy (never repeats exhausted ones)
6. Builds a root-cause retry prompt for Claude Code
7. Escalates to Slack + BLOCKED when limits hit

Configure in `configs/default.yaml` under `recovery:`. See [configs/recovery-chain-example.yaml](configs/recovery-chain-example.yaml).

## Git safety (Phase 6)

Atlas will **not** autonomously edit `main`/`master` by default.

- **Isolated branch** per task: `atlas/task-<id>`
- **Checkpoints** before retries: `atlas-checkpoint: task=... strategy=... attempt=...`
- **Rollback** when health regresses or build catastrophically fails
- **Health score** tracks verification, console, and network trends

```bash
pip install GitPython
```

See [configs/git-safety-example.yaml](configs/git-safety-example.yaml). Project directory must be inside a git repository.

## Operational memory (Phase 7)

Structured SQLite memory — **not** chat logs:

| Category | Stores |
|----------|--------|
| Project | Architecture, stack, auth/DB notes |
| Recovery | What strategies worked/failed |
| Repository | Fragile files, high-risk zones |
| Task | Verification outcomes, regression history |
| System | Strategy performance, safety rules |

Before each task: `ContextBuilder` injects concise history into prompts.  
After each task: `MemorySummarizer` distills outcomes and updates risk maps.

See [configs/operational-memory-example.yaml](configs/operational-memory-example.yaml).

## Persistent runtime (Phase 8)

Atlas survives restarts as operational infrastructure:

| Component | Role |
|-----------|------|
| `core/runtime_manager.py` | Startup recovery, periodic snapshots, graceful shutdown |
| `core/runtime_recovery.py` | Restore tasks, reconnect tmux, integrity checks |
| `core/task_store.py` | SQLite `persisted_tasks` — status, metadata, recovery chains |
| `core/runtime_state.py` | JSON snapshot at `logs/runtime_state.json` |
| `core/project_scheduler.py` | Per-project queues and priority |
| `core/resource_manager.py` | Max Claude/browser/total concurrency |
| `core/dashboard.py` | Terminal operational dashboard |

```bash
python main.py                  # persistent loop (Ctrl+C graceful shutdown)
python main.py --dashboard      # one-shot status view
```

Runtime settings in `configs/default.yaml` under `runtime:`.

Future extension points (not implemented): `core/future_systems.py`.

## Worker pools (Phase 13)

Atlas routes Claude tasks through **isolated worker pools** instead of switching auth in one session:

| Pool | Session prefix | Auth |
|------|----------------|------|
| Subscription | `atlas-sub-*` | Claude subscription login |
| API | `atlas-api-*` | `ANTHROPIC_API_KEY` (fallback / overflow) |
| Local | `atlas-local-*` | Placeholder (future) |

Subscription-first routing; on exhaustion, `AuthMonitor` cools down the subscription pool and overflows to API. Dashboard shows pool health and cooldowns.

Config: `configs/default.yaml` → `worker_pools:`. Details: [docs/ATLAS_CONTEXT.md](docs/ATLAS_CONTEXT.md), [docs/ATLAS_PHASES.md](docs/ATLAS_PHASES.md).

## Roadmap (high level)

1. **Phase 1** — Architecture skeleton
2. **Phase 2** — tmux + Claude Code terminal control
3. **Phase 3** — Slack remote control
4. **Phase 4** — Playwright browser verification
5. **Phase 5** — Adaptive recovery
6. **Phase 6** — Git safety
7. **Phase 7** — Operational memory
8. **Phase 8** — Persistent runtime, dashboard
9. **Phase 9–11** — PR lifecycle, cross-project intelligence, lesson feedback
10. **Phase 12** — Proactive task scanner
11. **Phase 13** — Worker pool orchestration (current)
12. **Future** — Local/OpenAI/Gemini pools, distributed workers, LangGraph, cost-aware routing

## License

Private / TBD
