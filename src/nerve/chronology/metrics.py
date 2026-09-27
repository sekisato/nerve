from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..memestate.models import MemecoinStateFact
from .models import ChronologyEvent, ChronologyEventType, CreatorLaunchEvidence, FundingEdge

SOURCE = "nerve:chronology-v1"


def activity_facts(
    events: list[ChronologyEvent],
    *,
    fetched_at: datetime,
    complete_since_creation: bool,
    incomplete_reason: str,
) -> list[MemecoinStateFact]:
    buys = [event for event in events if event.event_type is ChronologyEventType.BUY and event.user]
    sells = [event for event in events if event.event_type is ChronologyEventType.SELL and event.user]
    facts = [
        observed("observed_buy_event_count", len(buys), fetched_at),
        observed("observed_sell_event_count", len(sells), fetched_at),
        observed("observed_unique_buyers_count", len({event.user for event in buys}), fetched_at),
        observed("observed_unique_sellers_count", len({event.user for event in sells}), fetched_at),
    ]
    if complete_since_creation:
        facts.append(observed("unique_buyers_since_creation", len({event.user for event in buys}), fetched_at))
    else:
        facts.append(unavailable("unique_buyers_since_creation", incomplete_reason, fetched_at))
    return facts


def early_cohort(
    events: list[ChronologyEvent], target_n: int = 10
) -> tuple[list[str], int | None, dict[str, datetime | None]]:
    first: dict[str, tuple[int, datetime | None]] = {}
    for event in sorted(events, key=lambda item: (item.slot, item.instruction_path, item.signature)):
        if event.event_type is not ChronologyEventType.BUY or not event.user:
            continue
        first.setdefault(event.user, (event.slot, event.block_time))
    ordered = sorted(first.items(), key=lambda item: (item[1][0], item[0]))
    if not ordered:
        return [], None, {}
    cutoff_index = min(max(target_n, 1), len(ordered)) - 1
    cutoff_slot = ordered[cutoff_index][1][0]
    cohort = [wallet for wallet, (slot, _time) in ordered if slot <= cutoff_slot]
    times = {wallet: first[wallet][1] for wallet in cohort}
    return cohort, cutoff_slot, times


def chronology_metric_facts(
    events: list[ChronologyEvent], *, fetched_at: datetime, early_target_n: int = 10
) -> list[MemecoinStateFact]:
    buys = [event for event in events if event.event_type is ChronologyEventType.BUY and event.user]
    by_slot: dict[int, set[str]] = defaultdict(set)
    event_counts: Counter[int] = Counter()
    for event in buys:
        by_slot[event.slot].add(str(event.user))
        event_counts[event.slot] += 1
    multi_slots = [slot for slot, wallets in by_slot.items() if len(wallets) > 1]
    multi_events = sum(event_counts[slot] for slot in multi_slots)
    cohort, cutoff_slot, first_times = early_cohort(events, early_target_n)
    facts = [
        observed("multi_buyer_slot_count", len(multi_slots), fetched_at),
        observed("max_distinct_buyers_same_slot", max((len(value) for value in by_slot.values()), default=0), fetched_at),
        observed("buy_events_in_multi_buyer_slots", multi_events, fetched_at),
        observed(
            "multi_buyer_slot_event_share_pct",
            Decimal(multi_events) * Decimal(100) / Decimal(len(buys)) if buys else Decimal(0),
            fetched_at,
            unit="percent",
        ),
        observed("early_cohort_target_n", early_target_n, fetched_at),
        observed("early_cohort_actual_n", len(cohort), fetched_at),
    ]
    if cutoff_slot is None:
        facts.append(unavailable("early_cohort_cutoff_slot", "no observed buy event", fetched_at))
    else:
        facts.append(observed("early_cohort_cutoff_slot", cutoff_slot, fetched_at))
    if cohort:
        facts.extend(
            [
                observed("early_cohort_wallets", ",".join(cohort), fetched_at),
                observed("early_cohort_first_buy_times", _times_text(first_times), fetched_at),
            ]
        )
    else:
        facts.extend(
            unavailable(name, "no observed buy event", fetched_at)
            for name in ("early_cohort_wallets", "early_cohort_first_buy_times")
        )
    return facts


