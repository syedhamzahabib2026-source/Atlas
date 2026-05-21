# Atlas — Master Context Document

Last updated: 2026-05-21  
Current phase: **9 complete, Phase 10 in design**  
Repo: `https://github.com/syedhamzahabib2026-source/Atlas`  
Primary project target: `C:\Users\shamz\Desktop\Assignmint`

---

## What Atlas Is

Atlas is an autonomous AI engineering operating system. It receives tasks via Slack, runs Claude Code in isolated tmux sessions, manages git safety, handles failures with a recovery engine, and closes the PR lifecycle — all without human intervention beyond approving the final PR.

The operator sends `/atlas start --project assignmint <prompt>` from Slack. Atlas:
1. Creates a task, isolates a git branch in the target repo
2. Launches Claude Code in a tmux session with operational memory injected
3. Monitors execution, retries failures with escalating strategies
4. On success: pushes the branch, opens a GitHub PR, posts to Slack for approval
5. On `/atlas approve`: merges the PR, marks the task MERGED

---

## Project Layout

```
Atlas/
├── main.py                        # Entry point — runs the orchestrator loop
├── configs/
│   └── default.yaml               # All tunable defaults (no secrets)
├── core/
│   ├── orchestrator.py            # Central async loop, task dispatch, lifecycle
│   ├── atlas_controller.py        # Slack command handler (start/stop/approve/reject)
│   ├── task_manager.py            # In-memory task registry + TaskStatus enum
│   ├── task_store.py              # SQLite persistence for tasks
│   ├── config.py                  # Config loader: .env → YAML → env overrides
│   ├── pr_manager.py              # GitHub API: push_branch, create_pr, merge_pr
│   ├── approval_manager.py        # In-memory approval state (PR awaiting human)
│   ├── git_manager.py             # GitPython operations: branches, checkpoints, rollback
│   ├── git_safety.py              # GitSafetyCoordinator — orchestrator-facing facade
│   ├── git_config.py              # GitSafetyConfig dataclass
│   ├── recovery_engine.py         # Failure recovery: strategy selection, attempt tracking
│   ├── recovery_strategies.py     # Strategy definitions
│   ├── recovery_config.py         # RecoveryConfig dataclass
│   ├── recovery_prompt.py         # Prompt builder for retry strategies
│   ├── health_tracker.py          # Health score tracking, regression detection
│   ├── rollback_engine.py         # Rollback decision + execution
│   ├── failure_classifier.py      # Categorise failure type (auth/frontend/backend/etc)
│   ├── attempt_history.py         # Per-task attempt log
│   ├── blocked.py                 # Block detection + reason labels
│   ├── runtime_manager.py         # RuntimeManager: stale task cleanup, orphan detection
│   ├── runtime_state.py           # RuntimeState persistence (logs/runtime_state.json)
│   ├── runtime_safety.py          # Concurrency guards (max claude/browser/project)
│   ├── runtime_recovery.py        # Boot-time state normalisation
│   ├── resource_manager.py        # Per-resource concurrency slots
│   ├── project_scheduler.py       # Per-project concurrency limits
│   ├── dashboard.py               # `--dashboard` CLI output
│   ├── future_systems.py          # Stub registry for planned systems
│   ├── task_result.py             # TaskResult + ResultStatus
│   ├── browser_result.py          # Browser-specific result model
│   ├── logger.py                  # Structured logging setup
│   ├── memory_config.py           # MemoryConfig dataclass
│   └── runtime_config.py          # RuntimeConfig dataclass
├── agents/
│   ├── base.py                    # BaseAgent ABC
│   ├── claude_code.py             # ClaudeCodeAgent — tmux-driven Claude CLI execution
│   └── browser_task.py            # BrowserTaskAgent — Playwright verification
├── sessions/
│   └── tmux_manager.py            # TmuxManager — WSL backend on Windows
├── slack/
│   ├── bot.py                     # SlackBot — Socket Mode, notify helpers
│   ├── commands.py                # parse_atlas_command, format_duration
│   └── security.py                # User/channel allowlist enforcement
├── memory/
│   ├── operational_memory.py      # OperationalMemoryStore — SQLite schema + queries
│   ├── memory_coordinator.py      # MemoryCoordinator — orchestrator-facing API
│   ├── context_builder.py         # ContextBuilder — builds + injects prompt context
│   ├── memory_queries.py          # MemoryQueries — retrieval logic
│   ├── memory_summarizer.py       # MemorySummarizer — extract insights after tasks
│   ├── memory_models.py           # All dataclasses: OperationalContext, MemoryCategory, etc.
│   ├── models.py                  # Legacy memory models
│   └── store.py                   # Legacy MemoryStore (SQLite, used alongside new layer)
├── tools/
│   └── playwright/                # Playwright browser manager
├── scripts/
│   └── healthcheck.py             # Pre-flight: config load, Slack auth, DB, tmux, claude
└── logs/
    ├── atlas.log                  # Primary log file
    ├── screenshots/               # Playwright captures
    └── runtime_state.json         # Persisted runtime state
```

