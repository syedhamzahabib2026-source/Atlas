"""
BrowserTaskAgent — Playwright UI verification (Phase 4).

Executes structured browser steps; returns BrowserTaskResult embedded in TaskResult.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agents.base import BaseAgent
from core.browser_result import BrowserTaskResult
from core.logger import get_logger
from core.task_result import ResultStatus, TaskResult
from tools.playwright.browser_manager import BrowserConfig, BrowserManager
from tools.playwright.page_helpers import PageHelper
from tools.playwright.screenshots import ScreenshotStore
from tools.playwright.workflows import build_workflow

if TYPE_CHECKING:
    from core.task_manager import Task

logger = get_logger("agents.browser")


@dataclass
class BrowserAgentConfig:
    headless: bool = True
    timeout_ms: int = 30_000
    max_runtime_sec: float = 120.0
    capture_success_screenshot: bool = True
    capture_failure_screenshot: bool = True
    viewport_width: int = 1280
    viewport_height: int = 720


class BrowserTaskAgent(BaseAgent):
    """Runs frontend verification tasks via Playwright."""

    name = "browser"

    def __init__(
        self,
        screenshots_dir: Path,
        config: BrowserAgentConfig | None = None,
    ) -> None:
        self.config = config or BrowserAgentConfig()
        self.screenshots = ScreenshotStore(screenshots_dir)

    def can_handle(self, task: Task) -> bool:
        return task.metadata.get("agent") == self.name

    def _resolve_steps(self, task: Task) -> list[dict[str, Any]]:
        if steps := task.metadata.get("steps"):
            return list(steps)
        if workflow := task.metadata.get("workflow"):
            params = task.metadata.get("workflow_params", {})
            if "url" not in params and task.metadata.get("url"):
                params = {**params, "url": task.metadata["url"]}
            return build_workflow(workflow, params)
        url = task.metadata.get("url")
        if url:
            return [
                {"action": "goto", "url": url},
                {"action": "screenshot", "label": "loaded"},
            ]
        raise ValueError(
            "Browser task needs metadata.steps, metadata.workflow, or metadata.url"
        )

    def _browser_config(self, task: Task) -> BrowserConfig:
        m = task.metadata
        return BrowserConfig(
            headless=m.get("headless", self.config.headless),
            timeout_ms=m.get("timeout_ms", self.config.timeout_ms),
            max_runtime_sec=float(
                m.get("max_runtime_sec", self.config.max_runtime_sec)
            ),
            viewport_width=m.get("viewport_width", self.config.viewport_width),
            viewport_height=m.get("viewport_height", self.config.viewport_height),
            mobile=bool(m.get("mobile", False)),
            device_name=m.get("device_name", "iPhone 13"),
        )

    async def _dom_summary(self, page) -> str:
        try:
            title = await page.title()
            url = page.url
            counts = await page.evaluate(
                """() => ({
                    links: document.querySelectorAll('a').length,
                    buttons: document.querySelectorAll('button').length,
                    inputs: document.querySelectorAll('input').length,
                    h1: document.querySelectorAll('h1').length,
                })"""
            )
            return f"title={title!r} url={url} elements={counts}"
        except Exception as exc:
            return f"dom_summary unavailable: {exc}"

    async def run(self, task: Task) -> TaskResult:
        logger.info("Browser task start: %s", task.id[:8])

        if not BrowserManager.is_available():
            return self._fail_task(
                task,
                "Playwright not installed. Run: pip install playwright && playwright install chromium",
            )

        browser_result = BrowserTaskResult(
            success=False,
            summary="",
            metadata={"task_id": task.id, "agent": self.name},
        )
        manager = BrowserManager(self._browser_config(task))
        cancel: asyncio.Event | None = task.metadata.get("_cancel_event")

        try:
            steps = self._resolve_steps(task)

            async def _execute():
                session = await manager.start()
                page = session.page
                helper = PageHelper(page, timeout_ms=manager.config.timeout_ms)

                if steps and steps[0].get("action") != "goto" and task.metadata.get("url"):
                    await helper.goto(task.metadata["url"])

                for i, step in enumerate(steps):
                    if cancel and cancel.is_set():
                        raise asyncio.CancelledError("Browser task cancelled")

                    action = step.get("action", "").lower()
                    if action == "screenshot":
                        label = step.get("label", f"step-{i}")
                        path = await self.screenshots.capture(
                            page, task.id, label, full_page=step.get("full_page", False)
                        )
                        browser_result.screenshots.append(path)
                    else:
                        await helper.run_step(step)
                        browser_result.steps_run += 1

                browser_result.final_url = page.url
                browser_result.dom_summary = await self._dom_summary(page)
                browser_result.console_logs = list(session.console_logs)
                browser_result.network_failures = list(session.network_failures)

                if self.config.capture_success_screenshot or task.metadata.get(
                    "capture_success_screenshot", True
                ):
                    path = await self.screenshots.capture(
                        page, task.id, "success", full_page=False
                    )
                    browser_result.screenshots.append(path)

                # Fail if console errors or network failures (configurable)
                if task.metadata.get("fail_on_console_error", False):
                    errs = [l for l in session.console_logs if l.startswith("[error]")]
                    if errs:
                        raise RuntimeError(f"Console errors: {errs[:3]}")

                if task.metadata.get("fail_on_network_error", True) and session.network_failures:
                    raise RuntimeError(
                        f"Network failures: {session.network_failures[:3]}"
                    )

                browser_result.success = True
                browser_result.summary = (
                    f"Browser verification passed ({browser_result.steps_run} steps)"
                )

            await manager.run_with_timeout(_execute())

        except asyncio.TimeoutError:
            browser_result.success = False
            browser_result.summary = f"Browser task timed out after {manager.config.max_runtime_sec}s"
            browser_result.errors.append("timeout")
            await self._capture_failure_shot(manager, task, browser_result, "timeout")

        except asyncio.CancelledError:
            browser_result.success = False
            browser_result.summary = "Browser task cancelled"
            browser_result.errors.append("cancelled")
            await self._capture_failure_shot(manager, task, browser_result, "cancelled")

        except Exception as exc:
            logger.exception("Browser task failed: %s", task.id[:8])
            browser_result.success = False
            browser_result.summary = str(exc)
            browser_result.errors.append(str(exc))
            await self._capture_failure_shot(manager, task, browser_result, "failure")

        finally:
            await manager.stop()
            browser_result.finish()

        return self._to_task_result(task, browser_result)

    async def _capture_failure_shot(
        self,
        manager: BrowserManager,
        task: Task,
        browser_result: BrowserTaskResult,
        label: str,
    ) -> None:
        if not self.config.capture_failure_screenshot:
            return
        try:
            if manager._session and manager._session.page:
                path = await self.screenshots.capture(
                    manager._session.page, task.id, label, full_page=True
                )
                browser_result.screenshots.append(path)
        except Exception:
            logger.warning("Could not capture failure screenshot")

    def _to_task_result(self, task: Task, br: BrowserTaskResult) -> TaskResult:
        status = ResultStatus.COMPLETED if br.success else ResultStatus.FAILED
        if "timeout" in br.errors:
            status = ResultStatus.TIMEOUT
        if "cancelled" in br.errors:
            status = ResultStatus.CANCELLED

        lines = [
            br.summary,
            f"url={br.final_url}",
            f"steps={br.steps_run}",
            f"screenshots={len(br.screenshots)}",
        ]
        if br.network_failures:
            lines.append(f"network_failures={len(br.network_failures)}")
        if br.console_logs:
            lines.append("console_tail:")
            lines.extend(br.console_logs[-5:])

        tr = TaskResult(
            status=status,
            summary=br.summary,
            session_name=f"browser-{task.id[:8]}",
            raw_output="\n".join(lines),
            errors=br.errors,
            metadata={
                "task_id": task.id,
                "agent": self.name,
                "browser": br.to_dict(),
            },
        )
        tr.started_at = br.started_at
        tr.finished_at = br.finished_at
        return tr

    def _fail_task(self, task: Task, message: str) -> TaskResult:
        br = BrowserTaskResult(success=False, summary=message, errors=[message])
        br.finish()
        return self._to_task_result(task, br)
