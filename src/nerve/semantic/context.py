from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from ..chronology.models import (
    MemeChronologyObservation,
    experiment_context_input_hash,
    experiment_context_ready_at,
)
from ..lab.models import ObservationSnapshot
from ..memestate.models import MemecoinStateFact, MemecoinStateObservation, canonical_json
from .models import CONTEXT_VERSION, SemanticContext

STATE_SECTIONS = {
    "market": {
        "liquidity_usd", "m5_buys", "m5_sells", "h1_buys", "h1_sells", "price_usd",
        "price_change_m5_pct", "price_change_h1_pct", "volume_m5", "volume_h1",
    },
    "ownership": {
        "top1_token_accounts_pct", "top5_token_accounts_pct", "top10_token_accounts_pct",
        "largest_owner_within_top20_pct", "top5_owners_within_top20_pct",
        "sniper_supply_pct", "bundler_supply_pct",
    },
    "pump_lifecycle": {
        "pump_lifecycle_state", "curve_complete", "coin_creator",
        "canonical_pumpswap_verified", "creator_fee_bps",
    },
}

CHRONOLOGY_SECTIONS = {
    "creation": {
        "creation_signature", "creation_slot", "creation_block_time", "creation_user",
        "creation_creator", "creation_mint",
    },
    "activity": {
        "observed_buy_event_count", "observed_sell_event_count",
        "observed_unique_buyers_count", "observed_unique_sellers_count",
        "unique_buyers_since_creation", "first_60s_buy_event_count",
        "first_300s_buy_event_count", "first_60s_distinct_buyer_count",
        "first_300s_distinct_buyer_count",
    },
    "early_cohort": {
        "early_cohort_target_n", "early_cohort_actual_n", "early_cohort_cutoff_slot",
        "early_cohort_balance_coverage_pct", "early_cohort_current_supply_pct",
    },
    "same_slot": {
        "multi_buyer_slot_count", "max_distinct_buyers_same_slot",
        "buy_events_in_multi_buyer_slots", "multi_buyer_slot_event_share_pct",
    },
    "funding": {
        "funding_probe_enabled", "funding_probe_wallet_count", "funding_coverage_pct",
        "shared_funder_group_count", "max_early_buyers_same_funder",
        "creation_user_funded_early_buyer_count", "creation_creator_funded_early_buyer_count",
    },
    "creator_history": {
        "creator_history_probe_enabled", "creator_observed_launch_count",
        "creator_observed_prior_launch_count", "creator_history_truncated",
    },
}


def build_semantic_context(
    snapshot: ObservationSnapshot,
    state: MemecoinStateObservation,
    chronology: MemeChronologyObservation,
    *,
    created_at: datetime,
) -> SemanticContext:
    if state.snapshot_id != snapshot.snapshot_id or chronology.snapshot_id != snapshot.snapshot_id:
        raise ValueError("semantic context requires one exact snapshot/state/chronology triple")
    ready_at = experiment_context_ready_at(
        snapshot.captured_at, state.ready_at, chronology.ready_at
    )
    context_hash = experiment_context_input_hash(
        snapshot_input_hash=snapshot.input_hash,
        snapshot_version=snapshot.stage,
        state_hash=state.state_hash,
        state_version=state.state_version,
        chronology_hash=chronology.chronology_hash,
        chronology_version=chronology.chronology_version,
    )
    state_facts = {fact.field_name: fact for fact in state.facts}
    chronology_facts = {fact.field_name: fact for fact in chronology.facts}
    payload: dict[str, Any] = {
        "identity": {
            "snapshot_id": snapshot.snapshot_id,
            "state_id": state.state_id,
            "chronology_id": chronology.chronology_id,
            "snapshot_input_hash": snapshot.input_hash,
            "state_hash": state.state_hash,
            "chronology_hash": chronology.chronology_hash,
            "context_input_hash": context_hash,
            "chain": snapshot.chain,
            "token": snapshot.token,
            "pool": snapshot.pool,
        },
        "timing": {
            "snapshot_captured_at": snapshot.captured_at.isoformat(),
            "state_ready_at": state.ready_at.isoformat(),
            "chronology_ready_at": chronology.ready_at.isoformat(),
            "context_ready_at": ready_at.isoformat(),
        },
        "coverage": {
            "state_status": state.status.value,
            "chronology_status": chronology.coverage_status.value,
            "history_truncated": chronology.history_truncated,
            "creation_reached": chronology.reached_creation,
            "transaction_unavailable_count": chronology.transaction_unavailable_count,
            "decode_failure_count": chronology.decode_failure_count,
        },
    }
    for section, names in STATE_SECTIONS.items():
        payload[section] = _selected_facts(state_facts, names)
    for section, names in CHRONOLOGY_SECTIONS.items():
        payload[section] = _selected_facts(chronology_facts, names)
    all_facts = list(state_facts.values()) + list(chronology_facts.values())
    payload["missing_fields"] = sorted(
        fact.field_name for fact in all_facts if fact.status.value != "observed"
    )
    return SemanticContext(
        snapshot_id=snapshot.snapshot_id,
        state_id=state.state_id,
        chronology_id=chronology.chronology_id,
        snapshot_input_hash=snapshot.input_hash,
        state_hash=state.state_hash,
        chronology_hash=chronology.chronology_hash,
        context_input_hash=context_hash,
        snapshot_captured_at=snapshot.captured_at,
        state_ready_at=state.ready_at,
        chronology_ready_at=chronology.ready_at,
        context_ready_at=ready_at,
        context_version=CONTEXT_VERSION,
        payload_json=canonical_json(payload),
        created_at=created_at,
    )


def _selected_facts(
    facts: dict[str, MemecoinStateFact], names: set[str]
) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "value": _json_value(facts[name].value),
            "status": facts[name].status.value,
            "source": facts[name].source,
        }
        for name in sorted(names & facts.keys())
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value