---

## Phase History

### Phase 1 — Core Loop
`core/orchestrator.py`, `core/task_manager.py`, `core/task_result.py`  
Async orchestrator loop, task state machine (PENDING → RUNNING → COMPLETED/FAILED), in-memory task registry.

### Phase 2 — Claude Code Agent
`agents/claude_code.py`, `sessions/tmux_manager.py`  
Claude Code runs in a tmux pane. Atlas launches the session, injects the prompt via `send_keys`, polls output for completion markers or idle prompt patterns. WSL backend on Windows.

### Phase 3 — Browser Verification
`agents/browser_task.py`, `tools/playwright/`  
Playwright-based verification agent. Runs headless Chrome, takes screenshots, asserts page conditions. Triggered when a task has `agent: browser` metadata.

### Phase 4 — Recovery Engine
`core/recovery_engine.py`, `core/recovery_strategies.py`, `core/failure_classifier.py`  
On failure, classifies the error (auth/frontend/backend/dependency/network) and selects a recovery strategy. Escalates to investigation mode after N same-strategy failures, then asks human via Slack after M total attempts.

### Phase 5 — Git Safety
`core/git_manager.py`, `core/git_safety.py`, `core/rollback_engine.py`, `core/health_tracker.py`  
Every task gets an isolated branch `atlas/task-<id>`. Checkpoints before each retry. Health scoring with regression detection. Automatic rollback on worsening. Blocks work on protected branches (main/master).

### Phase 6 — Runtime Manager
`core/runtime_manager.py`, `core/runtime_state.py`, `core/runtime_safety.py`  
Persists runtime state across restarts. Detects stale tasks (>72h), runaway retry loops (>10 retries). Enforces concurrency limits: max 2 Claude sessions, max 2 browser sessions, max 2 tasks per project. Orphan session cleanup on boot.

### Phase 7 — Operational Memory
`memory/` (all files)  
SQLite-backed structured memory. After every task, extracts: failure patterns, strategy stats, risk zones, success records. Before every task, injects relevant history as a context block prepended to the Claude prompt. Memory is per-`project_id`.

### Phase 8 — Slack Integration
`slack/bot.py`, `slack/commands.py`, `slack/security.py`, `core/atlas_controller.py`  
Full Slack Socket Mode integration. Commands: `/atlas start`, `/atlas stop`, `/atlas status`, `/atlas sessions`, `/atlas logs`, `/atlas approve`, `/atlas reject`. Thread reply on a blocked task unblocks it with the human's response.

### Phase 9 — GitHub PR Lifecycle
`core/pr_manager.py`, `core/approval_manager.py`  
After a task succeeds and git isolation is active: push branch to origin → create GitHub PR → set task AWAITING_APPROVAL → post Slack message with approve/reject instructions. `/atlas approve <id>` → merge PR via API → MERGED. `/atlas reject <id> <reason>` → append reason to prompt → reset to PENDING for rework.

---

## Task Status State Machine

```
PENDING → RUNNING → VERIFYING → COMPLETED
                  ↓               ↓
              RETRYING      AWAITING_APPROVAL → MERGED
                  ↓
             INVESTIGATING
                  ↓
              BLOCKED (human input needed)
                  ↓
              PENDING (unblocked)
              
RUNNING/PENDING/BLOCKED → CANCELLED
RUNNING → FAILED
```

