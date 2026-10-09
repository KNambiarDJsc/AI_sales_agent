"""Rebuild the Excel call log now (it is also rebuilt automatically after every call).

    python scripts/export_call_log.py

Writes CALL_LOG_PATH (default exports/call_log.xlsx): sheet "Calls" — one row per call
with outcome, path through the script, transcript; sheet "Agreed to sales team" —
the leads who said yes. See workers/call_log.py."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.session import engine
from workers.call_log import refresh_call_log


async def main() -> int:
    try:
        path = await refresh_call_log()
    finally:
        await engine.dispose()
    if path is None:
        print("call log not written (disabled, or the file is open in Excel — close it and retry)")
        return 1
    print(f"call log written: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
