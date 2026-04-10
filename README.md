# polymarket-redeem

Claim **resolved** [Polymarket](https://polymarket.com) positions on **Polygon** using Polymarket’s **PROXY** relayer so you do **not** pay MATIC for those redemption transactions ([Builder API keys](https://docs.polymarket.com/builders/api-keys)).

[`redeem/auto_claim_proxy.py`](redeem/auto_claim_proxy.py) polls the [Data API](https://data-api.polymarket.com/) for `redeemable=true` positions, wraps **`redeemPositions`** on the Conditional Tokens contract in the proxy meta-tx format, submits to the relayer, and waits for confirmation. One position per **`conditionId`** per loop (deduplicated).

## Setup

- **Python 3.10+**
- From the **repository root** (where `README.md` and `.env` live):

  ```bash
  python -m venv .venv
  .venv\Scripts\activate          # Windows
  # source .venv/bin/activate    # macOS / Linux
  pip install -r requirements.txt
  ```

## Configuration

1. Copy [`.env.template`](.env.template) to **`.env`** in the repo root.
2. Only that file is loaded (see `redeem/auto_claim_proxy.py`). Always run commands from the **top level** so the path resolves correctly.

The template includes extra commented variables (e.g. `POLY_REDEEM_*`) for other tooling; **`auto_claim_proxy.py` ignores those**.

## Quick start

| Step | Action |
|------|--------|
| 1 | Set **`POLYMARKET_PRIVATE_KEY`** (or `PRIVATE_KEY` / `POLY_PRIVATE_KEY`) and **`POLYMARKET_WALLET_ADDRESS`** (or `USER_ADDRESS`). |
| 2 | For **live** submits, set **`POLYMARKET_BUILDER_API_KEY`**, **`POLYMARKET_BUILDER_SECRET`**, **`POLYMARKET_BUILDER_PASSPHRASE`** (or the `BUILDER_*` / `BUILDER_PASS_PHRASE` aliases). |
| 3 | Preview without submitting: `python redeem/auto_claim_proxy.py --dry-run` |
| 4 | Run the loop: `python redeem/auto_claim_proxy.py` — stop with **Ctrl+C**. |

## CLI

| Command | Behavior |
|---------|----------|
| `python redeem/auto_claim_proxy.py --dry-run` | One pass: fetch API, show derived proxy vs wallet, print payload previews. Builder credentials **not** required. |
| `python redeem/auto_claim_proxy.py` | Infinite loop: fetch → submit each new `conditionId` → poll relayer → sleep **`POLL_MS`**. |

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `POLYMARKET_PRIVATE_KEY`, `PRIVATE_KEY`, or `POLY_PRIVATE_KEY` | Yes | Owner EOA hex key |
| `POLYMARKET_WALLET_ADDRESS` or `USER_ADDRESS` | Yes | Data API `user` (wallet that lists your positions) |
| `POLYMARKET_BUILDER_API_KEY` / `BUILDER_API_KEY` | Live only | Builder HMAC `key` |
| `POLYMARKET_BUILDER_SECRET` / `BUILDER_SECRET` | Live only | Builder secret |
| `POLYMARKET_BUILDER_PASSPHRASE` / `BUILDER_PASS_PHRASE` | Live only | Builder passphrase |
| `POLY_RPC_URL` or `POLYGON_RPC_URL` | No | Polygon JSON-RPC (gas estimate; defaults exist) |
| `RELAYER_URL` | No | Default `https://relayer-v2.polymarket.com` |
| `CHAIN_ID` | No | Default `137` |
| `POLL_MS` | No | Loop delay in ms (default `60000`) |

## Troubleshooting

- **HTTP 401 / invalid authorization** — Create new Builder API credentials; copy **key**, **secret**, and **passphrase** together; ensure no typos or swapped fields.
- **Nothing redeems / wrong custody** — The private key must be for the **same** Polymarket account as **`POLYMARKET_WALLET_ADDRESS`**. Use **`--dry-run`** to compare **derived proxy** (from the key) to your configured wallet.
- **Dependency conflicts** — Use a dedicated venv and reinstall from **`requirements.txt`**.

## Security

Do not commit **`.env`**. Treat private keys and builder secrets as confidential; rotate if they leak.

## License

[LICENSE](LICENSE)