`AWAITING_APPROVAL` and `BLOCKED` are durable wait states — NOT in `ACTIVE_STATUSES`, NOT picked up by `next_pending()`. They survive restarts.  
`MERGED` is terminal like `COMPLETED`.

---

## Environment Variables (.env)

File location: `C:\Users\shamz\Desktop\Atlas\.env` — **never commit this file**.

```env
# Anthropic
ANTHROPIC_API_KEY=sk-ant-...

# Slack (all three required for Slack to activate)
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_CHANNEL_ID=C0B4RSNKX39
ATLAS_SLACK_ENABLED=true
SLACK_ALLOWED_USER_IDS=U0ARXBLK13R

# GitHub (all three required for PR lifecycle)
GITHUB_TOKEN=ghp_...
GITHUB_REPO_OWNER=syedhamzahabib2026-source
GITHUB_REPO_NAME=Assignmint
```

The GitHub vars point to the **target project repo** (Assignmint), not the Atlas repo itself. If you add a second project, you'll need per-project GitHub config — currently there's only one `PRManager` instance pointing at one repo.

---

## How to Run

```powershell
# Healthcheck first (confirms config, Slack, DB, tmux, claude on PATH)
.\.venv\Scripts\python.exe scripts\healthcheck.py

# Start Atlas (persistent, blocking)
.\.venv\Scripts\python.exe main.py

# Dashboard snapshot (non-blocking)
.\.venv\Scripts\python.exe main.py --dashboard

# Kill and restart cleanly
Get-Process python | Where-Object { $_.CommandLine -like "*main.py*" } | Stop-Process -Force
.\.venv\Scripts\python.exe main.py
```

Atlas runs on Windows with tmux via WSL. The tmux backend auto-detects `wsl` and translates Windows paths to `/mnt/c/...` for session working directories.

---

## Project Config (Adding a New Project)

In `configs/default.yaml`:

```yaml
projects:
  assignmint:
    repo_path: "C:/Users/shamz/Desktop/Assignmint"
    description: "AssignMint web app"
  myproject:
    repo_path: "C:/Users/shamz/Desktop/MyProject"
    description: "..."
```

Then from Slack: `/atlas start --project myproject <prompt>`

This injects `working_dir` into task metadata, which flows through to:
1. `git_safety.resolve_project_path()` → creates git branch in the correct repo
2. `agents/claude_code._resolve_working_dir()` → tmux session opens in the correct dir

---

## Bugs Fixed — Do Not Re-Investigate

### 1. Slack em-dash converting `--` to `—`
**File:** `slack/commands.py` → `parse_atlas_command()`  
**Symptom:** `/atlas start --project assignmint ...` had `--project` silently ignored; the entire raw text including `--project assignmint` was passed as the prompt.  
**Cause:** Slack's message formatting converts `--` to an em-dash `—` in certain contexts. `"--project" in args` never matched `"—project"`.  
**Fix:** Added normalization at the top of `parse_atlas_command`: `raw = raw.replace("—", "--").replace("–", "-")` before splitting.

### 2. Git branch created in Atlas repo instead of target project repo
**File:** `core/git_safety.py` → `resolve_project_path()`  
**Symptom:** Task branches (e.g. `atlas/task-8e6a0ca29db1`) appeared in the Atlas GitHub repo, not in Assignmint. PR creation against Assignmint got HTTP 422 Validation Failed.  
**Cause:** `resolve_project_path()` fell back to `projects_dir / task.project_id` (= `Atlas/projects/assignmint/`). That directory had no `.git`, so `git_manager.find_repo()` walked up and found `Atlas/.git`. Branch, checkpoint, and push all ran against the Atlas repo. PR creation targeted Assignmint → branch not found → 422.  
**Fix:** `resolve_project_path()` now iterates `("working_dir", "project_path")` and returns the first that resolves to an existing path:
```python
def resolve_project_path(self, projects_dir: Path, task: Task) -> Path:
    for key in ("working_dir", "project_path"):
        val = task.metadata.get(key)
        if val:
            p = Path(val).resolve()
            if p.exists():
                return p
    if task.project_id:
        return (projects_dir / task.project_id).resolve()
    return projects_dir.resolve()
```

