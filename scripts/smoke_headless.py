"""One-shot smoke test for HeadlessClaudeExecutor — run manually, not pytest."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.executor import ExecutionRequest, HeadlessClaudeExecutor


async def main() -> int:
    ex = HeadlessClaudeExecutor()
    print(f"available={ex.is_available()} use_wsl={ex._use_wsl()}")
    scratch = ROOT / "logs" / "smoke_headless"
    scratch.mkdir(parents=True, exist_ok=True)
    req = ExecutionRequest(
        prompt="Reply with exactly the word OK and nothing else. Do not use any tools.",
        working_dir=scratch,
        timeout_sec=180.0,
    )
    print(f"argv={ex.build_argv(req)}")
    result = await ex.execute(req)
    print(f"exit_code={result.exit_code}")
    print(f"output={result.output!r}")
    print(f"cost_usd={result.cost_usd}")
    print(f"session_id={result.session_id}")
    print(f"duration_sec={result.duration_sec:.1f}")
    print(f"error={result.error!r}")
    if not result.success:
        print(f"raw_output tail: {result.raw_output[-800:]!r}")
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
