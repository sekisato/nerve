from __future__ import annotations

import sqlite3
import struct
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from nerve.lab.arms import ControlArm
from nerve.lab.models import ObservationSnapshot
from nerve.memestate.collector import MemecoinStateCollector
from nerve.memestate.dexscreener import pair_facts, select_pair
from nerve.memestate.models import (
    FactStatus,
    LifecycleState,
    MemecoinStateFact,
    MemecoinStateObservation,
    StateStatus,
    experiment_state_input_hash,
)
from nerve.memestate.pump import (
    BONDING_CURVE_DISCRIMINATOR,
    POOL_DISCRIMINATOR,
    PUMP_PROGRAM_ID,
    PUMPSWAP_PROGRAM_ID,
    WSOL_MINT,
    b58decode,
    b58encode,
    canonical_pumpswap_verified,
    decode_bonding_curve,
    decode_pumpswap_pool,
    lifecycle_state,
    pool_authority_pda,
)
from nerve.memestate.solana_rpc import token_distribution_facts
from nerve.models import ChainName, Impulse
from nerve.observatory.read_store import ObservatoryReadStore
from nerve.store import NerveStore

NOW = datetime(2026, 9, 27, tzinfo=UTC)
MINT = b58encode(bytes(range(1, 33)))
PAIR = b58encode(bytes(range(33, 65)))


def snapshot() -> ObservationSnapshot:
    impulse = Impulse(
        id="solana-test-identity",
        chain=ChainName.SOLANA,
        token=MINT,
        pool=PAIR,
        created_at=NOW,
    )
    return ObservationSnapshot.capture(impulse, captured_at=NOW + timedelta(seconds=1))


def facts(value: int = 0) -> list[MemecoinStateFact]:
    return [
        MemecoinStateFact.observed(
            "observed_zero", value, source="fixture", fetched_at=NOW, unit="atomic"
        ),
        MemecoinStateFact.unavailable("missing_value", "not returned", source="fixture"),
    ]


def state(*, value: int = 0, seconds: int = 1) -> MemecoinStateObservation:
    snap = snapshot()
    return MemecoinStateObservation.create(
        snapshot_id=snap.snapshot_id,
        state_version="memestate-v1",
        started_at=NOW,
        ready_at=NOW + timedelta(seconds=seconds),
        status=StateStatus.PARTIAL,
        sources=["fixture"],
        facts=facts(value),
    )


def test_fact_contract_distinguishes_observed_zero_from_unavailable() -> None:
    observed, unavailable = facts()
    assert observed.status is FactStatus.OBSERVED
    assert observed.value_int == 0
    assert observed.value == 0
    assert unavailable.status is FactStatus.UNAVAILABLE
    assert unavailable.value is None
    with pytest.raises(ValueError, match="keep their value null"):
        MemecoinStateFact(field_name="missing", status=FactStatus.UNAVAILABLE, value_int=0)


@pytest.mark.parametrize("status", list(FactStatus))
def test_fact_status_round_trips_through_sqlite(tmp_path: Path, status: FactStatus) -> None:
    store = NerveStore(tmp_path / "truth.db")
    snap = snapshot()
    store.record_observation_snapshot(snap)
    fact = (
        MemecoinStateFact.observed("field", 1, source="fixture", fetched_at=NOW)
        if status is FactStatus.OBSERVED
        else MemecoinStateFact.unavailable("field", "fixture", status=status)
    )
    item = MemecoinStateObservation.create(
        snapshot_id=snap.snapshot_id,
        state_version="v1",
        started_at=NOW,
        ready_at=NOW,
        status=StateStatus.PARTIAL,
        sources=["fixture"],
        facts=[fact],
    )
    store.record_memecoin_state(item)
    assert store.list_memecoin_states()[0].facts[0].status is status
    store.close()


def test_state_is_append_only_latest_keeps_history_and_payload_is_immutable(tmp_path: Path) -> None:
    store = NerveStore(tmp_path / "truth.db")
    snap = snapshot()
    store.record_observation_snapshot(snap)
    first = state(value=0, seconds=1)
    second = state(value=1, seconds=2)
    store.record_memecoin_state(first)
    store.record_memecoin_state(second)
    assert len(store.list_memecoin_states(snap.snapshot_id)) == 2
    assert store.latest_memecoin_state(snap.snapshot_id).state_id == second.state_id  # type: ignore[union-attr]
    detached = first.facts[0].details
    detached["mutated"] = True
    assert "mutated" not in store.list_memecoin_states()[0].facts[0].details
    persisted = store.list_memecoin_states()[0].facts[0]
    assert persisted.source == "fixture"
    assert persisted.fetched_at == NOW
    store.close()


def test_state_hash_is_deterministic_and_fact_sensitive() -> None:
    first = state(value=0)
    same = state(value=0)
    changed = state(value=1)
    assert first.state_hash == same.state_hash
    assert first.state_hash != changed.state_hash
    assert experiment_state_input_hash("snap", first.state_hash, "v1") == experiment_state_input_hash(
        "snap", first.state_hash, "v1"
    )


