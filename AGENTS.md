# AGENTS.md

## Cursor Cloud specific instructions

Atlas is a single-product **Python 3.11+** asyncio orchestration system (no monorepo, no
JS/Docker services). Standard setup/run commands live in `README.md`; dependencies in
`requirements.txt`. The notes below only cover non-obvious cloud-environment caveats.

### Environment
- Dependencies are installed into a project-local virtualenv at `.venv/` (gitignored). The
  startup update script recreates/refreshes it, so always run Python via `.venv/bin/python`
  (e.g. `.venv/bin/python main.py --demo`).
- A local `.env` is created from `.env.example` with `ATLAS_SLACK_ENABLED=false` so the core
  orchestrator runs standalone. `.env` is gitignored — recreate it if missing. Leave Slack
  disabled unless you have real `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN`.
- `tmux` is available (backend reports `native`); it is required only for Claude Code tasks.

### Running / testing the app
- Core smoke checks (no external services): `.venv/bin/python main.py --demo` and
  `.venv/bin/python main.py --dashboard`.
- End-to-end hello-world uses the Playwright browser agent:
  `.venv/bin/python main.py --run-browser --url https://example.com`
  (Chromium is preinstalled via `playwright install chromium`; screenshots land in
  `logs/screenshots/`). This is the best no-credentials way to exercise real functionality.
- The persistent loop is `.venv/bin/python main.py` (Ctrl+C / SIGINT for graceful shutdown).
- There is **no test suite** in the repo (no `pytest`/`tests/`) and no linter config.
  `scripts/healthcheck.py` is the closest thing to a preflight check.

### Known non-blocking quirks (pre-existing, not environment issues)
- `scripts/healthcheck.py` exits non-zero and prints `slack_ready` / `Slack API` FAIL and a
  `claude` CLI warning whenever Slack/Claude aren't configured. This is expected — Slack and
  the `claude` CLI are optional; config, DB, and tmux checks passing means core is healthy.
- The `--demo` task intentionally fails with "No agent registered to handle this task"
  (it uses `agent: none`).
- After short one-shot runs (`--demo`, `--run-browser`) you may see trailing
  `RuntimeError: TaskStore not initialized` / `Event loop is closed` tracebacks during
  shutdown. These are harmless post-completion background-task noise; the task itself
  completes first (look for `Task completed`).

### Not runnable here
- The `tools/mobile/` AssignMint iOS extension is macOS-only and targets an external app at
  an absolute path not present in this repo. It cannot run on this Linux VM.
