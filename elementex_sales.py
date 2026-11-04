#!/usr/bin/env python3
"""Decode executed NFT trades from Element's own exchange contract (ElementEx).

Why this exists
---------------
`market_sales.py` reads Seaport settlement events. On BNB Chain that turned out to
be a rounding error: 50k blocks produced 2 Seaport trades but 88 ElementEx fills.
ElementEx (`0xb3e3DfCb2d9f3DdE16d78B9e6EB3538Eb32B5ae1`) emits 0x-Protocol-v4
style events, which is where the BSC NFT market actually clears.

Confirmed event signatures (4byte.directory):

    ERC721SellOrderFilled(bytes32 orderHash, address maker, address taker,
        uint256 nonce, address erc20Token, uint256 erc20TokenAmount,
        (address,uint256)[] fees, address erc721Token, uint256 erc721TokenId)
        0x9c248aa1a265aa616f707b979d57f4529bb63a4fc34dc7fc61fdddc18410f74e

    ERC721BuyOrderFilled(...same shape...)
        0xd90a5c60975c6ff8eafcf02088e7b50ae5d9e156a79206ba553df1c4fb4594c2
        -> someone's resting *bid* got filled: proof of executable demand

    ERC721OrderCancelled(address,uint256)   0xa015ad2d...  (not a trade)
    ERC721SellOrderPreSigned(...)           0x29806076...  (not a trade)

A `SellOrderFilled` means a seller's ask was taken (maker = seller).
A `BuyOrderFilled` means a buyer's bid was taken (maker = bidder) - that is the
side we care about when asking "could we mint and immediately sell".

Usage
-----
    python elementex_sales.py --blocks 45000
    python elementex_sales.py --blocks 45000 --contract 0x4addbd...
    python elementex_sales.py --blocks 45000 --json-out elementex.json

Measured on 2026-11-04: a ~50,000-block window (~6.2 h at 0.45 s/block) returned 89
fills across 10 collections in about 6 seconds. The same chain time on Seaport
returned 1-2 trades, which is why `market_sales.py` on its own reads as "nobody
trades on BSC".

Window caveat: the free public endpoints only serve roughly the last 50,000 blocks of
logs (publicnode answers older ranges with `[]`, not an error). `fetch_fills` measures
that limit and clamps the window, so `window=` in the output is what was actually read.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import chainlib as cl  # noqa: E402

ELEMENT_EX = "0xb3e3DfCb2d9f3DdE16d78B9e6EB3538Eb32B5ae1"
SELL_FILLED = "0x9c248aa1a265aa616f707b979d57f4529bb63a4fc34dc7fc61fdddc18410f74e"
BUY_FILLED = "0xd90a5c60975c6ff8eafcf02088e7b50ae5d9e156a79206ba553df1c4fb4594c2"
ZERO_ADDR = "0x" + "00" * 32

LOG_RPCS = [
    "https://bsc.publicnode.com",
    "https://bsc-dataseed.bnbchain.org",
    "https://rpc.ankr.com/bsc",
]


def word(data: bytes, index: int) -> bytes:
    return data[index * 32 : (index + 1) * 32]


def as_uint(data: bytes, index: int) -> int:
    chunk = word(data, index)
    return int.from_bytes(chunk, "big") if len(chunk) == 32 else 0


def as_address(data: bytes, index: int) -> str:
    chunk = word(data, index)
    return "0x" + chunk[12:32].hex() if len(chunk) == 32 else ""


class Assets:
    """Cache token symbol/decimals.

    Element uses two sentinels for native BNB in `erc20Token`: the zero address and
    0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee. Neither has metadata on chain, and an
    empty eth_call reply must not be read as decimals == 0 (that inflated every BNB
    price by 1e18 on the first run).
    """

    NATIVE = {
        "0x0000000000000000000000000000000000000000": {"symbol": "BNB", "decimals": 18},
        "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee": {"symbol": "BNB", "decimals": 18},
    }

    def __init__(self, rpc):
        self.rpc = rpc
        self.cache: dict[str, dict] = dict(self.NATIVE)

    def get(self, address: str) -> dict:
        key = (address or "").lower()
        if key in self.cache:
            return self.cache[key]
        meta = {"symbol": None, "decimals": None}
        for sig in ("symbol()", "decimals()"):
            try:
                raw = self.rpc.eth_call(key, cl.encode_call(sig, []))
                body = bytes.fromhex(raw[2:]) if raw and raw != "0x" else b""
                if sig.startswith("symbol"):
                    if len(body) < 64:
                        continue
                    length = int.from_bytes(body[32:64], "big")
                    meta["symbol"] = body[64 : 64 + length].decode("utf-8", "replace")
                elif len(body) >= 32:
                    meta["decimals"] = int.from_bytes(body[:32], "big")
            except Exception:  # noqa: BLE001
                pass
        meta["symbol"] = meta["symbol"] or key[:10]
        meta["decimals"] = 18 if meta["decimals"] is None else meta["decimals"]
        self.cache[key] = meta
        return meta


def decode_fill(log: dict, side: str, assets: Assets) -> dict | None:
    """ERC721{Sell,Buy}OrderFilled -> one trade row."""
    data = bytes.fromhex((log.get("data") or "0x")[2:])
    if len(data) < 9 * 32:
        return None
    erc20 = as_address(data, 4)
    amount = as_uint(data, 5)
    meta = assets.get(erc20)
    nft_contract = as_address(data, 7)
    token_id = as_uint(data, 8)
    if not nft_contract:
        return None
    # fees array (royalties + protocol fee) sits behind an offset at word 6
    fees = []
    offset = as_uint(data, 6)
    if 0 < offset <= len(data):
        base = offset
        if base + 32 <= len(data):
            count = int.from_bytes(data[base : base + 32], "big")
            for i in range(min(count, 6)):
                start = base + 32 + i * 64
                if start + 64 > len(data):
                    break
                fees.append(
                    {
                        "recipient": "0x" + data[start + 12 : start + 32].hex(),
                        "amount": int.from_bytes(data[start + 32 : start + 64], "big"),
                    }
                )
    return {
        "side": side,
        "order_hash": "0x" + data[0:32].hex(),
        "maker": as_address(data, 1),
        "taker": as_address(data, 2),
        "nonce": as_uint(data, 3),
        "payment_token": erc20,
        "payment_symbol": meta["symbol"],
        "payment_decimals": meta["decimals"],
        "payment_raw": amount,
        "payment": amount / 10 ** meta["decimals"] if meta["decimals"] is not None else None,
        "nft_contract": nft_contract,
        "token_id": token_id,
        "fees": fees,
        "tx": log.get("transactionHash"),
        "block": int(str(log.get("blockNumber")), 16),
    }


def fetch_fills(blocks: int, chunk: int = 10000, timeout: float = 40.0):
    """Returns (head, rpc, rows, source, effective_blocks).

    `effective_blocks` is clamped to what the endpoint can actually still serve: some
    providers answer old `eth_getLogs` ranges with an empty list instead of an error, so
    a 300,000-block request would silently become ~50,000 blocks and every total would
    be wrong. `cl.log_retention_blocks` measures the real limit first.
    """
    head = cl.BnbRpc(None, timeout=20.0).block_number()
    last_error = ""
    for url in LOG_RPCS:
        rpc = cl.BnbRpc([url], timeout=timeout)
        assets = Assets(rpc)
        retention = cl.log_retention_blocks(rpc, head)
        effective = blocks
        if retention and blocks > retention:
            effective = retention
            print(
                f"note: {url} keeps only ~{retention:,} blocks of logs "
                f"(~{retention * 0.45 / 3600:.1f}h); window {blocks:,} -> {effective:,}",
                flush=True,
            )
        floor = max(0, head - effective + 1)
        rows: list[dict] = []
        cursor = head
        ok = True
        while cursor >= floor:
            start = max(floor, cursor - chunk + 1)
            try:
                logs = rpc.call(
                    "eth_getLogs",
                    [
                        {
                            "address": ELEMENT_EX,
                            "fromBlock": hex(start),
                            "toBlock": hex(cursor),
                            "topics": [[SELL_FILLED, BUY_FILLED]],
                        }
                    ],
                ) or []
            except cl.RpcError as exc:
                last_error = f"{url}: {str(exc)[:90]}"
                ok = False
                break
            for log in logs:
                topic0 = (log.get("topics") or [""])[0]
                side = "sell" if topic0 == SELL_FILLED else "buy"
                row = decode_fill(log, side, assets)
                if row:
                    rows.append(row)
            cursor = start - 1
        if ok:
            return head, rpc, rows, url, effective
    return None, None, [], last_error, 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=int, default=100000)
    ap.add_argument("--chunk", type=int, default=10000)
    ap.add_argument("--contract", help="only report trades for this NFT contract")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    started = time.time()
    head, rpc, rows, source, effective = fetch_fills(args.blocks, args.chunk)
    if head is None:
        print(f"all RPCs failed: {rows if rows else ''}{source}", file=sys.stderr)
        return 1

    if args.contract:
        target = args.contract.lower()
        rows = [r for r in rows if r["nft_contract"].lower() == target]

    span = ""
    if rows:
        blocks_span = max(r["block"] for r in rows) - min(r["block"] for r in rows)
        span = f", trades span {blocks_span * 0.45 / 3600:.2f}h"
    print(
        f"head={head} window={effective} blocks fills={len(rows)} "
        f"source={source}{span} ({time.time() - started:.0f}s)"
    )

    agg = defaultdict(lambda: {"trades": 0, "sell": 0, "buy": 0, "paid": defaultdict(float), "best": (0.0, "")})
    for row in rows:
        entry = agg[row["nft_contract"].lower()]
        entry["trades"] += 1
        entry["sell" if row["side"] == "sell" else "buy"] += 1
        if row["payment"]:
            entry["paid"][row["payment_symbol"]] += row["payment"]
            if row["payment"] > entry["best"][0]:
                entry["best"] = (row["payment"], row["payment_symbol"])

    print(f"\n{'collection':<44}{'fills':>6}{'sell':>6}{'bid':>5}{'best':>14}  paid total")
    ranked = sorted(agg.items(), key=lambda kv: kv[1]["trades"], reverse=True)[: args.top]
    for contract, entry in ranked:
        money = " + ".join(f"{v:,.6g} {k}" for k, v in sorted(entry["paid"].items())) or "-"
        best = f"{entry['best'][0]:,.6g} {entry['best'][1]}".strip()
        print(f"{contract:<44}{entry['trades']:>6}{entry['sell']:>6}{entry['buy']:>5}{best:>14}  {money}")

    bid_fills = [r for r in rows if r["side"] == "buy"]
    print(f"\nbid-side fills (a resting offer got taken): {len(bid_fills)}")
    for row in bid_fills[:10]:
        price = f"{row['payment']:.6g} {row['payment_symbol']}" if row["payment"] else "?"
        print(f"  {row['nft_contract']} #{row['token_id']} <- {price} (block {row['block']})")

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps({"head": head, "window_blocks": args.blocks, "trades": rows}, indent=2),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
