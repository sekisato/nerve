from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nerve.chronology.models import (
    ChronologyCoverage,
    ChronologyEvent,
    ChronologyEventType,
    MemeChronologyObservation,
)
from nerve.lab.models import ObservationSnapshot
from nerve.memestate.models import MemecoinStateFact, MemecoinStateObservation, StateStatus
from nerve.models import ChainName, Impulse
from nerve.observatory.read_store import ObservatoryReadStore
from nerve.semantic.collector import SemanticReflexCollector
from nerve.semantic.context import build_semantic_context
from nerve.semantic.models import (
    DIMENSION_NAMES,
    DataSufficiency,
    DirectionState,
    InventoryTransition,
    SemanticDecisionStatus,
    SemanticDimension,
    SemanticVector,
    raw_json,
)
from nerve.semantic.provider import parse_semantic_answers, semantic_questions
from nerve.store import NerveStore

NOW = datetime(2026, 9, 27, tzinfo=UTC)


def snapshot(*, token: str = "mint") -> ObservationSnapshot:
    impulse = Impulse(id="semantic-identity", chain=ChainName.SOLANA, token=token, pool="pool", created_at=NOW)
    return ObservationSnapshot.capture(impulse, captured_at=NOW + timedelta(seconds=1))


def state(snap: ObservationSnapshot, *, value: int = 0, ready: int = 2) -> MemecoinStateObservation:
    return MemecoinStateObservation.create(
        snapshot_id=snap.snapshot_id, state_version="state-v1", started_at=NOW,
        ready_at=NOW + timedelta(seconds=ready), status=StateStatus.PARTIAL,
        sources=["fixture"], facts=[
            MemecoinStateFact.observed("m5_buys", value, source="fixture", fetched_at=NOW),
            MemecoinStateFact.unavailable("sniper_supply_pct", "unknown", source="fixture"),
            MemecoinStateFact.unavailable("bundler_supply_pct", "unknown", source="fixture"),
        ],
    )


def chronology(snap: ObservationSnapshot, *, user: str = "buyer", ready: int = 3) -> MemeChronologyObservation:
    event = ChronologyEvent(
        signature="sig", slot=1, block_time=NOW, instruction_path="outer:0",
        program_id="pump", venue="pump_curve", event_type=ChronologyEventType.BUY,
        user=user, mint=snap.token, instruction_discriminator="00",
    )
    return MemeChronologyObservation.create(
        snapshot_id=snap.snapshot_id, chronology_version="chron-v1", started_at=NOW,
        ready_at=NOW + timedelta(seconds=ready), source_cutoff_at=NOW,
        coverage_status=ChronologyCoverage.PARTIAL, history_truncated=True,
        reached_creation=False, signature_count=1, transaction_fetch_count=1,
        transaction_unavailable_count=0, unsupported_version_count=0, decode_failure_count=0,
        oldest_slot=1, newest_slot=1, oldest_block_time=NOW, newest_block_time=NOW,
        sources=["fixture"], events=[event], facts=[
            MemecoinStateFact.observed("observed_buy_event_count", 1, source="fixture", fetched_at=NOW),
            MemecoinStateFact.observed("observed_unique_buyers_count", 1, source="fixture", fetched_at=NOW),
            MemecoinStateFact.observed("multi_buyer_slot_count", 0, source="fixture", fetched_at=NOW),
            MemecoinStateFact.unavailable("funding_coverage_pct", "probe disabled", source="fixture"),
        ], funding_edges=[], creator_launches=[],
    )


def vector(*, abstain: bool = False, sufficient: DataSufficiency = DataSufficiency.SUFFICIENT) -> SemanticVector:
    dimensions = []
    for name in DIMENSION_NAMES:
        dimensions.append(SemanticDimension(
            dimension_name=name,
            state=InventoryTransition.STABLE if name == "inventory_transition" else DirectionState.LOW,
            semantic_confidence=0.75,
        ))
    return SemanticVector(overall_data_sufficiency=sufficient, abstain=abstain, dimensions=tuple(dimensions))


class FakeProvider:
    provider_name = "fixture-jev"

    def __init__(self, result: SemanticVector | Exception | None = None, returned_model: str = "jev-1.13") -> None:
        self.result = result or vector()
        self.returned_model = returned_model
        self.calls = 0

    def evaluate(self, payload: dict[str, Any], *, model: str, timeout_seconds: float) -> tuple[SemanticVector, str, str]:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result, self.returned_model, raw_json({"model": self.returned_model, "answers": {}})


