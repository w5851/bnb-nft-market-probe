#!/usr/bin/env python3
"""Read-only BNB Smart Chain JSON-RPC helpers (stdlib only).

Design boundary
---------------
This module never signs, never holds keys, and never broadcasts a transaction.
It only performs read-only JSON-RPC calls (eth_chainId, eth_getCode,
eth_call, eth_estimateGas, eth_getBalance, eth_getStorageAt, ...).

Everything here is stdlib-only so it can run on a bare Windows Python install.

Run `python chainlib.py --selftest` for offline crypto/ABI checks.
"""

from __future__ import annotations

import http.client
import json
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Any, Iterable, Sequence

CHAIN_ID_BSC = 56
ZERO_ADDRESS = "0x" + "00" * 20
MASK64 = (1 << 64) - 1

DEFAULT_RPC_URLS = (
    "https://bsc-dataseed.bnbchain.org",
    "https://bsc-dataseed1.defibit.io",
    "https://bsc-dataseed2.bnbchain.org",
    "https://bsc-dataseed3.binance.org",
    "https://bsc.blockrazor.xyz",
    "https://bsc.nodereal.io",
)

WEI_PER_BNB = Decimal(10) ** 18


# ---------------------------------------------------------------------------
# Keccak-256 (needed for selectors; hashlib only ships SHA3, which differs)
# ---------------------------------------------------------------------------
def _rotl(value: int, shift: int) -> int:
    shift %= 64
    if shift == 0:
        return value & MASK64
    return ((value << shift) | (value >> (64 - shift))) & MASK64


def _rc_bit(t: int) -> int:
    """Bit of the round-constant LFSR (Keccak reference rc(t))."""
    t %= 255
    if t == 0:
        return 1
    r = 1
    for _ in range(t):
        r = ((r << 1) ^ (((r >> 7) & 1) * 0x71)) & 0xFF
    return r & 1


def _round_constants() -> list[int]:
    constants = []
    for i in range(24):
        rc = 0
        for j in range(7):
            if _rc_bit(j + 7 * i):
                rc |= 1 << ((1 << j) - 1)
        constants.append(rc)
    return constants


_ROUND_CONSTANTS = _round_constants()


def _rotation_offsets() -> list[list[int]]:
    """rho offsets indexed as [x][y] per the Keccak specification."""
    offsets = [[0] * 5 for _ in range(5)]
    x, y = 1, 0
    for t in range(24):
        offsets[x][y] = ((t + 1) * (t + 2) // 2) % 64
        x, y = y, (2 * x + 3 * y) % 5
    return offsets


_ROTATION_OFFSETS = _rotation_offsets()


def _keccak_f1600(state: list[int]) -> None:
    for rnd in range(24):
        # theta
        columns = [
            state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20]
            for x in range(5)
        ]
        deltas = [
            columns[(x - 1) % 5] ^ _rotl(columns[(x + 1) % 5], 1) for x in range(5)
        ]
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] ^= deltas[x]

        # rho + pi
        lanes = [0] * 25
        for x in range(5):
            for y in range(5):
                lanes[y + 5 * ((2 * x + 3 * y) % 5)] = _rotl(
                    state[x + 5 * y], _ROTATION_OFFSETS[x][y]
                )

        # chi
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] = (
                    lanes[x + 5 * y]
                    ^ ((~lanes[(x + 1) % 5 + 5 * y] & MASK64) & lanes[(x + 2) % 5 + 5 * y])
                )

        # iota
        state[0] ^= _ROUND_CONSTANTS[rnd]


