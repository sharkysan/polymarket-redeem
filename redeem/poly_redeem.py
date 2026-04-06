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
  POLY_RPC_URL              Polygon JSON-RPC for on-chain balance checks (default public node)
  POLY_REDEEM_VERIFY_ONCHAIN  set 0/false to skip CTF balance filter (API rows only)
  POLY_REDEEM_ONCHAIN_MODE  balance | dual | simulate  (default dual = query CTF + NegRisk adapter)
  POLY_REDEEM_AGGRESSIVE_ZERO  if 1 with dual mode, treat a lone 0 balance as redeemed when the other RPC fails
  POLY_REDEEM_AUTO_DEPLOY_SAFE  if 1, deploy the Polymarket Gnosis Safe via relayer before redeem (signer EOA must match account)

Examples
  python redeem/poly_redeem.py --dry-run
  python redeem/poly_redeem.py --dry-run -v
  python redeem/poly_redeem.py --batch 8 --yes
  python redeem/poly_redeem.py --limit 3
  python redeem/poly_redeem.py --deploy-safe
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import requests
from dotenv import load_dotenv
from eth_abi import encode as eth_encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

from py_builder_relayer_client.builder.derive import derive
from py_builder_relayer_client.client import RelayClient
from py_builder_relayer_client.config import get_contract_config
from py_builder_relayer_client.exceptions import RelayerClientException
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
SEL_ERC1155_BALANCE = keccak(text="balanceOf(address,uint256)")[:4]


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


def _parse_uint256(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    s = str(raw).strip()
    if not s:
        return None
    try:
        return int(s, 10)
    except ValueError:
        pass
    if s.startswith("0x"):
        try:
            return int(s, 16)
        except ValueError:
            return None
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


def erc1155_balance_of(
    rpc_url: str,
    token_contract: str,
    owner: str,
    token_id: int,
    *,
    timeout: float = 15.0,
) -> int | None:
    """
    ERC1155 balance on ``token_contract``. Returns None if the call failed (treat as unknown).
    """
    try:
        owner_c = to_checksum_address(owner)
        contract_c = to_checksum_address(token_contract)
    except ValueError:
        return None
    call_data = SEL_ERC1155_BALANCE + eth_encode(
        ["address", "uint256"], [owner_c, token_id]
    )
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": contract_c, "data": "0x" + call_data.hex()}, "latest"],
        "id": 1,
    }
    try:
        r = requests.post(rpc_url.rstrip("/"), json=payload, timeout=timeout)
        r.raise_for_status()
        body = r.json()
    except Exception:
        return None
    err = body.get("error") if isinstance(body, dict) else None
    if err:
        return None
    res = body.get("result") if isinstance(body, dict) else None
    if not res or not isinstance(res, str):
        return None
    try:
        return int(res, 16)
    except ValueError:
        return None


def _signer_eoa_from_env() -> str | None:
    pk = (os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("POLY_PRIVATE_KEY") or "").strip()
    if not pk:
        return None
    try:
        return Account.from_key(pk).address
    except Exception:
        return None


def _expected_polymarket_safe(eoa_address: str) -> str:
    cfg = get_contract_config(CHAIN_ID)
    return derive(to_checksum_address(eoa_address), cfg.safe_factory)


