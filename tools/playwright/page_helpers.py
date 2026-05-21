"""
High-level Playwright page interactions for verification steps.
"""

from __future__ import annotations

from typing import Any

from core.logger import get_logger

logger = get_logger("playwright.page")


class PageHelper:
    """Async helpers wrapping common UI verification actions."""

    def __init__(self, page, timeout_ms: int = 30_000) -> None:
        self.page = page
        self.timeout_ms = timeout_ms

    async def goto(self, url: str) -> None:
        logger.info("goto %s", url)
        await self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)

    async def click(self, selector: str) -> None:
        logger.info("click %s", selector)
        await self.page.click(selector, timeout=self.timeout_ms)

    async def fill(self, selector: str, value: str) -> None:
        logger.info("fill %s", selector)
        await self.page.fill(selector, value, timeout=self.timeout_ms)

    async def wait_for_selector(self, selector: str, *, timeout_ms: int | None = None) -> None:
        t = timeout_ms or self.timeout_ms
        logger.info("wait_for %s", selector)
        await self.page.wait_for_selector(selector, timeout=t)

    async def assert_visible(self, selector: str) -> None:
        loc = self.page.locator(selector)
        if not await loc.is_visible():
            raise AssertionError(f"Element not visible: {selector}")
        logger.info("assert_visible ok: %s", selector)

    async def assert_text(self, selector: str, text: str) -> None:
        loc = self.page.locator(selector)
        content = await loc.inner_text()
        if text not in content:
            raise AssertionError(
                f"Expected text {text!r} in {selector}, got {content!r}"
            )

    async def assert_count(self, selector: str, count: int) -> None:
        n = await self.page.locator(selector).count()
        if n != count:
            raise AssertionError(f"Expected {count} matches for {selector}, got {n}")

    async def run_step(self, step: dict[str, Any]) -> None:
        """Execute one step dict from task metadata."""
        action = step.get("action", "").lower()

        if action == "goto":
            await self.goto(step["url"])
        elif action == "click":
            await self.click(step["selector"])
        elif action == "fill":
            await self.fill(step["selector"], step.get("value", ""))
        elif action == "wait_for":
            await self.wait_for_selector(
                step["selector"],
                timeout_ms=step.get("timeout_ms"),
            )
        elif action == "assert_visible":
            await self.assert_visible(step["selector"])
        elif action == "assert_text":
            await self.assert_text(step["selector"], step["text"])
        elif action == "assert_count":
            await self.assert_count(step["selector"], int(step["count"]))
        elif action == "press":
            await self.page.press(step["selector"], step.get("key", "Enter"))
        elif action == "screenshot":
            pass  # handled by agent with label
        else:
            raise ValueError(f"Unknown browser step action: {action}")
