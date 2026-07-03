import asyncio
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "tmux_manager", ROOT / "sessions" / "tmux_manager.py"
)
mod = importlib.util.module_from_spec(spec)
sys.modules["tmux_manager"] = mod
# Minimal stub for logger import
import types

logger_mod = types.ModuleType("core.logger")
logger_mod.get_logger = lambda name: logging.getLogger(name)  # type: ignore[name-defined]
sys.modules["core"] = types.ModuleType("core")
sys.modules["core.logger"] = logger_mod
import logging

spec.loader.exec_module(mod)
TmuxManager = mod.TmuxManager


async def main() -> None:
    tmux = TmuxManager(session_prefix="atlas-sub")
    name = tmux.session_name("assignmint-f04a43e9")
    cwd = "/mnt/c/Users/shamz/Desktop/Assignmint"

    await tmux.kill_session(name)
    ok = await tmux.create_session(name, working_dir=cwd)
    print("create", ok)

    ok = await tmux.send_keys(name, "claude")
    print("launch claude", ok)

    await asyncio.sleep(20)

    exists = await tmux.session_exists(name)
    print("exists after wait", exists)

    prompt = "Add a comment to HomeScreen\n" * 50
    ok = await tmux.send_keys(name, prompt)
    print("send prompt", ok)

    await tmux.kill_session(name)


asyncio.run(main())
