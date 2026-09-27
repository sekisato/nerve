from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Protocol

from ..lab.models import MeasurementEvent, MeasurementEventKind, ObservationSnapshot
from ..memestate.models import utc_now
from .context import build_semantic_context
from .models import (
    QUESTION_SET_VERSION,
    DataSufficiency,
    SemanticContext,
    SemanticDecisionStatus,
    SemanticForwardOutcome,
    SemanticReflexDecision,
)
from .provider import SemanticProvider

DEFAULT_HORIZONS = (60, 300, 900, 1800)


class SemanticStore(Protocol):
    def latest_memecoin_state(self, snapshot_id: str) -> Any: ...

    def latest_meme_chronology(self, snapshot_id: str) -> Any: ...

    def semantic_context_by_hash(self, context_input_hash: str) -> SemanticContext | None: ...

    def record_semantic_context(self, context: SemanticContext) -> None: ...

    def reusable_semantic_decision(
        self, context_input_hash: str, question_set_version: str, model: str
    ) -> SemanticReflexDecision | None: ...

    def latest_semantic_call_for_token(self, token: str) -> tuple[str, datetime] | None: ...

    def record_semantic_decision(
        self,
        decision: SemanticReflexDecision,
        outcomes: list[SemanticForwardOutcome],
    ) -> None: ...

    def record_measurement_event(self, event: MeasurementEvent) -> None: ...


