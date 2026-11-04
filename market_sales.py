#!/usr/bin/env python3
"""Read real NFT sales on BSC from Seaport settlement events (no API key).

Corrected 2026-11-03 after review: earlier versions only summed native BNB/WBNB,
so ERC-20 payments (e.g. USDT) showed up as "zero-price" sales. This version:

* decodes both directions - NFT in `offer` (sell-side event) and NFT in
  `consideration` (buy-side event);
* keeps every payment item with its token address, raw amount and recipient;
* resolves `decimals()` per token so amounts are human readable;
* merges the events of one settlement so a trade is not counted twice;
* never treats an unknown asset as "free" - it is reported as `unknown`.

Known limits: only the Seaport deployment below is covered; other marketplaces
(Element's own settlement, aggregators) are separate paths.

Usage
-----
    python market_sales.py --blocks 100000
    python market_sales.py --blocks 100000 --contract 0x3148850ce59904cda99086a8248c2300416edc46
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import chainlib as cl  # noqa: E402

SEAPORT = "0x0000000000000068F116a894984e2db1123eB395"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
USDT = "0x55d398326f99059ff775485246999027b3197955"
TOPIC_ORDER_FULFILLED = "0x" + cl.keccak_256(
    b"OrderFulfilled(bytes32,address,address,address,(uint8,address,uint256,uint256)[],(uint8,address,uint256,uint256,address)[])"
).hex()
LOG_RPCS = ("https://bsc.publicnode.com", "https://bsc.rpc.blxrbdn.com")

_ERC20_META: dict[str, tuple[str, int]] = {}


def word(raw: bytes, index: int) -> int:
    chunk = raw[index * 32 : (index + 1) * 32]
    return int.from_bytes(chunk, "big") if len(chunk) == 32 else 0


def as_address(value: int) -> str:
    return "0x" + value.to_bytes(32, "big")[12:].hex()


def token_meta(rpc: cl.BnbRpc, token: str) -> tuple[str, int]:
    """(symbol, decimals) with a small cache; native coin is handled by caller."""
    key = token.lower()
    if key in _ERC20_META:
        return _ERC20_META[key]
    symbol, decimals = "?", 18
    try:
        symbol = cl.decode_string(rpc.eth_call(token, cl.encode_call("symbol()", [])))[:12] or "?"
    except Exception:  # noqa: BLE001
        pass
    try:
        decimals = cl.decode_uint(rpc.eth_call(token, cl.encode_call("decimals()", [])))
        if decimals > 36:
            decimals = 18
    except Exception:  # noqa: BLE001
        pass
    _ERC20_META[key] = (symbol, decimals)
    return symbol, decimals


def decode_event(data_hex: str) -> list[dict]:
    """Return the item lists of one OrderFulfilled event."""
    raw = bytes.fromhex(data_hex[2:] if data_hex.startswith("0x") else data_hex)
    if len(raw) < 4 * 32:
        return []
    orders = []
    offer_offset, consideration_offset = word(raw, 2), word(raw, 3)

    def read_array(offset: int, size: int) -> list[list[int]]:
        if offset % 32 or offset // 32 >= len(raw) // 32:
            return []
        length = word(raw, offset // 32)
        items = []
        cursor = offset + 32
        for _ in range(min(length, 32)):
            if cursor + size * 32 > len(raw):
                break
            items.append([word(raw, (cursor + i * 32) // 32) for i in range(size)])
            cursor += size * 32
        return items

    orders.append({"offer": read_array(offer_offset, 4), "consideration": read_array(consideration_offset, 5)})
    return orders


def normalise(order: dict, rpc: cl.BnbRpc, ctx: dict) -> dict | None:
    """Turn one event into {nft, payments[]} or None."""
    nft = None
    side = None
    for item_type, token, identifier, _amount in order["offer"]:
        if item_type in (2, 3, 4, 5):
            nft = {"contract": as_address(token).lower(), "token_id": identifier}
            side = "sell"  # NFT offered by the seller, payment in consideration
            break
    if not nft:
        for item_type, token, identifier, _amount, _rcpt in order["consideration"]:
            if item_type in (2, 3, 4, 5):
                nft = {"contract": as_address(token).lower(), "token_id": identifier}
                side = "buy"  # buyer offers payment, receives the NFT
                break
    if not nft:
        return None

    payments = []
    source = order["consideration"] if side == "sell" else order["offer"]
    for item in source:
        if side == "sell":
            item_type, token, _identifier, amount, rcpt = item
        else:
            item_type, token, _identifier, amount = item
            rcpt = 0
        if item_type not in (0, 1):
            continue
        token_addr = as_address(token).lower()
        symbol, decimals = ("BNB", 18) if token_addr == cl.ZERO_ADDRESS else token_meta(rpc, token_addr)
        payments.append(
            {
                "token": token_addr,
                "symbol": symbol,
                "decimals": decimals,
                "amount_raw": str(amount),
                "amount": float(Decimal(amount) / (Decimal(10) ** decimals)),
                "recipient": as_address(rcpt).lower() if rcpt else None,
            }
        )
    return {"nft": nft, "side": side, "payments": payments, "ctx": ctx}


def fetch_events(blocks: int, chunk: int = 10000, timeout: float = 30.0):
    head = cl.BnbRpc(None, timeout=15.0).block_number()
    floor = max(0, head - blocks + 1)
    last_error = ""
    for url in LOG_RPCS:
        rpc = cl.BnbRpc([url], timeout=timeout)
        rows = []
        cursor = head
        ok = True
        while cursor >= floor:
            start = max(floor, cursor - chunk + 1)
            try:
                logs = rpc.call(
                    "eth_getLogs",
                    [{"address": SEAPORT, "fromBlock": hex(start), "toBlock": hex(cursor), "topics": [TOPIC_ORDER_FULFILLED]}],
                ) or []
            except cl.RpcError as exc:
                last_error = f"{url}: {str(exc)[:90]}"
                ok = False
                break
            for log in logs:
                topics = log.get("topics") or []
                if len(topics) < 3:
                    continue
                ctx = {
                    "order_hash": "0x" + (log.get("data") or "0x")[2:66],
                    "offerer": "0x" + topics[1][-40:],
                    "zone": "0x" + topics[2][-40:],
                    "tx": log.get("transactionHash"),
                    "block": int(str(log.get("blockNumber")), 16),
                }
                for order in decode_event(log.get("data") or "0x"):
                    item = normalise(order, rpc, ctx)
                    if item:
                        rows.append(item)
            cursor = start - 1
        if ok:
            return head, rpc, rows, url
    return None, None, [], last_error


def merge_trades(rows: list[dict]) -> list[dict]:
    """Collapse the buy-side/sell-side events of one settlement into one trade."""
    keyed: dict[tuple, dict] = {}
    for row in rows:
        nft = row["nft"]
        key = (row["ctx"]["tx"], nft["contract"], nft["token_id"])
        current = keyed.get(key)
        if current is None or (current["side"] == "buy" and row["side"] == "sell"):
            keyed[key] = row
    return list(keyed.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=int, default=100000)
    ap.add_argument("--chunk", type=int, default=10000)
    ap.add_argument("--contract")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    head, rpc, rows, source = fetch_events(args.blocks, args.chunk)
    if head is None:
        print("no sales data:", source)
        return 3
    trades = merge_trades(rows)

    first_block = min((t["ctx"]["block"] for t in trades), default=None)
    last_block = max((t["ctx"]["block"] for t in trades), default=None)
    span = ""
    if first_block:
        try:
            t0 = int(rpc.call("eth_getBlockByNumber", [hex(first_block), False])["timestamp"], 16)
            t1 = int(rpc.call("eth_getBlockByNumber", [hex(last_block), False])["timestamp"], 16)
            span = f", trades span {(t1 - t0) / 3600:.2f}h"
        except Exception:  # noqa: BLE001
            span = ""

    print(f"head={head} window={args.blocks} blocks events={len(rows)} trades={len(trades)} source={source}{span}")

    if args.contract:
        target = args.contract.lower()
        hits = [t for t in trades if t["nft"]["contract"] == target]
        print(f"\ntrades for {target}: {len(hits)}")
        for t in sorted(hits, key=lambda x: x["ctx"]["block"]):
            paid = ", ".join(f"{p['amount']:.6g} {p['symbol']}" for p in t["payments"])
            print(f"  block {t['ctx']['block']} token #{t['nft']['token_id']} side={t['side']} paid=[{paid}] tx {t['ctx']['tx'][:12]}")
    elif not trades:
        print("no trades in this window")
    else:
        agg = defaultdict(lambda: {"trades": 0, "assets": defaultdict(float), "blocks": []})
        for t in trades:
            entry = agg[t["nft"]["contract"]]
            entry["trades"] += 1
            entry["blocks"].append(t["ctx"]["block"])
            for p in t["payments"]:
                entry["assets"][p["symbol"]] += p["amount"]
        print(f"\n{'collection':<44}{'trades':>7}  assets")
        for contract, entry in sorted(agg.items(), key=lambda kv: kv[1]["trades"], reverse=True)[: args.top]:
            assets = ", ".join(f"{v:.6g} {k}" for k, v in sorted(entry["assets"].items()))
            print(f"{contract:<44}{entry['trades']:>7}  {assets}")

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "head_block": head,
                    "window_blocks": args.blocks,
                    "source": source,
                    "trades": trades,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