### 3. Atlas reused stale orphan tmux sessions
**Symptom:** On restart, Atlas reused existing tmux sessions from previous runs. A stale session with broken Claude state caused immediate `error_detected:error:`, triggering recovery loops that exhausted attempts and blocked the task.  
**Status:** Atlas already has `orphan_session_cleanup: true` in config (cleans orphans on boot). Ensure this stays enabled. The `--kill` flag on `/atlas stop` also kills the session explicitly.

### 4. Claude Code tasks completing without committing
**Symptom:** Tasks completed, push succeeded, but `git log origin/main..HEAD --oneline` was empty → PR creation got 422 (no diff).  
**Fix:** `agents/claude_code._build_prompt()` now appends to every prompt:
```
IMPORTANT: After completing all changes, you MUST run:
git add -A && git commit -m 'atlas: <describe what you did>'
Do not finish without committing.
```

### 5. Empty-diff PR creation
**Symptom:** After push, if the task branch had no new commits vs `origin/main`, GitHub returned 422 on PR creation.  
**Fix:** In `core/orchestrator._open_pr_and_request_approval()`, after push and before `create_pr()`, runs `git log origin/main..HEAD --oneline`. If empty output → mark COMPLETED, notify Slack "no code changes to merge", skip PR entirely.

---

## Key Architectural Decisions

**No new dependencies for GitHub API.** `pr_manager.py` uses `urllib` + `asyncio.to_thread`. Avoids `httpx`/`aiohttp` dep bloat. Pattern consistent with rest of codebase.

**Secrets in .env only, never in YAML.** `configs/default.yaml` contains only tunable, non-secret defaults. All tokens read from environment via `load_dotenv()` in `load_config()`.

**Task metadata is the message bus.** Rather than passing data through function arguments, agents and the orchestrator communicate through `task.metadata` keys (`working_dir`, `git`, `prompt`, `operational_context`, `pr_number`, etc.). This makes task state inspectable and persistent.

**`AWAITING_APPROVAL` is not active.** It's excluded from `ACTIVE_STATUSES` and `next_pending()` deliberately. A task waiting for PR approval should not be retried or touched by the orchestrator loop. Only `/atlas approve` or `/atlas reject` moves it out.

**Git safety works on the target repo, not Atlas.** The Atlas repo itself is never touched by task branches. Only the configured project repos get isolated branches.

**tmux session naming:** Sessions are named `atlas-<project_id>`. So `--project assignmint` → session `atlas-assignmint`. Without `--project`, the session is named `atlas-slack-<user_id>`.

---

## Memory System (Phase 7 — current)

### Storage
SQLite at `memory/atlas.db`. Five tables in `operational_memories` schema plus dedicated tables:
- `operational_memories` — free-form records keyed by `(project_id, category, subcategory)`
- `strategy_stats` — per-project per-strategy success/failure counts
- `risk_zones` — file/folder risk scores by failure + rollback frequency
- `failure_patterns` — recurring error signatures
- `task_outcomes` — one row per completed task

### Categories (`MemoryCategory` enum)
| Value | What it stores |
|---|---|
| `project` | Architecture notes, stack info, auth flow, DB notes |
| `recovery` | What strategies succeeded/failed per failure category |
| `repository` | Fragile files, dangerous dirs, dependency risks |
| `task` | Per-task outcome, health score, regression flag |
| `system` | Consolidated strategy performance observations |

### Context injection flow
1. `MemoryCoordinator.prepare_task_context(task)` called before every task runs
2. `ContextBuilder.build_for_task()` → `MemoryQueries.build_context()` queries memory for relevant history, risk zones, cautions, successful strategies
3. `ContextBuilder.apply_to_task()` stores the rendered block in `task.metadata["operational_context"]`
4. `ClaudeCodeAgent._build_prompt()` prepends `operational_context` as the first section of every prompt

