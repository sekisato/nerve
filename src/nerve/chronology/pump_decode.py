from __future__ import annotations

import struct
from datetime import UTC, datetime
from typing import Any

from ..memestate.models import canonical_json
from ..memestate.pump import PUMP_PROGRAM_ID, PUMPSWAP_PROGRAM_ID, b58decode, b58encode
from .models import ChronologyEvent, ChronologyEventType, DecodeStatus

PUMP_INSTRUCTIONS: dict[bytes, tuple[str, ChronologyEventType, int, int, int | None]] = {
    bytes([24, 30, 200, 40, 5, 28, 7, 119]): ("create", ChronologyEventType.CREATE, 0, 7, None),
    bytes([214, 144, 76, 236, 95, 139, 49, 180]): ("create_v2", ChronologyEventType.CREATE, 0, 5, None),
    bytes([102, 6, 61, 18, 1, 218, 235, 234]): ("buy", ChronologyEventType.BUY, 2, 6, None),
    bytes([56, 252, 116, 8, 158, 223, 205, 95]): ("buy_exact_sol_in", ChronologyEventType.BUY, 2, 6, None),
    bytes([184, 23, 238, 97, 103, 197, 211, 61]): ("buy_v2", ChronologyEventType.BUY, 1, 13, None),
    bytes([194, 171, 28, 70, 104, 77, 91, 47]): ("buy_exact_quote_in_v2", ChronologyEventType.BUY, 1, 13, None),
    bytes([51, 230, 133, 164, 1, 127, 131, 173]): ("sell", ChronologyEventType.SELL, 2, 6, None),
    bytes([93, 246, 130, 60, 231, 233, 64, 178]): ("sell_v2", ChronologyEventType.SELL, 1, 13, None),
    bytes([155, 234, 231, 146, 236, 158, 162, 30]): ("migrate", ChronologyEventType.MIGRATE, 2, 5, 9),
}

PUMPSWAP_INSTRUCTIONS: dict[bytes, tuple[str, ChronologyEventType]] = {
    bytes([102, 6, 61, 18, 1, 218, 235, 234]): ("buy", ChronologyEventType.BUY),
    bytes([198, 46, 21, 82, 180, 217, 232, 112]): ("buy_exact_quote_in", ChronologyEventType.BUY),
    bytes([51, 230, 133, 164, 1, 127, 131, 173]): ("sell", ChronologyEventType.SELL),
}


def decode_transaction(
    signature: str, transaction: dict[str, Any], *, target_mint: str | None = None
) -> list[ChronologyEvent]:
    slot = int(transaction.get("slot", 0))
    raw_block_time = transaction.get("blockTime")
    block_time = datetime.fromtimestamp(int(raw_block_time), tz=UTC) if raw_block_time else None
    result: list[ChronologyEvent] = []
    seen_paths: set[str] = set()
    for path, instruction in iter_instructions(transaction):
        if path in seen_paths:
            continue
        seen_paths.add(path)
        event = decode_instruction(
            signature, slot, block_time, path, instruction, target_mint=target_mint
        )
        if event is not None:
            result.append(event)
    return result


