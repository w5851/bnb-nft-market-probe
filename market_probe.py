#!/usr/bin/env python3
"""Read-only NFT marketability probe for BNB Smart Chain.

Answers, for one token (or a list), without signing anything:

    * what the collection is (name / symbol / standard / total supply)
    * what the wallet holds (ownerOf or ERC-1155 balance)
    * whether the token can be transferred at all (soulbound detection)
    * whether the collection declares a royalty (ERC-2981)
    * whether a fresh wallet can still mint it (value = 0 simulation)
    * how many secondary transfers the token saw in a block window

Usage
-----
    python market_probe.py --contract 0xab6b18c626daadd1ff7cc45f402141153a5a9759 --token-id 1835
    python market_probe.py --contract 0xab6b... --token-id 1835 --wallet 0xYourAddress
    python market_probe.py --contracts examples/contracts.sample.json --wallet 0xYourAddress

Everything is `eth_call` / `eth_getLogs` against public RPC endpoints.
No private key, no signature, no broadcast.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import chainlib as cl  # noqa: E402

TOPIC_TRANSFER = "0x" + cl.keccak_256(b"Transfer(address,address,uint256)").hex()
TOPIC_TRANSFER_SINGLE = "0x" + cl.keccak_256(
    b"TransferSingle(address,address,address,uint256,uint256)"
).hex()
LOG_RPCS = ("https://bsc.publicnode.com", "https://bsc.rpc.blxrbdn.com")
FRESH = "0x00000000000000000000000000000000C0FFEE01"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/147.0 Safari/537.36"
ERC721_ID = "0x80ac58cd"
ERC1155_ID = "0xd9b67a26"


def http_text(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Referer": "https://element.market/", "Accept-Encoding": "gzip"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        raw = resp.read()
        if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def element_indexed(contract: str, token_id: int) -> tuple[bool, str]:
    """Element renders metadata server-side: an indexed token yields a real <title>."""
    try:
        html = http_text(f"https://element.market/assets/bsc/{contract}/{token_id}")
    except Exception as exc:  # noqa: BLE001
        return False, f"ERR {str(exc)[:60]}"
    match = re.search(r"(?is)<title>(.*?)</title>", html)
    title = match.group(1).strip() if match else ""
    return (bool(title) and not title.startswith("-")), title[:80]


def try_call(rpc: cl.BnbRpc, to: str, data: str, from_address: str | None = None):
    try:
        return True, rpc.eth_call(to, data, from_address=from_address)
    except cl.RpcError as exc:
        return False, str(exc)


def probe(rpc: cl.BnbRpc, contract: str, token_id: int, wallet: str | None, blocks: int, head: int) -> dict:
    row: dict = {"contract": contract, "token_id": token_id}
    try:
        code = rpc.code(contract) or "0x"
    except cl.RpcError as exc:
        row["error"] = f"eth_getCode failed: {exc}"
        return row
    row["code_size"] = (len(code) - 2) // 2
    if row["code_size"] == 0:
        row["error"] = "no bytecode"
        return row

    ok, res = try_call(rpc, contract, cl.encode_call("name()", []))
    row["name"] = cl.decode_string(res) if ok else None
    ok, res = try_call(rpc, contract, cl.encode_call("symbol()", []))
    row["symbol"] = cl.decode_string(res) if ok else None
    ok, res = try_call(rpc, contract, cl.encode_call("totalSupply()", []))
    row["total_supply"] = cl.decode_uint(res) if ok else None

    ok, res = try_call(rpc, contract, cl.encode_call("supportsInterface(bytes4)", [ERC721_ID]))
    is_721 = cl.decode_bool(res) if ok else None
    ok, res = try_call(rpc, contract, cl.encode_call("supportsInterface(bytes4)", [ERC1155_ID]))
    is_1155 = cl.decode_bool(res) if ok else None
    row["standard"] = "ERC721" if is_721 else "ERC1155" if is_1155 else "unknown"

    if wallet:
        if is_721:
            ok, res = try_call(rpc, contract, cl.encode_call("ownerOf(uint256)", [token_id]))
            owner = cl.decode_address(res) if ok else None
            row["owner"] = owner
            row["holds_token"] = bool(owner) and owner.lower() == wallet.lower()
        elif is_1155:
            ok, res = try_call(
                rpc, contract, cl.encode_call("balanceOf(address,uint256)", [wallet, token_id])
            )
            row["balance"] = cl.decode_uint(res) if ok else None
            row["holds_token"] = bool(row.get("balance"))
        # transferability: ask the contract what a transfer would do (never signed)
        if is_721:
            data = cl.encode_call("transferFrom(address,address,uint256)", [wallet, FRESH, token_id])
        else:
            selector = cl.selector("safeTransferFrom(address,address,uint256,uint256,bytes)")
            data = selector + "".join(
                [
                    wallet[2:].rjust(64, "0"),
                    FRESH[2:].rjust(64, "0"),
                    f"{token_id:064x}",
                    f"{1:064x}",
                    f"{5 * 32:064x}",
                    f"{0:064x}",
                ]
            )
        ok, res = try_call(rpc, contract, data, from_address=wallet)
        row["transferable"] = "TRANSFERABLE" if ok else "SOULBOUND_OR_BLOCKED"

    ok, res = try_call(
        rpc, contract, cl.encode_call("royaltyInfo(uint256,uint256)", [token_id, 10**18])
    )
    if ok:
        try:
            raw = bytes.fromhex(res[2:])
            if len(raw) >= 64:
                amount = int.from_bytes(raw[32:64], "big")
                row["royalty"] = {"receiver": "0x" + raw[12:32].hex(), "bps": round(amount / 10**16, 1)}
        except ValueError:
            pass

    # can someone who never minted still mint it for free?
    for signature, args in (("mint(uint256)", [1]), ("mint()", []), ("freeMint()", []), ("claim()", [])):
        ok, _ = try_call(
            rpc,
            contract,
            cl.encode_call(signature, args),
            from_address=FRESH,
        )
        if ok:
            row["free_mint_open"] = signature
            break
    row.setdefault("free_mint_open", None)

    indexed, title = element_indexed(contract, max(1, token_id))
    row["element_indexed"] = indexed
    row["element_title"] = title

    row["secondary"] = secondary_transfers(contract, token_id, row["standard"], blocks, head)
    return row


def secondary_transfers(contract: str, token_id: int, standard: str, blocks: int, head: int) -> dict:
    topic = TOPIC_TRANSFER_SINGLE if standard == "ERC1155" else TOPIC_TRANSFER
    floor = max(0, head - blocks + 1)
    for url in LOG_RPCS:
        client = cl.BnbRpc([url], timeout=25.0)
        logs: list[dict] = []
        cursor = head
        ok = True
        while cursor >= floor:
            start = max(floor, cursor - 9999)
            try:
                logs += client.call(
                    "eth_getLogs",
                    [{"address": contract, "fromBlock": hex(start), "toBlock": hex(cursor), "topics": [topic]}],
                ) or []
            except cl.RpcError:
                ok = False
                break
            cursor = start - 1
        if not ok:
            continue
        mints = burns = secondary = 0
        for log in logs:
            topics = log.get("topics") or []
            try:
                if standard == "ERC1155":
                    frm, to = "0x" + topics[2][-40:], "0x" + topics[3][-40:]
                    raw = bytes.fromhex((log.get("data") or "0x")[2:])
                    tid = int.from_bytes(raw[0:32], "big") if len(raw) >= 32 else -1
                else:
                    frm, to, tid = "0x" + topics[1][-40:], "0x" + topics[2][-40:], int(topics[3], 16)
            except (IndexError, ValueError):
                continue
            if tid != token_id:
                continue
            if frm == cl.ZERO_ADDRESS:
                mints += 1
            elif to == cl.ZERO_ADDRESS:
                burns += 1
            else:
                secondary += 1
        return {"window_blocks": blocks, "mints": mints, "burns": burns, "secondary": secondary, "logs": len(logs)}
    return {"error": "eth_getLogs unavailable"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--contract")
    ap.add_argument("--token-id", type=int)
    ap.add_argument("--contracts", help="JSON file: [{\"contract\": \"0x..\", \"token_id\": 1}]")
    ap.add_argument("--wallet", help="optional: check holdings / transferability from this address")
    ap.add_argument("--blocks", type=int, default=20000, help="log window for secondary transfers")
    ap.add_argument("--rpc", action="append", help="RPC endpoint (repeatable)")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    items = []
    if args.contracts:
        for row in json.loads(Path(args.contracts).read_text(encoding="utf-8")):
            items.append((row["contract"], int(row.get("token_id", 1))))
    elif args.contract and args.token_id is not None:
        items.append((args.contract, args.token_id))
    else:
        ap.error("pass --contract/--token-id or --contracts")

    rpc = cl.BnbRpc(args.rpc or None, timeout=15.0)
    head = rpc.block_number()
    rows = [probe(rpc, c, t, args.wallet, args.blocks, head) for c, t in items]

    print(
        f"{'name':<24}{'std':<9}{'supply':>9}  {'free_mint':<14}{'transfer':<22}{'royalty':>8}{'2nd':>6}{'element':>9}"
    )
    for r in rows:
        print(
            f"{str(r.get('name'))[:23]:<24}{str(r.get('standard'))[:8]:<9}{str(r.get('total_supply')):>9}  "
            f"{str(r.get('free_mint_open') or '-'):<14}{str(r.get('transferable') or '-'):<22}"
            f"{('yes' if r.get('royalty') else 'no'):>8}{(r.get('secondary') or {}).get('secondary', '-'):>6}"
            f"{('yes' if r.get('element_indexed') else 'no'):>9}"
        )

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"head_block": head, "rows": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
