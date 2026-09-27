from __future__ import annotations

import json
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..memestate.models import canonical_json

QUESTION_SET_VERSION = "jev-semantic-reflex-v1"
CONTEXT_VERSION = "semantic-context-v1"
DEADLINE_POLICY_VERSION = "semantic-deadline-v1"
DIMENSION_NAMES = (
    "flow_toxicity",
    "buyer_persistence",
    "coordination_like",
    "inventory_transition",
    "distribution_risk",
    "late_entry_risk",
    "fresh_demand_like",
)


class DirectionState(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    INSUFFICIENT = "INSUFFICIENT"


class InventoryTransition(StrEnum):
    ACCUMULATION = "ACCUMULATION"
    DISTRIBUTION = "DISTRIBUTION"
    MIXED = "MIXED"
    STABLE = "STABLE"
    INSUFFICIENT = "INSUFFICIENT"


class DataSufficiency(StrEnum):
    SUFFICIENT = "SUFFICIENT"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"


class SemanticDecisionStatus(StrEnum):
    COMPLETED = "completed"
    ABSTAINED = "abstained"
    INSUFFICIENT = "insufficient"
    EXPIRED = "expired"
    FAILED = "failed"


class SemanticOutcomeStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    UNAVAILABLE = "unavailable"


class SemanticContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    context_id: str = Field(default_factory=lambda: uuid4().hex)
    snapshot_id: str
    state_id: str
    chronology_id: str
    snapshot_input_hash: str
    state_hash: str
    chronology_hash: str
    context_input_hash: str
    snapshot_captured_at: datetime
    state_ready_at: datetime
    chronology_ready_at: datetime
    context_ready_at: datetime
    context_version: str = CONTEXT_VERSION
    payload_json: str
    created_at: datetime

    @property
    def payload(self) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(self.payload_json)
        return value

    @model_validator(mode="after")
    def timing_contract(self) -> SemanticContext:
        if self.context_ready_at != max(
            self.snapshot_captured_at, self.state_ready_at, self.chronology_ready_at
        ):
            raise ValueError("context_ready_at must equal the latest required availability")
        if self.created_at < self.context_ready_at:
            raise ValueError("semantic context cannot be created before it is ready")
        return self


class SemanticDimension(BaseModel):
    model_config = ConfigDict(frozen=True)

    dimension_id: str = Field(default_factory=lambda: uuid4().hex)
    dimension_name: str
    state: str
    semantic_confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def valid_name_and_state(self) -> SemanticDimension:
        if self.dimension_name not in DIMENSION_NAMES:
            raise ValueError("unknown semantic dimension")
        if self.dimension_name == "inventory_transition":
            if self.state not in set(InventoryTransition):
                raise ValueError("inventory_transition requires its typed state")
        elif self.state not in set(DirectionState):
            raise ValueError("directional dimension requires LOW/MEDIUM/HIGH/INSUFFICIENT")
        return self


class SemanticVector(BaseModel):
    model_config = ConfigDict(frozen=True)

    overall_data_sufficiency: DataSufficiency
    abstain: bool
    dimensions: tuple[SemanticDimension, ...]

    @model_validator(mode="after")
    def complete_vector(self) -> SemanticVector:
        names = [item.dimension_name for item in self.dimensions]
        if len(names) != len(set(names)) or set(names) != set(DIMENSION_NAMES):
            raise ValueError("every semantic dimension must appear exactly once")
        return self


class SemanticReflexDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    semantic_decision_id: str = Field(default_factory=lambda: uuid4().hex)
    context_id: str
    question_set_version: str = QUESTION_SET_VERSION
    provider: str
    requested_model_id: str
    returned_model_id: str | None = None
    started_at: datetime
    completed_at: datetime
    deadline_at: datetime
    deadline_policy_version: str = DEADLINE_POLICY_VERSION
    latency_ms: int = Field(ge=0)
    status: SemanticDecisionStatus
    overall_data_sufficiency: DataSufficiency | None = None
    abstain: bool | None = None
    raw_answer_json: str | None = None
    validation_error: str | None = None
    model_mismatch: bool = False
    previous_context_hash: str | None = None
    seconds_since_previous_semantic_call: int | None = Field(default=None, ge=0)
    dimensions: tuple[SemanticDimension, ...] = Field(default_factory=tuple, repr=False)

    @model_validator(mode="after")
    def decision_contract(self) -> SemanticReflexDecision:
        elapsed = int((self.completed_at - self.started_at).total_seconds() * 1000)
        if self.completed_at < self.started_at or abs(elapsed - self.latency_ms) > 1:
            raise ValueError("semantic decision timing is inconsistent")
        if self.status is SemanticDecisionStatus.FAILED:
            if self.dimensions:
                raise ValueError("failed semantic decisions cannot contain dimensions")
        elif len(self.dimensions) != len(DIMENSION_NAMES):
            raise ValueError("non-failed semantic decisions require the full vector")
        return self

    @property
    def execution_eligible(self) -> bool:
        return False


class SemanticForwardOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    semantic_outcome_id: str = Field(default_factory=lambda: uuid4().hex)
    semantic_decision_id: str
    recorded_at: datetime
    horizon_seconds: int = Field(gt=0)
    anchor_at: datetime
    target_at: datetime
    resolved_at: datetime | None = None
    reference_price: str | None = None
    outcome_price: str | None = None
    return_pct: str | None = None
    status: SemanticOutcomeStatus = SemanticOutcomeStatus.PENDING

    @model_validator(mode="after")
    def target_uses_decision_anchor(self) -> SemanticForwardOutcome:
        if self.target_at != self.anchor_at + timedelta(seconds=self.horizon_seconds):
            raise ValueError("semantic outcome target must be decision-relative")
        return self


def raw_json(value: Any) -> str:
    return canonical_json(value)
