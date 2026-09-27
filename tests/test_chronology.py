from __future__ import annotations

import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nerve.chronology.collector import MemeChronologyCollector
from nerve.chronology.metrics import (
    activity_facts,
    chronology_metric_facts,
    creator_history_facts,
    early_cohort,
    funding_facts,
)
from nerve.chronology.models import (
    ChronologyCoverage,
    ChronologyEvent,
    ChronologyEventType,
    CreatorLaunchEvidence,
    DecodeStatus,
    FundingEdge,
    MemeChronologyObservation,
    experiment_context_input_hash,
    experiment_context_ready_at,
)
from nerve.chronology.pump_decode import decode_transaction
from nerve.chronology.solana_history import nearest_prior_funding_edge
from nerve.lab.models import ObservationSnapshot
from nerve.memestate.models import FactStatus
from nerve.memestate.pump import PUMP_PROGRAM_ID, b58encode
from nerve.models import ChainName, Impulse
from nerve.observatory.read_store import ObservatoryReadStore
from nerve.store import NerveStore

NOW = datetime(2026, 9, 27, tzinfo=UTC)
MINT = b58encode(bytes(range(1, 33)))
CREATOR = b58encode(bytes(range(33, 65)))
USER = b58encode(bytes(range(65, 97)))
BUY = bytes([102, 6, 61, 18, 1, 218, 235, 234])
CREATE = bytes([24, 30, 200, 40, 5, 28, 7, 119])


def snapshot() -> ObservationSnapshot:
    impulse = Impulse(id="chronology-identity", chain=ChainName.SOLANA, token=MINT, pool="pool", created_at=NOW)
    return ObservationSnapshot.capture(impulse, captured_at=NOW)


def instruction(discriminator: bytes, accounts: list[str], suffix: bytes = b"") -> dict[str, object]:
    return {"programId": PUMP_PROGRAM_ID, "accounts": accounts, "data": b58encode(discriminator + suffix)}


def transaction(*, outer: list[dict[str, object]], inner: list[dict[str, object]] | None = None, slot: int = 10, seconds: int = 0) -> dict[str, object]:
    return {
        "slot": slot,
        "blockTime": int((NOW + timedelta(seconds=seconds)).timestamp()),
        "transaction": {"message": {"accountKeys": [], "instructions": outer}},
        "meta": {"innerInstructions": [{"index": 0, "instructions": inner or []}]},
    }


def buy_event(user: str, slot: int, seconds: int = 0) -> ChronologyEvent:
    return ChronologyEvent(
        signature=f"sig-{user}-{slot}-{seconds}", slot=slot,
        block_time=NOW + timedelta(seconds=seconds), instruction_path="outer:0",
        program_id=PUMP_PROGRAM_ID, venue="pump_curve", event_type=ChronologyEventType.BUY,
        user=user, mint=MINT, instruction_discriminator=BUY.hex(),
    )


def observation(events: list[ChronologyEvent], *, seconds: int = 1) -> MemeChronologyObservation:
    return MemeChronologyObservation.create(
        snapshot_id=snapshot().snapshot_id, chronology_version="v1", started_at=NOW,
        ready_at=NOW + timedelta(seconds=seconds), source_cutoff_at=NOW,
        coverage_status=ChronologyCoverage.PARTIAL, history_truncated=True,
        reached_creation=False, signature_count=len(events), transaction_fetch_count=len(events),
        transaction_unavailable_count=0, unsupported_version_count=0, decode_failure_count=0,
        oldest_slot=min((item.slot for item in events), default=None),
        newest_slot=max((item.slot for item in events), default=None),
        oldest_block_time=None, newest_block_time=None, sources=["fixture"], events=events,
        facts=[], funding_edges=[], creator_launches=[],
    )


def test_outer_and_inner_pump_decode_preserve_unique_instruction_paths() -> None:
    accounts = ["a", "b", MINT, "d", "e", "f", USER]
    ix = instruction(BUY, accounts, struct.pack("<QQ", 5, 7))
    decoded = decode_transaction("sig", transaction(outer=[ix], inner=[ix]), target_mint=MINT)
    assert [item.instruction_path for item in decoded] == ["outer:0", "outer:0/inner:0"]
    assert all(item.user == USER and item.amount_token_raw == 5 for item in decoded)