def dex_row(pair: str, liquidity: int) -> dict[str, Any]:
    return {
        "chainId": "solana",
        "dexId": "pumpswap",
        "pairAddress": pair,
        "baseToken": {"address": MINT},
        "priceNative": "0",
        "priceUsd": "0.0001",
        "pairCreatedAt": 1_700_000_000_000,
        "liquidity": {"usd": liquidity, "base": 10, "quote": 20},
        "txns": {"m5": {"buys": 0, "sells": 2}, "h1": {"buys": 4, "sells": 5}},
        "volume": {"m5": 0, "h1": 7},
        "priceChange": {"m5": 0, "h1": 2},
    }


def test_dex_pair_selection_prefers_exact_then_deterministic_liquidity() -> None:
    high = dex_row("high", 100)
    exact = dex_row(PAIR, 1)
    selected, reason, candidates = select_pair([high, exact], token=MINT, snapshot_pool=PAIR)
    assert selected is exact
    assert reason == "exact_snapshot_pool"
    assert len(candidates) == 2
    mismatched = dex_row(PAIR, 0)
    mismatched["baseToken"] = {"address": "another-token"}
    selected, reason, _ = select_pair([high, mismatched], token=MINT, snapshot_pool=PAIR)
    assert selected is mismatched
    assert reason == "exact_snapshot_pool"
    selected, reason, _ = select_pair(
        [dex_row("z", 100), dex_row("a", 100)], token=MINT, snapshot_pool="missing"
    )
    assert selected is not None and selected["pairAddress"] == "a"
    assert reason == "highest_liquidity_fallback"


def test_dex_provenance_and_provider_timestamp_are_preserved_without_unique_buyers() -> None:
    result = pair_facts([dex_row(PAIR, 100)], token=MINT, snapshot_pool=PAIR, fetched_at=NOW)
    by_name = {fact.field_name: fact for fact in result}
    assert by_name["m5_buys"].value_int == 0
    assert by_name["m5_buys"].source == "dexscreener:token-pairs-v1"
    assert by_name["pair_created_at"].source_observed_at is not None
    assert by_name["pair_age_ms"].status is FactStatus.OBSERVED
    assert by_name["pair_age_ms"].value_int is not None
    assert "unique_buyers" not in by_name


def test_token_account_concentration_uses_supply_and_owner_metrics_are_partial() -> None:
    supply = {"value": {"amount": "1000", "decimals": 2}}
    largest = {"value": [{"address": f"a{i}", "amount": str(value)} for i, value in enumerate((400, 300, 200, 100))]}
    owners = [
        {"value": {"data": {"parsed": {"info": {"owner": "same" if i < 2 else f"owner{i}"}}}}}
        for i in range(4)
    ]
    result = token_distribution_facts(supply, largest, fetched_at=NOW, owner_rows=owners)
    by_name = {fact.field_name: fact for fact in result}
    assert by_name["top1_token_accounts_pct"].value_num == Decimal("40")
    assert by_name["top5_token_accounts_pct"].value_num == Decimal("100")
    assert by_name["largest_owner_within_top20_pct"].value_num == Decimal("70")
    assert by_name["largest_owner_within_top20_pct"].details["scope"].startswith("resolved owners")

    unresolved = token_distribution_facts(
        supply, largest, fetched_at=NOW, owner_rows=[None, None, None, None]
    )
    unresolved_by_name = {fact.field_name: fact for fact in unresolved}
    assert unresolved_by_name["top20_accounts_owner_coverage_pct"].value_num == 0
    assert unresolved_by_name["largest_owner_within_top20_pct"].status is FactStatus.UNAVAILABLE
    assert unresolved_by_name["largest_owner_within_top20_pct"].value is None


def bonding_curve_bytes(*, complete: bool, include_appended: bool = True) -> bytes:
    raw = BONDING_CURVE_DISCRIMINATOR + struct.pack("<QQQQQ?", 11, 12, 13, 14, 15, complete)
    if include_appended:
        raw += bytes(range(1, 33))
        raw += struct.pack("<??", True, False)
        raw += b"\0" * 32
        raw += struct.pack("<Q??", 25, False, True)
    return raw


def test_pump_decoder_validates_owner_discriminator_and_short_accounts() -> None:
    decoded = decode_bonding_curve(bonding_curve_bytes(complete=False), PUMP_PROGRAM_ID)
    assert decoded["curve_complete"] is False
    assert decoded["coin_creator"] == MINT
    assert "deployer" not in decoded
    short = decode_bonding_curve(
        bonding_curve_bytes(complete=True, include_appended=False), PUMP_PROGRAM_ID
    )
    assert short["curve_complete"] is True
    assert short["coin_creator"] is None
    assert short["is_holder_reward"] is None
    with pytest.raises(ValueError, match="not owned"):
        decode_bonding_curve(bonding_curve_bytes(complete=False), PUMPSWAP_PROGRAM_ID)
    with pytest.raises(ValueError, match="discriminator"):
        decode_bonding_curve(b"x" * 80, PUMP_PROGRAM_ID)