class SemanticReflexCollector:
    """Explicit lab-only semantic measurement; never an execution input."""

    def __init__(
        self,
        store: SemanticStore,
        *,
        provider: SemanticProvider | None,
        requested_model: str = "jev-latest",
        clock: Any = utc_now,
    ) -> None:
        self.store = store
        self.provider = provider
        self.requested_model = requested_model
        self.clock = clock

    def prepare_context(self, snapshot: ObservationSnapshot) -> SemanticContext:
        state = self.store.latest_memecoin_state(snapshot.snapshot_id)
        chronology = self.store.latest_meme_chronology(snapshot.snapshot_id)
        if state is None or chronology is None:
            raise ValueError("snapshot needs an eligible state and chronology observation")
        context = build_semantic_context(snapshot, state, chronology, created_at=self.clock())
        existing = self.store.semantic_context_by_hash(context.context_input_hash)
        if existing is not None:
            return existing
        self.store.record_semantic_context(context)
        return context

    def collect(
        self,
        snapshot: ObservationSnapshot,
        *,
        live_jev: bool = False,
        deadline_ms: int = 2000,
        min_recall_seconds: int = 60,
    ) -> dict[str, Any]:
        deadline_ms = max(100, min(deadline_ms, 60_000))
        min_recall_seconds = max(0, min(min_recall_seconds, 86_400))
        try:
            context = self.prepare_context(snapshot)
        except Exception as exc:
            self._failure_event(snapshot, MeasurementEventKind.SEMANTIC_CONTEXT_FAILED, exc)
            return _result(context=None, eligible=0, failed=1, reason=str(exc))
        reusable = self.store.reusable_semantic_decision(
            context.context_input_hash, QUESTION_SET_VERSION, self.requested_model
        )
        if reusable is not None:
            return _result(context=context, eligible=1, deduped=1, decision=reusable)
        if not live_jev:
            return _result(context=context, eligible=1, reason="dry run: --live-jev was not supplied")
        if self.provider is None:
            provider_error = ValueError("Jev provider is unavailable; configure TYPESAFE_API_KEY")
            self._failure_event(snapshot, MeasurementEventKind.JEV_REFLEX_FAILED, provider_error)
            return _result(context=context, eligible=1, failed=1, reason=str(provider_error))
        previous = self.store.latest_semantic_call_for_token(snapshot.token)
        now: datetime = self.clock()
        if now < context.context_ready_at:
            timing_error = ValueError("Jev start cannot precede context_ready_at")
            self._failure_event(snapshot, MeasurementEventKind.JEV_REFLEX_FAILED, timing_error)
            return _result(context=context, eligible=0, failed=1, reason=str(timing_error))
        previous_hash = previous[0] if previous else None
        seconds_since = int((now - previous[1]).total_seconds()) if previous else None
        if seconds_since is not None and seconds_since < min_recall_seconds:
            return _result(context=context, eligible=1, deduped=1, reason="minimum recall interval active")
        started_at = now
        deadline_at = started_at + timedelta(milliseconds=deadline_ms)
        try:
            vector, returned_model, raw = self.provider.evaluate(
                context.payload,
                model=self.requested_model,
                timeout_seconds=min(65.0, deadline_ms / 1000 + 2.0),
            )
            completed_at: datetime = self.clock()
        except Exception as exc:
            completed_at = self.clock()
            decision = SemanticReflexDecision(
                context_id=context.context_id,
                provider=self.provider.provider_name,
                requested_model_id=self.requested_model,
                started_at=started_at,
                completed_at=completed_at,
                deadline_at=deadline_at,
                latency_ms=max(0, int((completed_at - started_at).total_seconds() * 1000)),
                status=SemanticDecisionStatus.FAILED,
                raw_answer_json=getattr(exc, "raw_answer_json", None),
                validation_error=f"{type(exc).__name__}: {str(exc)[:800]}",
                previous_context_hash=previous_hash,
                seconds_since_previous_semantic_call=seconds_since,
            )
            self.store.record_semantic_decision(decision, [])
            self._failure_event(snapshot, MeasurementEventKind.JEV_REFLEX_FAILED, exc)
            return _result(context=context, eligible=1, calls=1, failed=1, decision=decision)
        if completed_at > deadline_at:
            status = SemanticDecisionStatus.EXPIRED
        elif vector.overall_data_sufficiency is DataSufficiency.INSUFFICIENT:
            status = SemanticDecisionStatus.INSUFFICIENT
        elif vector.abstain:
            status = SemanticDecisionStatus.ABSTAINED
        else:
            status = SemanticDecisionStatus.COMPLETED
        decision = SemanticReflexDecision(
            context_id=context.context_id,
            provider=self.provider.provider_name,
            requested_model_id=self.requested_model,
            returned_model_id=returned_model,
            started_at=started_at,
            completed_at=completed_at,
            deadline_at=deadline_at,
            latency_ms=int((completed_at - started_at).total_seconds() * 1000),
            status=status,
            overall_data_sufficiency=vector.overall_data_sufficiency,
            abstain=vector.abstain,
            raw_answer_json=raw,
            model_mismatch=returned_model != self.requested_model,
            previous_context_hash=previous_hash,
            seconds_since_previous_semantic_call=seconds_since,
            dimensions=vector.dimensions,
        )
        outcomes = [
            SemanticForwardOutcome(
                semantic_decision_id=decision.semantic_decision_id,
                recorded_at=completed_at,
                horizon_seconds=horizon,
                anchor_at=completed_at,
                target_at=completed_at + timedelta(seconds=horizon),
            )
            for horizon in DEFAULT_HORIZONS
        ]
        self.store.record_semantic_decision(decision, outcomes)
        return _result(
            context=context,
            eligible=1,
            calls=1,
            successful=int(status is not SemanticDecisionStatus.EXPIRED),
            expired=int(status is SemanticDecisionStatus.EXPIRED),
            decision=decision,
        )

    def _failure_event(
        self, snapshot: ObservationSnapshot, kind: MeasurementEventKind, exc: Exception
    ) -> None:
        self.store.record_measurement_event(
            MeasurementEvent(
                kind=kind,
                stage="jev_semantic_reflex",
                impulse_id=snapshot.impulse_id,
                snapshot_id=snapshot.snapshot_id,
                error_type=type(exc).__name__,
                message=str(exc)[:1000],
                details={"question_set_version": QUESTION_SET_VERSION},
            )
        )


def _result(
    *,
    context: SemanticContext | None,
    eligible: int,
    deduped: int = 0,
    calls: int = 0,
    successful: int = 0,
    expired: int = 0,
    failed: int = 0,
    reason: str | None = None,
    decision: SemanticReflexDecision | None = None,
) -> dict[str, Any]:
    return {
        "context_id": context.context_id if context else None,
        "context_input_hash": context.context_input_hash if context else None,
        "context_ready_at": context.context_ready_at.isoformat() if context else None,
        "eligible_contexts": eligible,
        "deduped_contexts": deduped,
        "actual_jev_calls": calls,
        "successful_responses": successful,
        "expired_responses": expired,
        "failed_responses": failed,
        "reason": reason,
        "semantic_decision_id": decision.semantic_decision_id if decision else None,
        "status": decision.status.value if decision else None,
    }
