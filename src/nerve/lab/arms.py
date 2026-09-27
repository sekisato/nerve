from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Protocol

from .models import (
    DecisionStatus,
    ExperimentDecision,
    MeasurementEvent,
    MeasurementEventKind,
    ObservationSnapshot,
    utc_now,
)

logger = logging.getLogger(__name__)


class ExperimentArm(Protocol):
    arm_id: str

    def evaluate(self, snapshot: ObservationSnapshot, deadline_at: datetime) -> ExperimentDecision: ...


class ControlArm:
    """A no-model baseline that records an explicit abstention."""

    arm_id = "CONTROL"

    def evaluate(self, snapshot: ObservationSnapshot, deadline_at: datetime) -> ExperimentDecision:
        started = utc_now()
        completed = utc_now()
        return ExperimentDecision(
            snapshot_id=snapshot.snapshot_id,
            input_hash=snapshot.input_hash,
            arm_id=self.arm_id,
            strategy_id="control-abstain-v1",
            question_version="control-v1",
            status=DecisionStatus.ABSTAINED,
            started_at=started,
            completed_at=completed,
            deadline_at=deadline_at,
            latency_ms=int((completed - started).total_seconds() * 1000),
            raw_answer={"action": "ABSTAIN"},
            reason="Phase 0 control performs no model call and cannot execute",
        )


class DecisionSink(Protocol):
    def record_experiment_decision(self, decision: ExperimentDecision) -> None: ...

    def record_measurement_event(self, event: MeasurementEvent) -> None: ...


class LabRunner:
    """Runs independent arms against one exact immutable input."""

    def __init__(self, sink: DecisionSink, arms: Iterable[ExperimentArm]) -> None:
        self.sink = sink
        self.arms = tuple(arms)

    def evaluate(
        self,
        snapshot: ObservationSnapshot,
        *,
        deadline_at: datetime | None = None,
    ) -> list[ExperimentDecision]:
        deadline = deadline_at or (utc_now() + timedelta(seconds=30))
        decisions: list[ExperimentDecision] = []
        for arm in self.arms:
            try:
                decision = arm.evaluate(snapshot, deadline)
                if (
                    decision.snapshot_id != snapshot.snapshot_id
                    or decision.input_hash != snapshot.input_hash
                ):
                    raise ValueError("experiment arm did not use the supplied frozen snapshot")
                self.sink.record_experiment_decision(decision)
                decisions.append(decision)
            except Exception as exc:
                event = MeasurementEvent(
                    kind=MeasurementEventKind.ARM_FAILED,
                    stage=f"arm:{arm.arm_id}",
                    impulse_id=snapshot.impulse_id,
                    snapshot_id=snapshot.snapshot_id,
                    error_type=type(exc).__name__,
                    message=str(exc)[:1000],
                    details={"arm_id": arm.arm_id},
                )
                try:
                    self.sink.record_measurement_event(event)
                except Exception:
                    logger.exception("failed to persist arm failure measurement event")
        return decisions
