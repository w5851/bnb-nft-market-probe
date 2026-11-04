#!/usr/bin/env python3
"""Cursor-based watch for new NFT mints on BNB Smart Chain (read-only).

Every cycle walks a *contiguous* range of blocks instead of only the newest few,
so coverage can be measured instead of guessed:

    * state file remembers `last_scanned`, failed ranges (`pending`) and totals
    * failed chunks are queued and retried on the next cycle
    * each contract found in the window gets a name / supply / free-mint check
      and an Element indexing check (server-rendered page title)

Usage
-----
    python mint_watch.py --cycles 1 --max-blocks-per-cycle 600
    python mint_watch.py --cycles 20 --max-blocks-per-cycle 1200 --state state.json

No key, no signature, no broadcast. Stdlib only.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import chainlib as cl  # noqa: E402

TOPIC_TRANSFER = "0x" + cl.keccak_256(b"Transfer(address,address,uint256)").hex()
TOPIC_TRANSFER_SINGLE = "0x" + cl.keccak_256(
    b"TransferSingle(address,address,address,uint256,uint256)"
).hex()
ZERO_TOPIC = "0x" + "00" * 32
FRESH = "0x00000000000000000000000000000000C0FFEE01"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/147.0 Safari/537.36"
LOG_RPCS = ("https://bsc.blockrazor.xyz", "https://bsc.publicnode.com")


def load_state(path: Path) -> dict:
    base = {"last_scanned": None, "pending": [], "started_at_block": None, "covered": 0, "cycles": 0}
    if path.exists():
        try:
            base.update({k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if k in base})
        except json.JSONDecodeError:
            pass
    return base


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def scan_range(rpc: cl.BnbRpc, start: int, end: int, chunk: int, sleep: float):
    """Return (counts, failed_ranges, covered_blocks) for [start, end]."""
    counts: dict[str, int] = {}
    failed: list[list[int]] = []
    covered = 0
    cursor = start
    while cursor <= end:
        to_block = min(end, cursor + chunk - 1)
        ok = True
        for topic, from_index, label in (
            (TOPIC_TRANSFER, 1, "ERC721"),
            (TOPIC_TRANSFER_SINGLE, 2, "ERC1155"),
        ):
            topics = [topic, ZERO_TOPIC, None, None] if label == "ERC721" else [topic, None, ZERO_TOPIC, None]
            try:
                logs = rpc.call(
                    "eth_getLogs",
                    [{"fromBlock": hex(cursor), "toBlock": hex(to_block), "topics": topics}],
                ) or []
            except cl.RpcError:
                failed.append([cursor, to_block])
                ok = False
                break
            for log in logs:
                topics_field = log.get("topics") or []
                if len(topics_field) <= from_index:
                    continue
                if "0x" + topics_field[from_index][-40:] != cl.ZERO_ADDRESS:
                    continue
                address = (log.get("address") or "").lower()
                if cl.is_address(address):
                    counts[address] = counts.get(address, 0) + 1
            time.sleep(sleep)
        if ok:
            covered += to_block - cursor + 1
        cursor = to_block + 1
    return counts, failed, covered


def element_indexed(contract: str, token_id: int) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(
            f"https://element.market/assets/bsc/{contract}/{token_id}",
            headers={"User-Agent": UA, "Referer": "https://element.market/", "Accept-Encoding": "gzip"},
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read()
            if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                raw = gzip.decompress(raw)
        html = raw.decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return False, f"ERR {str(exc)[:60]}"
    match = re.search(r"(?is)<title>(.*?)</title>", html)
    title = match.group(1).strip() if match else ""
    return (bool(title) and not title.startswith("-")), title[:70]


def inspect(rpc: cl.BnbRpc, contract: str, wallet: str | None) -> dict:
    row: dict = {"contract": contract}
    for label, signature in (("name", "name()"), ("symbol", "symbol()"), ("supply", "totalSupply()")):
        try:
            raw = rpc.eth_call(contract, cl.encode_call(signature, []))
            row[label] = cl.decode_string(raw) if label != "supply" else cl.decode_uint(raw)
        except Exception:  # noqa: BLE001
            row[label] = None
    caller = wallet or FRESH
    row["free_mint"] = None
    for signature, args in (("mint(uint256)", [1]), ("mint()", []), ("freeMint()", []), ("claim()", [])):
        try:
            rpc.eth_call(contract, cl.encode_call(signature, args), from_address=caller, value=0)
            row["free_mint"] = signature
            break
        except cl.RpcError:
            continue
    token_id = max(1, row.get("supply") or 1)
    row["element_indexed"], row["element_title"] = element_indexed(contract, token_id)
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=1)
    ap.add_argument("--max-blocks-per-cycle", type=int, default=1200)
    ap.add_argument("--chunk", type=int, default=25, help="blocks per eth_getLogs call (blockrazor caps at 25)")
    ap.add_argument("--sleep", type=float, default=0.2)
    ap.add_argument("--max-contracts", type=int, default=20, help="contracts to inspect per cycle")
    ap.add_argument("--wallet", help="optional: simulate mint from this address instead of a fresh one")
    ap.add_argument("--state", default="mint-watch-state.json")
    ap.add_argument("--out", default="mint-watch.jsonl")
    ap.add_argument("--rpc", action="append", help="log-capable RPC endpoint (repeatable)")
    args = ap.parse_args()

    state_path = Path(args.state)
    out_path = Path(args.out)
    state = load_state(state_path)
    log_rpc = cl.BnbRpc(args.rpc or list(LOG_RPCS), timeout=25.0)
    account_rpc = cl.BnbRpc(None, timeout=15.0)

    for cycle in range(args.cycles):
        head = log_rpc.block_number()
        if state["started_at_block"] is None:
            state["started_at_block"] = max(0, head - args.max_blocks_per_cycle)
        counts: dict[str, int] = {}
        budget = args.max_blocks_per_cycle

        while state["pending"] and budget > 0:
            start, end = state["pending"][0]
            take = min(end - start + 1, budget)
            part, failed, covered = scan_range(log_rpc, start, start + take - 1, args.chunk, args.sleep)
            for k, v in part.items():
                counts[k] = counts.get(k, 0) + v
            state["covered"] += covered
            budget -= take
            if start + take > end:
                state["pending"].pop(0)
            else:
                state["pending"][0] = [start + take, end]
            state["pending"].extend(failed)

        if budget > 0:
            start = state["last_scanned"] + 1 if state["last_scanned"] is not None else max(0, head - args.max_blocks_per_cycle)
            if start <= head:
                end = min(head, start + budget - 1)
                part, failed, covered = scan_range(log_rpc, start, end, args.chunk, args.sleep)
                for k, v in part.items():
                    counts[k] = counts.get(k, 0) + v
                state["covered"] += covered
                state["last_scanned"] = end
                state["pending"].extend(failed)

        state["cycles"] += 1
        save_state(state_path, state)
        elapsed = max(1, head - state["started_at_block"] + 1)
        coverage = 100.0 * state["covered"] / elapsed
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(
            f"[{stamp}] head={head} contracts={len(counts)} coverage={coverage:.1f}% pending={len(state['pending'])}",
            flush=True,
        )

        rows = []
        for contract, mints in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[: args.max_contracts]:
            row = inspect(account_rpc, contract, args.wallet)
            row["mints"] = mints
            rows.append(row)
        with out_path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        free = sum(1 for r in rows if r["free_mint"])
        indexed = sum(1 for r in rows if r["element_indexed"])
        print(f"   inspected={len(rows)} free_mint={free} indexed={indexed}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
