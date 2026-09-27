from __future__ import annotations

import base64
import hashlib
import struct
from datetime import datetime
from typing import Any

from .models import LifecycleState, MemecoinStateFact

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL_MINT = "So11111111111111111111111111111111111111112"
PUMP_DOCS_SHA = "81091419e4457566469d4e2a27f64ed84d42419c"
BONDING_CURVE_DISCRIMINATOR = bytes([23, 183, 248, 55, 96, 216, 172, 96])
POOL_DISCRIMINATOR = bytes([241, 154, 109, 4, 17, 177, 109, 188])
SOURCE = f"solana:json-rpc:pump-account@pump-public-docs:{PUMP_DOCS_SHA}"
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(value: str) -> bytes:
    number = 0
    for char in value:
        number = number * 58 + _B58_ALPHABET.index(char)
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\0" * (len(value) - len(value.lstrip("1"))) + raw


def b58encode(value: bytes) -> str:
    number = int.from_bytes(value, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = _B58_ALPHABET[remainder] + encoded
    return "1" * (len(value) - len(value.lstrip(b"\0"))) + (encoded or "")


def find_program_address(seeds: list[bytes], program_id: str) -> str:
    program = b58decode(program_id)
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(
            b"".join([*seeds, bytes([bump]), program, b"ProgramDerivedAddress"])
        ).digest()
        if not _is_ed25519_point(digest):
            return b58encode(digest)
    raise ValueError("unable to find program address")


def _is_ed25519_point(value: bytes) -> bool:
    if len(value) != 32:
        return False
    p = 2**255 - 19
    d = (-121665 * pow(121666, p - 2, p)) % p
    y = int.from_bytes(value, "little") & ((1 << 255) - 1)
    if y >= p:
        return False
    y2 = y * y % p
    x2 = (y2 - 1) * pow((d * y2 + 1) % p, p - 2, p) % p
    return pow(x2, (p - 1) // 2, p) in (0, 1)


def bonding_curve_pda(mint: str) -> str:
    return find_program_address([b"bonding-curve", b58decode(mint)], PUMP_PROGRAM_ID)


def pool_authority_pda(mint: str) -> str:
    return find_program_address([b"pool-authority", b58decode(mint)], PUMP_PROGRAM_ID)


def decode_bonding_curve(data: bytes, owner: str) -> dict[str, Any]:
    if owner != PUMP_PROGRAM_ID:
        raise ValueError("bonding curve account is not owned by the Pump program")
    if len(data) < 49 or data[:8] != BONDING_CURVE_DISCRIMINATOR:
        raise ValueError("invalid Pump BondingCurve discriminator or layout")
    offset = 8
    names = (
        "virtual_token_reserves",
        "virtual_quote_reserves",
        "real_token_reserves",
        "real_quote_reserves",
        "token_total_supply",
    )
    result: dict[str, Any] = {}
    for name in names:
        result[name] = struct.unpack_from("<Q", data, offset)[0]
        offset += 8
    result["curve_complete"] = bool(data[offset])
    offset += 1
    optional = (
        ("coin_creator", 32, "pubkey"),
        ("is_mayhem_mode", 1, "bool"),
        ("is_cashback_coin", 1, "bool"),
        ("quote_mint", 32, "pubkey"),
        ("creator_fee_bps", 8, "u64"),
        ("can_edit_creator_fee", 1, "bool"),
        ("is_holder_reward", 1, "bool"),
    )
    for name, size, kind in optional:
        if len(data) < offset + size:
            result[name] = None
            offset += size
            continue
        raw = data[offset : offset + size]
        offset += size
        if kind == "pubkey":
            result[name] = b58encode(raw)
        elif kind == "bool":
            result[name] = bool(raw[0])
        else:
            result[name] = struct.unpack("<Q", raw)[0]
    return result


def decode_pumpswap_pool(data: bytes, owner: str) -> dict[str, Any]:
    if owner != PUMPSWAP_PROGRAM_ID:
        raise ValueError("pool account is not owned by the PumpSwap program")
    minimum = 8 + 1 + 2 + (6 * 32) + 8
    if len(data) < minimum or data[:8] != POOL_DISCRIMINATOR:
        raise ValueError("invalid PumpSwap Pool discriminator or layout")
    offset = 8
    pool_bump = data[offset]
    offset += 1
    index = struct.unpack_from("<H", data, offset)[0]
    offset += 2
    names = (
        "creator",
        "base_mint",
        "quote_mint",
        "lp_mint",
        "pool_base_token_account",
        "pool_quote_token_account",
    )
    result: dict[str, Any] = {"pool_bump": pool_bump, "index": index}
    for name in names:
        result[name] = b58encode(data[offset : offset + 32])
        offset += 32
    result["lp_supply"] = struct.unpack_from("<Q", data, offset)[0]
    return result


def canonical_pumpswap_verified(pool: dict[str, Any], *, mint: str) -> bool:
    return bool(
        pool.get("index") == 0
        and pool.get("base_mint") == mint
        and pool.get("quote_mint") == WSOL_MINT
        and pool.get("creator") == pool_authority_pda(mint)
    )


def lifecycle_state(
    *, pump_account_valid: bool, curve_complete: bool | None, canonical_pool_verified: bool
) -> LifecycleState:
    if not pump_account_valid:
        return LifecycleState.NOT_PUMP
    if curve_complete is False:
        return LifecycleState.PUMP_CURVE_ACTIVE
    if canonical_pool_verified:
        return LifecycleState.PUMP_CANONICAL_PUMPSWAP_VERIFIED
    if curve_complete is True:
        return LifecycleState.PUMP_CURVE_COMPLETE_MIGRATION_UNKNOWN
    return LifecycleState.UNKNOWN


def account_value(result: Any) -> tuple[bytes, str] | None:
    value = result.get("value") if isinstance(result, dict) else None
    if not isinstance(value, dict):
        return None
    encoded = value.get("data")
    if not isinstance(encoded, list) or not encoded or not isinstance(encoded[0], str):
        return None
    return base64.b64decode(encoded[0]), str(value.get("owner") or "")


def bonding_curve_facts(
    decoded: dict[str, Any] | None,
    *,
    mint: str,
    pda: str,
    fetched_at: datetime,
    error: str | None = None,
) -> list[MemecoinStateFact]:
    base = [
        (
            MemecoinStateFact.observed(
                "bonding_curve_pda", pda, source=SOURCE, fetched_at=fetched_at
            )
            if pda != "Unavailable"
            else MemecoinStateFact.unavailable(
                "bonding_curve_pda", error or "invalid Solana mint", source=SOURCE
            )
        )
    ]
    names = (
        "pump_program_owned",
        "bonding_curve_exists",
        "virtual_token_reserves",
        "virtual_quote_reserves",
        "real_token_reserves",
        "real_quote_reserves",
        "token_total_supply",
        "curve_complete",
        "coin_creator",
        "is_mayhem_mode",
        "is_cashback_coin",
        "quote_mint",
        "creator_fee_bps",
        "is_holder_reward",
    )
    if decoded is None:
        base.extend(
            MemecoinStateFact.unavailable(
                name,
                error or "Pump BondingCurve account was not available",
                source=SOURCE,
                fetched_at=fetched_at,
            )
            for name in names
        )
        return base
    base.extend(
        [
            MemecoinStateFact.observed(
                "pump_program_owned", True, source=SOURCE, fetched_at=fetched_at
            ),
            MemecoinStateFact.observed(
                "bonding_curve_exists", True, source=SOURCE, fetched_at=fetched_at
            ),
        ]
    )
    for name in names[2:]:
        value = decoded.get(name)
        if value is None:
            base.append(
                MemecoinStateFact.unavailable(
                    name,
                    "field is absent from this shorter legacy account",
                    source=SOURCE,
                    fetched_at=fetched_at,
                )
            )
        else:
            unit = "atomic" if "reserves" in name or name == "token_total_supply" else None
            base.append(
                MemecoinStateFact.observed(
                    name, value, source=SOURCE, fetched_at=fetched_at, unit=unit
                )
            )
    return base
