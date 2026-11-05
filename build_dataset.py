#!/usr/bin/env python3
"""Fold one decrypted Element-exchange sample into the public fill dataset.

`elementex_sales.py --json-out` produces a point-in-time sample: the fills the
public RPC still serves, which is roughly the last 6 hours. Run on a schedule,
those samples overlap and leave gaps; this script dedupes them by
(transaction, order hash) so the dataset accumulates instead of duplicating.

Output
------
data/elementex/fills.jsonl   one fill per line, sorted by block
data/index.json              counts per day and per collection, plus provenance

Usage
-----
    python elementex_sales.py --blocks 45000 --json-out sample.json
    python build_dataset.py --input sample.json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

FILLS = Path("elementex") / "fills.jsonl"


def normalize(trade: dict, observed_at: str) -> dict:
    """Keep the fields a consumer can actually use, and pin the raw amount."""
    return {
        "block": trade.get("block"),
        "tx": trade.get("tx"),
        "order_hash": trade.get("order_hash"),
        "side": trade.get("side"),
        "nft_contract": trade.get("nft_contract"),
        "token_id": trade.get("token_id"),
        "payment_token": trade.get("payment_token"),
        "payment_symbol": trade.get("payment_symbol"),
        "payment_decimals": trade.get("payment_decimals"),
        "payment_raw": str(trade.get("payment_raw")),
        "payment": trade.get("payment"),
        "maker": trade.get("maker"),
        "taker": trade.get("taker"),
        "fees": trade.get("fees") or [],
        "observed_at": observed_at,
    }


def key(row: dict) -> tuple:
    return (row.get("tx"), row.get("order_hash"), row.get("side"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="JSON written by elementex_sales.py --json-out")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    trades = payload.get("trades") or []
    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    data_dir = Path(args.data_dir)
    fills_path = data_dir / FILLS
    existing: dict[tuple, dict] = {}
    if fills_path.exists():
        for line in fills_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            existing[key(row)] = row

    added = 0
    for trade in trades:
        row = normalize(trade, observed_at)
        if key(row) in existing:
            continue
        existing[key(row)] = row
        added += 1

    rows = sorted(existing.values(), key=lambda r: (r.get("block") or 0, r.get("tx") or ""))
    by_day: dict[str, int] = {}
    by_collection: dict[str, int] = {}
    for row in rows:
        day = (row.get("observed_at") or "")[:10]
        by_day[day] = by_day.get(day, 0) + 1
        contract = row.get("nft_contract") or "?"
        by_collection[contract] = by_collection.get(contract, 0) + 1

    index = {
        "updated_at": observed_at,
        "total_fills": len(rows),
        "block_range": {
            "from": rows[0].get("block") if rows else None,
            "to": rows[-1].get("block") if rows else None,
        },
        "by_observed_day": dict(sorted(by_day.items())),
        "by_collection": dict(sorted(by_collection.items(), key=lambda kv: -kv[1])[:200]),
        "source": "Element exchange 0xb3e3DfCb2d9f3DdE16d78B9e6EB3538Eb32B5ae1 (0x-Protocol v4 fills)",
        "note": "Samples, not a continuous record: each run reads whatever the public RPC still serves (~50k blocks).",
    }

    print(f"input={len(trades)} new={added} total={len(rows)}")
    if args.dry_run:
        return 0

    fills_path.parent.mkdir(parents=True, exist_ok=True)
    with open(fills_path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    (data_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {fills_path} and {data_dir / 'index.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
