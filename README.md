# polymarket-redeem

Claim resolved [Polymarket](https://polymarket.com) positions on Polygon via direct PROXY relayer submission (Builder HMAC auth) using `redeem/auto_claim_proxy.py`.

The script reads a top-level `.env`, deduplicates by `conditionId`, and bundles up to `POLY_REDEEM_BATCH_SIZE` redeems per signed submit. It logs to the console and, by default, appends to a **rolling** log file under `logs/polymarket-redeem.log` (see `POLY_REDEEM_LOG_*` below).

## Setup

- Python 3.10+
- Run from repository root:

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate    # macOS / Linux
pip install -r requirements.txt
```

## Configuration

1. Copy [`.env.template`](.env.template) to `.env` in the repository root.
2. Run commands from repository root (`python redeem/...`) so `.env` path resolution stays consistent.

## Quick start

| Step | Action |
|------|--------|
| 1 | Set `POLYMARKET_PRIVATE_KEY` (or `PRIVATE_KEY` / `POLY_PRIVATE_KEY`) and `POLYMARKET_WALLET_ADDRESS` (or `USER_ADDRESS`). |
| 2 | For live submit, set `POLYMARKET_BUILDER_API_KEY`, `POLYMARKET_BUILDER_SECRET`, `POLYMARKET_BUILDER_PASSPHRASE` (or `BUILDER_*` aliases). |
| 3 | Dry-run: `python redeem/auto_claim_proxy.py --dry-run` |

## Commands

| Command | Behavior |
|---------|----------|
| `python redeem/auto_claim_proxy.py --dry-run` | One pass, no submit; prints batched payload previews and key/wallet checks. |
| `python redeem/auto_claim_proxy.py` | Infinite relayer loop; batches redeems per submit and polls transaction state. |

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `POLYMARKET_PRIVATE_KEY` / `PRIVATE_KEY` / `POLY_PRIVATE_KEY` | Yes | Owner EOA private key |
| `POLYMARKET_WALLET_ADDRESS` / `USER_ADDRESS` | Yes | Data API `user` wallet address |
| `POLYMARKET_BUILDER_API_KEY` / `BUILDER_API_KEY` | Relayer live only | Builder key |
| `POLYMARKET_BUILDER_SECRET` / `BUILDER_SECRET` | Relayer live only | Builder secret |
| `POLYMARKET_BUILDER_PASSPHRASE` / `BUILDER_PASS_PHRASE` | Relayer live only | Builder passphrase |
| `RELAYER_URL` | Optional | Relayer endpoint (default `https://relayer-v2.polymarket.com`) |
| `CHAIN_ID` | Optional | Chain id (default `137`) |
| `POLY_RPC_URL` / `POLYGON_RPC_URL` | Optional | Polygon RPC for estimation/checks |
| `POLL_MS` | Optional | Loop poll delay in ms (default `60000`) |
| `POLY_REDEEM_BATCH_SIZE` | Optional | Max redeems per signed submit (default `20`; set `1` for one-per-submit) |
| `POLY_REDEEM_LOG_FILE` | Optional | Log file path (absolute or relative to repo root); default `logs/polymarket-redeem.log` |
| `POLY_REDEEM_LOG_MAX_BYTES` | Optional | Size in bytes before rotation (default `5242880`, 5 MiB) |
| `POLY_REDEEM_LOG_BACKUPS` | Optional | Number of rotated files to keep (default `5`) |
| `POLY_REDEEM_LOG_LEVEL` | Optional | `DEBUG`, `INFO`, `WARNING`, or `ERROR` (default `INFO`) |
| `POLY_REDEEM_LOG_DISABLE` | Optional | Set to `1` / `true` to skip the file handler (console only) |

## Troubleshooting

- `401 invalid authorization`: rotate Builder credentials and verify key/secret/passphrase belong together.
- No redeems / wrong custody: key must be for the same Polymarket account as `POLYMARKET_WALLET_ADDRESS`.
- `quota exceeded`: relayer Builder-API rate limit; the loop will sleep until the `resets in N seconds` hint expires (capped at 1h).

## Security

Never commit `.env`. Treat private keys and builder secrets as confidential.

## License

[LICENSE](LICENSE)