class Clock:
    def __init__(self, *values: datetime) -> None:
        self.values = list(values)

    def __call__(self) -> datetime:
        return self.values.pop(0)


def populated_store(path: Path) -> tuple[NerveStore, ObservationSnapshot]:
    store = NerveStore(path)
    snap = snapshot()
    store.record_observation_snapshot(snap)
    store.record_memecoin_state(state(snap))
    store.record_meme_chronology(chronology(snap))
    return store, snap


def test_context_binds_exact_latest_triple_and_ready_time(tmp_path: Path) -> None:
    store, snap = populated_store(tmp_path / "truth.db")
    later_state = state(snap, value=2, ready=4)
    store.record_memecoin_state(later_state)
    context = SemanticReflexCollector(store, provider=None, clock=lambda: NOW + timedelta(seconds=5)).prepare_context(snap)
    assert context.snapshot_id == snap.snapshot_id
    assert context.state_id == later_state.state_id
    assert context.chronology_id == store.latest_meme_chronology(snap.snapshot_id).chronology_id  # type: ignore[union-attr]
    assert context.context_ready_at == NOW + timedelta(seconds=4)
    assert context.payload["coverage"]["chronology_status"] == "partial"
    assert context.payload["market"]["m5_buys"]["value"] == 2
    assert context.payload["ownership"]["sniper_supply_pct"]["status"] == "unavailable"
    assert context.payload["ownership"]["bundler_supply_pct"]["status"] == "unavailable"
    assert "TYPESAFE_API_KEY" not in context.payload_json
    store.close()


def test_context_hash_changes_with_state_or_chronology() -> None:
    snap = snapshot()
    base = build_semantic_context(snap, state(snap), chronology(snap), created_at=NOW + timedelta(seconds=4))
    changed_state = build_semantic_context(snap, state(snap, value=1), chronology(snap), created_at=NOW + timedelta(seconds=4))
    changed_chron = build_semantic_context(snap, state(snap), chronology(snap, user="other"), created_at=NOW + timedelta(seconds=4))
    assert len({base.context_input_hash, changed_state.context_input_hash, changed_chron.context_input_hash}) == 3


def test_context_cannot_be_created_or_called_before_ready(tmp_path: Path) -> None:
    snap = snapshot()
    with pytest.raises(ValueError, match="cannot be created"):
        build_semantic_context(snap, state(snap), chronology(snap), created_at=NOW)
    store, snap = populated_store(tmp_path / "truth.db")
    result = SemanticReflexCollector(store, provider=FakeProvider(), clock=Clock(NOW + timedelta(seconds=4), NOW)).collect(snap, live_jev=True)
    assert result["actual_jev_calls"] == 0 and result["failed_responses"] == 1
    store.close()


def test_typed_vector_enums_confidence_insufficient_and_no_profit_probability() -> None:
    assert vector(abstain=True, sufficient=DataSufficiency.INSUFFICIENT).abstain is True
    with pytest.raises(ValueError):
        SemanticDimension(dimension_name="flow_toxicity", state="EXTREME", semantic_confidence=0.5)
    with pytest.raises(ValueError):
        SemanticDimension(dimension_name="flow_toxicity", state="LOW", semantic_confidence=1.1)
    fields = SemanticVector.model_fields | SemanticDimension.model_fields
    assert "win_probability" not in fields and "success_probability" not in fields
    assert "semantic_confidence" in SemanticDimension.model_fields
    assert "jev_score" not in fields


def answer(choice: str, confidence: float = 0.8) -> dict[str, Any]:
    return {"type": "choice", "choice": choice, "confidence": confidence, "probabilities": {choice: 1.0}}


def test_official_multi_question_shape_parses_strict_full_vector() -> None:
    answers = {name: answer("STABLE" if name == "inventory_transition" else "LOW") for name in DIMENSION_NAMES}
    answers["overall_data_sufficiency"] = answer("PARTIAL")
    answers["abstain"] = answer("TRUE")
    parsed = parse_semantic_answers(answers)
    assert parsed.abstain and parsed.overall_data_sufficiency is DataSufficiency.PARTIAL
    assert len(parsed.dimensions) == 7
    questions = semantic_questions()
    assert len(questions) == 9 and all(item["type"] == "choice" for item in questions.values())
    with pytest.raises(ValueError, match="missing"):
        parse_semantic_answers({})


