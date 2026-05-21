"""
Screenshot capture and storage for browser verification.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from core.logger import get_logger

logger = get_logger("playwright.screenshots")


class ScreenshotStore:
    """Save Playwright screenshots under logs/screenshots/."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, task_id: str, label: str) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]
        filename = f"{task_id[:8]}_{ts}_{safe_label}.png"
        return self.base_dir / filename

    async def capture(
        self,
        page,
        task_id: str,
        label: str,
        *,
        full_page: bool = False,
    ) -> str:
        """Capture screenshot; returns absolute path string."""
        path = self.path_for(task_id, label)
        await page.screenshot(path=str(path), full_page=full_page)
        logger.info("Screenshot saved: %s", path)
        return str(path.resolve())