### Memory keying
**Strictly per `project_id`.** Tasks without `--project` use `project_id = "slack-<user_id>"` or `"default"`. Memory learned from Assignmint tasks does NOT flow to other projects. Cross-project learning is **Phase 10**.

---

## Phase 10 — Cross-Project Intelligence (Planned)

**Goal:** After every task completes, extract *reusable* lessons into a global layer. Future tasks on any project benefit from what Atlas learned on previous projects.

**What is reusable vs. project-specific:**
- Project-specific (stays per-project): risk zones for specific files, architecture notes, project-specific auth flows
- Reusable (global): strategy success rates by failure category, patterns like "auth changes break notification flows", general recovery heuristics

**Design:**

1. **`memory/global_memory.py`** — new `GlobalMemoryStore` using `project_id = "__global__"` sentinel (already used for `strategy_stats`). Methods: `upsert_global_lesson()`, `get_global_lessons(topics)`, `get_global_strategy_stats(category)`.

2. **`memory/lesson_extractor.py`** — `LessonExtractor` called from `MemorySummarizer.extract_from_task()`. Decides what's worth globalising:
   - Strategy that succeeded after ≥2 prior failures of same category → global lesson
   - Recurring failure pattern (≥3 occurrences) seen across ≥2 projects → global warning
   - High-value architecture pattern from `architecture_note` metadata → global hint

3. **`memory/context_builder.py`** — extend `build_for_task()` to also query global memory and append a `## Cross-project intelligence` section to the injected context block. Keep it short (≤3 lines) so it doesn't crowd project-specific context.

4. **`memory/operational_memory.py`** — add `get_global_lessons(topics, limit)` and `upsert_global_lesson()` methods. Reuse the existing `operational_memories` table with `project_id = "__global__"`.

5. **No new tables needed.** The existing schema already supports `project_id = "__global__"` — `strategy_stats` already does this. Extend the same pattern to `operational_memories`.

**Implementation order:**
1. Add `get_global_lessons` / `upsert_global_lesson` to `OperationalMemoryStore`
2. Write `LessonExtractor` — pure extraction logic, no I/O except store calls
3. Wire `LessonExtractor` into `MemorySummarizer.extract_from_task()` (after per-project extraction)
4. Extend `ContextBuilder.build_for_task()` to append global lessons section
5. Extend `OperationalContext.to_prompt_block()` to render the new section

**What NOT to build for Phase 10:**
- No vector embeddings or semantic search (TODO comment already in code — future phase)
- No AI-generated summaries (same)
- No separate database or new tables
- No cross-project risk zone sharing (file paths are project-specific, meaningless globally)

---

## Design Rules

1. **No overengineering.** A one-shot fix doesn't need a helper class. Three similar lines is better than a premature abstraction.

2. **No new dependencies without discussion.** The stdlib-urllib choice for GitHub API was deliberate. Check existing deps before adding.

3. **Secrets never in code or YAML.** Always `.env` → environment → `load_config()`.

4. **Task metadata is the source of truth for in-flight state.** Don't add function parameters when metadata already carries the data.

5. **Agents are stateless between tasks.** `ClaudeCodeAgent` and `BrowserTaskAgent` hold no cross-task state. The tmux session may persist (for inspection) but the agent object is re-entered fresh each time.

6. **The orchestrator does not know about Slack.** It calls `self.slack.notify(...)` but has no knowledge of Slack message formats. Formatting lives in `slack/bot.py`.

7. **Memory is write-cheap, read-selective.** Record everything after a task. But only inject the high-signal subset (top 8 history, top 6 risk zones, top 5 cautions, top 4 strategies) into prompts. Don't bloat the context.

8. **Git isolation is non-negotiable for project tasks.** Tasks with a `working_dir` get an isolated branch. The only exception is tasks that explicitly set `git_safety: false` in metadata.

9. **PowerShell here-strings for multi-line git commits.** Bash `<<'EOF'` heredocs don't work in PowerShell. Always use `@'...'@`.

10. **One Atlas process at a time.** Kill all existing `python main.py` processes before starting a new one. Multiple instances will fight over the same SQLite DB and tmux sessions.