def test_creation_user_and_creator_are_distinct() -> None:
    strings = b"".join(struct.pack("<I", len(value)) + value for value in (b"name", b"SYM", b"uri"))
    accounts = [MINT, "a", "b", "c", "d", "e", "f", USER]
    [event] = decode_transaction("create", transaction(outer=[instruction(CREATE, accounts, strings + bytes(range(33, 65)))]), target_mint=MINT)
    assert event.creation_user == USER
    assert event.creator == CREATOR
    assert event.creation_user != event.creator


def test_unknown_target_mint_instruction_remains_explicitly_unsupported() -> None:
    [event] = decode_transaction(
        "unknown",
        transaction(outer=[instruction(b"12345678", [MINT])]),
        target_mint=MINT,
    )
    assert event.event_type is ChronologyEventType.UNKNOWN
    assert event.decode_status is DecodeStatus.UNSUPPORTED


def test_activity_counts_events_unique_wallets_and_sellers_separately() -> None:
    events = [buy_event("a", 1), buy_event("a", 2), buy_event("b", 3)]
    events.append(events[0].model_copy(update={"event_id": "sell", "event_type": ChronologyEventType.SELL, "user": "seller"}))
    facts = {fact.field_name: fact for fact in activity_facts(events, fetched_at=NOW, complete_since_creation=True, incomplete_reason="")}
    assert facts["observed_buy_event_count"].value == 3
    assert facts["observed_unique_buyers_count"].value == 2
    assert facts["observed_unique_sellers_count"].value == 1


def test_same_slot_and_tied_cutoff_expand_deterministically() -> None:
    events = [buy_event(f"w{i}", i if i < 10 else 9) for i in range(12)]
    cohort, cutoff, _times = early_cohort(events)
    facts = {fact.field_name: fact for fact in chronology_metric_facts(events, fetched_at=NOW)}
    assert cutoff == 9 and len(cohort) == 12
    assert facts["multi_buyer_slot_count"].value == 1
    assert facts["max_distinct_buyers_same_slot"].value == 3
    assert facts["early_cohort_actual_n"].value == 12


def test_partial_history_blocks_complete_claim_and_empty_is_unavailable() -> None:
    facts = {fact.field_name: fact for fact in activity_facts([], fetched_at=NOW, complete_since_creation=False, incomplete_reason="creation not reached")}
    cohort_facts = {fact.field_name: fact for fact in chronology_metric_facts([], fetched_at=NOW)}
    assert facts["observed_buy_event_count"].status is FactStatus.OBSERVED
    assert facts["observed_buy_event_count"].value == 0
    assert facts["unique_buyers_since_creation"].status is FactStatus.UNAVAILABLE
    assert cohort_facts["early_cohort_wallets"].status is FactStatus.UNAVAILABLE


def test_hash_is_deterministic_event_sensitive_and_context_is_availability_bounded() -> None:
    first = observation([buy_event("a", 1)])
    same = observation([buy_event("a", 1)])
    changed = observation([buy_event("b", 1)])
    assert first.chronology_hash == same.chronology_hash
    assert first.chronology_hash != changed.chronology_hash
    ready = experiment_context_ready_at(NOW, NOW + timedelta(seconds=2), NOW + timedelta(seconds=3))
    assert ready == NOW + timedelta(seconds=3)
    left = experiment_context_input_hash(snapshot_input_hash="s", snapshot_version="1", state_hash="a", state_version="1", chronology_hash="c", chronology_version="1")
    right = experiment_context_input_hash(snapshot_input_hash="s", snapshot_version="1", state_hash="a", state_version="1", chronology_hash="d", chronology_version="1")
    assert left != right


def system_transfer(source: str, destination: str, lamports: int, seconds: int, signature: str) -> tuple[str, dict[str, object]]:
    tx = transaction(outer=[], slot=100 + seconds, seconds=seconds)
    tx["transaction"] = {"message": {"accountKeys": [], "instructions": [{"program": "system", "parsed": {"type": "transfer", "info": {"source": source, "destination": destination, "lamports": lamports}}}]}}
    return signature, tx


