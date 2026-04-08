# polymarket-redeem

Automate **claiming / redeeming** resolved [**Polymarket**](https://polymarket.com) positions on **Polygon** using the Polymarket **PROXY** relayer (gasless execution with [Builder API keys](https://docs.polymarket.com/builders/api-keys)).

The included tool polls the Data API for `redeemable=true` rows and submits **`redeemPositions`** on the Conditional Tokens contract through the same proxy meta-transaction flow the app can use.

## What’s in this repo

| Path | Role |
|------|------|
| [`redeem/auto_claim_proxy.py`](redeem/auto_claim_proxy.py) | Python loop: fetch positions → build proxy relayer payload → submit → poll. Supports **`--dry-run`**. |
| [`.env.template`](.env.template) | Copy to **`.env`** at the repo root; documents required and optional variables (including extras if you use other redeem scripts with the same file). |

Run everything from the **repository root** so `.env` is found next to `README.md`.

The shared [`.env.template`](.env.template) also lists commented variables (e.g. `POLY_REDEEM_*`) for **other** Polymarket redeem automation you may keep in the same repo; they are not required for `auto_claim_proxy.py`.

## Requirements

- **Python 3.10+**
- Install deps (prefer a [venv](https://docs.python.org/3/tutorial/venv.html)):

  ```bash
  pip install -r requirements.txt
  ```

## Quick start

1. Copy **`.env.template`** → **`.env`** in the repo root.
2. Set at minimum:
   - **`POLYMARKET_PRIVATE_KEY`** (or `PRIVATE_KEY` / `POLY_PRIVATE_KEY`) — signer for your Polymarket account  
   - **`POLYMARKET_WALLET_ADDRESS`** (or `USER_ADDRESS`) — address the Data API uses for your positions (`user=` query)  
   - For **live** redeem: **`POLYMARKET_BUILDER_API_KEY`**, **`POLYMARKET_BUILDER_SECRET`**, **`POLYMARKET_BUILDER_PASSPHRASE`** (do not prefix the API key with a stray `.`)
3. Sanity check without sending transactions:

   ```bash
   python redeem/auto_claim_proxy.py --dry-run
   ```

4. Live loop (submits via relayer):

   ```bash
   python redeem/auto_claim_proxy.py
   ```

Stop with **Ctrl+C**.

## Configuration

- **Single env file:** only **`.env`** at the **top level** is loaded (see `_load_env()` in the script).
- Full variable list and comments: **[`.env.template`](.env.template)**.
- **`py_builder_signing_sdk`** imports: use submodules, e.g. `from py_builder_signing_sdk.config import BuilderConfig` (the package top level often does not re-export names).

## Environment variables (proxy script)

| Variable | When | Purpose |
|----------|------|---------|
| `POLYMARKET_PRIVATE_KEY` / `PRIVATE_KEY` | Always | Owner EOA private key |
| `POLYMARKET_WALLET_ADDRESS` / `USER_ADDRESS` | Always | Data API user / profile wallet |
| `POLYMARKET_BUILDER_*` / `BUILDER_*` | Live only | Builder HMAC for authenticated relayer `POST` |
| `POLY_RPC_URL` / `POLYGON_RPC_URL` | Optional | Polygon JSON-RPC for gas estimation (defaults exist) |
| `RELAYER_URL` | Optional | Default `https://relayer-v2.polymarket.com` |
| `CHAIN_ID` | Optional | Default `137` |
| `POLL_MS` | Optional | Loop interval in ms (default `60000`) |

## Troubleshooting

- **`401` / invalid authorization** — regenerate Builder keys; confirm secret and passphrase match the key; no leading `.` on the API key string.
- **No tokens / relayer skips** — the signing key must belong to the **same** Polymarket login as **`POLYMARKET_WALLET_ADDRESS`**. The dry-run prints **derived proxy vs `USER_ADDRESS`** to spot mismatches.
- **Conda / global Python conflicts** — use a dedicated venv and `pip install -r requirements.txt` inside it.

## Security

- Never commit **`.env`** (see `.gitignore`).
- Treat private keys and builder secrets as production secrets; rotate if exposed.

## License

See [LICENSE](LICENSE).
