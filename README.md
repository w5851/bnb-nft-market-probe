# bnb-nft-market-probe

Read-only research toolkit for **BNB Smart Chain NFT markets**: it watches new mints, checks whether a collection is actually tradeable, and decodes real sales from on-chain settlement events.

It answers three questions with public RPC data only:

1. **What is being minted right now?** — contiguous block scanning with a persistent cursor (`mint_watch.py`).
2. **Can this NFT be sold at all?** — transfer simulation, royalty, mint-open state, marketplace indexing (`market_probe.py`).
3. **Did anyone actually pay for it?** — settlement-event decoding into collection / token / price / buyer / seller, for both venues that matter on BSC: Element's own exchange (`elementex_sales.py`) and Seaport (`market_sales.py`).

No API keys are required, nothing is signed, and nothing is broadcast.

## Components

| File | What it does | Network calls |
| --- | --- | --- |
| `chainlib.py` | Minimal JSON-RPC client + keccak/ABI helpers (stdlib only) | `eth_*` read calls |
| `elementex_sales.py` | Decodes Element's exchange (`0xb3e3…ae1`): `ERC721SellOrderFilled` and `ERC721BuyOrderFilled`, with royalty/fee array and native-BNB handling | `eth_getLogs`, `eth_call` |
| `bid_depth.py` | Bid-side depth from history: per collection, how many bids were taken, by how many wallets, at what price (USD-normalised) — the closest free substitute for an order book | `eth_getLogs`, `eth_call` |
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
python elementex_sales.py --blocks 45000          # Element exchange: the venue that actually clears
python elementex_sales.py --blocks 45000 --contract 0x4addbd796edf3d4d186fb5aa314caff3f98984a6
python market_sales.py --blocks 45000             # Seaport: much lower volume on BSC

# 4) is there anyone on the buy side?
python bid_depth.py --blocks 45000
```

Python 3.10+, standard library only.

## How the pieces work

**Mint discovery.** `eth_getLogs` is asked for `Transfer` events with `from = 0x0` **at the RPC level** (topic filter), which keeps responses small: a 25-block window returns tens of events instead of thousands. A cursor in `mint-watch-state.json` records `last_scanned`, queued failed ranges and total covered blocks, so coverage is measured (`coverage=xx%`) instead of assumed.

**Market indexing.** Element renders token metadata server-side. An indexed token returns a real `<title>` (`"#2605 - Un-Written | Element"`); an unknown one returns `"- | Element"`. That single HTTPS request is a cheap "is this collection visible to buyers" signal.

**Real sales — pick the right venue.** On BSC, Seaport is *not* where the market is. Over the same chain time, Element's exchange (`0xb3e3DfCb2d9f3DdE16d78B9e6EB3538Eb32B5ae1`) produced **89 fills in 50,000 blocks (~6.25 h)**, while Seaport produced **1–2 trades per 100,000 blocks**. Anyone measuring BSC NFT demand from Seaport alone will conclude "nobody trades", which is wrong.

ElementEx emits 0x-Protocol-v4 style events. `ERC721SellOrderFilled` means a seller's ask was taken; `ERC721BuyOrderFilled` means a resting **bid** was taken — that second one is the only public evidence of executable demand when you have no order-book API. The decoder reads the nine-word head (`orderHash`, `maker`, `taker`, `nonce`, `erc20Token`, `erc20TokenAmount`, fees offset, `erc721Token`, `erc721TokenId`) plus the dynamic royalty/fee array. Native BNB is encoded as `0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee` **or** the zero address, and `symbol()`/`decimals()` on those returns empty bytes — reading that as `decimals = 0` inflates every BNB price by 1e18.

Seaport processing still applies: `OrderFulfilled(orderHash, offerer, zone, recipient, offer[], consideration[])` is decoded in both directions (NFT in `offer` for sell-side, in `consideration` for buy-side), every payment item is summed with its token `decimals()`, and the two events of one settlement are merged so a trade is not double counted. Unknown assets are reported as `unknown`, never as "free".

**How far back can we see?** Not far. Free public endpoints keep only ~50,000 blocks of logs (~6.2 h) and answer anything older with an **empty list rather than an error** — a 300,000-block request silently degrades into a 50,000-block one. `chainlib.log_retention_blocks()` measures the real limit by probing USDT `Transfer` events (never idle for ten consecutive blocks) and both decoders clamp to it, printing `window=` for what was actually read. Anything longer needs an endpoint with deep log history, or forward collection by a running scanner.

**Bid depth without an order book.** A resting bid cannot be read from chain state, but `ERC721BuyOrderFilled` fires every time one gets taken. Counting those per collection gives a lower bound on buy-side demand: how many bids were hit, how many distinct wallets did the hitting, and at what price. In a 45,000-block sample (5.6 h) that was **9 bid fills across 3 collections**, median $0.04–$0.21 — thin, but non-zero, and invisible to any Seaport-only view. `bid_depth.py` also flags concentration, because one wallet hitting most of the bids is not a market.

## What this repository deliberately does not do

- no private keys, no seed phrases, no signing, no transactions
- no order placement, no listings, no approvals
- no scraping behind a login, no DMs, no bulk account operations

## What an Element API key would add

The public web pages expose *indexing* (above), and on-chain settlement exposes *executed* trades. Neither exposes **open bids**.

An Element API key would let this toolkit read BNB Chain collection/item order data (listings and offers) to compare primary mint cost with executable secondary prices. Intended use is read-only analytics: a few hundred requests per day, well under the documented 120 requests/minute limit. No orders would be created or cancelled through the API.

## Limitations

- Venue coverage is limited to Element's exchange and Seaport; other BSC marketplaces settle on their own contracts and are not decoded here.
- `ERC721BuyOrderFilled` proves a bid *was* taken; it does not reveal bids that are still resting. For live order books an API key is still required.
- `market_probe.py` needs a wallet address to test transferability from that wallet; without one it reports on-chain facts only.
- Block time on BSC is ~0.45 s, so block windows are converted to time using timestamps, not a fixed constant.
- Public RPC endpoints rate-limit log queries; failures are queued and retried, but very wide backfills take time.

## License

MIT — see [LICENSE](LICENSE).
