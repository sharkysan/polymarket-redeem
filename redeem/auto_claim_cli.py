"""
Redeem resolved Polymarket positions by shelling out to the Polymarket CLI.

Top-level .env only:
  - POLYMARKET_PRIVATE_KEY / POLY_PRIVATE_KEY / PRIVATE_KEY
  - POLYMARKET_WALLET_ADDRESS / USER_ADDRESS
  - POLY_CLI_BIN (optional, default: polymarket)
  - POLY_CLI_POLL_MS (optional, default: 60000)

Examples (run from repo root):
  python redeem/auto_claim_cli.py --dry-run
  python redeem/auto_claim_cli.py
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

POSITIONS_URL = "https://data-api.polymarket.com/positions"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_env() -> None:
    load_dotenv(_repo_root() / ".env")


def _private_key_from_env() -> str:
    return (
        os.getenv("POLYMARKET_PRIVATE_KEY")
        or os.getenv("POLY_PRIVATE_KEY")
        or os.getenv("PRIVATE_KEY")
        or ""
    ).strip()


def _user_address_from_env() -> str:
    return (os.getenv("POLYMARKET_WALLET_ADDRESS") or os.getenv("USER_ADDRESS") or "").strip()


def fetch_redeemable_positions(user: str) -> list[dict[str, Any]]:
    r = requests.get(
        POSITIONS_URL,
        params={"user": user, "redeemable": "true", "limit": 500, "offset": 0},
        timeout=20,
    )
    r.raise_for_status()
    payload = r.json()
    return payload if isinstance(payload, list) else []


def planned_unique_conditions(positions: list[dict[str, Any]], claimed: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen_round: set[str] = set()
    for p in positions:
        cid = p.get("conditionId")
        if not cid or not bool(p.get("redeemable")):
            continue
        s = str(cid)
        if s in claimed or s in seen_round:
            continue
        seen_round.add(s)
        out.append(p)
    return out


def run_redeem_with_cli(cli_bin: str, private_key: str, condition_id: str) -> subprocess.CompletedProcess[str]:
    cmd = [cli_bin, "ctf", "redeem", "--condition", condition_id, "--private-key", private_key]
    return subprocess.run(cmd, text=True, capture_output=True, check=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Redeem Polymarket positions by invoking `polymarket ctf redeem`."
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch + plan only; do not invoke CLI")
    parser.add_argument("--once", action="store_true", help="Run a single pass then exit")
    args = parser.parse_args(argv)

    _load_env()

    cli_bin = (os.getenv("POLY_CLI_BIN") or "polymarket").strip()
    poll_ms = int((os.getenv("POLY_CLI_POLL_MS") or os.getenv("POLL_MS") or "60000").strip())
    private_key = _private_key_from_env()
    user_address = _user_address_from_env()

    if not user_address:
        print("Set POLYMARKET_WALLET_ADDRESS or USER_ADDRESS in root .env", file=sys.stderr)
        return 2
    if not args.dry_run and not private_key:
        print(
            "Set POLYMARKET_PRIVATE_KEY (or POLY_PRIVATE_KEY / PRIVATE_KEY) in root .env",
            file=sys.stderr,
        )
        return 2
    if not args.dry_run and shutil.which(cli_bin) is None:
        print(
            f"Polymarket CLI not found: {cli_bin!r}. Install it or set POLY_CLI_BIN.",
            file=sys.stderr,
        )
        return 2

    print(f"Data API user: {user_address}")
    print(f"CLI binary: {cli_bin}")
    print(f"Mode: {'dry-run' if args.dry_run else 'live'}")

    claimed: set[str] = set()
    while True:
        try:
            rows = fetch_redeemable_positions(user_address)
            planned = planned_unique_conditions(rows, claimed)
            print(f"\nRows={len(rows)} | planned unique conditionIds={len(planned)}")

            for i, p in enumerate(planned, 1):
                cid = str(p.get("conditionId"))
                title = str(p.get("title") or "")
                outcome = str(p.get("outcome") or "")
                size = p.get("size")

                if args.dry_run:
                    print(
                        f"  {i}. would run: {cli_bin} ctf redeem --condition {cid} "
                        f"| title={title!r} outcome={outcome!r} size={size}"
                    )
                    claimed.add(cid)
                    continue

                print(f"  {i}. redeem {cid} | title={title!r} outcome={outcome!r} size={size}")
                proc = run_redeem_with_cli(cli_bin, private_key, cid)
                if proc.returncode == 0:
                    if proc.stdout.strip():
                        print(proc.stdout.strip())
                    claimed.add(cid)
                else:
                    msg = proc.stderr.strip() or proc.stdout.strip() or f"exit={proc.returncode}"
                    print(f"     CLI error: {msg}", file=sys.stderr)

        except Exception as e:
            print(f"Loop error: {e}", file=sys.stderr)

        if args.once or args.dry_run:
            return 0
        time.sleep(max(1000, poll_ms) / 1000.0)


if __name__ == "__main__":
    raise SystemExit(main())