def keccak_256(data: bytes) -> bytes:
    rate = 136  # 1088 bits
    padded = bytearray(data)
    pad_len = rate - (len(padded) % rate)
    if pad_len == 1:
        padded += b"\x81"
    else:
        padded += b"\x01" + b"\x00" * (pad_len - 2) + b"\x80"

    state = [0] * 25
    for offset in range(0, len(padded), rate):
        block = padded[offset : offset + rate]
        for i in range(rate // 8):
            state[i] ^= int.from_bytes(block[i * 8 : (i + 1) * 8], "little")
        _keccak_f1600(state)

    return b"".join(state[i].to_bytes(8, "little") for i in range(4))


def selector(signature: str) -> str:
    return "0x" + keccak_256(signature.encode()).hex()[:8]


# ---------------------------------------------------------------------------
# Minimal ABI encoding for static types (enough for mint-like calls)
# ---------------------------------------------------------------------------
def _parse_types(signature: str) -> list[str]:
    start = signature.find("(")
    end = signature.rfind(")")
    if start < 0 or end < start:
        raise ValueError(f"bad function signature: {signature!r}")
    inner = signature[start + 1 : end].strip()
    if not inner:
        return []
    if "(" in inner or ")" in inner:
        raise ValueError(
            "tuple/array arguments are not supported; pass raw --calldata instead"
        )
    return [part.strip() for part in inner.split(",")]


def encode_arg(type_name: str, value: Any) -> bytes:
    t = type_name.strip().lower()
    if t == "address":
        text = str(value).lower()
        if not text.startswith("0x") or len(text) != 42:
            raise ValueError(f"bad address value: {value!r}")
        return bytes(12) + bytes.fromhex(text[2:])
    if t == "bool":
        return (1 if value else 0).to_bytes(32, "big")
    if t.startswith("uint"):
        number = int(value)
        if number < 0:
            raise ValueError("negative uint")
        return number.to_bytes(32, "big")
    if t.startswith("int"):
        number = int(value)
        return number.to_bytes(32, "big", signed=True)
    if t.startswith("bytes") and t != "bytes":
        size = int(t[5:])
        raw = bytes.fromhex(str(value)[2:] if str(value).startswith("0x") else str(value))
        if len(raw) > size:
            raise ValueError(f"{type_name} too long")
        return raw.ljust(32, b"\x00")
    raise ValueError(f"unsupported static type: {type_name}")


def encode_call(signature: str, args: Sequence[Any] = ()) -> str:
    types = _parse_types(signature)
    if len(types) != len(args):
        raise ValueError(
            f"signature expects {len(types)} args, got {len(args)}: {signature}"
        )
    payload = bytes.fromhex(selector(signature)[2:])
    for type_name, value in zip(types, args):
        payload += encode_arg(type_name, value)
    return "0x" + payload.hex()


# ---------------------------------------------------------------------------
# Decoding helpers (ABI return data)
# ---------------------------------------------------------------------------
def _as_bytes(hex_data: str) -> bytes:
    text = hex_data[2:] if hex_data.startswith("0x") else hex_data
    if len(text) % 2:
        text = "0" + text
    return bytes.fromhex(text) if text else b""


def decode_uint(hex_data: str) -> int:
    raw = _as_bytes(hex_data)
    if len(raw) < 32:
        raise ValueError("return data shorter than 32 bytes")
    return int.from_bytes(raw[:32], "big")


def decode_bool(hex_data: str) -> bool:
    return decode_uint(hex_data) != 0


def decode_address(hex_data: str) -> str:
    raw = _as_bytes(hex_data)
    if len(raw) < 32:
        raise ValueError("return data shorter than 32 bytes")
    return "0x" + raw[12:32].hex()


def decode_bytes32(hex_data: str) -> str:
    raw = _as_bytes(hex_data)
    if len(raw) < 32:
        raise ValueError("return data shorter than 32 bytes")
    return "0x" + raw[:32].hex()


def decode_string(hex_data: str) -> str:
    """Decode a solidity `string` return value (offset, length, payload)."""
    raw = _as_bytes(hex_data)
    if len(raw) >= 64:
        offset = int.from_bytes(raw[:32], "big")
        if offset + 32 <= len(raw):
            length = int.from_bytes(raw[offset : offset + 32], "big")
            body = raw[offset + 32 : offset + 32 + length]
            if len(body) == length:
                try:
                    return body.decode("utf-8")
                except UnicodeDecodeError:
                    return body.decode("utf-8", "replace")
    # Some contracts return bytes32-style strings.
    text = raw[:32].rstrip(b"\x00")
    return text.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# RPC client
# ---------------------------------------------------------------------------
class RpcError(RuntimeError):
    """JSON-RPC error or transport failure."""

    def __init__(self, message: str, code: int | None = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data

    @property
    def is_revert(self) -> bool:
        text = str(self).lower()
        return "revert" in text or "execution reverted" in text or bool(self.data)


class BnbRpc:
    def __init__(self, urls: Sequence[str] | None = None, timeout: float = 20.0):
        urls = [u for u in (urls or DEFAULT_RPC_URLS) if u]
        if not urls:
            raise RpcError("no RPC URL configured")
        self.urls = urls
        self.timeout = timeout
        self._request_id = 0
        self._active = 0
        self.last_endpoint: str | None = None

    # -- transport ---------------------------------------------------------
    def _post(self, url: str, payload: Any, timeout: float | None = None) -> Any:
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "bnb-nft-market-probe/1.0 (read-only analytics)",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout or self.timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
        return json.loads(text)

    def _rotate(self) -> None:
        self._active = (self._active + 1) % len(self.urls)

    def call(self, method: str, params: Sequence[Any] | None = None) -> Any:
        params = list(params or [])
        errors: list[str] = []
        rounds = 2
        for attempt in range(len(self.urls) * rounds):
            if attempt >= len(self.urls):
                time.sleep(0.2)
            url = self.urls[(self._active + attempt) % len(self.urls)]
            self._request_id += 1
            payload = {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params,
            }
            try:
                reply = self._post(url, payload)
            except (urllib.error.URLError, urllib.error.HTTPError, http.client.HTTPException, TimeoutError, OSError, ValueError) as exc:
                errors.append(f"{url}: {exc}")
                continue
            if isinstance(reply, dict) and reply.get("error"):
                error = reply["error"]
                raise RpcError(
                    f"{method} failed on {url}: {error.get('message')}",
                    code=error.get("code"),
                    data=error.get("data"),
                )
            self.last_endpoint = url
            return reply.get("result")
        raise RpcError("all RPC endpoints failed: " + " | ".join(errors))

    def batch(self, calls: Iterable[tuple[str, Sequence[Any]]]) -> list[dict[str, Any]]:
        """Send a JSON-RPC batch. Individual items may carry an `error` key."""
        calls = list(calls)
        if not calls:
            return []
        base = self._request_id
        payload = []
        for index, (method, params) in enumerate(calls, start=1):
            payload.append(
                {
                    "jsonrpc": "2.0",
                    "id": base + index,
                    "method": method,
                    "params": list(params),
                }
            )
        self._request_id = base + len(calls)

        errors: list[str] = []
        rounds = 2
        for attempt in range(len(self.urls) * rounds):
            if attempt >= len(self.urls):
                time.sleep(0.2)
            url = self.urls[(self._active + attempt) % len(self.urls)]
            try:
                reply = self._post(url, payload)
            except (urllib.error.URLError, urllib.error.HTTPError, http.client.HTTPException, TimeoutError, OSError, ValueError) as exc:
                errors.append(f"{url}: {exc}")
                continue
            if not isinstance(reply, list) or not reply:
                errors.append(f"{url}: batch not supported")
                continue
            first = reply[0]
            if isinstance(first, dict) and first.get("id") is None and first.get("error"):
                message = first["error"].get("message") if isinstance(first["error"], dict) else str(first["error"])
                errors.append(f"{url}: batch-level error: {message}")
                continue
            by_id = {
                item.get("id"): item
                for item in reply
                if isinstance(item, dict) and item.get("id") is not None
            }
            self.last_endpoint = url
            return [by_id.get(base + i, {"error": {"message": "missing batch result"}}) for i in range(1, len(calls) + 1)]
        raise RpcError("all RPC endpoints failed: " + " | ".join(errors))

    # -- convenience -------------------------------------------------------
    def chain_id(self) -> int:
        return int(self.call("eth_chainId"), 16)

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber"), 16)

    def gas_price_wei(self) -> int:
        return int(self.call("eth_gasPrice"), 16)

    def balance_wei(self, address: str) -> int:
        return int(self.call("eth_getBalance", [address, "latest"]), 16)

    def nonce(self, address: str) -> int:
        return int(self.call("eth_getTransactionCount", [address, "latest"]), 16)

    def code(self, address: str) -> str:
        return self.call("eth_getCode", [address, "latest"]) or "0x"

    def storage_at(self, address: str, slot: str) -> str:
        return self.call("eth_getStorageAt", [address, slot, "latest"]) or "0x"

    def eth_call(
        self,
        to: str,
        data: str,
        from_address: str | None = None,
        value: int = 0,
        gas: int | None = None,
    ) -> str:
        tx: dict[str, Any] = {"to": to, "data": data, "value": hex(value)}
        if from_address:
            tx["from"] = from_address
        if gas:
            tx["gas"] = hex(gas)
        return self.call("eth_call", [tx, "latest"]) or "0x"

    def estimate_gas(
        self, to: str, data: str, from_address: str, value: int = 0
    ) -> int:
        tx = {"to": to, "data": data, "from": from_address, "value": hex(value)}
        return int(self.call("eth_estimateGas", [tx]), 16)


# ---------------------------------------------------------------------------
# Log retention probe
# ---------------------------------------------------------------------------
USDT_BSC = "0x55d398326f99059fF775485246999027B3197955"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def log_retention_blocks(rpc: "BnbRpc", head: int, low: int = 12_000, high: int = 400_000, span: int = 10) -> int:
    """How many blocks back `rpc` can still serve `eth_getLogs` for.

    Some public BSC endpoints answer old `eth_getLogs` ranges with an empty list instead
    of an error: no warning, just `[]`. A 300,000-block request then silently becomes a
    ~50,000 block scan, and every total computed from it is wrong without looking wrong.

    Probing a high-traffic token makes the limit measurable, because USDT transfers are
    never idle for ten consecutive BSC blocks. Measured 2026-11-04: depth 50,000 returned
    ~1,500 logs per 50 blocks, depth 52,500 returned 0 - i.e. ~6.2 h of history.

    Returns the oldest depth (in blocks below `head`) that still returns logs, or 0 if
    even `low` is already beyond retention.
    """

    def alive(depth: int) -> bool:
        end = head - depth
        start = end - span + 1
        try:
            logs = rpc.call(
                "eth_getLogs",
                [
                    {
                        "address": USDT_BSC,
                        "fromBlock": hex(start),
                        "toBlock": hex(end),
                        "topics": [TRANSFER_TOPIC],
                    }
                ],
            )
        except RpcError:
            return False
        return bool(logs)

    if not alive(low):
        return 0
    lo, hi = low, high
    while hi - lo > 2_000:
        mid = (lo + hi) // 2
        if alive(mid):
            lo = mid
        else:
            hi = mid
    return lo


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def wei_to_bnb(wei: int) -> Decimal:
    return Decimal(wei) / WEI_PER_BNB


def format_bnb(wei: int) -> str:
    return f"{wei_to_bnb(wei):.9f}".rstrip("0").rstrip(".")


def is_address(value: str) -> bool:
    text = (value or "").strip()
    if not text.startswith("0x") or len(text) != 42:
        return False
    try:
        bytes.fromhex(text[2:])
    except ValueError:
        return False
    return True


def short(value: str, head: int = 6, tail: int = 4) -> str:
    if not value or len(value) <= head + tail + 2:
        return value
    return f"{value[: head + 2]}...{value[-tail:]}"


# ---------------------------------------------------------------------------
# Self-test (offline)
# ---------------------------------------------------------------------------
def _selftest() -> int:
    failures: list[str] = []

    def check(name: str, actual: Any, expected: Any) -> None:
        if actual != expected:
            failures.append(f"{name}: got {actual!r}, expected {expected!r}")

    check(
        "keccak256('')",
        keccak_256(b"").hex(),
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470",
    )
    check(
        "keccak256('abc')",
        keccak_256(b"abc").hex(),
        "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45",
    )
    selectors = {
        "mint(uint256)": "0xa0712d68",
        "mint()": "0x1249c58b",
        "approve(address,uint256)": "0x095ea7b3",
        "setApprovalForAll(address,bool)": "0xa22cb465",
        "transfer(address,uint256)": "0xa9059cbb",
        "transferFrom(address,address,uint256)": "0x23b872dd",
        "safeTransferFrom(address,address,uint256)": "0x42842e0e",
        "safeTransferFrom(address,address,uint256,bytes)": "0xb88d4fde",
        "permit(address,address,uint256,uint256,uint8,bytes32,bytes32)": "0xd505accf",
        "claim()": "0x4e71d92d",
        "totalSupply()": "0x18160ddd",
        "owner()": "0x8da5cb5b",
        "balanceOf(address)": "0x70a08231",
        "name()": "0x06fdde03",
        "symbol()": "0x95d89b41",
        "paused()": "0x5c975abb",
        "maxSupply()": "0xd5abeb01",
        "mintPrice()": "0x6817c76c",
        "price()": "0xa035b1fe",
        "getPrice()": "0x98d5fdca",
        "cost()": "0x13faede6",
    }
    for signature, expected in selectors.items():
        check(f"selector {signature}", selector(signature), expected)

    encoded = encode_call(
        "mint(uint256)", [1]
    )
    check(
        "encode mint(1)",
        encoded,
        "0xa0712d68" + "0" * 63 + "1",
    )
    wallet = "0x1111111111111111111111111111111111111111"
    encoded = encode_call("balanceOf(address)", [wallet])
    check("encode balanceOf", encoded, "0x70a08231" + "0" * 24 + "11" * 20)

    check("decode_uint", decode_uint("0x" + "0" * 63 + "f"), 15)
    check("decode_bool", decode_bool("0x" + "0" * 64), False)
    check(
        "decode_address",
        decode_address("0x" + "0" * 24 + "ab" * 20),
        "0x" + "ab" * 20,
    )
    string_payload = (
        "0x"
        + (32).to_bytes(32, "big").hex()
        + (3).to_bytes(32, "big").hex()
        + b"abc".hex().ljust(64, "0")
    )
    check("decode_string", decode_string(string_payload), "abc")

    if failures:
        print("SELFTEST FAILED")
        for failure in failures:
            print(" -", failure)
        return 1
    print(f"SELFTEST OK ({len(selectors)} selectors + keccak + ABI + decoders)")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print(__doc__)
    print("usage: python scripts/chainlib.py --selftest")

