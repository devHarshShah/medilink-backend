"""Background re-scorer.

Periodically re-evaluates every waiting cross-hospital entry and moves patients
to a better centre while they are still at home. This is what makes the queue
"floating" rather than a one-shot assignment.

Runs as a single asyncio task in the app lifespan. With more than one worker
each would run its own copy; the moves are idempotent (a second pass finds no
improvement) but a production deployment should elect one runner -- noted in the
README rather than solved here, since we run a single worker today.
"""

import asyncio
import contextlib
import logging

from src.core.config import settings
from src.db.session import AsyncSessionLocal
from src.services.queue.engine import rescore_all_waiting

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None


async def _loop() -> None:
    interval = settings.queue_rescore_interval_seconds
    logger.info("Queue re-scorer started (every %ss)", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            async with AsyncSessionLocal() as db:
                moved = await rescore_all_waiting(db)
            if moved:
                logger.info("Re-scorer moved %d patient(s) to a better centre", moved)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let one bad pass kill the loop -- a dead re-scorer means
            # patients silently stop floating, with no error anywhere.
            logger.warning("Queue re-scoring pass failed", exc_info=True)


def start_rescorer() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def stop_rescorer() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task
        _task = None
        logger.info("Queue re-scorer stopped")