def _ctf_holder_addresses_for_row(
    pos: dict[str, Any], query_wallet: str, eoa_for_safe: str | None
) -> list[str]:
    """
    Tokens for builder/Safe flow sit on the Gnosis Safe derived from the signing EOA,
    not necessarily on ``proxyWallet``. Check every distinct candidate.
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(addr: str) -> None:
        if not addr.strip():
            return
        try:
            c = to_checksum_address(addr.strip())
        except ValueError:
            return
        k = c.lower()
        if k in seen:
            return
        seen.add(k)
        out.append(c)

    add(query_wallet)
    pw = pos.get("proxyWallet")
    if isinstance(pw, str):
        add(pw)
    if eoa_for_safe:
        try:
            add(_expected_polymarket_safe(eoa_for_safe))
        except Exception:
            pass
    return out


def _token_contract_for_position(pos: dict[str, Any]) -> str:
    """Neg-risk tokens live on the NEG_RISK_ADAPTER; standard CTF tokens on CTF."""
    neg = _coerce_bool_neg_risk(pos.get("negativeRisk"))
    return NEG_RISK_ADAPTER if neg is True else CTF


def _aggregate_balance(
    rpc_url: str,
    token_contract: str,
    holders: list[str],
    token_id: int,
    *,
    attempts: int = 3,
    timeout: float = 15.0,
) -> int | None:
    """
    Max balance across holders on the given ERC1155 contract.
    Returns None if every RPC read failed. Returns 0 if all successful reads were zero.
    """
    resolved: list[int] = []
    for h in holders:
        b = erc1155_balance_of_retry(
            rpc_url, token_contract, h, token_id, attempts=attempts, timeout=timeout
        )
        if b is not None:
            resolved.append(b)
    if not resolved:
        return None
    return max(resolved)


def eth_call(
    rpc_url: str,
    *,
    to: str,
    data: str,
    from_addr: str | None = None,
    timeout: float = 30.0,
) -> tuple[bool, str | None]:
    """
    Returns (ok, err_detail). ok True means JSON-RPC returned a result (may be 0x).
    ok False means an error object or HTTP/transport failure; err_detail is short text.
    """
    try:
        t = to_checksum_address(to)
        call: dict[str, Any] = {
            "to": t,
            "data": data if data.startswith("0x") else "0x" + data,
        }
        if from_addr:
            call["from"] = to_checksum_address(from_addr)
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [call, "latest"],
            "id": 1,
        }
        r = requests.post(rpc_url.rstrip("/"), json=payload, timeout=timeout)
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        return False, str(e)[:200]
    if not isinstance(body, dict):
        return False, "bad json"
    if body.get("error"):
        err = body["error"]
        if isinstance(err, dict):
            msg = str(err.get("message", err))
        else:
            msg = str(err)
        return False, msg[:220]
    if body.get("result") is not None:
        return True, None
    return False, "no result"


def build_redeem_calldata(
    pos: dict[str, Any],
    *,
    neg_fill_amount: int | None = None,
) -> tuple[str, str] | None:
    """
    (to_checksum, data with 0x prefix) for redeemPositions, or None if skipped.
    ``neg_fill_amount`` overrides API size×1e6 for neg-risk (use on-chain balance when known).
    """
    cid = _normalize_cid(str(pos.get("conditionId") or pos.get("condition_id") or ""))
    if not cid:
        return None
    cond = bytes.fromhex(cid[2:])
    neg = _coerce_bool_neg_risk(pos.get("negativeRisk"))

    if neg is True:
        size_raw = (
            int(neg_fill_amount)
            if neg_fill_amount is not None
            else int(float(pos.get("size", 0)) * 1e6)
        )
        if size_raw <= 0:
            return None
        idx = int(pos.get("outcomeIndex", 0))
        n_slots = max(2, idx + 1)
        amounts = [0] * n_slots
        amounts[idx] = size_raw
        payload = SEL_REDEEM_NEG + eth_encode(
            ["bytes32", "uint256[]"], [cond, amounts]
        )
        return (to_checksum_address(NEG_RISK_ADAPTER), "0x" + payload.hex())

    if neg is False:
        payload = SEL_REDEEM_CTF + eth_encode(
            ["address", "bytes32", "bytes32", "uint256[]"],
            [USDC_e, b"\x00" * 32, cond, [1, 2]],
        )
        return (to_checksum_address(CTF), "0x" + payload.hex())

    return None


def erc1155_balance_of_retry(
    rpc_url: str,
    token_contract: str,
    owner: str,
    token_id: int,
    *,
    attempts: int = 3,
    timeout: float = 15.0,
) -> int | None:
    last: int | None = None
    for i in range(max(1, attempts)):
        last = erc1155_balance_of(
            rpc_url, token_contract, owner, token_id, timeout=timeout
        )
        if last is not None:
            return last
        if i + 1 < attempts:
            time.sleep(0.35 * (i + 1))
    return last


def _dual_balance_decision(
    rpc_url: str,
    holders: list[str],
    token_id: int,
    *,
    aggressive_single_zero: bool,
    attempts: int = 3,
    timeout: float = 15.0,
) -> tuple[int | None, str]:
    """
    Query CTF and NegRisk adapter. Returns (balance, debug_note).
    ``None`` balance => inconclusive, keep row. ``0`` => drop. ``>0`` => keep.
    """
    b_ctf = _aggregate_balance(rpc_url, CTF, holders, token_id, attempts=attempts, timeout=timeout)
    b_ad = _aggregate_balance(
        rpc_url, NEG_RISK_ADAPTER, holders, token_id, attempts=attempts, timeout=timeout
    )
    if b_ctf is not None and b_ad is not None:
        m = max(b_ctf, b_ad)
        return m, f"CTF={b_ctf} adapter={b_ad}"
    if aggressive_single_zero:
        if b_ctf is not None and b_ctf == 0 and b_ad is None:
            return 0, "aggressive CTF=0 adapter_RPC_fail"
        if b_ad is not None and b_ad == 0 and b_ctf is None:
            return 0, "aggressive adapter=0 CTF_RPC_fail"
    if b_ctf is not None:
        return (None if b_ctf == 0 else b_ctf), f"only_CTF={b_ctf} adapter={'fail' if b_ad is None else b_ad}"
    if b_ad is not None:
        return (None if b_ad == 0 else b_ad), f"only_adapter={b_ad} CTF={'fail' if b_ctf is None else b_ctf}"
    return None, "both_fail"


def filter_positions_still_onchain(
    positions: list[dict[str, Any]],
    query_wallet: str,
    *,
    eoa_for_safe: str | None,
    safe_address: str | None,
    rpc_url: str,
    verbose: bool,
    rpc_timeout: float = 15.0,
    mode: str = "dual",
    aggressive_single_zero: bool = False,
) -> list[dict[str, Any]]:
    """
    Drop rows that look already redeemed on-chain.

    * ``mode=dual`` — ERC1155 balance on **both** CTF and NegRisk adapter (recommended).
    * ``mode=balance`` — only the contract implied by API ``negativeRisk`` (legacy).
    * ``mode=simulate`` — ``eth_call`` redeem calldata from the Safe (needs ``safe_address``).
    """
    kept: list[dict[str, Any]] = []
    n_already = 0
    n_no_asset = 0
    n_rpc_fail = 0
    mode_l = (mode or "dual").strip().lower()
    use_sim = mode_l == "simulate" and safe_address
    filter_label = mode_l
    if mode_l == "simulate" and not safe_address:
        print(
            f"{_ts()}  Warning: simulate mode needs a Safe address; "
            f"set POLYMARKET_PRIVATE_KEY so we can derive it — falling back to dual.",
            flush=True,
        )
        mode_l = "dual"

    for p in positions:
        holders = _ctf_holder_addresses_for_row(p, query_wallet, eoa_for_safe)
        tid = _parse_uint256(p.get("asset"))
        if tid is None:
            n_no_asset += 1
            _log(
                f"keep  no asset id for on-chain check: {str(p.get('title', '?'))[:50]}",
                verbose,
                force=False,
            )
            kept.append(p)
            continue
        if not holders:
            n_rpc_fail += 1
            kept.append(p)
            continue

        bal: int | None
        reason = ""

        if use_sim and safe_address:
            neg = _coerce_bool_neg_risk(p.get("negativeRisk"))
            neg_amt: int | None = None
            if neg is True:
                b_ctf = _aggregate_balance(
                    rpc_url, CTF, holders, tid, attempts=2, timeout=rpc_timeout
                )
                b_ad = _aggregate_balance(
                    rpc_url, NEG_RISK_ADAPTER, holders, tid, attempts=2, timeout=rpc_timeout
                )
                known = [x for x in (b_ctf, b_ad) if x is not None]
                if not known:
                    n_rpc_fail += 1
                    kept.append(p)
                    continue
                neg_amt = max(known)
                if neg_amt <= 0:
                    n_already += 1
                    continue
            pair = build_redeem_calldata(p, neg_fill_amount=neg_amt)
            if not pair:
                n_rpc_fail += 1
                kept.append(p)
                continue
            to_c, data = pair
            ok, err = eth_call(
                rpc_url, to=to_c, data=data, from_addr=safe_address, timeout=rpc_timeout + 10.0
            )
            if not ok:
                reason = err or "revert"
                if verbose:
                    _log(
                        f"simulate not redeemable ({reason[:80]}): {str(p.get('title', '?'))[:40]}",
                        True,
                        force=False,
                    )
                n_already += 1
                continue
            bal = 1
        elif mode_l == "balance":
            token_contract = _token_contract_for_position(p)
            b = _aggregate_balance(
                rpc_url, token_contract, holders, tid, attempts=3, timeout=rpc_timeout
            )
            bal, reason = (b, str(token_contract)[:10] if b is not None else "fail")
        else:
            bal, reason = _dual_balance_decision(
                rpc_url,
                holders,
                tid,
                aggressive_single_zero=aggressive_single_zero,
                attempts=3,
                timeout=rpc_timeout,
            )

        if bal is None:
            n_rpc_fail += 1
            _log(
                f"keep  balance/sim unknown [{reason}] ({len(holders)} holder(s), "
                f"asset {str(tid)[:12]}…) {str(p.get('title', '?'))[:30]}",
                verbose,
                force=False,
            )
            kept.append(p)
            continue
        if bal == 0:
            n_already += 1
            t = p.get("title", "?")
            if verbose:
                print(
                    f"{_ts()}  skip  already redeemed ({reason})  {str(t)[:50]}",
                    flush=True,
                )
            continue
        kept.append(p)

    n_api = len(positions)
    print(
        f"{_ts()}  On-chain filter [{filter_label}]: {len(kept)}/{n_api} rows still redeemable, "
        f"{n_already} dropped as already done, "
        f"{n_no_asset} missing asset id (kept), {n_rpc_fail} inconclusive (kept)",
        flush=True,
    )
    return kept


def position_to_tx(pos: dict[str, Any]) -> tuple[SafeTransaction, str] | None:
    """
    Build one Safe tx + short label. Returns None if skipped.
    """
    cid = _normalize_cid(str(pos.get("conditionId") or pos.get("condition_id") or ""))
    if not cid:
        return None
    title = str(pos.get("title") or cid[:12])
    pair = build_redeem_calldata(pos)
    if not pair:
        return None
    to_c, data = pair
    txn = SafeTransaction(
        to=to_c,
        operation=OperationType.Call,
        data=data,
        value="0",
    )
    neg = _coerce_bool_neg_risk(pos.get("negativeRisk"))
    suffix = "(neg-risk)" if neg is True else "(CTF)" if neg is False else ""
    return txn, f"{title[:48]} {suffix}".strip()


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


def cmd_deploy_safe(cfg: RelayerConfig) -> int:
    """Deploy the Polymarket Gnosis Safe derived from ``cfg.private_key`` (one-time per EOA)."""
    client = make_client(cfg)
    safe = client.get_expected_safe()
    print(f"{_ts()}  Polymarket Safe for this signer: {safe}", flush=True)
    if client.get_deployed(safe):
        print(f"{_ts()}  Already deployed — nothing to do.", flush=True)
        return 0
    print(f"{_ts()}  Submitting Safe deployment via relayer…", flush=True)
    try:
        resp = client.deploy()
        resp.wait()
    except RelayerClientException as e:
        if "already deployed" in str(e).lower():
            print(f"{_ts()}  Safe is already deployed.", flush=True)
            return 0
        print(f"{_ts()}  Deploy failed: {e}", flush=True)
        return 1
    except Exception as e:
        print(f"{_ts()}  Deploy failed: {e}", flush=True)
        return 1
    print(f"{_ts()}  Safe deployed. You can run redemption without this error.", flush=True)
    return 0


def ensure_safe_deployed_for_redeem(client: RelayClient, *, auto_deploy: bool) -> bool:
    """
    Builder relayer ``execute`` requires the derived Safe to exist on Polygon.
    Returns False if deployment is missing and ``auto_deploy`` is off.
    """
    safe = client.get_expected_safe()
    if client.get_deployed(safe):
        return True
    print(
        f"{_ts()}  Relayer needs your Polymarket Gnosis Safe on-chain, but it is not deployed yet:\n"
        f"      {safe}\n"
        f"      Deploy once with:  python redeem/poly_redeem.py --deploy-safe\n"
        f"      Or set POLY_REDEEM_AUTO_DEPLOY_SAFE=1 to deploy automatically before redeem.",
        flush=True,
    )
    if not auto_deploy:
        return False
    print(f"{_ts()}  POLY_REDEEM_AUTO_DEPLOY_SAFE=1 — deploying Safe via relayer…", flush=True)
    try:
        resp = client.deploy()
        resp.wait()
    except RelayerClientException as e:
        if "already deployed" in str(e).lower():
            return True
        print(f"{_ts()}  Auto-deploy failed: {e}", flush=True)
        return False
    except Exception as e:
        print(f"{_ts()}  Auto-deploy failed: {e}", flush=True)
        return False
    if not client.get_deployed(safe):
        print(f"{_ts()}  Deploy tx finished but relayer still reports Safe not deployed — retry shortly.", flush=True)
        return False
    return True


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
    p.add_argument(
        "--no-on-chain-verify",
        action="store_true",
        help="do not drop API rows with zero CTF outcome-token balance (stale API mode)",
    )
    p.add_argument(
        "--deploy-safe",
        action="store_true",
        help="only deploy the Polymarket Gnosis Safe for this signer via relayer (fix 'Safe is not deployed')",
    )
    args = p.parse_args(argv)

    if args.deploy_safe:
        _, cfg_ds = load_env_config(require_signing=True)
        if cfg_ds is None:
            return 2
        return cmd_deploy_safe(cfg_ds)

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

    verify_raw = os.getenv("POLY_REDEEM_VERIFY_ONCHAIN", "1").strip().lower()
    verify_onchain = verify_raw not in ("0", "false", "no", "off") and not args.no_on_chain_verify
    if verify_onchain:
        rpc_url = (os.getenv("POLY_RPC_URL") or "https://polygon-bor.publicnode.com").strip()
        if rpc_url:
            eoa = _signer_eoa_from_env()
            safe_addr: str | None = None
            if eoa:
                try:
                    safe_addr = _expected_polymarket_safe(eoa)
                    _log(
                        f"On-chain check uses Safe {safe_addr[:10]}… + proxyWallet + wallet (mode "
                        f"{(os.getenv('POLY_REDEEM_ONCHAIN_MODE') or 'dual').strip().lower()})",
                        args.verbose,
                        force=False,
                    )
                except Exception:
                    safe_addr = None
            else:
                print(
                    f"{_ts()}  Warning: no POLYMARKET_PRIVATE_KEY in env — on-chain filter cannot "
                    f"derive your Polymarket Safe; only proxyWallet / configured wallet are used. "
                    f"Add your key, set POLY_REDEEM_ONCHAIN_MODE=simulate with key, or use --no-on-chain-verify.",
                    flush=True,
                )
            mode = (os.getenv("POLY_REDEEM_ONCHAIN_MODE") or "dual").strip().lower()
            if mode not in ("dual", "balance", "simulate"):
                print(f"{_ts()}  Invalid POLY_REDEEM_ONCHAIN_MODE={mode!r}, using dual", flush=True)
                mode = "dual"
            aggressive = os.getenv("POLY_REDEEM_AGGRESSIVE_ZERO", "0").strip().lower() in (
                "1",
                "true",
                "yes",
            )
            positions = filter_positions_still_onchain(
                positions,
                user,
                eoa_for_safe=eoa,
                safe_address=safe_addr,
                rpc_url=rpc_url,
                verbose=args.verbose,
                mode=mode,
                aggressive_single_zero=aggressive,
            )
        if not positions:
            print(f"{_ts()}  No positions left to redeem (all already redeemed on-chain?)", flush=True)
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

    print(
        f"{_ts()}  Planned {len(built)} redemption(s) from {len(positions)} position(s) after filters",
        flush=True,
    )
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
    auto_deploy_safe = os.getenv("POLY_REDEEM_AUTO_DEPLOY_SAFE", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if not ensure_safe_deployed_for_redeem(client, auto_deploy=auto_deploy_safe):
        return 3

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
