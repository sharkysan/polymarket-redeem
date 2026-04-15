# polymarket-redeem

Claim resolved [Polymarket](https://polymarket.com) positions on Polygon with either:

- `redeem/auto_claim_proxy.py`: direct PROXY relayer submission (Builder HMAC auth).
- `redeem/auto_claim_cli.py`: invokes `polymarket ctf redeem` for each condition.

Both scripts read the same top-level `.env` and deduplicate by `conditionId`.

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
| 2 | For relayer live submit, set `POLYMARKET_BUILDER_API_KEY`, `POLYMARKET_BUILDER_SECRET`, `POLYMARKET_BUILDER_PASSPHRASE` (or `BUILDER_*` aliases). |
| 3 | Dry-run proxy relayer: `python redeem/auto_claim_proxy.py --dry-run` |
| 4 | Dry-run CLI wrapper: `python redeem/auto_claim_cli.py --dry-run` |

## Commands

| Command | Behavior |
|---------|----------|
| `python redeem/auto_claim_proxy.py --dry-run` | One pass, no submit; prints payload previews and key/wallet checks. |
| `python redeem/auto_claim_proxy.py` | Infinite relayer loop; submits and polls transaction state. |
| `python redeem/auto_claim_cli.py --dry-run` | One pass; prints planned `polymarket ctf redeem` commands only. |
| `python redeem/auto_claim_cli.py --once` | One live pass with CLI submits, then exit. |
| `python redeem/auto_claim_cli.py` | Infinite CLI loop; sleeps `POLY_CLI_POLL_MS` (or `POLL_MS`). |

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
| `POLL_MS` | Optional | Proxy loop poll delay in ms |
| `POLY_CLI_BIN` | Optional | CLI executable name/path for `auto_claim_cli.py` (default `polymarket`) |
| `POLY_CLI_POLL_MS` | Optional | Poll delay override for `auto_claim_cli.py` |

## Troubleshooting

- `401 invalid authorization`: rotate Builder credentials and verify key/secret/passphrase belong together.
- No redeems / wrong custody: key must be for the same Polymarket account as `POLYMARKET_WALLET_ADDRESS`.
- CLI not found: install Polymarket CLI or set `POLY_CLI_BIN` to full executable path.

## Security

Never commit `.env`. Treat private keys and builder secrets as confidential.

## License

[LICENSE](LICENSE)
