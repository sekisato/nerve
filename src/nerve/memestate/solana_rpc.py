from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from .models import MemecoinStateFact

SOURCE = "solana:json-rpc"


class SolanaRpcClient:
    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = 8.0,
        retries: int = 1,
        min_interval_seconds: float = 0.25,
    ) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.retries = max(0, min(retries, 2))
        self.min_interval_seconds = min_interval_seconds
        self._request_id = 0
        self._last_request = 0.0

    def call(self, method: str, params: list[Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            delay = self.min_interval_seconds - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
            if attempt:
                time.sleep(0.5 * attempt)
            self._request_id += 1
            try:
                response = httpx.post(
                    self.url,
                    json={"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params},
                    timeout=self.timeout_seconds,
                    headers={"Accept": "application/json", "User-Agent": "nerve-memestate/1"},
                )
                self._last_request = time.monotonic()
                response.raise_for_status()
                payload = response.json()
                if payload.get("error") is not None:
                    raise ValueError(f"Solana RPC {method} error: {payload['error']}")
                return payload.get("result")
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error


def token_distribution_facts(
    supply_result: dict[str, Any],
    largest_result: dict[str, Any],
    *,
    fetched_at: datetime,
    owner_rows: list[dict[str, Any] | None] | None = None,
) -> list[MemecoinStateFact]:
    supply_value = (supply_result.get("value") or {}) if isinstance(supply_result, dict) else {}
    raw_supply = int(str(supply_value.get("amount", "0")))
    entries = largest_result.get("value", []) if isinstance(largest_result, dict) else []
    if raw_supply <= 0:
        return unavailable_distribution_facts("token supply is missing or zero", fetched_at)
    amounts = [int(str(row.get("amount", "0"))) for row in entries if isinstance(row, dict)]
    facts = supply_facts(supply_result, fetched_at=fetched_at)
    facts.extend(
        [
            MemecoinStateFact.observed(
                f"top{count}_token_accounts_pct",
                Decimal(sum(amounts[:count])) * Decimal(100) / Decimal(raw_supply),
                source=SOURCE,
                fetched_at=fetched_at,
                unit="percent",
                details={"denominator": "getTokenSupply raw amount", "sample": count},
            )
            for count in (1, 5, 10, 20)
        ]
    )
    if owner_rows is None:
        facts.extend(
            [
                MemecoinStateFact.unavailable(
                    "top20_accounts_owner_coverage_pct",
                    "owner resolution was not attempted or unavailable",
                    source=SOURCE,
                    fetched_at=fetched_at,
                ),
                MemecoinStateFact.unavailable(
                    "largest_owner_within_top20_pct",
                    "owner resolution was not attempted or unavailable",
                    source=SOURCE,
                    fetched_at=fetched_at,
                ),
                MemecoinStateFact.unavailable(
                    "top5_owners_within_top20_pct",
                    "owner resolution was not attempted or unavailable",
                    source=SOURCE,
                    fetched_at=fetched_at,
                ),
            ]
        )
        return facts

    owner_totals: dict[str, int] = {}
    resolved_amount = 0
    for index, row in enumerate(owner_rows[:20]):
        if row is None or index >= len(amounts):
            continue
        owner = _parsed_owner(row)
        if owner is None:
            continue
        amount = amounts[index]
        resolved_amount += amount
        owner_totals[owner] = owner_totals.get(owner, 0) + amount
    coverage = Decimal(resolved_amount) * Decimal(100) / Decimal(sum(amounts[:20]) or 1)
    sorted_owner_amounts = sorted(owner_totals.values(), reverse=True)
    details = {"scope": "resolved owners within sampled top 20 token accounts"}
    facts.append(
        MemecoinStateFact.observed(
            "top20_accounts_owner_coverage_pct",
            coverage,
            source=SOURCE,
            fetched_at=fetched_at,
            unit="percent",
            details=details,
        )
    )
    if sorted_owner_amounts:
        facts.extend(
            [
                MemecoinStateFact.observed(
                    "largest_owner_within_top20_pct",
                    Decimal(sum(sorted_owner_amounts[:1])) * Decimal(100) / Decimal(raw_supply),
                    source=SOURCE,
                    fetched_at=fetched_at,
                    unit="percent",
                    details=details,
                ),
                MemecoinStateFact.observed(
                    "top5_owners_within_top20_pct",
                    Decimal(sum(sorted_owner_amounts[:5])) * Decimal(100) / Decimal(raw_supply),
                    source=SOURCE,
                    fetched_at=fetched_at,
                    unit="percent",
                    details=details,
                ),
            ]
        )
    else:
        facts.extend(
            [
                MemecoinStateFact.unavailable(
                    name,
                    "no token-account owner could be resolved in the sampled top 20",
                    source=SOURCE,
                    fetched_at=fetched_at,
                    details=details,
                )
                for name in ("largest_owner_within_top20_pct", "top5_owners_within_top20_pct")
            ]
        )
    return facts


def supply_facts(
    supply_result: dict[str, Any], *, fetched_at: datetime
) -> list[MemecoinStateFact]:
    value = supply_result.get("value") or {}
    raw_supply = int(str(value.get("amount", "0")))
    if raw_supply <= 0:
        return [
            MemecoinStateFact.unavailable(name, "token supply is missing or zero", source=SOURCE, fetched_at=fetched_at)
            for name in ("token_supply_raw", "token_decimals")
        ]
    return [
        MemecoinStateFact.observed(
            "token_supply_raw", raw_supply, source=SOURCE, fetched_at=fetched_at, unit="atomic"
        ),
        MemecoinStateFact.observed(
            "token_decimals", int(value.get("decimals", 0)), source=SOURCE, fetched_at=fetched_at
        ),
    ]


def unavailable_concentration_facts(
    reason: str, fetched_at: datetime | None = None
) -> list[MemecoinStateFact]:
    names = (
        "top1_token_accounts_pct",
        "top5_token_accounts_pct",
        "top10_token_accounts_pct",
        "top20_token_accounts_pct",
        "top20_accounts_owner_coverage_pct",
        "largest_owner_within_top20_pct",
        "top5_owners_within_top20_pct",
    )
    return [
        MemecoinStateFact.unavailable(name, reason, source=SOURCE, fetched_at=fetched_at)
        for name in names
    ]


def _parsed_owner(row: dict[str, Any]) -> str | None:
    value = row.get("value") or {}
    data = value.get("data") or {}
    parsed = data.get("parsed") or {}
    info = parsed.get("info") or {}
    owner = info.get("owner")
    return str(owner) if owner else None


def unavailable_distribution_facts(
    reason: str, fetched_at: datetime | None = None
) -> list[MemecoinStateFact]:
    return [
        MemecoinStateFact.unavailable(name, reason, source=SOURCE, fetched_at=fetched_at)
        for name in ("token_supply_raw", "token_decimals")
    ] + unavailable_concentration_facts(reason, fetched_at)


def owner_accounts_params(largest_result: dict[str, Any]) -> list[Any]:
    rows = largest_result.get("value", []) if isinstance(largest_result, dict) else []
    addresses = [str(row["address"]) for row in rows[:20] if isinstance(row, dict) and row.get("address")]
    return [addresses, {"encoding": "jsonParsed", "commitment": "confirmed"}]
