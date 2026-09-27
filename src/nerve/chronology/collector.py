from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from ..lab.models import MeasurementEvent, MeasurementEventKind, ObservationSnapshot
from ..memestate.models import MemecoinStateFact, utc_now
from ..memestate.solana_rpc import SolanaRpcClient
from .metrics import (
    SOURCE,
    activity_facts,
    chronology_metric_facts,
    creator_history_facts,
    early_cohort,
    funding_facts,
    observed,
    unavailable,
    window_facts,
)
from .models import (
    ChronologyCoverage,
    ChronologyEvent,
    ChronologyEventType,
    CreatorLaunchEvidence,
    DecodeStatus,
    FundingEdge,
    MemeChronologyObservation,
)
from .pump_decode import decode_transaction
from .solana_history import nearest_prior_funding_edge, signature_page_params, transaction_params

CHRONOLOGY_VERSION = "meme-chronology-v1"
MAX_SIGNATURES_HARD = 1000
MAX_FUNDING_BUYERS_HARD = 20
MAX_CREATOR_HISTORY_SIGNATURES_HARD = 500
MAX_BALANCE_LOOKUPS = 20
logger = logging.getLogger(__name__)


class ChronologyStore(Protocol):
    def record_meme_chronology(self, observation: MemeChronologyObservation) -> None: ...

    def record_measurement_event(self, event: MeasurementEvent) -> None: ...


