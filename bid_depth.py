#!/usr/bin/env python3
"""Bid depth from history: which collections actually have buyers, and at what price.

We cannot read resting order books without an Element API key, but `ERC721BuyOrderFilled`
on Element's exchange tells us every time a resting **bid** got taken. That is a lower
bound on demand and it is the closest thing to an order book we have for free.

This script answers, per collection:
  * how many bids were hit, by how many distinct wallets
  * the price distribution of those bids (median / max, normalised to USD)
  * when the last one happened
  * how that compares with ask-side fills (sellers hitting buyers vs buyers taking asks)

Usage
-----
    python bid_depth.py --blocks 300000
    python bid_depth.py --blocks 300000 --bnb-usd 692 --top 25
    python bid_depth.py --blocks 300000 --watchlist data/scans/bid-watchlist.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import elementex_sales as ex  # noqa: E402

STABLES = {"USDT", "BUSD", "USDC", "FDUSD"}
BLOCK_SECONDS = 0.45


def usd_rate(symbol: str, bnb_usd: float) -> float | None:
    if symbol in ("BNB", "WBNB"):
        return bnb_usd
    if symbol in STABLES:
        return 1.0
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=int, default=300000)
    ap.add_argument("--chunk", type=int, default=10000)
    ap.add_argument("--bnb-usd", type=float, default=692.0, help="BNB price used to normalise prices")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--min-usd", type=float, default=0.0, help="ignore collections below this traded value")
    ap.add_argument("--watchlist", default="", help="write contracts with recurring bids to this JSON")
    args = ap.parse_args()

    head, rpc, rows, source, effective = ex.fetch_fills(args.blocks, args.chunk)
    if head is None:
        print(f"all RPCs failed: {source}", file=sys.stderr)
        return 1

    bids = [r for r in rows if r["side"] == "buy"]
    asks = [r for r in rows if r["side"] == "sell"]
    span_h = effective * BLOCK_SECONDS / 3600
    print(
        f"head={head} window={effective} blocks (~{span_h:.1f}h) "
        f"fills={len(rows)} (bid-side {len(bids)}, ask-side {len(asks)}) source={source}"
    )

    per: dict[str, dict] = defaultdict(
        lambda: {"bids": [], "asks": 0, "bidders": set(), "last_block": 0, "symbols": set()}
    )
    unmapped = 0
    for row in rows:
        contract = row["nft_contract"].lower()
        entry = per[contract]
        entry["last_block"] = max(entry["last_block"], row["block"])
        rate = usd_rate(row["payment_symbol"], args.bnb_usd)
        if row["side"] == "sell":
            entry["asks"] += 1
            continue
        if rate is None:
            unmapped += 1
            continue
        price_usd = (row["payment"] or 0) * rate
        entry["bids"].append(price_usd)
        entry["bidders"].add(row["maker"].lower())
        entry["symbols"].add(row["payment_symbol"])

    ranked = []
    for contract, entry in per.items():
        if not entry["bids"]:
            continue
        total = sum(entry["bids"])
        if total < args.min_usd:
            continue
        ranked.append(
            {
                "contract": contract,
                "bid_fills": len(entry["bids"]),
                "bidders": len(entry["bidders"]),
                "ask_fills": entry["asks"],
                "total_usd": total,
                "median_usd": statistics.median(entry["bids"]),
                "max_usd": max(entry["bids"]),
                "last_block": entry["last_block"],
                "age_h": (head - entry["last_block"]) * BLOCK_SECONDS / 3600,
                "symbols": sorted(entry["symbols"]),
            }
        )
    ranked.sort(key=lambda r: (r["bid_fills"], r["total_usd"]), reverse=True)

    print(
        f"\ncollections with a bid actually taken: {len(ranked)}"
        f"{f' ({unmapped} fills in unpriced assets were skipped)' if unmapped else ''}"
    )
    print(
        f"\n{'collection':<44}{'bids':>5}{'wallets':>8}{'asks':>5}"
        f"{'median$':>9}{'max$':>9}{'total$':>9}{'last':>8}  pay"
    )
    for r in ranked[: args.top]:
        print(
            f"{r['contract']:<44}{r['bid_fills']:>5}{r['bidders']:>8}{r['ask_fills']:>5}"
            f"{r['median_usd']:>9.2f}{r['max_usd']:>9.2f}{r['total_usd']:>9.2f}"
            f"{r['age_h']:>7.1f}h  {'/'.join(r['symbols'])}"
        )

    # bidder concentration: are these many small buyers or one wallet farming?
    by_bidder: dict[str, int] = defaultdict(int)
    for row in bids:
        by_bidder[row["maker"].lower()] += 1
    if by_bidder:
        top_bidder, top_count = max(by_bidder.items(), key=lambda kv: kv[1])
        share = 100.0 * top_count / len(bids)
        print(
            f"\ndistinct bidders: {len(by_bidder)}; "
            f"busiest {top_bidder} hit {top_count} bids ({share:.1f}% of all bid fills)"
        )

    recurring = [r for r in ranked if r["bid_fills"] >= 2 and r["bidders"] >= 2]
    print(f"recurring demand (>=2 bid fills from >=2 wallets): {len(recurring)}")
    for r in recurring[:10]:
        print(f"  {r['contract']} bids={r['bid_fills']} wallets={r['bidders']} median=${r['median_usd']:.2f}")

    if args.watchlist:
        out = Path(args.watchlist)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "head": head,
                    "window_blocks": args.blocks,
                    "bnb_usd": args.bnb_usd,
                    "collections": ranked,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {out} ({len(ranked)} collections)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
