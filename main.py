#!/usr/bin/env python3
"""
Atlas entry point.

Usage:
    python main.py                  # run orchestrator loop (persistent)
    python main.py --dashboard      # print runtime dashboard and exit
    python main.py --demo           # quick orchestrator tick (no agents)
    python main.py --run-claude     # one Claude Code task via tmux
    python main.py --run-browser    # one Playwright verification task
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import load_config
from core.logger import setup_logging, get_logger
from core.task_manager import TaskManager
from core.task_store import TaskStore
from core.runtime_manager import RuntimeManager
from core.orchestrator import Orchestrator
from core.recovery_engine import RecoveryEngine
from core.git_safety import GitSafetyCoordinator
from memory.store import MemoryStore
from memory.memory_coordinator import MemoryCoordinator
from sessions.tmux_manager import TmuxManager
from agents.claude_code import ClaudeCodeAgent, ClaudeCodeConfig
from agents.browser_task import BrowserTaskAgent, BrowserAgentConfig


logger = get_logger("main")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Atlas autonomous orchestrator")
    parser.add_argument("--demo", action="store_true", help="Orchestrator tick (no agent)")
    parser.add_argument("--dashboard", action="store_true", help="Print runtime dashboard")
    parser.add_argument("--run-claude", action="store_true", help="Run one Claude Code task")
    parser.add_argument("--run-browser", action="store_true", help="Run one browser verify task")
    parser.add_argument("--prompt", type=str, default="Say hello in tmux.")
    parser.add_argument(
        "--url",
        type=str,
        default="https://example.com",
        help="URL for --run-browser",
    )
    return parser.parse_args()


def _claude_agent_config(app_config) -> ClaudeCodeConfig:
    c = app_config.claude_code
    return ClaudeCodeConfig(
        command=c.command,
        max_execution_sec=c.max_execution_sec,
        poll_interval_sec=c.poll_interval_sec,
        launch_wait_sec=c.launch_wait_sec,
        idle_stable_polls=c.idle_stable_polls,
        capture_lines=c.capture_lines,
        kill_session_on_finish=c.kill_session_on_finish,
        kill_session_on_success=c.kill_session_on_success,
    )


def _browser_agent_config(app_config) -> BrowserAgentConfig:
    b = app_config.browser
    return BrowserAgentConfig(
        headless=b.headless,
        timeout_ms=b.timeout_ms,
        max_runtime_sec=b.max_runtime_sec,
        capture_success_screenshot=b.capture_success_screenshot,
        capture_failure_screenshot=b.capture_failure_screenshot,
        viewport_width=b.viewport_width,
        viewport_height=b.viewport_height,
    )


def build_orchestrator(
    config,
    task_manager,
    memory,
    tmux,
    slack,
    memory_coordinator=None,
    task_store=None,
    runtime=None,
    worker_pools=None,
) -> Orchestrator:
    browser = BrowserTaskAgent(
        screenshots_dir=config.screenshots_dir,
        config=_browser_agent_config(config),
    )
    pool_tmux = worker_pools.pool_tmux if worker_pools else None
    claude = ClaudeCodeAgent(
        tmux=tmux,
        projects_dir=config.projects_dir,
        config=_claude_agent_config(config),
        pool_tmux=pool_tmux,
    )
    recovery = RecoveryEngine(config.recovery, log_dir=config.log_dir)
    git_safety = GitSafetyCoordinator(config.git)
    return Orchestrator(
        config=config,
        task_manager=task_manager,
        memory=memory,
        tmux=tmux,
        slack=slack,
        agents=[browser, claude],
        recovery=recovery,
        git_safety=git_safety,
        memory_coordinator=memory_coordinator,
        task_store=task_store,
        runtime=runtime,
        worker_pools=worker_pools,
    )


async def run(
    demo: bool = False,
    dashboard: bool = False,
    run_claude: bool = False,
    run_browser: bool = False,
    prompt: str = "",
    url: str = "https://example.com",
) -> None:
    config = load_config(root_dir=ROOT)
    setup_logging(config)
    logger.info("Atlas root: %s", config.root_dir)

    task_store = TaskStore(config.memory_db_path) if config.runtime.enabled else None
    task_manager = TaskManager(
        max_concurrent=config.max_concurrent_tasks,
        store=task_store,
    )
    runtime = (
        RuntimeManager(config) if config.runtime.enabled else None
    )
    memory = MemoryStore(config.memory_db_path)
    memory_coordinator = MemoryCoordinator(
        config.memory_db_path,
        enabled=config.memory_config.enabled,
    )
    tmux = TmuxManager(
        session_prefix=config.tmux_session_prefix,
        socket_path=config.tmux_socket,
    )
    from core.worker_pool_manager import WorkerPoolManager

    worker_pools = (
        WorkerPoolManager(config.worker_pools, tmux_socket=config.tmux_socket)
        if config.worker_pools.enabled
        else None
    )
    slack = None
    if config.slack.enabled:
        from slack.bot import SlackBot

        slack = SlackBot(config.slack)
    orchestrator = build_orchestrator(
        config,
        task_manager,
        memory,
        tmux,
        slack,
        memory_coordinator=memory_coordinator,
        task_store=task_store,
        runtime=runtime,
        worker_pools=worker_pools,
    )

    if dashboard:
        if task_store:
            await task_store.initialize()
            task_manager.bind_store(task_store)
            for t in await task_store.load_all():
                task_manager._tasks[t.id] = t
        sessions = await tmux.list_sessions() if tmux.is_available() else []
        from core.dashboard import print_dashboard

        state = runtime.state_store.load() if runtime else None
        from core.approval_policy import ApprovalPolicy
        from core.approval_engine import ApprovalEngine
        from core.deployment_manager import DeploymentManager
        from core.deployment_policy import DeploymentPolicy

        approval_engine = ApprovalEngine(policy=ApprovalPolicy(config.approval))
        approval_engine.rehydrate_from_tasks(task_manager.list_all())
        deployment_manager = DeploymentManager(
            config=config.deployment,
            policy=DeploymentPolicy(config.deployment),
        )

        print_dashboard(
            config,
            task_manager,
            state,
            tmux_sessions=sessions,
            worker_pools=worker_pools,
            approval_engine=approval_engine,
            deployment_manager=deployment_manager,
        )
        if task_store:
            await task_store.close()
        return

    if demo:
        task_manager.create(
            title="Demo task",
            description="Orchestrator tick",
            metadata={"agent": "none", "recovery_enabled": False, "git_safety": False},
        )
        await orchestrator._bootstrap()
        await orchestrator._tick()
        await orchestrator.stop(graceful=True)
        await memory.close()
        if memory_coordinator:
            await memory_coordinator.close()
        logger.info("Demo complete")
        return

    if run_browser:
        from tools.playwright.browser_manager import BrowserManager

        if not BrowserManager.is_available():
            logger.error("Install: pip install playwright && playwright install chromium")
            sys.exit(1)

        task_manager.create(
            title="Browser verify example.com",
            description=f"Verify {url} loads",
            project_id="demo-browser",
            metadata={
                "agent": "browser",
                "url": url,
                "steps": [
                    {"action": "goto", "url": url},
                    {"action": "assert_visible", "selector": "h1"},
                    {"action": "screenshot", "label": "homepage"},
                ],
                "max_runtime_sec": 60,
                "fail_on_network_error": False,
            },
        )
        await orchestrator._bootstrap()
        pending = task_manager.next_pending()
        if pending:
            await orchestrator._run_task(pending)
        await orchestrator.stop(graceful=True)
        await memory.close()
        logger.info("Browser run finished — see logs/screenshots/")
        return

    if run_claude:
        if not tmux.is_available():
            logger.error("tmux not available. Install tmux or use WSL on Windows.")
            sys.exit(1)

        task_manager.create(
            title="Claude Code run",
            description=prompt,
            project_id="demo-claude",
            metadata={
                "agent": "claude_code",
                "prompt": prompt,
                "max_execution_sec": 120,
            },
        )
        await orchestrator._bootstrap()
        pending = task_manager.next_pending()
        if pending:
            await orchestrator._run_task(pending)
        await orchestrator.stop(graceful=True)
        await memory.close()
        logger.info("Claude run finished")
        return

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    async def _run_with_stop() -> None:
        runner = asyncio.create_task(orchestrator.start())
        await stop_event.wait()
        await orchestrator.stop(graceful=True)
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass
        await memory.close()
        if memory_coordinator:
            await memory_coordinator.close()

    try:
        if sys.platform == "win32":
            await orchestrator.start()
        else:
            await _run_with_stop()
    except KeyboardInterrupt:
        await orchestrator.stop(graceful=True)
        await memory.close()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(
            run(
                demo=args.demo,
                dashboard=args.dashboard,
                run_claude=args.run_claude,
                run_browser=args.run_browser,
                prompt=args.prompt,
                url=args.url,
            )
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