class MemeChronologyCollector:
    """Bounded, explicit lab process. It is not reachable from the live Spine."""

    def __init__(
        self,
        store: ChronologyStore,
        *,
        solana_rpc_url: str,
        rpc_client: SolanaRpcClient | Any | None = None,
        clock: Any = utc_now,
    ) -> None:
        self.store = store
        self.rpc = rpc_client or (SolanaRpcClient(solana_rpc_url) if solana_rpc_url else None)
        self.clock = clock

    def _rpc_call(self, method: str, params: list[Any]) -> Any:
        if self.rpc is None:
            raise RuntimeError("SOLANA_RPC_URL is not configured")
        return self.rpc.call(method, params)

    def collect(
        self,
        snapshot: ObservationSnapshot,
        *,
        max_signatures: int = 500,
        funding_buyers: int = 0,
        creator_history_signatures: int = 0,
    ) -> MemeChronologyObservation:
        started_at: datetime = self.clock()
        source_cutoff_at = started_at
        max_signatures = max(1, min(max_signatures, MAX_SIGNATURES_HARD))
        funding_buyers = max(0, min(funding_buyers, MAX_FUNDING_BUYERS_HARD))
        creator_history_signatures = max(
            0, min(creator_history_signatures, MAX_CREATOR_HISTORY_SIGNATURES_HARD)
        )
        if self.rpc is None:
            return self._unavailable(snapshot, started_at, source_cutoff_at, "SOLANA_RPC_URL is not configured")

        request_start = int(getattr(self.rpc, "_request_id", 0))
        signatures, pages_complete, pages_fetched = self._signatures(
            snapshot.token, max_signatures
        )
        events: list[ChronologyEvent] = []
        fetch_count = 0
        unavailable_count = 0
        unsupported_count = 0
        decode_failures = 0
        reached_creation = False
        included_rows: list[dict[str, Any]] = []
        for row in signatures:
            signature = str(row.get("signature") or "")
            if not signature:
                continue
            included_rows.append(row)
            try:
                transaction = self._rpc_call("getTransaction", transaction_params(signature))
            except Exception as exc:
                if "version" in str(exc).lower():
                    unsupported_count += 1
                else:
                    unavailable_count += 1
                continue
            if not isinstance(transaction, dict):
                unavailable_count += 1
                continue
            fetch_count += 1
            try:
                decoded = decode_transaction(signature, transaction, target_mint=snapshot.token)
                events.extend(decoded)
                decode_failures += sum(
                    event.decode_status is not DecodeStatus.DECODED for event in decoded
                )
                if any(event.event_type is ChronologyEventType.CREATE for event in decoded):
                    reached_creation = True
                    break
            except Exception:
                decode_failures += 1

        events.sort(key=lambda event: (event.slot, event.instruction_path, event.signature))
        history_truncated = not reached_creation and (
            len(signatures) >= max_signatures or not pages_complete
        )
        full = (
            reached_creation
            and not history_truncated
            and unavailable_count == 0
            and unsupported_count == 0
            and decode_failures == 0
        )
        coverage = (
            ChronologyCoverage.COMPLETE_SINCE_CREATION
            if full
            else ChronologyCoverage.PARTIAL
            if signatures
            else ChronologyCoverage.UNAVAILABLE
        )
        creation = next(
            (event for event in events if event.event_type is ChronologyEventType.CREATE), None
        )
        incomplete_reason = _coverage_reason(
            reached_creation, history_truncated, unavailable_count, unsupported_count, decode_failures
        )
        ready_for_metrics = self.clock()
        facts = _creation_facts(creation, ready_for_metrics)
        facts.extend(
            [
                observed("max_signatures_requested", max_signatures, ready_for_metrics),
                observed("signature_pages_fetched", pages_fetched, ready_for_metrics),
                (
                    observed(
                        "oldest_returned_signature",
                        str(signatures[-1].get("signature") or ""),
                        ready_for_metrics,
                    )
                    if signatures
                    else unavailable(
                        "oldest_returned_signature", "no signature returned", ready_for_metrics
                    )
                ),
            ]
        )
        facts.extend(
            activity_facts(
                events,
                fetched_at=ready_for_metrics,
                complete_since_creation=full,
                incomplete_reason=incomplete_reason,
            )
        )
        facts.extend(chronology_metric_facts(events, fetched_at=ready_for_metrics))
        facts.extend(
            window_facts(
                events,
                creation_time=creation.block_time if creation else None,
                fetched_at=ready_for_metrics,
            )
        )
        cohort, _cutoff, first_times = early_cohort(events)
        facts.extend(self._balance_facts(snapshot.token, cohort, ready_for_metrics))
        edges, funding_extra = self._funding_probe(
            cohort[:funding_buyers],
            first_times,
            creation,
            ready_for_metrics,
            enabled=funding_buyers > 0,
        )
        facts.extend(funding_extra)
        launches, creator_extra = self._creator_history(
            creation,
            snapshot.token,
            creator_history_signatures,
            ready_for_metrics,
        )
        facts.extend(creator_extra)
        facts.extend(
            [
                unavailable("sniper_supply_pct", "chronology evidence is not a sniper classification"),
                unavailable("bundler_supply_pct", "funding evidence is not a bundler classification"),
            ]
        )
        facts.append(
            observed(
                "chronology_rpc_call_count",
                int(getattr(self.rpc, "_request_id", request_start)) - request_start,
                ready_for_metrics,
            )
        )
        slots = [int(row["slot"]) for row in included_rows if row.get("slot") is not None]
        times = [
            datetime.fromtimestamp(int(row["blockTime"]), tz=UTC)
            for row in included_rows
            if row.get("blockTime") is not None
        ]
        ready_at: datetime = self.clock()
        observation = MemeChronologyObservation.create(
            snapshot_id=snapshot.snapshot_id,
            chronology_version=CHRONOLOGY_VERSION,
            started_at=started_at,
            ready_at=ready_at,
            source_cutoff_at=source_cutoff_at,
            coverage_status=coverage,
            history_truncated=history_truncated,
            reached_creation=reached_creation,
            signature_count=len(signatures),
            transaction_fetch_count=fetch_count,
            transaction_unavailable_count=unavailable_count,
            unsupported_version_count=unsupported_count,
            decode_failure_count=decode_failures,
            oldest_slot=min(slots) if slots else None,
            newest_slot=max(slots) if slots else None,
            oldest_block_time=min(times) if times else None,
            newest_block_time=max(times) if times else None,
            sources=["solana:json-rpc", SOURCE],
            events=events,
            facts=facts,
            funding_edges=edges,
            creator_launches=launches,
        )
        self.store.record_meme_chronology(observation)
        return observation

    def collect_safely(self, snapshot: ObservationSnapshot, **options: int) -> MemeChronologyObservation | None:
        try:
            return self.collect(snapshot, **options)
        except Exception as exc:
            try:
                self.store.record_measurement_event(
                    MeasurementEvent(
                        kind=MeasurementEventKind.CHRONOLOGY_FAILED,
                        stage="meme_chronology_collect",
                        impulse_id=snapshot.impulse_id,
                        snapshot_id=snapshot.snapshot_id,
                        error_type=type(exc).__name__,
                        message=str(exc)[:1000],
                        details={"chronology_version": CHRONOLOGY_VERSION},
                    )
                )
            except Exception:
                logger.exception("failed to persist chronology failure")
            return None

    def _signatures(
        self, address: str, maximum: int
    ) -> tuple[list[dict[str, Any]], bool, int]:
        rows: list[dict[str, Any]] = []
        before: str | None = None
        complete = True
        pages_fetched = 0
        while len(rows) < maximum:
            limit = min(100, maximum - len(rows))
            page = self._rpc_call(
                "getSignaturesForAddress",
                signature_page_params(address, limit=limit, before=before),
            )
            pages_fetched += 1
            if not isinstance(page, list):
                complete = False
                break
            valid = [row for row in page if isinstance(row, dict)]
            rows.extend(valid)
            if len(valid) < limit:
                break
            before = str(valid[-1].get("signature") or "")
            if not before:
                complete = False
                break
        return rows, complete, pages_fetched

    def _balance_facts(
        self, mint: str, cohort: list[str], fetched_at: datetime
    ) -> list[MemecoinStateFact]:
        if not cohort:
            return [
                unavailable(name, "no early-buyer cohort was observed", fetched_at)
                for name in (
                    "early_cohort_balance_coverage_pct",
                    "early_cohort_current_token_raw",
                    "early_cohort_current_supply_pct",
                )
            ]
        if len(cohort) > MAX_BALANCE_LOOKUPS:
            return [
                unavailable(name, "tied early cohort exceeds the bounded balance lookup cap", fetched_at)
                for name in (
                    "early_cohort_balance_coverage_pct",
                    "early_cohort_current_token_raw",
                    "early_cohort_current_supply_pct",
                )
            ]
        resolved = 0
        total = 0
        for wallet in cohort:
            try:
                result = self._rpc_call(
                    "getTokenAccountsByOwner",
                    [wallet, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
                )
                values = result.get("value", []) if isinstance(result, dict) else []
                for row in values:
                    amount = (((row.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info", {}).get("tokenAmount", {}).get("amount")
                    if amount is not None:
                        total += int(str(amount))
                resolved += 1
            except Exception:
                continue
        coverage = Decimal(resolved) * Decimal(100) / Decimal(len(cohort))
        facts = [
            observed("early_cohort_balance_coverage_pct", coverage, fetched_at, "percent")
        ]
        if resolved != len(cohort):
            facts.extend(
                [
                    unavailable("early_cohort_current_token_raw", "not every cohort wallet balance was resolved", fetched_at),
                    unavailable("early_cohort_current_supply_pct", "not every cohort wallet balance was resolved", fetched_at),
                ]
            )
            return facts
        facts.append(observed("early_cohort_current_token_raw", total, fetched_at, "atomic"))
        try:
            supply = self._rpc_call("getTokenSupply", [mint, {"commitment": "confirmed"}])
            raw_supply = int(str((supply.get("value") or {}).get("amount")))
            if raw_supply <= 0:
                raise ValueError("token supply is zero")
            facts.append(
                observed(
                    "early_cohort_current_supply_pct",
                    Decimal(total) * Decimal(100) / Decimal(raw_supply),
                    fetched_at,
                    "percent",
                )
            )
        except Exception:
            facts.append(unavailable("early_cohort_current_supply_pct", "token supply lookup failed", fetched_at))
        return facts

    def _funding_probe(
        self,
        wallets: list[str],
        first_times: dict[str, datetime | None],
        creation: ChronologyEvent | None,
        fetched_at: datetime,
        *,
        enabled: bool,
    ) -> tuple[list[FundingEdge], list[MemecoinStateFact]]:
        if not enabled:
            return [], [
                observed("funding_probe_enabled", False, fetched_at),
                *[
                    unavailable(name, "funding probe disabled", fetched_at)
                    for name in (
                        "funding_probe_wallet_count",
                        "funding_edge_found_count",
                        "funding_coverage_pct",
                        "shared_funder_group_count",
                        "max_early_buyers_same_funder",
                        "creation_user_funded_early_buyer_count",
                        "creation_creator_funded_early_buyer_count",
                    )
                ],
            ]
        edges = []
        for wallet in wallets:
            rows = self._rpc_call(
                "getSignaturesForAddress", signature_page_params(wallet, limit=20)
            )
            transactions = []
            for row in rows if isinstance(rows, list) else []:
                signature = str(row.get("signature") or "")
                tx = self._rpc_call("getTransaction", transaction_params(signature))
                if isinstance(tx, dict):
                    transactions.append((signature, tx))
            edge = nearest_prior_funding_edge(
                target_wallet=wallet,
                first_buy_time=first_times.get(wallet),
                transactions=transactions,
                creation_user=creation.creation_user if creation else None,
                creation_creator=creation.creator if creation else None,
            )
            if edge is not None:
                edges.append(edge)
        return edges, [observed("funding_probe_enabled", True, fetched_at), *funding_facts(edges, probed_wallets=len(wallets), fetched_at=fetched_at)]

    def _creator_history(
        self,
        creation: ChronologyEvent | None,
        current_mint: str,
        maximum: int,
        fetched_at: datetime,
    ) -> tuple[list[CreatorLaunchEvidence], list[MemecoinStateFact]]:
        if maximum == 0:
            return [], [
                observed("creator_history_probe_enabled", False, fetched_at),
                *[
                    unavailable(name, "creator history probe disabled", fetched_at)
                    for name in (
                        "creator_history_signature_count",
                        "creator_observed_launch_count",
                        "creator_observed_prior_launch_count",
                        "creator_history_truncated",
                    )
                ],
            ]
        if creation is None or not creation.creator:
            return [], [
                observed("creator_history_probe_enabled", True, fetched_at),
                *[
                    unavailable(name, "creation_creator is unavailable", fetched_at)
                    for name in (
                        "creator_history_signature_count",
                        "creator_observed_launch_count",
                        "creator_observed_prior_launch_count",
                        "creator_history_truncated",
                    )
                ],
            ]
        rows = self._rpc_call(
            "getSignaturesForAddress",
            signature_page_params(creation.creator, limit=maximum),
        )
        rows = rows if isinstance(rows, list) else []
        by_mint: dict[str, CreatorLaunchEvidence] = {}
        for row in rows:
            signature = str(row.get("signature") or "")
            tx = self._rpc_call("getTransaction", transaction_params(signature))
            if not isinstance(tx, dict):
                continue
            for event in decode_transaction(signature, tx):
                if event.event_type is not ChronologyEventType.CREATE or event.creator != creation.creator or not event.mint:
                    continue
                by_mint.setdefault(
                    event.mint,
                    CreatorLaunchEvidence(
                        creator=creation.creator,
                        mint=event.mint,
                        creation_signature=event.signature,
                        slot=event.slot,
                        block_time=event.block_time,
                        is_current_mint=event.mint == current_mint,
                        source="solana:json-rpc:pump-create",
                    ),
                )
        truncated = len(rows) >= maximum
        launches = list(by_mint.values())
        return launches, [
            observed("creator_history_probe_enabled", True, fetched_at),
            *creator_history_facts(
                launches,
                signature_count=len(rows),
                truncated=truncated,
                fetched_at=fetched_at,
            ),
        ]

    def _unavailable(
        self,
        snapshot: ObservationSnapshot,
        started_at: datetime,
        source_cutoff_at: datetime,
        reason: str,
    ) -> MemeChronologyObservation:
        ready_at = self.clock()
        facts = [
            unavailable("chronology", reason, ready_at),
            unavailable("sniper_supply_pct", "chronology unavailable"),
            unavailable("bundler_supply_pct", "chronology unavailable"),
        ]
        result = MemeChronologyObservation.create(
            snapshot_id=snapshot.snapshot_id,
            chronology_version=CHRONOLOGY_VERSION,
            started_at=started_at,
            ready_at=ready_at,
            source_cutoff_at=source_cutoff_at,
            coverage_status=ChronologyCoverage.UNAVAILABLE,
            history_truncated=False,
            reached_creation=False,
            signature_count=0,
            transaction_fetch_count=0,
            transaction_unavailable_count=0,
            unsupported_version_count=0,
            decode_failure_count=0,
            oldest_slot=None,
            newest_slot=None,
            oldest_block_time=None,
            newest_block_time=None,
            sources=[],
            events=[],
            facts=facts,
            funding_edges=[],
            creator_launches=[],
        )
        self.store.record_meme_chronology(result)
        return result


def _creation_facts(
    creation: ChronologyEvent | None, fetched_at: datetime
) -> list[MemecoinStateFact]:
    names = (
        "creation_signature",
        "creation_slot",
        "creation_block_time",
        "creation_user",
        "creation_creator",
        "creation_mint",
    )
    if creation is None:
        return [unavailable(name, "creation instruction was not reached", fetched_at) for name in names]
    values: tuple[Any, ...] = (
        creation.signature,
        creation.slot,
        creation.block_time.isoformat() if creation.block_time else None,
        creation.creation_user,
        creation.creator,
        creation.mint,
    )
    return [
        observed(name, value, fetched_at)
        if value is not None
        else unavailable(name, "decoded creation field was absent", fetched_at)
        for name, value in zip(names, values, strict=True)
    ]


def _coverage_reason(
    reached: bool, truncated: bool, unavailable: int, unsupported: int, failures: int
) -> str:
    reasons = []
    if not reached:
        reasons.append("creation transaction not reached")
    if truncated:
        reasons.append("bounded history truncated")
    if unavailable:
        reasons.append(f"{unavailable} transactions unavailable")
    if unsupported:
        reasons.append(f"{unsupported} unsupported transaction versions")
    if failures:
        reasons.append(f"{failures} decode failures")
    return "; ".join(reasons) or "coverage is incomplete"
