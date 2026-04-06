# polymarket-redeem

Small Python utility to **redeem resolved Polymarket positions on Polygon** using the Polymarket builder relayer. It pulls `redeemable=true` rows from the Polymarket Data API (including dust), **drops rows that are already redeemed on-chain** (by default: ERC1155 balance on **both** the CTF and the NegRisk adapter, across **your Polymarket Gnosis Safe** derived from the signing EOA, plus `proxyWallet` and `POLYMARKET_WALLET_ADDRESS`—or via optional `eth_call` simulation), then builds the right contract calls for standard CTF vs neg-risk markets and submits them through the relayer.

For the on-chain filter to see tokens held in the Safe, your **`POLYMARKET_PRIVATE_KEY`** must be in `.env` even for `--dry-run`. Without it, the script only checks `proxyWallet` / wallet address and the filter is often wrong.

Each run prints an **on-chain filter summary** (how many rows still hold tokens vs already redeemed vs RPC issues). Only rows that still hold tokens appear under **Planned** bullet list.

## Setup

1. Python 3.10+ recommended.

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Configure environment variables:

   ```bash
   # Windows
   copy .env.template .env
   # macOS / Linux
   cp .env.template .env
   ```

   Edit `.env` with your keys and wallet address. The script loads `.env` from the **repository root** (next to this README).

## Usage

Dry run — fetch positions and print the redemption plan without submitting:

```bash
python redeem/poly_redeem.py --dry-run
python redeem/poly_redeem.py --dry-run -v
```

Live redemption (prompts for confirmation unless you pass `--yes`):

```bash
python redeem/poly_redeem.py --yes
python redeem/poly_redeem.py --batch 8 --yes
python redeem/poly_redeem.py --limit 3
```

Override the address used for the Data API query:

```bash
python redeem/poly_redeem.py --dry-run --user 0xYourAddress
```

Trust the Data API only (no on-chain balance filter):

```bash
python redeem/poly_redeem.py --dry-run --no-on-chain-verify
```

### “Safe is not deployed”

The builder relayer only submits txs from your **Polymarket Gnosis Safe** (address derived from `POLYMARKET_PRIVATE_KEY`). If you have not used that flow on-chain yet, deploy the Safe once:

```bash
python redeem/poly_redeem.py --deploy-safe
```

Or set `POLY_REDEEM_AUTO_DEPLOY_SAFE=1` in `.env` to deploy automatically before the first redeem.

## Environment variables

| Variable | Required (live) | Description |
|----------|------------------|-------------|
| `POLYMARKET_PRIVATE_KEY` | Yes* | Hex private key for relayer signing |
| `POLYMARKET_WALLET_ADDRESS` | Yes† | Proxy/Safe that holds positions |
| `POLYMARKET_BUILDER_API_KEY` | Yes | Builder API key |
| `POLYMARKET_BUILDER_SECRET` | Yes | Builder secret |
| `POLYMARKET_BUILDER_PASSPHRASE` | Yes | Builder passphrase |
| `POLYMARKET_SIGNATURE_TYPE` | No (default `1`) | `1` = proxy, `0`/`2` = Safe |

\* Not required for `--dry-run`.  
† Required for Data API lookups (and must match the wallet that holds positions).

Optional tuning: `POLY_REDEEM_BATCH`, `POLY_REDEEM_RELAYER_WAIT`, `POLY_REDEEM_DATA_RETRIES`, `POLY_RPC_URL`, `POLY_REDEEM_VERIFY_ONCHAIN` (`0` = no filter, same as `--no-on-chain-verify`).

**On-chain filter** — `POLY_REDEEM_ONCHAIN_MODE` (default `dual`): read ERC1155 balance on **both** CTF and NegRisk adapter; keep a row if **either** balance is &gt; 0; drop only when **both** reads succeed and **both** are 0. If one RPC fails and the other is 0, the row stays (inconclusive) unless `POLY_REDEEM_AGGRESSIVE_ZERO=1`. **Legacy:** `balance` uses only the API `negativeRisk` contract. **Simulation:** `POLY_REDEEM_ONCHAIN_MODE=simulate` uses `eth_call` of the redeem calldata from your derived Safe (needs `POLYMARKET_PRIVATE_KEY`); reverting calls are treated as already redeemed.

See `.env.template` and `redeem/poly_redeem.py`.

## Security

- Keep `.env` local and out of version control.
- Treat your private key and builder credentials like production secrets.

## License

[MIT](LICENSE) — permissive open license; use at your own risk (especially around keys and on-chain actions).
