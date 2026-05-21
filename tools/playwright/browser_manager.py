"""
Async Playwright browser lifecycle — launch, observe, safe cleanup.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from core.logger import get_logger

logger = get_logger("playwright.browser")

# Lazy import so Atlas runs without Playwright until browser tasks are used
_playwright = None


async def _get_playwright():
    global _playwright
    from playwright.async_api import async_playwright

    if _playwright is None:
        _playwright = await async_playwright().start()
    return _playwright


@dataclass
class BrowserConfig:
    headless: bool = True
    timeout_ms: int = 30_000
    max_runtime_sec: float = 120.0
    viewport_width: int = 1280
    viewport_height: int = 720
    mobile: bool = False
    device_name: str = "iPhone 13"


@dataclass
class BrowserSession:
    """Active browser session with collected diagnostics."""

    browser: Any
    context: Any
    page: Any
    console_logs: list[str] = field(default_factory=list)
    network_failures: list[str] = field(default_factory=list)

    def attach_listeners(self) -> None:
        def on_console(msg):
            entry = f"[{msg.type}] {msg.text}"
            self.console_logs.append(entry)
            if msg.type in ("error", "warning"):
                logger.warning("Console: %s", entry)

        def on_request_failed(request):
            failure = f"{request.method} {request.url} — {request.failure}"
            self.network_failures.append(failure)
            logger.warning("Network failure: %s", failure)

        def on_page_error(exc):
            entry = f"[pageerror] {exc}"
            self.console_logs.append(entry)
            logger.error("Page error: %s", exc)

        self.page.on("console", on_console)
        self.page.on("requestfailed", on_request_failed)
        self.page.on("pageerror", on_page_error)


class BrowserManager:
    """
    Manages Chromium lifecycle with timeouts and guaranteed cleanup.

    TODO: GPT/Claude vision analysis on screenshots
    TODO: screenshot comparison / visual diffing
    """

    def __init__(self, config: BrowserConfig | None = None) -> None:
        self.config = config or BrowserConfig()
        self._pw = None
        self._browser = None
        self._session: BrowserSession | None = None

    @staticmethod
    def is_available() -> bool:
        try:
            import playwright  # noqa: F401

            return True
        except ImportError:
            return False

    async def start(self) -> BrowserSession:
        """Launch browser and return session."""
        self._pw = await _get_playwright()
        logger.info(
            "Launching Chromium (headless=%s, mobile=%s)",
            self.config.headless,
            self.config.mobile,
        )
        self._browser = await self._pw.chromium.launch(headless=self.config.headless)

        if self.config.mobile:
            device = self._pw.devices.get(self.config.device_name)
            if not device:
                raise ValueError(f"Unknown device: {self.config.device_name}")
            context = await self._browser.new_context(**device)
        else:
            context = await self._browser.new_context(
                viewport={
                    "width": self.config.viewport_width,
                    "height": self.config.viewport_height,
                },
            )

        page = await context.new_page()
        page.set_default_timeout(self.config.timeout_ms)
        session = BrowserSession(
            browser=self._browser,
            context=context,
            page=page,
        )
        session.attach_listeners()
        self._session = session
        return session

    @property
    def session(self) -> BrowserSession:
        if self._session is None:
            raise RuntimeError("Browser not started — call start() first")
        return self._session

    async def stop(self) -> None:
        """Close context, browser; prevent orphan processes."""
        logger.info("Closing browser session")
        try:
            if self._session:
                if self._session.context:
                    await self._session.context.close()
                self._session = None
            if self._browser:
                await self._browser.close()
                self._browser = None
        except Exception:
            logger.exception("Error during browser cleanup")
        # Do not stop global playwright — reused across tasks in-process

    async def run_with_timeout(self, coro) -> Any:
        """Wrap coroutine with max_runtime_sec."""
        return await asyncio.wait_for(coro, timeout=self.config.max_runtime_sec)

    async def __aenter__(self) -> BrowserSession:
        return await self.start()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()
