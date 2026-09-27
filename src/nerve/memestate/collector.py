from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol

from ..lab.models import MeasurementEvent, MeasurementEventKind, ObservationSnapshot
from .dexscreener import DexScreenerClient, pair_facts, select_pair, unavailable_market_facts
from .models import (
    FactStatus,
    LifecycleState,
    MemecoinStateFact,
    MemecoinStateObservation,
    StateStatus,
    utc_now,
)
from .pump import (
    SOURCE as PUMP_SOURCE,
)
from .pump import (
    account_value,
    bonding_curve_facts,
    bonding_curve_pda,
    canonical_pumpswap_verified,
    decode_bonding_curve,
    decode_pumpswap_pool,
    lifecycle_state,
)
from .solana_rpc import (
    SOURCE as RPC_SOURCE,
)
from .solana_rpc import (
    SolanaRpcClient,
    owner_accounts_params,
    supply_facts,
    token_distribution_facts,
    unavailable_concentration_facts,
    unavailable_distribution_facts,
)

STATE_VERSION = "memestate-v1"
logger = logging.getLogger(__name__)


class StateStore(Protocol):
    def record_memecoin_state(self, state: MemecoinStateObservation) -> None: ...

    def record_measurement_event(self, event: MeasurementEvent) -> None: ...