def window_facts(
    events: list[ChronologyEvent], *, creation_time: datetime | None, fetched_at: datetime
) -> list[MemecoinStateFact]:
    names = []
    for seconds in (60, 300):
        prefix = f"first_{seconds}s"
        names.extend(
            [
                f"{prefix}_buy_event_count",
                f"{prefix}_distinct_buyer_count",
                f"{prefix}_sell_event_count",
                f"{prefix}_distinct_seller_count",
                f"{prefix}_multi_buyer_slot_count",
            ]
        )
    if creation_time is None or any(event.block_time is None for event in events):
        return [unavailable(name, "creation or event block time coverage is incomplete", fetched_at) for name in names]
    facts: list[MemecoinStateFact] = []
    for seconds in (60, 300):
        included = [
            event
            for event in events
            if event.block_time is not None
            and 0 <= (event.block_time - creation_time).total_seconds() <= seconds
        ]
        buys = [event for event in included if event.event_type is ChronologyEventType.BUY and event.user]
        sells = [event for event in included if event.event_type is ChronologyEventType.SELL and event.user]
        slots: dict[int, set[str]] = defaultdict(set)
        for event in buys:
            slots[event.slot].add(str(event.user))
        prefix = f"first_{seconds}s"
        facts.extend(
            [
                observed(f"{prefix}_buy_event_count", len(buys), fetched_at),
                observed(f"{prefix}_distinct_buyer_count", len({e.user for e in buys}), fetched_at),
                observed(f"{prefix}_sell_event_count", len(sells), fetched_at),
                observed(f"{prefix}_distinct_seller_count", len({e.user for e in sells}), fetched_at),
                observed(f"{prefix}_multi_buyer_slot_count", sum(len(v) > 1 for v in slots.values()), fetched_at),
            ]
        )
    return facts


def funding_facts(
    edges: list[FundingEdge], *, probed_wallets: int, fetched_at: datetime
) -> list[MemecoinStateFact]:
    sources = Counter(edge.source_wallet for edge in edges)
    return [
        observed("funding_probe_wallet_count", probed_wallets, fetched_at),
        observed("funding_edge_found_count", len(edges), fetched_at),
        observed(
            "funding_coverage_pct",
            Decimal(len(edges)) * Decimal(100) / Decimal(probed_wallets) if probed_wallets else Decimal(0),
            fetched_at,
            unit="percent",
        ),
        observed("shared_funder_group_count", sum(count > 1 for count in sources.values()), fetched_at),
        observed("max_early_buyers_same_funder", max(sources.values(), default=0), fetched_at),
        observed("creation_user_funded_early_buyer_count", sum(edge.relation_to_creation_user is True for edge in edges), fetched_at),
        observed("creation_creator_funded_early_buyer_count", sum(edge.relation_to_creation_creator is True for edge in edges), fetched_at),
    ]


def creator_history_facts(
    launches: list[CreatorLaunchEvidence],
    *,
    signature_count: int,
    truncated: bool,
    fetched_at: datetime,
) -> list[MemecoinStateFact]:
    unique = {item.mint for item in launches}
    prior = {item.mint for item in launches if not item.is_current_mint}
    return [
        observed("creator_history_signature_count", signature_count, fetched_at),
        observed("creator_observed_launch_count", len(unique), fetched_at),
        observed("creator_observed_prior_launch_count", len(prior), fetched_at),
        observed("creator_history_truncated", truncated, fetched_at),
    ]


def observed(name: str, value: Any, fetched_at: datetime, unit: str | None = None) -> MemecoinStateFact:
    return MemecoinStateFact.observed(name, value, source=SOURCE, fetched_at=fetched_at, unit=unit)


def unavailable(name: str, reason: str, fetched_at: datetime | None = None) -> MemecoinStateFact:
    return MemecoinStateFact.unavailable(name, reason, source=SOURCE, fetched_at=fetched_at)


def _times_text(values: dict[str, datetime | None]) -> str:
    return ",".join(
        f"{wallet}={(time.isoformat() if time else 'Unavailable')}" for wallet, time in values.items()
    )
