from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import AdministrativeAuditEvent, Event, RunCommitment, utc_now


def canonical_timestamp(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return f"{value.isoformat(timespec='microseconds')}Z"


def audit_chain_body(event: AdministrativeAuditEvent) -> dict[str, Any]:
    return {
        "actor_id": event.actor_id,
        "actor_type": event.actor_type,
        "organization_id": event.organization_id,
        "action": event.action,
        "target_type": event.target_type,
        "target_id": event.target_id,
        "result": event.result,
        "request_id": event.request_id,
        "source_ip_classification": event.source_ip_classification,
        "before_digest": event.before_digest,
        "after_digest": event.after_digest,
        "corrects_event_id": event.corrects_event_id,
        "occurred_at": canonical_timestamp(event.occurred_at),
        "previous_hash": event.previous_hash,
    }


def digest_body(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def verify_audit_events(events: Iterable[AdministrativeAuditEvent]) -> tuple[bool, str]:
    remaining = {event.event_hash: event for event in events}
    previous = "0" * 64
    index = 0
    while remaining:
        matches = [event for event in remaining.values() if event.previous_hash == previous]
        if len(matches) != 1:
            return False, f"event {index + 1} chain link is missing or ambiguous"
        event = matches[0]
        index += 1
        if digest_body(audit_chain_body(event)) != event.event_hash:
            return False, f"event {index} hash mismatch"
        previous = event.event_hash
        remaining.pop(event.event_hash)
    return True, previous


def backfill_legacy_audit_hashes(session: Session) -> int:
    """Commit pre-chain audit rows once during upgrade without changing their facts."""
    rows = list(
        session.scalars(
            select(AdministrativeAuditEvent).order_by(
                AdministrativeAuditEvent.organization_id,
                AdministrativeAuditEvent.occurred_at,
                AdministrativeAuditEvent.id,
            )
        )
    )
    changed = 0
    previous_by_organization: dict[str, str] = {}
    for event in rows:
        previous = previous_by_organization.get(event.organization_id, "0" * 64)
        if event.event_hash == "0" * 64:
            body = audit_chain_body(event)
            body["previous_hash"] = previous
            digest = digest_body(body)
            session.execute(
                update(AdministrativeAuditEvent)
                .where(AdministrativeAuditEvent.id == event.id)
                .values(previous_hash=previous, event_hash=digest)
                .execution_options(synchronize_session=False)
            )
            changed += 1
            previous_by_organization[event.organization_id] = digest
        else:
            previous_by_organization[event.organization_id] = event.event_hash
    return changed


def commit_run(session: Session, organization_id: str, run_id: str) -> RunCommitment:
    events = list(
        session.scalars(
            select(Event)
            .where(Event.organization_id == organization_id, Event.run_id == run_id)
            .order_by(Event.observed_at, Event.internal_id)
        )
    )
    leaves = [bytes.fromhex(event.payload_sha256) for event in events]
    if not leaves:
        raise ValueError("cannot commit an empty run")
    while len(leaves) > 1:
        if len(leaves) % 2:
            leaves.append(leaves[-1])
        leaves = [
            hashlib.sha256(leaves[index] + leaves[index + 1]).digest()
            for index in range(0, len(leaves), 2)
        ]
    existing = session.get(RunCommitment, (organization_id, run_id))
    root = leaves[0].hex()
    if existing:
        if existing.root_sha256 != root:
            raise ValueError("finalized run commitment cannot be rewritten; append a correction")
        return existing
    record = RunCommitment(
        organization_id=organization_id,
        run_id=run_id,
        root_sha256=root,
        item_count=len(events),
        finalized_at=utc_now(),
    )
    session.add(record)
    session.commit()
    return record


def verify_json_file(path: Path) -> tuple[bool, str]:
    document = json.loads(path.read_text(encoding="utf-8"))
    expected = document.pop("commitment_sha256", None)
    actual = digest_body(document)
    return (expected == actual, actual)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a Villani tamper-evident JSON commitment")
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    valid, digest = verify_json_file(args.path)
    print(json.dumps({"valid": valid, "computed_sha256": digest}, sort_keys=True))
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