def test_live_gate_dedupe_versions_dimensions_and_forward_clock(tmp_path: Path) -> None:
    store, snap = populated_store(tmp_path / "truth.db")
    provider = FakeProvider()
    collector = SemanticReflexCollector(
        store, provider=provider, requested_model="jev-latest",
        clock=Clock(NOW + timedelta(seconds=4), NOW + timedelta(seconds=4), NOW + timedelta(milliseconds=4500), NOW + timedelta(seconds=5)),
    )
    dry = collector.collect(snap, live_jev=False)
    assert dry["actual_jev_calls"] == 0 and provider.calls == 0
    live = collector.collect(snap, live_jev=True, deadline_ms=2000, min_recall_seconds=0)
    assert live["successful_responses"] == 1 and provider.calls == 1
    decision = store.list_semantic_decisions()[0]
    assert decision.question_set_version == "jev-semantic-reflex-v1"
    assert decision.requested_model_id == "jev-latest" and decision.returned_model_id == "jev-1.13"
    assert decision.model_mismatch is True
    assert len(decision.dimensions) == 7
    assert decision.execution_eligible is False
    outcomes = store.list_semantic_forward_outcomes(decision.semantic_decision_id)
    assert len(outcomes) == 4 and all(item.status.value == "pending" for item in outcomes)
    assert outcomes[0].anchor_at == decision.completed_at
    assert outcomes[0].target_at == decision.completed_at + timedelta(seconds=60)
    deduped = SemanticReflexCollector(store, provider=provider, clock=lambda: NOW + timedelta(seconds=10)).collect(snap, live_jev=True)
    assert deduped["deduped_contexts"] == 1 and provider.calls == 1
    store.close()


def test_new_context_is_eligible_and_cooldown_blocks_paid_recall(tmp_path: Path) -> None:
    store, snap = populated_store(tmp_path / "truth.db")
    provider = FakeProvider(returned_model="jev-latest")
    first = SemanticReflexCollector(store, provider=provider, clock=Clock(NOW + timedelta(seconds=4), NOW + timedelta(seconds=4), NOW + timedelta(seconds=5)))
    first.collect(snap, live_jev=True, min_recall_seconds=0)
    store.record_memecoin_state(state(snap, value=9, ready=6))
    second = SemanticReflexCollector(store, provider=provider, clock=Clock(NOW + timedelta(seconds=7), NOW + timedelta(seconds=7)))
    result = second.collect(snap, live_jev=True, min_recall_seconds=60)
    assert result["eligible_contexts"] == 1 and result["deduped_contexts"] == 1
    assert provider.calls == 1
    store.close()


def test_malformed_is_failed_and_late_valid_result_is_expired(tmp_path: Path) -> None:
    store, snap = populated_store(tmp_path / "truth.db")
    broken = SemanticReflexCollector(store, provider=FakeProvider(ValueError("malformed")), clock=Clock(NOW + timedelta(seconds=4), NOW + timedelta(seconds=4), NOW + timedelta(seconds=5)))
    result = broken.collect(snap, live_jev=True, min_recall_seconds=0)
    assert result["failed_responses"] == 1
    failed = store.list_semantic_decisions()[-1]
    assert failed.status is SemanticDecisionStatus.FAILED and not failed.dimensions
    store.record_memecoin_state(state(snap, value=4, ready=6))
    late = SemanticReflexCollector(store, provider=FakeProvider(returned_model="jev-latest"), clock=Clock(NOW + timedelta(seconds=7), NOW + timedelta(seconds=7), NOW + timedelta(seconds=10)))
    result = late.collect(snap, live_jev=True, deadline_ms=2000, min_recall_seconds=0)
    assert result["expired_responses"] == 1
    expired = store.list_semantic_decisions()[-1]
    assert expired.status is SemanticDecisionStatus.EXPIRED and expired.execution_eligible is False
    store.close()


def test_observatory_exposes_no_key_and_remains_read_only(tmp_path: Path) -> None:
    store, snap = populated_store(tmp_path / "truth.db")
    SemanticReflexCollector(store, provider=None, clock=lambda: NOW + timedelta(seconds=4)).collect(snap)
    store.close()
    with ObservatoryReadStore(tmp_path / "truth.db") as reader:
        detail = reader.snapshot_detail(snap.snapshot_id)
        assert detail is not None and detail["semantic_reflex"]["contexts"]
        assert "api_key" not in raw_json(detail).lower()
        with pytest.raises(sqlite3.OperationalError):
            reader.conn.execute("DELETE FROM semantic_contexts")
