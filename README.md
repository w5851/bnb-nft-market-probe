# bnb-nft-market-probe

Read-only research toolkit for **BNB Smart Chain NFT markets**: it watches new mints, checks whether a collection is actually tradeable, and decodes real sales from on-chain settlement events.

It answers three questions with public RPC data only:

1. **What is being minted right now?** — contiguous block scanning with a persistent cursor (`mint_watch.py`).
2. **Can this NFT be sold at all?** — transfer simulation, royalty, mint-open state, marketplace indexing (`market_probe.py`).
3. **Did anyone actually pay for it?** — Seaport `OrderFulfilled` decoding into collection / token / price / buyer / seller (`market_sales.py`).

No API keys are required, nothing is signed, and nothing is broadcast.

## Components

| File | What it does | Network calls |
| --- | --- | --- |
| `chainlib.py` | Minimal JSON-RPC client + keccak/ABI helpers (stdlib only) | `eth_*` read calls |
| `market_sales.py` | Decodes Seaport `OrderFulfilled` (both directions, multi-asset, `decimals()` aware) into trades | `eth_getLogs`, `eth_call` |
| `market_probe.py` | Per-token report: standard, supply, transferability, royalty, free-mint state, secondary transfers, marketplace indexing | `eth_call`, `eth_getLogs`, one HTTPS page |
| `mint_watch.py` | Cursor-based scanner: contiguous block coverage, failure backfill, per-contract inspection | `eth_getLogs`, `eth_call`, one HTTPS page per contract |

## Quick start

```bash
python chainlib.py --selftest          # offline crypto/ABI self-test

# 1) what is minting, and is it covered?
python mint_watch.py --cycles 1 --max-blocks-per-cycle 600

# 2) is this token sellable?
python market_probe.py --contract 0xab6b18c626daadd1ff7cc45f402141153a5a9759 --token-id 1835

# 3) what did people actually pay?
python market_sales.py --blocks 100000
python market_sales.py --blocks 100000 --contract 0x3148850ce59904cda99086a8248c2300416edc46
```

Python 3.10+, standard library only.

## How the pieces work

**Mint discovery.** `eth_getLogs` is asked for `Transfer` events with `from = 0x0` **at the RPC level** (topic filter), which keeps responses small: a 25-block window returns tens of events instead of thousands. A cursor in `mint-watch-state.json` records `last_scanned`, queued failed ranges and total covered blocks, so coverage is measured (`coverage=xx%`) instead of assumed.

**Market indexing.** Element renders token metadata server-side. An indexed token returns a real `<title>` (`"#2605 - Un-Written | Element"`); an unknown one returns `"- | Element"`. That single HTTPS request is a cheap "is this collection visible to buyers" signal.

**Real sales.** Seaport emits `OrderFulfilled(orderHash, offerer, zone, recipient, offer[], consideration[])`. The decoder extracts the NFT side (either `offer` for sell-side events or `consideration` for buy-side events), sums every payment item with its token `decimals()`, and merges the events of one settlement so a trade is not double counted. Unknown assets are reported as `unknown`, never as "free".

## What this repository deliberately does not do

- no private keys, no seed phrases, no signing, no transactions
- no order placement, no listings, no approvals
- no scraping behind a login, no DMs, no bulk account operations

## What an Element API key would add

The public web pages expose *indexing* (above), and on-chain settlement exposes *executed* trades. Neither exposes **open bids**.

An Element API key would let this toolkit read BNB Chain collection/item order data (listings and offers) to compare primary mint cost with executable secondary prices. Intended use is read-only analytics: a few hundred requests per day, well under the documented 120 requests/minute limit. No orders would be created or cancelled through the API.

## Limitations

- Seaport is one settlement path; Element's own settlement and other marketplaces are separate contracts.
- `market_probe.py` needs a wallet address to test transferability from that wallet; without one it reports on-chain facts only.
- Block time on BSC is ~0.45 s, so block windows are converted to time using timestamps, not a fixed constant.
- Public RPC endpoints rate-limit log queries; failures are queued and retried, but very wide backfills take time.

## License

MIT — see [LICENSE](LICENSE).
