"""Event fan-out for live queue updates.

Redis pub/sub backed so it survives more than one uvicorn worker: a patient's
browser may be connected to a different process than the one the doctor's
"call next patient" request landed on.

Publishing is best-effort. A queue operation must never fail because Redis is
down -- the frontend keeps its 30s poll as a fallback precisely so this layer
can be lossy.
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as redis

from src.core.config import settings

logger = logging.getLogger(__name__)

# Event names the frontend switches on.
EVENT_POSITION_CHANGED = "position_changed"
EVENT_REASSIGNED = "reassigned"
EVENT_TIME_TO_LEAVE = "time_to_leave"
EVENT_CALLED = "called"
EVENT_COMPLETED = "completed"
EVENT_CANCELLED = "cancelled"
EVENT_QUEUE_UPDATED = "queue_updated"

_pool: redis.Redis | None = None


def _client() -> redis.Redis | None:
    global _pool
    if _pool is None:
        try:
            _pool = redis.from_url(settings.redis_url, decode_responses=True)
        except Exception:
            logger.warning("Could not create Redis client; live updates disabled", exc_info=True)
            return None
    return _pool


async def close_events() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


def queue_channel(queue_id: int) -> str:
    return f"medilink:queue:{queue_id}"


def doctor_channel(doctor_id: int) -> str:
    return f"medilink:doctor:{doctor_id}"


async def publish(channel: str, event: str, data: dict[str, Any]) -> None:
    """Best-effort publish. Never raises into a request."""
    client = _client()
    if client is None:
        return
    payload = json.dumps({"event": event, "data": data, "at": datetime.now(UTC).isoformat()}, default=str)
    try:
        await client.publish(channel, payload)
    except Exception:
        logger.warning("Failed to publish %s on %s", event, channel, exc_info=True)


async def publish_queue_event(queue_id: int, event: str, data: dict[str, Any]) -> None:
    await publish(queue_channel(queue_id), event, data)


async def publish_doctor_event(doctor_id: int, event: str, data: dict[str, Any]) -> None:
    await publish(doctor_channel(doctor_id), event, data)


async def subscribe(channel: str) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted frames for a channel.

    Emits a comment heartbeat every 20s so proxies don't close an idle
    connection, and so the client can tell 'quiet' from 'dead'.
    """
    client = _client()
    if client is None:
        # Redis unavailable: hold the connection open with heartbeats rather
        # than 500-ing. The client's polling fallback covers the data.
        while True:
            yield ": heartbeat\n\n"
            await asyncio.sleep(20)

    pubsub = client.pubsub()
    await pubsub.subscribe(channel)
    try:
        yield f"event: connected\ndata: {json.dumps({'channel': channel})}\n\n"
        while True:
            try:
                message = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=20.0),
                    timeout=25.0,
                )
            except TimeoutError:
                message = None

            if message is None:
                yield ": heartbeat\n\n"
                continue

            try:
                payload = json.loads(message["data"])
                yield f"event: {payload['event']}\ndata: {json.dumps(payload['data'], default=str)}\n\n"
            except (KeyError, json.JSONDecodeError):
                logger.warning("Dropping malformed event on %s", channel)
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
        except Exception:
            logger.debug("Error closing pubsub for %s", channel, exc_info=True)
