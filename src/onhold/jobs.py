"""The cron jobs, for running onhold outside Cloudflare.

Run from src/, every 15 minutes and once a day:

    ONHOLD_DB=/data/onhold.db uv run python -m onhold.jobs reconcile
    ONHOLD_DB=/data/onhold.db uv run python -m onhold.jobs purge
"""

from __future__ import annotations

import asyncio
import os
import sys

from onhold.app import build_service


async def run(job: str) -> dict:
    service = build_service(dict(os.environ))
    if job == "reconcile":
        return await service.reconcile()
    if job == "purge":
        return await service.purge()
    raise SystemExit(f"unknown job {job!r}: reconcile or purge")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(asyncio.run(run(sys.argv[1])))
