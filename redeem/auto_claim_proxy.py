"""
Parallel to auto-claim-proxy.js: poll Data API for redeemable positions and redeem via
Polymarket **proxy** relayer (gasless), using Builder HMAC credentials.

Loads **only** the repository root ``.env`` (next to ``README.md``). Run from repo root, e.g.:

  ``python redeem/auto_claim_proxy.py`` / ``python redeem/auto_claim_proxy.py --dry-run``

Env (same file as ``poly_redeem.py``):
  PRIVATE_KEY or POLYMARKET_PRIVATE_KEY or POLY_PRIVATE_KEY — owner EOA
  USER_ADDRESS or POLYMARKET_WALLET_ADDRESS — Data API / profile wallet
  BUILDER_* or POLYMARKET_BUILDER_* — live submit only
  RELAYER_URL, CHAIN_ID, POLYGON_RPC_URL or POLY_RPC_URL, POLL_MS — optional

CLI: live loop or ``--dry-run`` (one-shot plan, no submit; Builder keys optional for dry-run).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from eth_abi import encode
from eth_utils import keccak, to_checksum_address
from web3 import Web3

from py_builder_relayer_client.signer import Signer
from py_builder_signing_sdk.config import BuilderConfig
from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

# --- Polygon (Polymarket builder-relayer-client config / constants) ---
PROXY_FACTORY = to_checksum_address("0xaB45c5A4B0c941a2F231C04C3f49182e1A254052")
RELAY_HUB = to_checksum_address("0xD216153c06E857cD7f72665E0aF1d7D82172F494")
PROXY_INIT_CODE_HASH = bytes.fromhex(
    "d21df8dc65880a8606f09fe0ce3df9b8869287ab0b058be05aa9e8af6330a00b"
)

CTF_ADDRESS = to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
USDC_E = to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
ZERO_BYTES32 = b"\x00" * 32

POSITIONS_URL = "https://data-api.polymarket.com/positions"
DEFAULT_RELAYER = "https://relayer-v2.polymarket.com"

GET_NONCE = "/nonce"
GET_RELAY_PAYLOAD = "/relay-payload"
GET_TRANSACTION = "/transaction"
SUBMIT_TRANSACTION = "/submit"

# selector for proxy((uint8,address,uint256,bytes)[])
_PROXY_SELECTOR = Web3.keccak(text="proxy((uint8,address,uint256,bytes)[])")[:4]
DEFAULT_GAS_LIMIT = 10_000_000


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_env() -> None:
    """Load a single ``.env`` at repository top level only."""
    load_dotenv(_repo_root() / ".env")


def _builder_creds_from_env() -> tuple[str, str, str]:
    import os

    key = (
        (os.getenv("BUILDER_API_KEY") or os.getenv("POLYMARKET_BUILDER_API_KEY") or "")
        .strip()
    )
    secret = (
        (os.getenv("BUILDER_SECRET") or os.getenv("POLYMARKET_BUILDER_SECRET") or "")
        .strip()
    )
    passphrase = (
        (
            os.getenv("BUILDER_PASS_PHRASE")
            or os.getenv("POLYMARKET_BUILDER_PASSPHRASE")
            or ""
        )
        .strip()
    )
    return key, secret, passphrase


def derive_proxy_wallet(owner: str, proxy_factory: str) -> str:
    """CREATE2 proxy address — matches @polymarket/builder-relayer-client deriveProxyWallet."""
    owner_c = to_checksum_address(owner)
    factory = bytes.fromhex(proxy_factory[2:])
    salt = keccak(bytes.fromhex(owner_c[2:]))
    digest = keccak(b"\xff" + factory + salt + PROXY_INIT_CODE_HASH)
    return to_checksum_address("0x" + digest[-20:].hex())


def encode_redeem_positions_calldata(condition_id: str) -> bytes:
    if not isinstance(condition_id, str) or not condition_id.startswith("0x"):
        raise ValueError(f"Invalid conditionId: {condition_id!r}")
    cid = bytes.fromhex(condition_id[2:])
    if len(cid) != 32:
        raise ValueError(f"conditionId must be 32 bytes: {condition_id}")
    sel = Web3.keccak(
        text="redeemPositions(address,bytes32,bytes32,uint256[])"
    )[:4]
    body = encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [USDC_E, ZERO_BYTES32, cid, [1, 2]],
    )
    return bytes(sel) + body


def encode_proxy_transaction_data(
    inner_to: str, inner_data: bytes, *, type_code: int = 1
) -> str:
    """Wrap inner call in Polymarket proxy factory `proxy(calls)` — matches TS encodeProxyTransactionData."""
    calls = [
        (
            type_code,
            to_checksum_address(inner_to),
            0,
            inner_data,
        )
    ]
    enc_args = encode(["(uint8,address,uint256,bytes)[]"], [calls])
    return "0x" + (bytes(_PROXY_SELECTOR) + enc_args).hex()


def create_proxy_struct_hash(
    from_addr: str,
    to_factory: str,
    data_hex: str,
    tx_fee: str,
    gas_price: str,
    gas_limit: str,
    nonce: str,
    relay_hub: str,
    relay_addr: str,
) -> bytes:
    def a20(a: str) -> bytes:
        return bytes.fromhex(to_checksum_address(a)[2:])

    dh = data_hex[2:] if data_hex.startswith("0x") else data_hex
    parts = (
        b"rlx:",
        a20(from_addr),
        a20(to_factory),
        bytes.fromhex(dh),
        int(tx_fee).to_bytes(32, "big"),
        int(gas_price).to_bytes(32, "big"),
        int(gas_limit).to_bytes(32, "big"),
        int(str(nonce)).to_bytes(32, "big"),
        a20(relay_hub),
        a20(relay_addr),
    )
    return keccak(b"".join(parts))


def _relayer_get(relayer_url: str, path: str, params: dict[str, Any]) -> Any:
    url = relayer_url.rstrip("/") + path
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def _post_submit(
    relayer_url: str,
    builder_config: BuilderConfig,
    body_obj: dict[str, Any],
) -> dict[str, Any]:
    body_str = json.dumps(body_obj, separators=(",", ":"))
    path = SUBMIT_TRANSACTION
    headers_payload = builder_config.generate_builder_headers(
        "POST", path, body_str
    )
    if headers_payload is None:
        raise RuntimeError("Could not generate builder headers")
    headers = headers_payload.to_dict()
    headers["Content-Type"] = "application/json"
    url = relayer_url.rstrip("/") + path
    r = requests.post(url, data=body_str.encode("utf-8"), headers=headers, timeout=120)
    if r.status_code != 200:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise RuntimeError(f"relayer HTTP {r.status_code}: {detail}")
    return r.json()


def _poll_transaction(
    relayer_url: str, tx_id: str, *, max_rounds: int = 90, sleep_s: float = 2.0
) -> dict[str, Any] | None:
    url_base = relayer_url.rstrip("/") + GET_TRANSACTION
    ok = {"STATE_MINED", "STATE_CONFIRMED"}
    fail = "STATE_FAILED"
    for _ in range(max_rounds):
        r = requests.get(url_base, params={"id": tx_id}, timeout=60)
        r.raise_for_status()
        rows = r.json()
        if isinstance(rows, list) and rows:
            row = rows[0]
            st = row.get("state")
            if st in ok:
                return row
            if st == fail:
                raise RuntimeError(
                    f"relayer tx failed: {row.get('transactionHash')!r} state={st}"
                )
        time.sleep(sleep_s)
    return None


def _estimate_proxy_gas(
    w3: Web3, from_addr: str, proxy_factory: str, data_hex: str
) -> int:
    try:
        return int(
            w3.eth.estimate_gas(
                {
                    "from": to_checksum_address(from_addr),
                    "to": to_checksum_address(proxy_factory),
                    "data": data_hex,
                }
            )
        )
    except Exception:
        return DEFAULT_GAS_LIMIT


def build_proxy_submit_payload(
    *,
    private_key: str,
    chain_id: int,
    rpc_url: str,
    inner_to: str,
    inner_calldata: bytes,
    relay_address: str,
    nonce: str,
    metadata: str,
) -> dict[str, Any]:
    signer = Signer(private_key, chain_id)
    from_addr = signer.address()
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        raise RuntimeError(f"RPC not connected: {rpc_url}")

    data_hex = encode_proxy_transaction_data(inner_to, inner_calldata)
    gas_price = "0"
    relayer_fee = "0"
    gas_limit = str(
        _estimate_proxy_gas(w3, from_addr, PROXY_FACTORY, data_hex)
    )
    struct = create_proxy_struct_hash(
        from_addr,
        PROXY_FACTORY,
        data_hex,
        relayer_fee,
        gas_price,
        gas_limit,
        str(nonce),
        RELAY_HUB,
        relay_address,
    )
    sig = signer.sign_eip712_struct_hash("0x" + struct.hex())
    proxy_wallet = derive_proxy_wallet(from_addr, PROXY_FACTORY)

    return {
        "from": from_addr,
        "to": PROXY_FACTORY,
        "proxyWallet": proxy_wallet,
        "data": data_hex,
        "nonce": str(nonce),
        "signature": sig,
        "signatureParams": {
            "gasPrice": gas_price,
            "gasLimit": gas_limit,
            "relayerFee": relayer_fee,
            "relayHub": RELAY_HUB,
            "relay": relay_address,
        },
        "type": "PROXY",
        "metadata": metadata,
    }


def _planned_rows(positions: list[dict[str, Any]], claimed: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in positions:
        cid = p.get("conditionId")
        if not cid or not bool(p.get("redeemable")):
            continue
        s = str(cid)
        if s in claimed or s in seen:
            continue
        seen.add(s)
        out.append(p)
    return out


def dry_run_once(
    *,
    relayer_url: str,
    chain_id: int,
    rpc_url: str,
    private_key: str,
    user_address: str,
) -> int:
    """Fetch API, print each planned PROXY redeem; no Builder auth or submit."""
    signer = Signer(private_key, chain_id)
    from_addr = signer.address()
    derived_proxy = derive_proxy_wallet(from_addr, PROXY_FACTORY)

    print("Dry-run: no relayer POST; Builder credentials not required.")
    print(f"Polygon RPC: {rpc_url}")
    print(f"Owner signer: {from_addr}")
    print(f"Data API user (USER_ADDRESS): {user_address}")
    print(f"Derived proxy (from key, CREATE2): {derived_proxy}")
    if derived_proxy.lower() != to_checksum_address(user_address).lower():
        print(
            "Note: USER_ADDRESS differs from derived proxy — ensure this matches how "
            "Polymarket lists positions for your login.",
            file=sys.stderr,
        )

    try:
        positions = fetch_redeemable_positions(user_address)
    except Exception as e:
        print(f"Data API error: {e}", file=sys.stderr)
        return 1

    planned = _planned_rows(positions, set())
    print(f"\nRedeemable rows from API: {len(positions)}")
    print(f"Planned unique conditionIds: {len(planned)}")
    if not planned:
        return 0

    try:
        rp = _relayer_get(
            relayer_url,
            GET_RELAY_PAYLOAD,
            {"address": from_addr, "type": "PROXY"},
        )
    except Exception as e:
        print(f"Relay-payload GET failed (optional for dry-run): {e}", file=sys.stderr)
        rp = {}

    relay_a = rp.get("address")
    nonce = rp.get("nonce")
    if relay_a is None or nonce is None:
        print(
            "Could not fetch relay address/nonce; listing conditions only.",
            file=sys.stderr,
        )
        for i, p in enumerate(planned, 1):
            cid = p.get("conditionId")
            print(f"  {i}. {cid} | {p.get('title', '')!r}")
        return 0

    print(
        "Dry-run uses one relay nonce for all previews; the live loop fetches a fresh nonce per redeem.",
        file=sys.stderr,
    )
    for i, p in enumerate(planned, 1):
        cid = str(p.get("conditionId"))
        title = p.get("title") or ""
        try:
            inner = encode_redeem_positions_calldata(cid)
            payload = build_proxy_submit_payload(
                private_key=private_key,
                chain_id=chain_id,
                rpc_url=rpc_url,
                inner_to=CTF_ADDRESS,
                inner_calldata=inner,
                relay_address=str(relay_a),
                nonce=str(nonce),
                metadata="redeem positions (dry-run)",
            )
        except Exception as e:
            print(f"  {i}. {cid} — encode/build error: {e}", file=sys.stderr)
            continue
        d = payload["data"]
        d_preview = d[:22] + "..." + d[-8:] if len(d) > 40 else d
        sig = payload["signature"]
        sig_prev = sig[:18] + "..." if len(sig) > 22 else sig
        print(
            f"  {i}. condition={cid}\n"
            f"      title={title!r}\n"
            f"      proxyWallet={payload['proxyWallet']}  gasLimit="
            f"{payload['signatureParams']['gasLimit']}\n"
            f"      data ({len(d)} chars)={d_preview}\n"
            f"      signature={sig_prev}\n"
            f"      -> would POST to {relayer_url}{SUBMIT_TRANSACTION}"
        )
    return 0


def fetch_redeemable_positions(user: str) -> list[dict[str, Any]]:
    r = requests.get(
        POSITIONS_URL,
        params={
            "user": user,
            "redeemable": "true",
            "limit": 500,
            "offset": 0,
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def main(argv: list[str] | None = None) -> int:
    import os

    parser = argparse.ArgumentParser(
        description="Poll Polymarket redeemable positions and redeem via PROXY relayer (gasless)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="One-shot: fetch positions, print planned redeems and signed payload preview; no HTTP submit",
    )
    args = parser.parse_args(argv)

    _load_env()

    relayer_url = (os.getenv("RELAYER_URL") or DEFAULT_RELAYER).strip().rstrip("/")
    chain_id = int(os.getenv("CHAIN_ID") or "137")
    rpc_url = (
        (os.getenv("POLYGON_RPC_URL") or os.getenv("POLY_RPC_URL") or "").strip()
        or "https://polygon-bor.publicnode.com"
    )
    private_key = (
        os.getenv("PRIVATE_KEY")
        or os.getenv("POLYMARKET_PRIVATE_KEY")
        or os.getenv("POLY_PRIVATE_KEY")
        or ""
    ).strip()
    user_address = (
        (os.getenv("USER_ADDRESS") or os.getenv("POLYMARKET_WALLET_ADDRESS") or "").strip()
    )
    poll_ms = int(os.getenv("POLL_MS") or "60000")

    if not private_key or not user_address:
        print(
            "Set PRIVATE_KEY and USER_ADDRESS (or POLYMARKET_PRIVATE_KEY / "
            "POLY_PRIVATE_KEY and POLYMARKET_WALLET_ADDRESS) in root .env",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        return dry_run_once(
            relayer_url=relayer_url,
            chain_id=chain_id,
            rpc_url=rpc_url,
            private_key=private_key,
            user_address=user_address,
        )

    bk, bs, bp = _builder_creds_from_env()
    if not bk or not bs or not bp:
        print(
            "Set BUILDER_API_KEY, BUILDER_SECRET, BUILDER_PASS_PHRASE "
            "(or POLYMARKET_BUILDER_* )",
            file=sys.stderr,
        )
        return 2
    if bk.startswith("."):
        print(
            "Warning: BUILDER_API_KEY starts with '.' — causes 401; remove the leading dot.",
            file=sys.stderr,
        )

    builder_config = BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(key=bk, secret=bs, passphrase=bp),
    )

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if w3.is_connected():
        acct = w3.eth.account.from_key(private_key)
        print(f"Polygon RPC: {rpc_url}")
        print(f"Owner signer: {acct.address}")
    else:
        print(f"Polygon RPC: {rpc_url} (not connected; estimates may fail)")
        print("Owner signer: <from key>")

    print(f"Proxy wallet (Data API user): {user_address}")
    print("Relayer mode: PROXY")

    claimed: set[str] = set()

    while True:
        try:
            positions = fetch_redeemable_positions(user_address)
            print(f"\nFound {len(positions)} redeemable rows")

            seen_round: set[str] = set()
            for p in positions:
                condition_id = p.get("conditionId")
                redeemable = bool(p.get("redeemable"))
                title = p.get("title") or ""
                outcome = p.get("outcome") or ""
                size = p.get("size")

                if not redeemable or not condition_id:
                    continue
                if condition_id in claimed or condition_id in seen_round:
                    continue

                print(
                    f"Redeeming condition={condition_id} title={title!r} "
                    f"outcome={outcome} size={size}"
                )
                inner = encode_redeem_positions_calldata(str(condition_id))

                from_addr = Signer(private_key, chain_id).address()
                rp = _relayer_get(
                    relayer_url,
                    GET_RELAY_PAYLOAD,
                    {"address": from_addr, "type": "PROXY"},
                )
                relay_a = rp.get("address")
                nonce = rp.get("nonce")
                if not relay_a or nonce is None:
                    raise RuntimeError(f"Bad relay-payload: {rp!r}")

                payload = build_proxy_submit_payload(
                    private_key=private_key,
                    chain_id=chain_id,
                    rpc_url=rpc_url,
                    inner_to=CTF_ADDRESS,
                    inner_calldata=inner,
                    relay_address=str(relay_a),
                    nonce=str(nonce),
                    metadata="redeem positions",
                )
                resp = _post_submit(relayer_url, builder_config, payload)
                tx_id = resp.get("transactionID")
                if not tx_id:
                    raise RuntimeError(f"Unexpected submit response: {resp!r}")

                row = _poll_transaction(relayer_url, tx_id)
                if row:
                    print(
                        "Redeem completed:",
                        row.get("transactionHash") or row.get("state"),
                    )
                else:
                    print("Redeem submitted; poll timed out for", tx_id)

                claimed.add(str(condition_id))
                seen_round.add(str(condition_id))

        except Exception as e:
            print(f"Loop error: {e}", file=sys.stderr)

        time.sleep(poll_ms / 1000.0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        raise SystemExit(0)