class MemecoinStateCollector:
    """Explicit lab-side collector; never called by Spine or Executor."""

    def __init__(
        self,
        store: StateStore,
        *,
        solana_rpc_url: str = "",
        dex_client: DexScreenerClient | None = None,
        rpc_client: SolanaRpcClient | None = None,
        clock: Any = utc_now,
    ) -> None:
        self.store = store
        self.dex_client = dex_client or DexScreenerClient()
        self.rpc_client = rpc_client or (SolanaRpcClient(solana_rpc_url) if solana_rpc_url else None)
        self.clock = clock

    def collect(self, snapshot: ObservationSnapshot) -> MemecoinStateObservation:
        started_at: datetime = self.clock()
        facts: list[MemecoinStateFact] = []
        sources: list[str] = []
        dex_rows: list[dict[str, Any]] = []
        fetched_at = self.clock()
        try:
            dex_rows = self.dex_client.token_pairs(snapshot.token)
            facts.extend(
                pair_facts(
                    dex_rows,
                    token=snapshot.token,
                    snapshot_pool=snapshot.pool,
                    fetched_at=fetched_at,
                )
            )
            sources.append("dexscreener:token-pairs-v1")
        except Exception as exc:
            facts.extend(unavailable_market_facts(f"DexScreener request failed: {exc}", fetched_at))

        lifecycle = LifecycleState.UNKNOWN
        if self.rpc_client is None:
            reason = "SOLANA_RPC_URL is not configured"
            facts.extend(unavailable_distribution_facts(reason))
            facts.extend(
                bonding_curve_facts(
                    None,
                    mint=snapshot.token,
                    pda=_safe_bonding_curve_pda(snapshot.token),
                    fetched_at=self.clock(),
                    error=reason,
                )
            )
            facts.append(
                MemecoinStateFact.unavailable(
                    "canonical_pumpswap_verified", reason, source=PUMP_SOURCE
                )
            )
        else:
            sources.extend([RPC_SOURCE, PUMP_SOURCE])
            facts.extend(self._collect_solana(snapshot.token))
            pump_facts, lifecycle = self._collect_pump(snapshot, dex_rows)
            facts.extend(pump_facts)

        facts.append(
            MemecoinStateFact.observed(
                "pump_lifecycle_state",
                lifecycle.value,
                source=PUMP_SOURCE,
                fetched_at=self.clock(),
            )
        )
        facts.extend(_semantic_unavailable_facts())
        ready_at: datetime = self.clock()
        observed = sum(fact.status is FactStatus.OBSERVED for fact in facts)
        status = (
            StateStatus.FAILED
            if observed == 0
            else StateStatus.SUCCESS
            if all(fact.status is FactStatus.OBSERVED for fact in facts)
            else StateStatus.PARTIAL
        )
        state = MemecoinStateObservation.create(
            snapshot_id=snapshot.snapshot_id,
            state_version=STATE_VERSION,
            started_at=started_at,
            ready_at=ready_at,
            status=status,
            sources=sources,
            facts=facts,
        )
        self.store.record_memecoin_state(state)
        return state

    def _collect_solana(self, mint: str) -> list[MemecoinStateFact]:
        assert self.rpc_client is not None
        fetched_at = self.clock()
        try:
            supply = self.rpc_client.call(
                "getTokenSupply", [mint, {"commitment": "confirmed"}]
            )
        except Exception as exc:
            return unavailable_distribution_facts(f"Solana token supply failed: {exc}", fetched_at)
        facts = supply_facts(supply, fetched_at=fetched_at)
        try:
            largest = self.rpc_client.call(
                "getTokenLargestAccounts", [mint, {"commitment": "confirmed"}]
            )
            owner_result = self.rpc_client.call("getMultipleAccounts", owner_accounts_params(largest))
            owner_rows = owner_result.get("value") if isinstance(owner_result, dict) else None
            distribution = token_distribution_facts(
                supply, largest, fetched_at=fetched_at, owner_rows=owner_rows
            )
            return distribution
        except Exception as exc:
            facts.extend(
                unavailable_concentration_facts(
                    f"Solana largest-account or owner resolution failed: {exc}", fetched_at
                )
            )
            return facts

    def _collect_pump(
        self, snapshot: ObservationSnapshot, dex_rows: list[dict[str, Any]]
    ) -> tuple[list[MemecoinStateFact], LifecycleState]:
        assert self.rpc_client is not None
        fetched_at = self.clock()
        try:
            pda = bonding_curve_pda(snapshot.token)
        except ValueError as exc:
            invalid_mint = bonding_curve_facts(
                None,
                mint=snapshot.token,
                pda="Unavailable",
                fetched_at=fetched_at,
                error=f"invalid Solana mint: {exc}",
            )
            invalid_mint.append(
                MemecoinStateFact.unavailable(
                    "canonical_pumpswap_verified",
                    "invalid Solana mint",
                    source=PUMP_SOURCE,
                    fetched_at=fetched_at,
                )
            )
            return (
                invalid_mint,
                LifecycleState.UNKNOWN,
            )
        try:
            result = self.rpc_client.call(
                "getAccountInfo", [pda, {"encoding": "base64", "commitment": "confirmed"}]
            )
        except Exception as exc:
            missing = bonding_curve_facts(
                None,
                mint=snapshot.token,
                pda=pda,
                fetched_at=fetched_at,
                error=f"Pump RPC request failed: {exc}",
            )
            missing.append(
                MemecoinStateFact.unavailable(
                    "canonical_pumpswap_verified",
                    "Pump state was not available for canonical migration verification",
                    source=PUMP_SOURCE,
                    fetched_at=fetched_at,
                )
            )
            return missing, LifecycleState.UNKNOWN
        account = account_value(result)
        if account is None:
            missing = bonding_curve_facts(
                None,
                mint=snapshot.token,
                pda=pda,
                fetched_at=fetched_at,
                error="BondingCurve PDA account does not exist",
            )
            missing.append(
                MemecoinStateFact.unavailable(
                    "canonical_pumpswap_verified",
                    "Pump state was not available for canonical migration verification",
                    source=PUMP_SOURCE,
                    fetched_at=fetched_at,
                )
            )
            return missing, LifecycleState.UNKNOWN
        try:
            decoded = decode_bonding_curve(*account)
            facts = bonding_curve_facts(
                decoded, mint=snapshot.token, pda=pda, fetched_at=fetched_at
            )
        except Exception as exc:
            invalid = bonding_curve_facts(
                None,
                mint=snapshot.token,
                pda=pda,
                fetched_at=fetched_at,
                error=f"Pump account validation failed: {exc}",
            )
            invalid.append(
                MemecoinStateFact.unavailable(
                    "canonical_pumpswap_verified",
                    "Pump account could not be validated",
                    source=PUMP_SOURCE,
                    fetched_at=fetched_at,
                )
            )
            return (
                invalid,
                LifecycleState.NOT_PUMP,
            )

        selected, _reason, _candidates = select_pair(
            dex_rows, token=snapshot.token, snapshot_pool=snapshot.pool
        )
        verified: bool | None = None
        evidence: dict[str, Any] = {"candidate_pair": None, "on_chain_owner": None}
        if selected is not None and str(selected.get("dexId", "")).lower() == "pumpswap":
            candidate = str(selected.get("pairAddress") or "")
            evidence["candidate_pair"] = candidate
            if candidate:
                try:
                    result = self.rpc_client.call(
                        "getAccountInfo",
                        [candidate, {"encoding": "base64", "commitment": "confirmed"}],
                    )
                    account = account_value(result)
                    if account is not None:
                        evidence["on_chain_owner"] = account[1]
                        pool = decode_pumpswap_pool(*account)
                        evidence.update(
                            {
                                "index": pool.get("index"),
                                "creator": pool.get("creator"),
                                "base_mint": pool.get("base_mint"),
                                "quote_mint": pool.get("quote_mint"),
                            }
                        )
                        verified = canonical_pumpswap_verified(pool, mint=snapshot.token)
                except Exception as exc:
                    evidence["verification_error"] = str(exc)
        if verified is None:
            facts.append(
                MemecoinStateFact.unavailable(
                    "canonical_pumpswap_verified",
                    "no PumpSwap candidate was fully decoded and verified on-chain",
                    source=PUMP_SOURCE,
                    fetched_at=fetched_at,
                    details=evidence,
                )
            )
        else:
            facts.append(
                MemecoinStateFact.observed(
                    "canonical_pumpswap_verified",
                    verified,
                    source=PUMP_SOURCE,
                    fetched_at=fetched_at,
                    details=evidence,
                )
            )
        lifecycle = lifecycle_state(
            pump_account_valid=True,
            curve_complete=decoded.get("curve_complete"),
            canonical_pool_verified=verified is True,
        )
        return facts, lifecycle

    def collect_safely(self, snapshot: ObservationSnapshot) -> MemecoinStateObservation | None:
        try:
            return self.collect(snapshot)
        except Exception as exc:
            try:
                self.store.record_measurement_event(
                    MeasurementEvent(
                        kind=MeasurementEventKind.MEMESTATE_FAILED,
                        stage="memestate_collect",
                        impulse_id=snapshot.impulse_id,
                        snapshot_id=snapshot.snapshot_id,
                        error_type=type(exc).__name__,
                        message=str(exc)[:1000],
                        details={"state_version": STATE_VERSION},
                    )
                )
            except Exception:
                logger.exception("failed to persist memestate collection failure")
            return None


def _semantic_unavailable_facts() -> list[MemecoinStateFact]:
    reasons = {
        "unique_buyers": "DexScreener buy transactions are not unique buyers",
        "sniper_supply_pct": "transaction chronology and early-buyer attribution are not implemented",
        "bundler_supply_pct": "funding and coordination evidence are not implemented",
    }
    return [MemecoinStateFact.unavailable(name, reason) for name, reason in reasons.items()]


def _safe_bonding_curve_pda(mint: str) -> str:
    try:
        return bonding_curve_pda(mint)
    except ValueError:
        return "Unavailable"
