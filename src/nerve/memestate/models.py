from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class FactStatus(StrEnum):
    OBSERVED = "observed"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    INVALID = "invalid"
    CONFLICT = "conflict"
    NOT_APPLICABLE = "not_applicable"


class StateStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class LifecycleState(StrEnum):
    PUMP_CURVE_ACTIVE = "pump_curve_active"
    PUMP_CURVE_COMPLETE_MIGRATION_UNKNOWN = "pump_curve_complete_migration_unknown"
    PUMP_CANONICAL_PUMPSWAP_VERIFIED = "pump_canonical_pumpswap_verified"
    NOT_PUMP = "not_pump"
    UNKNOWN = "unknown"


class MemecoinStateFact(BaseModel):
    """One typed fact with explicit absence and provenance semantics."""

    model_config = ConfigDict(frozen=True)

    fact_id: str = Field(default_factory=lambda: uuid4().hex)
    field_name: str
    status: FactStatus
    value_num: Decimal | None = None
    value_int: int | None = None
    value_text: str | None = None
    value_bool: bool | None = None
    unit: str | None = None
    source: str | None = None
    source_observed_at: datetime | None = None
    fetched_at: datetime | None = None
    age_ms: int | None = Field(default=None, ge=0)
    reason: str | None = None
    details_json: str = "{}"

    @model_validator(mode="after")
    def validate_fact_contract(self) -> MemecoinStateFact:
        values = (self.value_num, self.value_int, self.value_text, self.value_bool)
        populated = sum(value is not None for value in values)
        if self.status is FactStatus.OBSERVED:
            if populated != 1:
                raise ValueError("observed facts require exactly one typed value")
            if not self.source or self.fetched_at is None:
                raise ValueError("observed facts require source and fetched_at provenance")
        elif populated:
            raise ValueError("non-observed facts must keep their value null")
        json.loads(self.details_json)
        return self

    @property
    def value_kind(self) -> str | None:
        for name, value in (
            ("num", self.value_num),
            ("int", self.value_int),
            ("text", self.value_text),
            ("bool", self.value_bool),
        ):
            if value is not None:
                return name
        return None

    @property
    def value(self) -> Decimal | int | str | bool | None:
        for value in (self.value_num, self.value_int, self.value_text, self.value_bool):
            if value is not None:
                return value
        return None

    @property
    def details(self) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(self.details_json)
        return value

    @classmethod
    def observed(
        cls,
        field_name: str,
        value: Decimal | int | str | bool,
        *,
        source: str,
        fetched_at: datetime,
        unit: str | None = None,
        source_observed_at: datetime | None = None,
        age_ms: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> MemecoinStateFact:
        typed: dict[str, Any]
        if isinstance(value, bool):
            typed = {"value_bool": value}
        elif isinstance(value, int):
            typed = {"value_int": value}
        elif isinstance(value, Decimal):
            typed = {"value_num": value}
        else:
            typed = {"value_text": value}
        return cls(
            field_name=field_name,
            status=FactStatus.OBSERVED,
            source=source,
            fetched_at=fetched_at,
            source_observed_at=source_observed_at,
            age_ms=age_ms,
            unit=unit,
            details_json=canonical_json(details or {}),
            **typed,
        )

    @classmethod
    def unavailable(
        cls,
        field_name: str,
        reason: str,
        *,
        source: str | None = None,
        fetched_at: datetime | None = None,
        status: FactStatus = FactStatus.UNAVAILABLE,
        details: dict[str, Any] | None = None,
    ) -> MemecoinStateFact:
        return cls(
            field_name=field_name,
            status=status,
            source=source,
            fetched_at=fetched_at,
            reason=reason,
            details_json=canonical_json(details or {}),
        )


class MemecoinStateObservation(BaseModel):
    """Append-only collection attempt associated with one Phase 0 snapshot."""

    model_config = ConfigDict(frozen=True)

    state_id: str = Field(default_factory=lambda: uuid4().hex)
    snapshot_id: str
    state_version: str
    started_at: datetime
    ready_at: datetime
    latency_ms: int = Field(ge=0)
    state_hash: str
    status: StateStatus
    sources_json: str
    payload_json: str
    facts: tuple[MemecoinStateFact, ...] = Field(default_factory=tuple, repr=False)

    @model_validator(mode="after")
    def valid_clock(self) -> MemecoinStateObservation:
        if self.ready_at < self.started_at:
            raise ValueError("ready_at cannot precede started_at")
        elapsed = int((self.ready_at - self.started_at).total_seconds() * 1000)
        if abs(elapsed - self.latency_ms) > 1:
            raise ValueError("latency_ms must match collection clock")
        if self.state_hash != sha256(self.payload_json.encode()).hexdigest():
            raise ValueError("state_hash must match the exact canonical state payload")
        json.loads(self.sources_json)
        return self

    @classmethod
    def create(
        cls,
        *,
        snapshot_id: str,
        state_version: str,
        started_at: datetime,
        ready_at: datetime,
        status: StateStatus,
        sources: list[str],
        facts: list[MemecoinStateFact],
    ) -> MemecoinStateObservation:
        fact_payload = [
            {
                "field_name": fact.field_name,
                "status": fact.status.value,
                "value_kind": fact.value_kind,
                "value": str(fact.value) if isinstance(fact.value, Decimal) else fact.value,
                "unit": fact.unit,
                "source": fact.source,
                "source_observed_at": (
                    fact.source_observed_at.isoformat() if fact.source_observed_at else None
                ),
                "fetched_at": fact.fetched_at.isoformat() if fact.fetched_at else None,
                "age_ms": fact.age_ms,
                "reason": fact.reason,
                "details": fact.details,
            }
            for fact in facts
        ]
        payload_json = canonical_json(
            {
                "snapshot_id": snapshot_id,
                "state_version": state_version,
                "facts": fact_payload,
            }
        )
        return cls(
            snapshot_id=snapshot_id,
            state_version=state_version,
            started_at=started_at,
            ready_at=ready_at,
            latency_ms=int((ready_at - started_at).total_seconds() * 1000),
            state_hash=sha256(payload_json.encode()).hexdigest(),
            status=status,
            sources_json=canonical_json(sorted(set(sources))),
            payload_json=payload_json,
            facts=tuple(facts),
        )


def experiment_state_input_hash(
    snapshot_input_hash: str, state_hash: str, state_version: str
) -> str:
    payload = canonical_json(
        {
            "snapshot_input_hash": snapshot_input_hash,
            "state_hash": state_hash,
            "state_version": state_version,
        }
    )
    return sha256(payload.encode()).hexdigest()
