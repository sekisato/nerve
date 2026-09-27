from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from .models import FactStatus, MemecoinStateFact

DEXSCREENER_BASE_URL = "https://api.dexscreener.com"
SOURCE = "dexscreener:token-pairs-v1"


class DexScreenerClient:
    """Conservative client for the documented public token-pairs endpoint."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 8.0,
        min_interval_seconds: float = 1.0,
        retries: int = 1,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.min_interval_seconds = min_interval_seconds
        self.retries = max(0, min(retries, 2))
        self._last_request = 0.0

    def token_pairs(self, token: str) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            delay = self.min_interval_seconds - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
            if attempt:
                time.sleep(0.5 * attempt)
            try:
                response = httpx.get(
                    f"{DEXSCREENER_BASE_URL}/token-pairs/v1/solana/{token}",
                    timeout=self.timeout_seconds,
                    headers={"Accept": "application/json", "User-Agent": "nerve-memestate/1"},
                )
                self._last_request = time.monotonic()
                response.raise_for_status()
                payload = response.json()
                break
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
        else:
            assert last_error is not None
            raise last_error
        if not isinstance(payload, list):
            raise ValueError("DexScreener token-pairs response is not a list")
        return [row for row in payload if isinstance(row, dict)]


def select_pair(
    rows: list[dict[str, Any]], *, token: str, snapshot_pool: str
) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
    solana_rows = [row for row in rows if row.get("chainId") == "solana"]
    exact = [row for row in solana_rows if row.get("pairAddress") == snapshot_pool]
    candidates = [
        row
        for row in solana_rows
        if isinstance(row.get("baseToken"), dict)
        and row["baseToken"].get("address") == token
    ]
    metadata = [
        {
            "pair_address": row.get("pairAddress"),
            "dex_id": row.get("dexId"),
            "base_address": _nested(row, "baseToken", "address"),
            "liquidity_usd": (row.get("liquidity") or {}).get("usd"),
        }
        for row in solana_rows
    ]
    if exact:
        return exact[0], "exact_snapshot_pool", metadata
    if not candidates:
        return None, "no_matching_solana_base_pair", metadata

    def liquidity_key(row: dict[str, Any]) -> tuple[Decimal, str]:
        try:
            liquidity = Decimal(str((row.get("liquidity") or {}).get("usd") or "-1"))
        except InvalidOperation:
            liquidity = Decimal("-1")
        return (-liquidity, str(row.get("pairAddress") or ""))

    return sorted(candidates, key=liquidity_key)[0], "highest_liquidity_fallback", metadata


def pair_facts(
    rows: list[dict[str, Any]], *, token: str, snapshot_pool: str, fetched_at: datetime
) -> list[MemecoinStateFact]:
    pair, reason, candidates = select_pair(rows, token=token, snapshot_pool=snapshot_pool)
    selection_details = {"selection_reason": reason, "candidates": candidates}
    if pair is None:
        return [
            MemecoinStateFact.unavailable(
                name,
                "no documented Solana pair with the snapshot token as base",
                source=SOURCE,
                fetched_at=fetched_at,
                details=selection_details,
            )
            for name in _MARKET_FIELDS
        ]

    pair_created_at = _unix_ms(pair.get("pairCreatedAt"))
    pair_age_ms = (
        max(0, int((fetched_at - pair_created_at).total_seconds() * 1000))
        if pair_created_at is not None
        else None
    )
    facts = [
        _fact("dex_id", pair.get("dexId"), fetched_at, details=selection_details),
        _fact("pair_address", pair.get("pairAddress"), fetched_at, details=selection_details),
        _fact("pair_selection_reason", reason, fetched_at, details=selection_details),
        _fact(
            "pair_created_at",
            pair.get("pairCreatedAt"),
            fetched_at,
            unit="unix_ms",
            source_observed_at=pair_created_at,
            age_ms=pair_age_ms,
        ),
        _fact("pair_age_ms", pair_age_ms, fetched_at, unit="ms"),
        _fact("price_native", pair.get("priceNative"), fetched_at, numeric=True),
        _fact("price_usd", pair.get("priceUsd"), fetched_at, numeric=True, unit="USD"),
        _fact("liquidity_usd", _nested(pair, "liquidity", "usd"), fetched_at, numeric=True, unit="USD"),
        _fact("liquidity_base", _nested(pair, "liquidity", "base"), fetched_at, numeric=True),
        _fact("liquidity_quote", _nested(pair, "liquidity", "quote"), fetched_at, numeric=True),
        _fact("fdv_usd", pair.get("fdv"), fetched_at, numeric=True, unit="USD"),
        _fact("market_cap_usd", pair.get("marketCap"), fetched_at, numeric=True, unit="USD"),
    ]
    for window in ("m5", "h1", "h24"):
        facts.extend(
            [
                _fact(f"{window}_buys", _nested(pair, "txns", window, "buys"), fetched_at),
                _fact(f"{window}_sells", _nested(pair, "txns", window, "sells"), fetched_at),
                _fact(f"volume_{window}", _nested(pair, "volume", window), fetched_at, numeric=True, unit="USD"),
                _fact(
                    f"price_change_{window}_pct",
                    _nested(pair, "priceChange", window),
                    fetched_at,
                    numeric=True,
                    unit="percent",
                ),
            ]
        )
    return facts


def unavailable_market_facts(reason: str, fetched_at: datetime | None = None) -> list[MemecoinStateFact]:
    return [
        MemecoinStateFact.unavailable(name, reason, source=SOURCE, fetched_at=fetched_at)
        for name in _MARKET_FIELDS
    ]


def _nested(value: dict[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _fact(
    name: str,
    value: Any,
    fetched_at: datetime,
    *,
    numeric: bool = False,
    unit: str | None = None,
    details: dict[str, Any] | None = None,
    source_observed_at: datetime | None = None,
    age_ms: int | None = None,
) -> MemecoinStateFact:
    if value is None:
        return MemecoinStateFact.unavailable(
            name, "field absent from documented response", source=SOURCE, fetched_at=fetched_at
        )
    if numeric:
        try:
            value = Decimal(str(value))
        except InvalidOperation:
            return MemecoinStateFact.unavailable(
                name,
                "documented numeric field was invalid",
                source=SOURCE,
                fetched_at=fetched_at,
                status=FactStatus.INVALID,
            )
    elif isinstance(value, float):
        value = Decimal(str(value))
    return MemecoinStateFact.observed(
        name,
        value,
        source=SOURCE,
        fetched_at=fetched_at,
        source_observed_at=source_observed_at,
        age_ms=age_ms,
        unit=unit,
        details=details,
    )


def _unix_ms(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


_MARKET_FIELDS = (
    "dex_id",
    "pair_address",
    "pair_selection_reason",
    "pair_created_at",
    "pair_age_ms",
    "price_native",
    "price_usd",
    "liquidity_usd",
    "liquidity_base",
    "liquidity_quote",
    "fdv_usd",
    "market_cap_usd",
    "m5_buys",
    "m5_sells",
    "h1_buys",
    "h1_sells",
    "h24_buys",
    "h24_sells",
    "volume_m5",
    "volume_h1",
    "volume_h24",
    "price_change_m5_pct",
    "price_change_h1_pct",
    "price_change_h24_pct",
)
