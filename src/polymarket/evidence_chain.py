"""Acquire finalized CTF payout slots; token position mapping remains unverified."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

RPC_URL = "https://polygon-bor-rpc.publicnode.com"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
# Public mapping getters in Polymarket/conditional-tokens-contracts:
# payoutDenominator(bytes32), payoutNumerators(bytes32,uint256).
DENOMINATOR_SELECTOR = "0xdd34de67"
NUMERATOR_SELECTOR = "0x0504c814"
MAX_RPC_REQUESTS = 5


def _result(reply: Any) -> Any:
    if not isinstance(reply, dict) or reply.get("error") is not None:
        return None
    return reply.get("result")


def _word(value: Any) -> int | None:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        return None
    return int(value, 16)


def capture_payout_slots(rpc: Callable[[str, list], Any], condition_id: str) -> dict:
    """The caller records every RPC request/reply before returning decoded JSON.

    At most five requests. A finalized block number pins all contract reads.
    This records binary condition slots, never collateral/token payout mappings
    or historical redemption availability. Failures leave already captured raw
    evidence available for inspection.
    """
    base = {"condition_id": condition_id, "contract": CTF, "token_mapping": "unverified"}
    if not isinstance(condition_id, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", condition_id):
        return {**base, "status": "invalid_condition"}
    if _result(rpc("eth_chainId", [])) != "0x89":
        return {**base, "status": "wrong_chain"}
    block = _result(rpc("eth_getBlockByNumber", ["finalized", False]))
    if (
        not isinstance(block, dict)
        or not isinstance(block.get("number"), str)
        or not re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)", block["number"])
        or not isinstance(block.get("hash"), str)
        or not re.fullmatch(r"0x[0-9a-fA-F]{64}", block["hash"])
    ):
        return {**base, "status": "invalid_block"}
    base.update(chain_id=137, block_number=block["number"], block_hash=block["hash"])

    def call(data: str) -> int | None:
        return _word(_result(rpc("eth_call", [{"to": CTF, "data": data}, block["number"]])))

    denominator = call(DENOMINATOR_SELECTOR + condition_id[2:])
    if denominator is None:
        return {**base, "status": "invalid_denominator"}
    if denominator == 0:
        return {**base, "status": "unresolved", "denominator": 0}
    numerators = [call(NUMERATOR_SELECTOR + condition_id[2:] + f"{i:064x}") for i in range(2)]
    if any(n is None for n in numerators) or sum(numerators) != denominator:
        return {**base, "status": "invalid_vector"}
    return {
        **base,
        "status": "resolved_slots",
        "denominator": denominator,
        "numerators": numerators,
    }