def pumpswap_pool_bytes(mint: str) -> bytes:
    creator = b58decode(pool_authority_pda(mint))
    pubkeys = [creator, b58decode(mint), b58decode(WSOL_MINT), bytes(32), bytes(32), bytes(32)]
    return POOL_DISCRIMINATOR + struct.pack("<BH", 1, 0) + b"".join(pubkeys) + struct.pack("<Q", 1)


def test_lifecycle_never_equates_curve_complete_with_verified_migration() -> None:
    assert lifecycle_state(pump_account_valid=True, curve_complete=False, canonical_pool_verified=False) is LifecycleState.PUMP_CURVE_ACTIVE
    assert lifecycle_state(pump_account_valid=True, curve_complete=True, canonical_pool_verified=False) is LifecycleState.PUMP_CURVE_COMPLETE_MIGRATION_UNKNOWN
    assert lifecycle_state(pump_account_valid=True, curve_complete=True, canonical_pool_verified=True) is LifecycleState.PUMP_CANONICAL_PUMPSWAP_VERIFIED
    pool = decode_pumpswap_pool(pumpswap_pool_bytes(MINT), PUMPSWAP_PROGRAM_ID)
    assert canonical_pumpswap_verified(pool, mint=MINT)
    pool["index"] = 1
    assert not canonical_pumpswap_verified(pool, mint=MINT)


class FakeDex:
    def token_pairs(self, _token: str) -> list[dict[str, Any]]:
        return [dex_row(PAIR, 100)]


class FakeRpc:
    def call(self, method: str, params: list[Any]) -> Any:
        if method == "getTokenSupply":
            return {"value": {"amount": "1000", "decimals": 6}}
        if method == "getTokenLargestAccounts":
            return {"value": [{"address": "account", "amount": "500"}]}
        if method == "getMultipleAccounts":
            return {"value": [None]}
        if method == "getAccountInfo" and params[0] == PAIR:
            import base64

            return {"value": {"owner": PUMPSWAP_PROGRAM_ID, "data": [base64.b64encode(pumpswap_pool_bytes(MINT)).decode(), "base64"]}}
        if method == "getAccountInfo":
            import base64

            return {"value": {"owner": PUMP_PROGRAM_ID, "data": [base64.b64encode(bonding_curve_bytes(complete=True)).decode(), "base64"]}}
        raise AssertionError(method)


def test_collector_keeps_semantic_fields_unavailable_and_control_unchanged(tmp_path: Path) -> None:
    store = NerveStore(tmp_path / "truth.db")
    snap = snapshot()
    store.record_observation_snapshot(snap)
    collector = MemecoinStateCollector(
        store, dex_client=FakeDex(), rpc_client=FakeRpc(), clock=lambda: NOW
    )
    result = collector.collect(snap)
    by_name = {fact.field_name: fact for fact in result.facts}
    for name in ("unique_buyers", "sniper_supply_pct", "bundler_supply_pct"):
        assert by_name[name].status is FactStatus.UNAVAILABLE
        assert by_name[name].value is None
    decision = ControlArm().evaluate(snap, NOW + timedelta(seconds=30))
    assert decision.strategy_id == "control-abstain-v1"
    assert decision.input_hash == snap.input_hash
    store.close()


def test_collector_failure_is_audited_and_exits_cleanly(tmp_path: Path) -> None:
    store = NerveStore(tmp_path / "truth.db")
    result = MemecoinStateCollector(
        store, dex_client=FakeDex(), rpc_client=FakeRpc(), clock=lambda: NOW
    ).collect_safely(snapshot())
    assert result is None
    events = store.list_measurement_events()
    assert events[-1].kind.value == "memestate_failed"
    store.close()


def test_core_impulse_and_snapshot_identity_are_unchanged() -> None:
    first = Impulse(id="fixed-identity", chain=ChainName.SOLANA, token=MINT, pool=PAIR, created_at=NOW)
    second = first.model_copy(deep=True)
    assert first.id == second.id == "fixed-identity"
    captured = NOW + timedelta(seconds=1)
    assert ObservationSnapshot.capture(first, captured_at=captured).snapshot_id == ObservationSnapshot.capture(second, captured_at=captured).snapshot_id


def test_observatory_memestate_is_read_only_and_exposes_coverage(tmp_path: Path) -> None:
    path = tmp_path / "truth.db"
    store = NerveStore(path)
    snap = snapshot()
    store.record_observation_snapshot(snap)
    store.record_memecoin_state(state())
    store.close()
    with ObservatoryReadStore(path) as reader:
        detail = reader.snapshot_detail(snap.snapshot_id)
        assert detail is not None
        meme = detail["memecoin_state"]
        assert meme["latest"]["state_version"] == "memestate-v1"
        observed = next(fact for fact in meme["facts"] if fact["field_name"] == "observed_zero")
        missing = next(fact for fact in meme["facts"] if fact["field_name"] == "missing_value")
        assert observed["value"] == 0
        assert missing["value"] is None
        summary = reader.memecoin_state_summary()
        assert summary["observation_count"] == 1
        assert summary["successful_count"] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.conn.execute("DELETE FROM memecoin_state_observations")
