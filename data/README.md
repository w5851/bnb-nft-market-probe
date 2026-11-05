# Dataset — BNB Chain NFT fills

Every five hours a GitHub Action samples the Element exchange
(`0xb3e3DfCb2d9f3DdE16d78B9e6EB3538Eb32B5ae1`, which emits 0x-Protocol v4 fills),
decodes the trades, and appends whatever is new. This is the part of the
repository that is meant to be *used*, not just read.

| File | What it holds |
| --- | --- |
| `elementex/fills.jsonl` | one executed fill per line, sorted by block |
| `index.json` | totals, block range, fills per observed day, top collections |

## Why it is sampled rather than continuous

Public BSC RPCs serve roughly the last **50,000 blocks** of `eth_getLogs`
(≈5.6 h) and then return an *empty array* instead of an error — the failure is
silent, which is exactly why so many "300k block" analyses are wrong. A once-a-day
job would therefore leave a 19-hour hole. Running every 5 hours makes consecutive
windows overlap slightly, and `build_dataset.py` dedupes by
`(tx, order_hash, side)`, so coverage accumulates instead of repeating.

Gaps are still possible: a failed or delayed run leaves a hole, and GitHub's
scheduler is best-effort. `index.json` records `by_observed_day`, so a missing
day is visible rather than hidden.

## Row schema

| Field | Meaning |
| --- | --- |
| `block` | block the fill landed in |
| `tx` | transaction hash |
| `order_hash` | 0x-Protocol order hash — together with `tx` it identifies the fill |
| `side` | `sell` (a listing was taken) or `buy` (a resting offer was hit) |
| `nft_contract` | ERC-721 contract |
| `token_id` | token id |
| `payment_token` | ERC-20 used for payment, or `0xeeee…eeee` for native BNB |
| `payment_symbol` | `BNB`, `WBNB`, `USDT`, … |
| `payment_decimals` | decimals used to scale `payment_raw` |
| `payment_raw` | amount in base units, as a string (no float rounding) |
| `payment` | `payment_raw / 10**decimals`, for convenience |
| `maker`, `taker` | order maker and the address that filled it |
| `fees` | `[{recipient, amount}]` — royalty/marketplace cuts, raw units |
| `observed_at` | when the sampler saw it (UTC), *not* the block time |

Block timestamps are deliberately not stored: they are one `eth_getBlockByNumber`
away, and caching them would go stale.

## What this dataset is not

- **Not the whole BSC NFT market.** It is one venue. Seaport on BSC settles a
  couple of trades per 100k blocks — a rounding error next to this.
- **Not USD-priced.** Fills are in the token that paid. Converting needs a rate
  at the time of the fill; picking one today would silently rewrite history.
- **Not a price oracle.** Thin collections trade at whatever the last person
  accepted. A $0.21 fill on a 10k-supply collection is not a valuation.

## Reproduce

```bash
python elementex_sales.py --blocks 45000 --json-out sample.json
python build_dataset.py --input sample.json
```

`build_dataset.py` rewrites `fills.jsonl` sorted by block, so the file stays
diff-friendly and stable across runs.
