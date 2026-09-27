from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..memestate.models import canonical_json
from .models import FundingEdge
from .pump_decode import iter_instructions

SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"


def signature_page_params(
    address: str, *, limit: int, before: str | None = None
) -> list[Any]:
    options: dict[str, Any] = {"limit": max(1, min(limit, 1000)), "commitment": "confirmed"}
    if before:
        options["before"] = before
    return [address, options]


def transaction_params(signature: str) -> list[Any]:
    return [
        signature,
        {
            "encoding": "jsonParsed",
            "commitment": "confirmed",
            "maxSupportedTransactionVersion": 0,
        },
    ]


def nearest_prior_funding_edge(
    *,
    target_wallet: str,
    first_buy_time: datetime | None,
    transactions: list[tuple[str, dict[str, Any]]],
    creation_user: str | None,
    creation_creator: str | None,
    window_seconds: int = 7200,
) -> FundingEdge | None:
    if first_buy_time is None:
        return None
    candidates: list[FundingEdge] = []
    for signature, transaction in transactions:
        raw_time = transaction.get("blockTime")
        if raw_time is None:
            continue
        block_time = datetime.fromtimestamp(int(raw_time), tz=UTC)
        seconds_before = int((first_buy_time - block_time).total_seconds())
        if seconds_before < 0 or seconds_before > window_seconds:
            continue
        for path, instruction in iter_instructions(transaction):
            if instruction.get("program") != "system":
                continue
            parsed = instruction.get("parsed") or {}
            if parsed.get("type") not in {"transfer", "transferWithSeed"}:
                continue
            info = parsed.get("info") or {}
            if info.get("destination") != target_wallet:
                continue
            source = info.get("source")
            lamports = info.get("lamports")
            if not source or not isinstance(lamports, int) or lamports <= 0:
                continue
            candidates.append(
                FundingEdge(
                    target_wallet=target_wallet,
                    source_wallet=str(source),
                    funding_signature=signature,
                    slot=int(transaction.get("slot", 0)),
                    block_time=block_time,
                    lamports=lamports,
                    seconds_before_first_buy=seconds_before,
                    relation_to_creation_user=(str(source) == creation_user if creation_user else None),
                    relation_to_creation_creator=(
                        str(source) == creation_creator if creation_creator else None
                    ),
                    details_json=canonical_json({"instruction_path": path}),
                )
            )
    return min(candidates, key=lambda edge: (edge.seconds_before_first_buy, edge.funding_signature)) if candidates else None