def iter_instructions(transaction: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    message = ((transaction.get("transaction") or {}).get("message") or {})
    account_keys = _account_keys(transaction)
    outer = message.get("instructions") or []
    result: list[tuple[str, dict[str, Any]]] = []
    for index, instruction in enumerate(outer):
        if isinstance(instruction, dict):
            result.append((f"outer:{index}", _resolve_instruction(instruction, account_keys)))
    inner_groups = ((transaction.get("meta") or {}).get("innerInstructions") or [])
    for group in inner_groups:
        if not isinstance(group, dict):
            continue
        outer_index = int(group.get("index", -1))
        for inner_index, instruction in enumerate(group.get("instructions") or []):
            if isinstance(instruction, dict):
                result.append(
                    (
                        f"outer:{outer_index}/inner:{inner_index}",
                        _resolve_instruction(instruction, account_keys),
                    )
                )
    return result


def decode_instruction(
    signature: str,
    slot: int,
    block_time: datetime | None,
    path: str,
    instruction: dict[str, Any],
    *,
    target_mint: str | None = None,
) -> ChronologyEvent | None:
    program_id = str(instruction.get("programId") or "")
    data_text = instruction.get("data")
    accounts = [str(value) for value in instruction.get("accounts") or []]
    if not program_id or not isinstance(data_text, str):
        return None
    try:
        data = b58decode(data_text)
    except (ValueError, IndexError):
        return None
    if len(data) < 8:
        return None
    discriminator = data[:8]
    if program_id == PUMP_PROGRAM_ID and discriminator in PUMP_INSTRUCTIONS:
        name, event_type, mint_index, user_index, pool_index = PUMP_INSTRUCTIONS[discriminator]
        if max(mint_index, user_index, pool_index or 0) >= len(accounts):
            return None
        mint = accounts[mint_index]
        if target_mint is not None and mint != target_mint:
            return None
        creator = _creation_creator(data) if event_type is ChronologyEventType.CREATE else None
        amount_token, amount_quote = _amounts(data, name)
        user = accounts[user_index]
        return ChronologyEvent(
            signature=signature,
            slot=slot,
            block_time=block_time,
            instruction_path=path,
            program_id=program_id,
            venue="pump_curve",
            event_type=event_type,
            user=user,
            mint=mint,
            pool=accounts[pool_index] if pool_index is not None else None,
            creator=creator,
            creation_user=user if event_type is ChronologyEventType.CREATE else None,
            amount_token_raw=amount_token,
            amount_quote_raw=amount_quote,
            instruction_discriminator=discriminator.hex(),
            details_json=canonical_json({"instruction_name": name}),
        )
    if program_id == PUMPSWAP_PROGRAM_ID and discriminator in PUMPSWAP_INSTRUCTIONS:
        name, event_type = PUMPSWAP_INSTRUCTIONS[discriminator]
        if len(accounts) < 5:
            return None
        mint = accounts[3]
        if target_mint is not None and mint != target_mint:
            return None
        amount_token, amount_quote = _amounts(data, name)
        return ChronologyEvent(
            signature=signature,
            slot=slot,
            block_time=block_time,
            instruction_path=path,
            program_id=program_id,
            venue="pumpswap",
            event_type=event_type,
            user=accounts[1],
            mint=mint,
            pool=accounts[0],
            amount_token_raw=amount_token,
            amount_quote_raw=amount_quote,
            instruction_discriminator=discriminator.hex(),
            details_json=canonical_json({"instruction_name": name}),
        )
    if (
        program_id in {PUMP_PROGRAM_ID, PUMPSWAP_PROGRAM_ID}
        and target_mint is not None
        and target_mint in accounts
    ):
        return ChronologyEvent(
            signature=signature,
            slot=slot,
            block_time=block_time,
            instruction_path=path,
            program_id=program_id,
            venue="pump_curve" if program_id == PUMP_PROGRAM_ID else "pumpswap",
            event_type=ChronologyEventType.UNKNOWN,
            mint=target_mint,
            instruction_discriminator=discriminator.hex(),
            decode_status=DecodeStatus.UNSUPPORTED,
            details_json=canonical_json({"reason": "unknown instruction discriminator"}),
        )
    return None


def _account_keys(transaction: dict[str, Any]) -> list[str]:
    message = ((transaction.get("transaction") or {}).get("message") or {})
    keys = [
        str(item.get("pubkey")) if isinstance(item, dict) else str(item)
        for item in message.get("accountKeys") or []
    ]
    loaded = ((transaction.get("meta") or {}).get("loadedAddresses") or {})
    keys.extend(str(value) for value in loaded.get("writable") or [])
    keys.extend(str(value) for value in loaded.get("readonly") or [])
    return keys


def _resolve_instruction(instruction: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    result = dict(instruction)
    program = result.get("programId")
    if program is None and isinstance(result.get("programIdIndex"), int):
        index = int(result["programIdIndex"])
        result["programId"] = keys[index] if index < len(keys) else ""
    resolved = []
    for item in result.get("accounts") or []:
        resolved.append(keys[item] if isinstance(item, int) and item < len(keys) else item)
    result["accounts"] = resolved
    return result


def _creation_creator(data: bytes) -> str | None:
    offset = 8
    try:
        for _ in range(3):
            length = struct.unpack_from("<I", data, offset)[0]
            offset += 4 + length
        if len(data) < offset + 32:
            return None
        return b58encode(data[offset : offset + 32])
    except struct.error:
        return None


def _amounts(data: bytes, name: str) -> tuple[int | None, int | None]:
    if len(data) < 24 or name in {"create", "create_v2", "migrate"}:
        return None, None
    first, second = struct.unpack_from("<QQ", data, 8)
    if "exact_sol_in" in name or "exact_quote_in" in name:
        return second, first
    return first, second
