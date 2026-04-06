#!/usr/bin/env python3
"""
Redeem resolved Polymarket positions on Polygon via the Polymarket builder relayer.

  • Pulls ``redeemable=true`` rows from the Data API (includes dust).
  • Standard CTF markets → ``redeemPositions`` on the conditional-tokens contract.
  • Neg-risk markets → ``redeemPositions`` on the neg-risk adapter.

Environment (execution)
  POLYMARKET_PRIVATE_KEY or POLY_PRIVATE_KEY
  POLYMARKET_WALLET_ADDRESS   (proxy/Safe that holds positions)
  POLYMARKET_BUILDER_API_KEY, POLYMARKET_BUILDER_SECRET, POLYMARKET_BUILDER_PASSPHRASE
  POLYMARKET_SIGNATURE_TYPE or POLY_SIGNATURE_TYPE   default 1  (1=proxy, 0/2=Safe)

Optional
  POLY_REDEEM_BATCH         max redemptions per relayer ``execute`` (default 1; try 5–12 if stable)
  POLY_REDEEM_RELAYER_WAIT  seconds to sleep on HTTP 429/1015 (default 60)
  POLY_REDEEM_DATA_RETRIES  Data API retries (default 3)

Examples
  python redeem/poly_redeem.py --dry-run
  python redeem/poly_redeem.py --dry-run -v
  python redeem/poly_redeem.py --batch 8 --yes
  python redeem/poly_redeem.py --limit 3
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

import requests
from dotenv import load_dotenv
from eth_abi import encode as eth_encode
from eth_utils import keccak

from py_builder_relayer_client.client import RelayClient
from py_builder_relayer_client.models import OperationType, SafeTransaction
from py_builder_signing_sdk.config import BuilderApiKeyCreds, BuilderConfig

# ── Polygon mainnet contracts ─────────────────────────────────────────────
USDC_e = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e on Polygon (bridged)
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

DATA_API = "https://data-api.polymarket.com/positions"
RELAYER_URL = "https://relayer-v2.polymarket.com"
CHAIN_ID = 137

SEL_REDEEM_CTF = keccak(
    text="redeemPositions(address,bytes32,bytes32,uint256[])"
)[:4]
SEL_REDEEM_NEG = keccak(text="redeemPositions(bytes32,uint256[])")[:4]


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _log(msg: str, verbose: bool, *, force: bool = False) -> None:
    if force or verbose:
        print(f"{_ts()}  {msg}", flush=True)


def _coerce_bool_neg_risk(raw: Any) -> bool | None:
    if raw is True or raw is False:
        return raw
    if isinstance(raw, str):
        v = raw.strip().lower()
        if v in ("true", "1", "yes"):
            return True
        if v in ("false", "0", "no"):
            return False
    return None


def _normalize_cid(raw: str) -> str | None:
    cid = (raw or "").strip()
    if not cid:
        return None
    if not cid.startswith("0x"):
        cid = "0x" + cid
    try:
        bytes.fromhex(cid[2:])
    except ValueError:
        return None
    if len(cid) != 66:
        return None
    return cid


def fetch_positions(
    user: str,
    *,
    timeout: float = 20.0,
    max_retries: int = 3,
    relayer_wait: float = 60.0,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    params = {"user": user, "redeemable": "true", "sizeThreshold": 0}
    session = requests.Session()
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            r = session.get(DATA_API, params=params, timeout=timeout)
            if r.status_code in (429, 503, 502):
                wait = relayer_wait if r.status_code == 429 else min(30.0, 5.0 * (attempt + 1))
                _log(
                    f"Data API HTTP {r.status_code}, retry in {wait:.0f}s "
                    f"({attempt + 1}/{max_retries})",
                    verbose,
                    force=True,
                )
                time.sleep(wait)
                continue
            if not r.ok:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]!r}")
            data = r.json()
            if not isinstance(data, list):
                raise RuntimeError(f"expected list, got {type(data).__name__}")
            return [p for p in data if float(p.get("size", 0)) > 0]
        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise last_err from None
    return []


def position_to_tx(pos: dict[str, Any]) -> tuple[SafeTransaction, str] | None:
    """
    Build one Safe tx + short label. Returns None if skipped.
    """
    cid = _normalize_cid(str(pos.get("conditionId") or pos.get("condition_id") or ""))
    if not cid:
        return None
    title = str(pos.get("title") or cid[:12])
    cond = bytes.fromhex(cid[2:])
    neg = _coerce_bool_neg_risk(pos.get("negativeRisk"))

    if neg is True:
        size_raw = int(float(pos.get("size", 0)) * 1e6)
        if size_raw <= 0:
            return None
        idx = int(pos.get("outcomeIndex", 0))
        n_slots = max(2, idx + 1)
        amounts = [0] * n_slots
        amounts[idx] = size_raw
        payload = SEL_REDEEM_NEG + eth_encode(
            ["bytes32", "uint256[]"], [cond, amounts]
        )
        txn = SafeTransaction(
            to=NEG_RISK_ADAPTER,
            operation=OperationType.Call,
            data="0x" + payload.hex(),
            value="0",
        )
        return txn, f"{title[:48]} (neg-risk)"

    if neg is False:
        payload = SEL_REDEEM_CTF + eth_encode(
            ["address", "bytes32", "bytes32", "uint256[]"],
            [USDC_e, b"\x00" * 32, cond, [1, 2]],
        )
        txn = SafeTransaction(
            to=CTF,
            operation=OperationType.Call,
            data="0x" + payload.hex(),
            value="0",
        )
        return txn, f"{title[:48]} (CTF)"

    return None


def batched(xs: list[Any], n: int) -> Iterator[list[Any]]:
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


@dataclass
class RelayerConfig:
    private_key: str
    wallet_address: str
    signature_type: int
    builder_key: str
    builder_secret: str
    builder_passphrase: str


def make_client(cfg: RelayerConfig) -> RelayClient:
    # py-builder-relayer-client 0.0.1+: RelayClient.execute routes via the Safe
    # derived from the signer; there is no relay_tx_type / RelayerTxType knob.
    return RelayClient(
        RELAYER_URL,
        chain_id=CHAIN_ID,
        private_key=cfg.private_key,
        builder_config=BuilderConfig(
            local_builder_creds=BuilderApiKeyCreds(
                key=cfg.builder_key,
                secret=cfg.builder_secret,
                passphrase=cfg.builder_passphrase,
            )
        ),
    )


def execute_with_retry(
    client: RelayClient,
    txns: list[SafeTransaction],
    note: str,
    *,
    relayer_wait: float,
    verbose: bool,
) -> None:
    try:
        resp = client.execute(txns, note)
        resp.wait()
    except Exception as e:
        status = getattr(e, "status_code", None)
        if status in (429, 1015):
            print(f"{_ts()}  Relayer rate limited ({status}), sleeping {relayer_wait:.0f}s…", flush=True)
            time.sleep(relayer_wait)
            resp = client.execute(txns, note)
            resp.wait()
        else:
            _log(f"relayer error: {e!r}", verbose, force=True)
            raise


def load_env_config(*, require_signing: bool) -> tuple[str | None, RelayerConfig | None]:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    sig_raw = os.getenv("POLYMARKET_SIGNATURE_TYPE", os.getenv("POLY_SIGNATURE_TYPE", "1"))
    try:
        sig = int(sig_raw)
    except ValueError:
        print(f"Invalid POLY_SIGNATURE_TYPE: {sig_raw!r}", file=sys.stderr)
        return None, None

    wallet = (os.getenv("POLYMARKET_WALLET_ADDRESS") or "").strip()
    if not wallet:
        print("Set POLYMARKET_WALLET_ADDRESS", file=sys.stderr)
        return None, None

    if not require_signing:
        return wallet, None

    pk = (os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("POLY_PRIVATE_KEY") or "").strip()
    bk = (os.getenv("POLYMARKET_BUILDER_API_KEY") or "").strip()
    bs = (os.getenv("POLYMARKET_BUILDER_SECRET") or "").strip()
    bp = (os.getenv("POLYMARKET_BUILDER_PASSPHRASE") or "").strip()
    missing = [
        n
        for n, v in [
            ("POLYMARKET_PRIVATE_KEY or POLY_PRIVATE_KEY", pk),
            ("POLYMARKET_BUILDER_API_KEY", bk),
            ("POLYMARKET_BUILDER_SECRET", bs),
            ("POLYMARKET_BUILDER_PASSPHRASE", bp),
        ]
        if not v
    ]
    if missing:
        print("Missing: " + ", ".join(missing), file=sys.stderr)
        return None, None

    return wallet, RelayerConfig(
        private_key=pk,
        wallet_address=wallet,
        signature_type=sig,
        builder_key=bk,
        builder_secret=bs,
        builder_passphrase=bp,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dry-run", action="store_true", help="fetch + plan only, no relayer")
    p.add_argument(
        "-v", "--verbose", action="store_true", help="extra diagnostics"
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="redeem at most N positions (0 = all)",
    )
    p.add_argument(
        "--batch",
        type=int,
        default=int(os.getenv("POLY_REDEEM_BATCH", "1")),
        metavar="N",
        help="Safe txs per relayer submit (env POLY_REDEEM_BATCH, default 1)",
    )
    p.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="skip confirmation when submitting",
    )
    p.add_argument(
        "--user",
        metavar="0x…",
        default="",
        help="override wallet/proxy address for Data API (default: env)",
    )
    args = p.parse_args(argv)

    require_signing = not args.dry_run
    wallet_env, cfg = load_env_config(require_signing=require_signing)
    if wallet_env is None:
        return 2

    user = (args.user or wallet_env).strip()
    batch = max(1, args.batch)
    relayer_wait = float(os.getenv("POLY_REDEEM_RELAYER_WAIT", "60"))
    data_retries = int(os.getenv("POLY_REDEEM_DATA_RETRIES", "3"))

    try:
        positions = fetch_positions(
            user,
            max_retries=data_retries,
            relayer_wait=relayer_wait,
            verbose=args.verbose,
        )
    except Exception as e:
        print(f"{_ts()}  Data API failed: {e}", flush=True)
        return 1

    if not positions:
        print(f"{_ts()}  No redeemable positions for {user[:10]}…", flush=True)
        return 0

    built: list[tuple[SafeTransaction, str, dict[str, Any]]] = []
    for pos in positions:
        out = position_to_tx(pos)
        if out is None:
            neg = pos.get("negativeRisk")
            t = pos.get("title", "?")
            print(f"{_ts()}  skip  {str(t)[:50]}  (negativeRisk={neg!r})", flush=True)
            continue
        txn, label = out
        built.append((txn, label, pos))

    if args.limit > 0:
        built = built[: args.limit]

    print(f"{_ts()}  Planned {len(built)} redemption(s) from {len(positions)} API row(s)", flush=True)
    for _, label, pos in built:
        sz = float(pos.get("size", 0))
        cid = pos.get("conditionId", "")[:16]
        print(f"     • {label}  size={sz:.6g}  {cid}…", flush=True)

    if args.dry_run or not built:
        return 0

    assert cfg is not None
    if not args.yes and len(built) > 0:
        try:
            ans = input(f"Submit {len(built)} redemption(s) in batches of {batch}? [y/N] ")
        except EOFError:
            ans = "n"
        if ans.strip().lower() not in ("y", "yes"):
            print("Aborted.", flush=True)
            return 0

    client = make_client(cfg)
    done = 0
    for chunk in batched(built, batch):
        txns = [t for t, _, _ in chunk]
        labels = [lbl for _, lbl, _ in chunk]
        note = "redeem: " + "; ".join(labels[:3]) + ("…" if len(labels) > 3 else "")
        try:
            execute_with_retry(
                client,
                txns,
                note[:120],
                relayer_wait=relayer_wait,
                verbose=args.verbose,
            )
            done += len(chunk)
            for _, lbl, _ in chunk:
                print(f"{_ts()}  ok    {lbl}", flush=True)
        except Exception as e:
            if batch > 1 and len(chunk) > 1:
                print(
                    f"{_ts()}  batch failed ({e!r}); retrying singles for this batch…",
                    flush=True,
                )
                for txn, lbl, _ in chunk:
                    try:
                        execute_with_retry(
                            client,
                            [txn],
                            lbl[:80],
                            relayer_wait=relayer_wait,
                            verbose=args.verbose,
                        )
                        done += 1
                        print(f"{_ts()}  ok    {lbl}", flush=True)
                    except Exception as e2:
                        print(f"{_ts()}  FAIL  {lbl}: {e2}", flush=True)
            else:
                print(f"{_ts()}  FAIL  {note}: {e}", flush=True)

    print(f"{_ts()}  Finished {done}/{len(built)}", flush=True)
    return 0 if done == len(built) else 1


if __name__ == "__main__":
    raise SystemExit(main())