def test_funding_uses_nearest_prior_transfer_and_never_infers_self_funded() -> None:
    rows = [system_transfer(CREATOR, USER, 10, -20, "old"), system_transfer("near", USER, 20, -5, "near"), system_transfer("after", USER, 30, 2, "after")]
    edge = nearest_prior_funding_edge(target_wallet=USER, first_buy_time=NOW, transactions=rows, creation_user="other", creation_creator=CREATOR)
    assert edge is not None and edge.funding_signature == "near"
    assert nearest_prior_funding_edge(target_wallet="missing", first_buy_time=NOW, transactions=rows, creation_user=None, creation_creator=None) is None
    creator_edge = FundingEdge(target_wallet=USER, source_wallet=CREATOR, funding_signature="f", slot=1, block_time=NOW, lamports=1, seconds_before_first_buy=1, relation_to_creation_creator=True)
    facts = {fact.field_name: fact for fact in funding_facts([creator_edge], probed_wallets=2, fetched_at=NOW)}
    assert facts["creation_creator_funded_early_buyer_count"].value == 1
    assert all("bundler" not in fact.field_name for fact in facts.values())


def test_creator_launch_summary_deduplicates_mints_and_marks_bounded_history() -> None:
    launches = [CreatorLaunchEvidence(creator=CREATOR, mint="m1", creation_signature="1", slot=1, is_current_mint=False, source="fixture"), CreatorLaunchEvidence(creator=CREATOR, mint="m1", creation_signature="2", slot=2, is_current_mint=False, source="fixture"), CreatorLaunchEvidence(creator=CREATOR, mint=MINT, creation_signature="3", slot=3, is_current_mint=True, source="fixture")]
    facts = {fact.field_name: fact for fact in creator_history_facts(launches, signature_count=100, truncated=True, fetched_at=NOW)}
    assert facts["creator_observed_launch_count"].value == 2
    assert facts["creator_observed_prior_launch_count"].value == 1
    assert facts["creator_history_truncated"].value is True


def test_chronology_storage_is_append_only_and_observatory_read_only(tmp_path: Path) -> None:
    store = NerveStore(tmp_path / "truth.db")
    snap = snapshot()
    store.record_observation_snapshot(snap)
    first = observation([buy_event("a", 1)], seconds=1)
    second = observation([buy_event("b", 2)], seconds=2)
    store.record_meme_chronology(first)
    store.record_meme_chronology(second)
    assert len(store.list_meme_chronologies(snap.snapshot_id)) == 2
    assert store.latest_meme_chronology(snap.snapshot_id).chronology_id == second.chronology_id  # type: ignore[union-attr]
    store.close()
    with ObservatoryReadStore(tmp_path / "truth.db") as reader:
        assert reader.snapshot_detail(snap.snapshot_id)["chronology"]["latest"]["chronology_id"] == second.chronology_id  # type: ignore[index]
        try:
            reader.conn.execute("DELETE FROM meme_chronology_observations")
        except Exception:
            pass
        else:
            raise AssertionError("Observatory connection must reject writes")


class FakeRpc:
    def __init__(self) -> None:
        self._request_id = 0

    def call(self, method: str, params: list[object]) -> object:
        self._request_id += 1
        if method == "getSignaturesForAddress":
            return [{"signature": "missing", "slot": 2, "blockTime": int(NOW.timestamp())}, {"signature": "buy", "slot": 1, "blockTime": int(NOW.timestamp())}]
        if method == "getTransaction" and params[0] == "missing":
            return None
        if method == "getTransaction":
            return transaction(outer=[instruction(BUY, ["a", "b", MINT, "d", "e", "f", USER], struct.pack("<QQ", 1, 2))])
        if method == "getTokenAccountsByOwner":
            return {"value": []}
        if method == "getTokenSupply":
            return {"value": {"amount": "1"}}
        raise AssertionError(method)


def test_failed_transaction_lowers_coverage_and_semantic_percentages_stay_unavailable(tmp_path: Path) -> None:
    store = NerveStore(tmp_path / "truth.db")
    snap = snapshot()
    store.record_observation_snapshot(snap)
    item = MemeChronologyCollector(store, solana_rpc_url="", rpc_client=FakeRpc(), clock=lambda: NOW).collect(snap, max_signatures=2)
    by_name = {fact.field_name: fact for fact in item.facts}
    assert item.coverage_status is ChronologyCoverage.PARTIAL
    assert item.transaction_unavailable_count == 1
    assert by_name["unique_buyers_since_creation"].status is FactStatus.UNAVAILABLE
    assert by_name["sniper_supply_pct"].status is FactStatus.UNAVAILABLE
    assert by_name["bundler_supply_pct"].status is FactStatus.UNAVAILABLE
    store.close()
