from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select

from .config import get_settings
from .database import SessionFactory
from .models import Outbox, utc_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LiveMessage:
    id: str
    organization_id: str
    workspace_id: str
    topic: str
    payload: dict[str, Any]


@dataclass(slots=True, eq=False)
class Subscription:
    organization_id: str
    workspace_id: str
    run_id: str | None
    queue: asyncio.Queue[LiveMessage | None]


class LiveBroker:
    def __init__(self, queue_size: int) -> None:
        self.queue_size = queue_size
        self.subscriptions: set[Subscription] = set()
        self.delivered = deque(maxlen=10_000)
        self.delivered_set: set[str] = set()

    def subscribe(
        self, organization_id: str, workspace_id: str, run_id: str | None
    ) -> Subscription:
        subscription = Subscription(
            organization_id, workspace_id, run_id, asyncio.Queue(self.queue_size)
        )
        self.subscriptions.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        self.subscriptions.discard(subscription)

    async def publish(self, message: LiveMessage) -> None:
        if message.id in self.delivered_set:
            return
        if len(self.delivered) == self.delivered.maxlen:
            self.delivered_set.discard(self.delivered[0])
        self.delivered.append(message.id)
        self.delivered_set.add(message.id)
        run_id = message.payload.get("run_id")
        for subscription in tuple(self.subscriptions):
            if (
                subscription.organization_id != message.organization_id
                or subscription.workspace_id != message.workspace_id
                or (subscription.run_id is not None and subscription.run_id != run_id)
            ):
                continue
            try:
                subscription.queue.put_nowait(message)
            except asyncio.QueueFull:
                self.subscriptions.discard(subscription)
                while not subscription.queue.empty():
                    subscription.queue.get_nowait()
                subscription.queue.put_nowait(None)


broker = LiveBroker(get_settings().subscription_queue_size)


def claim_outbox(owner: str, limit: int = 100) -> list[LiveMessage]:
    now = utc_now()
    leased_until = now + timedelta(seconds=get_settings().outbox_lease_seconds)
    with SessionFactory() as session:
        rows = list(
            session.scalars(
                select(Outbox)
                .where(
                    Outbox.published_at.is_(None),
                    or_(Outbox.leased_until.is_(None), Outbox.leased_until < now),
                )
                .order_by(Outbox.created_at, Outbox.id)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        )
        for row in rows:
            row.lease_owner = owner
            row.leased_until = leased_until
            row.attempts += 1
        messages = [
            LiveMessage(
                row.id,
                row.organization_id,
                row.workspace_id,
                row.topic,
                row.payload,
            )
            for row in rows
        ]
        session.commit()
        return messages


def acknowledge_outbox(owner: str, message_id: str) -> None:
    with SessionFactory() as session:
        row = session.get(Outbox, message_id)
        if row is not None and row.lease_owner == owner and row.published_at is None:
            row.published_at = utc_now()
            row.lease_owner = None
            row.leased_until = None
            session.commit()


async def outbox_worker(stop: asyncio.Event) -> None:
    owner = f"outbox-{secrets.token_hex(8)}"
    while not stop.is_set():
        try:
            messages = await asyncio.to_thread(claim_outbox, owner)
        except Exception:
            logger.exception("outbox claim failed; retrying")
            messages = []
        for message in messages:
            try:
                await broker.publish(message)
                await asyncio.to_thread(acknowledge_outbox, owner, message.id)
            except Exception:
                # The lease makes the message eligible again after expiry. Publishing is
                # idempotent by outbox ID for this process, so retry cannot duplicate it.
                logger.exception(
                    "outbox delivery failed; lease will expire", extra={"outbox_id": message.id}
                )
        if not messages:
            try:
                await asyncio.wait_for(stop.wait(), timeout=get_settings().outbox_poll_seconds)
            except TimeoutError:
                pass


def encode_sse(message: LiveMessage) -> str:
    body = json.dumps(
        {"topic": message.topic, "payload": message.payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"id: {message.id}\nevent: {message.topic}\ndata: {body}\n\n"
