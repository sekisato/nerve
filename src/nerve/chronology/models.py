from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..memestate.models import MemecoinStateFact, canonical_json


class ChronologyCoverage(StrEnum):
    COMPLETE_SINCE_CREATION = "complete_since_creation"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class ChronologyEventType(StrEnum):
    CREATE = "create"
    BUY = "buy"
    SELL = "sell"
    MIGRATE = "migrate"
    UNKNOWN = "unknown"


class DecodeStatus(StrEnum):
    DECODED = "decoded"
    UNSUPPORTED = "unsupported"
    INVALID = "invalid"


class ChronologyEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    signature: str
    slot: int = Field(ge=0)
    block_time: datetime | None = None
    instruction_path: str
    program_id: str
    venue: str
    event_type: ChronologyEventType
    user: str | None = None
    mint: str | None = None
    pool: str | None = None
    creator: str | None = None
    creation_user: str | None = None
    amount_token_raw: int | None = Field(default=None, ge=0)
    amount_quote_raw: int | None = Field(default=None, ge=0)
    instruction_discriminator: str
    decode_status: DecodeStatus = DecodeStatus.DECODED
    details_json: str = "{}"

    @property
    def details(self) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(self.details_json)
        return value


class FundingEdge(BaseModel):
    model_config = ConfigDict(frozen=True)

    edge_id: str = Field(default_factory=lambda: uuid4().hex)
    target_wallet: str
    source_wallet: str
    funding_signature: str
    slot: int = Field(ge=0)
    block_time: datetime | None = None
    lamports: int = Field(gt=0)
    seconds_before_first_buy: int = Field(ge=0)
    source_kind: str = "system_program_transfer"
    relation_to_creation_user: bool | None = None
    relation_to_creation_creator: bool | None = None
    details_json: str = "{}"


class CreatorLaunchEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_id: str = Field(default_factory=lambda: uuid4().hex)
    creator: str
    mint: str
    creation_signature: str
    slot: int = Field(ge=0)
    block_time: datetime | None = None
    is_current_mint: bool
    source: str
    details_json: str = "{}"


class MemeChronologyObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    chronology_id: str = Field(default_factory=lambda: uuid4().hex)
    snapshot_id: str
    chronology_version: str
    started_at: datetime
    ready_at: datetime
    latency_ms: int = Field(ge=0)
    source_cutoff_at: datetime
    coverage_status: ChronologyCoverage
    history_truncated: bool
    reached_creation: bool
    signature_count: int = Field(ge=0)
    transaction_fetch_count: int = Field(ge=0)
    transaction_unavailable_count: int = Field(ge=0)
    unsupported_version_count: int = Field(ge=0)
    decode_failure_count: int = Field(ge=0)
    decoded_event_count: int = Field(ge=0)
    oldest_slot: int | None = Field(default=None, ge=0)
    newest_slot: int | None = Field(default=None, ge=0)
    oldest_block_time: datetime | None = None
    newest_block_time: datetime | None = None
    chronology_hash: str
    sources_json: str
    payload_json: str
    events: tuple[ChronologyEvent, ...] = Field(default_factory=tuple, repr=False)
    facts: tuple[MemecoinStateFact, ...] = Field(default_factory=tuple, repr=False)
    funding_edges: tuple[FundingEdge, ...] = Field(default_factory=tuple, repr=False)
    creator_launches: tuple[CreatorLaunchEvidence, ...] = Field(default_factory=tuple, repr=False)

    @model_validator(mode="after")
    def validate_observation(self) -> MemeChronologyObservation:
        if self.ready_at < self.started_at or self.source_cutoff_at > self.ready_at:
            raise ValueError("chronology timing is inconsistent")
        elapsed = int((self.ready_at - self.started_at).total_seconds() * 1000)
        if abs(elapsed - self.latency_ms) > 1:
            raise ValueError("latency_ms must match chronology clock")
        if self.chronology_hash != sha256(self.payload_json.encode()).hexdigest():
            raise ValueError("chronology_hash must match exact canonical payload")
        return self

    @classmethod
    def create(
        cls,
        *,
        snapshot_id: str,
        chronology_version: str,
        started_at: datetime,
        ready_at: datetime,
        source_cutoff_at: datetime,
        coverage_status: ChronologyCoverage,
        history_truncated: bool,
        reached_creation: bool,
        signature_count: int,
        transaction_fetch_count: int,
        transaction_unavailable_count: int,
        unsupported_version_count: int,
        decode_failure_count: int,
        oldest_slot: int | None,
        newest_slot: int | None,
        oldest_block_time: datetime | None,
        newest_block_time: datetime | None,
        sources: list[str],
        events: list[ChronologyEvent],
        facts: list[MemecoinStateFact],
        funding_edges: list[FundingEdge],
        creator_launches: list[CreatorLaunchEvidence],
    ) -> MemeChronologyObservation:
        payload = {
            "snapshot_id": snapshot_id,
            "chronology_version": chronology_version,
            "source_cutoff_at": source_cutoff_at.isoformat(),
            "coverage_status": coverage_status.value,
            "history_truncated": history_truncated,
            "reached_creation": reached_creation,
            "signature_count": signature_count,
            "transaction_fetch_count": transaction_fetch_count,
            "transaction_unavailable_count": transaction_unavailable_count,
            "unsupported_version_count": unsupported_version_count,
            "decode_failure_count": decode_failure_count,
            "oldest_slot": oldest_slot,
            "newest_slot": newest_slot,
            "oldest_block_time": oldest_block_time.isoformat() if oldest_block_time else None,
            "newest_block_time": newest_block_time.isoformat() if newest_block_time else None,
            "events": [_hashable(event) for event in events],
            "facts": [_hashable_fact(fact) for fact in facts],
            "funding_edges": [_hashable(edge) for edge in funding_edges],
            "creator_launches": [_hashable(item) for item in creator_launches],
        }
        payload_json = canonical_json(payload)
        return cls(
            snapshot_id=snapshot_id,
            chronology_version=chronology_version,
            started_at=started_at,
            ready_at=ready_at,
            latency_ms=int((ready_at - started_at).total_seconds() * 1000),
            source_cutoff_at=source_cutoff_at,
            coverage_status=coverage_status,
            history_truncated=history_truncated,
            reached_creation=reached_creation,
            signature_count=signature_count,
            transaction_fetch_count=transaction_fetch_count,
            transaction_unavailable_count=transaction_unavailable_count,
            unsupported_version_count=unsupported_version_count,
            decode_failure_count=decode_failure_count,
            decoded_event_count=len(events),
            oldest_slot=oldest_slot,
            newest_slot=newest_slot,
            oldest_block_time=oldest_block_time,
            newest_block_time=newest_block_time,
            chronology_hash=sha256(payload_json.encode()).hexdigest(),
            sources_json=canonical_json(sorted(set(sources))),
            payload_json=payload_json,
            events=tuple(events),
            facts=tuple(facts),
            funding_edges=tuple(funding_edges),
            creator_launches=tuple(creator_launches),
        )


def experiment_context_ready_at(
    snapshot_captured_at: datetime, state_ready_at: datetime, chronology_ready_at: datetime
) -> datetime:
    return max(snapshot_captured_at, state_ready_at, chronology_ready_at)


def experiment_context_input_hash(
    *,
    snapshot_input_hash: str,
    snapshot_version: str,
    state_hash: str,
    state_version: str,
    chronology_hash: str,
    chronology_version: str,
) -> str:
    return sha256(
        canonical_json(
            {
                "snapshot_input_hash": snapshot_input_hash,
                "snapshot_version": snapshot_version,
                "state_hash": state_hash,
                "state_version": state_version,
                "chronology_hash": chronology_hash,
                "chronology_version": chronology_version,
            }
        ).encode()
    ).hexdigest()


def _hashable(model: BaseModel) -> dict[str, Any]:
    value = model.model_dump(mode="json", exclude={"event_id", "edge_id", "evidence_id"})
    return value


def _hashable_fact(fact: MemecoinStateFact) -> dict[str, Any]:
    return fact.model_dump(mode="json", exclude={"fact_id"})
