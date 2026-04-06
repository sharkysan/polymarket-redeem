# polymarket-redeem

Small Python utility to **redeem resolved Polymarket positions on Polygon** using the Polymarket builder relayer. It pulls `redeemable=true` rows from the Polymarket Data API (including dust), builds the right contract calls for standard CTF vs neg-risk markets, and submits them through the relayer.

## Setup

1. Python 3.10+ recommended.

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Configure environment variables:

   ```bash
   # Windows
   copy env.template .env
   # macOS / Linux
   cp env.template .env
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

Optional tuning: `POLY_REDEEM_BATCH`, `POLY_REDEEM_RELAYER_WAIT`, `POLY_REDEEM_DATA_RETRIES`. See `env.template` and the module docstring in `redeem/poly_redeem.py`.

## Security

- Keep `.env` local and out of version control.
- Treat your private key and builder credentials like production secrets.

## License

Specify your license here if you publish this repository.
