# Muneo

Muneo is an open-source crypto market analysis stack. It runs on a daily schedule, pulls data from across the market using only free-tier APIs, and produces structured JSON (and optional Markdown) reports covering price action, technical indicators, on-chain data, macro context, and news — all in one place, ready for a human or an AI to read and act on.
<img src="assets/muneo.png" align="right" width="570">

Muneo is read-only by design and fetches public market data and writes report files. The different reports are produced daily - Muneo Global (covering the whole crypto market), Muneo Solana (covering Solana-specific market data) and Muneo Sui (covering Sui-specific market data). More blockchain-specific builds may be incoming and I encourage others to contribute to fork this thing to make that happen.

The purpose of Muneo is to give you people market data that you or your agent can use for market information. This can be used for research, trading or whatever you want. Power to the people. Cheers.

---

## Architecture

| Script | Purpose | Schedule |
|---|---|---|
| `market_backfill.py` | One-time historical baseline (365 days). Bootstrap only, not scheduled. | — |
| `market_report.py` | Daily token-level snapshot. Point it at a `configs/*.json` file to report on a specific token. | 21:25 UTC (SUI), 21:50 UTC (SOL) |
| `market_global_report.py` | Daily global market snapshot — BTC dominance, Fear & Greed, sector data, macro indicators, macro news. | 21:10 UTC |
| `market_accumulator.py` | Rolls completed daily reports up into weekly/monthly/quarterly context windows for longer-horizon trend data. | 09:00 UTC every Monday |
| `rss_utils.py` | Shared RSS/Atom feed fetching and parsing, used by both report scripts for token and macro news. | — |

All four schedule-bearing scripts run as independent cron services on [Railway](https://railway.app), deployed directly from this repository. **Production runs entirely on Railway** — there is no server or process running locally. Each run pushes its output straight to `reports/` or `context/` in this repo via the GitHub Contents API, using a fine-grained access token scoped to this repository only.

Cloning this repo and running the scripts locally is supported for development and testing, but local runs are not part of the production pipeline and their output is not part of the repo's data product.

---

## Data sources

All free-tier, no paid endpoints:

| Source | Data | Auth |
|---|---|---|
| [CoinGecko](https://coingecko.com/en/api) | Market data, token metadata | Yes — free Demo key |
| [CoinMarketCap](https://coinmarketcap.com/api/) | Price, market cap, volume, OHLCV, Fear & Greed | Yes — free Basic key |
| [FRED](https://fredaccount.stlouisfed.org) | DXY, macro economic series | Yes — free key |
| [Alpha Vantage](https://alphavantage.co) | SPY, VIX, equities data | Yes — free key, 25 req/day |
| [CryptoPanic](https://cryptopanic.com/developers/api) | Aggregated crypto news | Yes — free Developer key |
| [Finnhub](https://finnhub.io) | Macro news (global report only) | Yes — free key, 60 req/min |
| [Binance](https://binance-docs.github.io/apidocs/spot/en/) | CEX ticker, funding rates, long/short ratio | No |
| [DeFiLlama](https://defillama.com/docs/api) | Chain TVL | No |
| RSS/Atom feeds (via `rss_utils.py`) | Token and macro news | No |

Every fetch goes through a shared `safe_fetch` wrapper: timeout, 429 backoff, and a null value on failure — a single failed call never crashes the run. Any run-level problems are collected in a `fetch_errors` array in the output; an empty array means a fully clean run.

> **Note:** an earlier version of the news pipeline used a paid-adjacent third-party news API. That's been fully replaced by free RSS/Atom feeds — no news-related paid key is required.

---

## Output

```
reports/
  market_report_sui_{YYYYMMDD_HHMMSS}.json
  market_report_sol_{YYYYMMDD_HHMMSS}.json
  market_report_global_{YYYYMMDD_HHMMSS}.json

context/
  sui/accumulation_index.json
  sui/accumulated/{weekly,monthly,quarterly}/*.json
  sol/accumulation_index.json
  sol/accumulated/{weekly,monthly,quarterly}/*.json
```

Both directories are intentionally tracked in this repo and grow continuously as the Railway cron runs — they *are* the data product, not build artifacts. Every report carries a `script_version` and `schema_version` field; `schema_version` follows semver, so a consumer can detect breaking vs. additive vs. patch-level changes to the output shape without guessing.

---

## Configuration

Each token is described by a small JSON file in `configs/`:

```json
{
  "token_name": "SUI",
  "cmc_slug": "sui",
  "binance_symbol": "SUIUSDT",
  "defillama_chain": "Sui",
  "output_prefix": "market_report_sui",
  "rss_keywords": ["Sui", "Sui Network", "$SUI"],
  "upcoming_unlocks": [
    { "date": "2026-04-01", "amount_tokens": 42940000, "note": "Foundation unlock" }
  ]
}
```

Currently configured: `configs/sui.json` (SUI) and `configs/solana.json` (SOL). To add another token, create a new config following this schema and run `market_report.py --config configs/<name>.json`.

`rss_keywords` drives that token's news search against the RSS feed pool — this replaced an earlier org-name/title-search scheme. `upcoming_unlocks` feeds the near-term-unlock signal: an unlock within 30 days contributes a bearish lean.

---

## Signals

Both report scripts compute a set of directional leans per category, rolled into an overall bias for the token or the market as a whole. Categories currently covered:

**Token report** — RSI, MACD, EMA200, Bollinger Bands, volume split, funding rate, BTC long/short ratio, TVL change, BTC dominance, DXY, SPY, VIX, Fear & Greed, news sentiment votes, BTC direction, and upcoming-unlock proximity.

**Global report** — market cap direction, BTC dominance, altcoin season index, BTC direction, funding rate, BTC long/short ratio, ETH/BTC relative strength, DXY, SPY, VIX, Fear & Greed, and news volume.

---

## Running locally (development only)

```bash
git clone https://github.com/sm33f3r/muneo.git
cd muneo
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\Activate.ps1 on Windows
pip install -r requirements.txt
cp .env.example .env   # fill in your own free-tier API keys
python market_report.py --config configs/sui.json --md
```

Local runs write to your local `reports/`/`context/` folders and, if `GITHUB_TOKEN`/`GITHUB_REPO` are set, will attempt to push to GitHub exactly like production does. If you're just testing, either omit those two env vars (push is skipped, logged as a warning, and never blocks the run) or point `GITHUB_REPO` at a scratch repo of your own — not this one.

### Required environment variables

| Key | Used by |
|---|---|
| `COINGECKO_API_KEY` | report, global |
| `CMC_API_KEY` | report, global |
| `FRED_API_KEY` | report, global |
| `ALPHA_VANTAGE_API_KEY` | report, global |
| `CRYPTOPANIC_API_KEY` | report, global |
| `FINNHUB_API_KEY` | global only |
| `GITHUB_TOKEN` | report, global (push) — optional locally |
| `GITHUB_REPO` | report, global (push) — optional locally |
| `GITHUB_BRANCH` | report, global (push) — defaults to `main` |

---

## Consuming the data

`reports/` and `context/` are public and stable enough to build against. If you're wiring an external agent or dashboard up to Muneo's output:

- Read the latest file by sorting on the timestamp in the filename, or watch the repo for new commits.
- Always check `fetch_errors` before trusting a report — a non-empty array means part of that run's data is missing (fields will be `null`/`N/A`), not that the whole report is bad.
- Check `schema_version` on each run rather than hardcoding field expectations — additive changes bump the minor version, breaking changes bump the major version.
- Reports are produced on the schedule listed in [Architecture](#architecture) above (all times UTC); there's no guaranteed intraday freshness beyond that cadence.

---

## Disclaimer

Muneo provides market data and computed indicators for informational purposes only. It does not make price predictions and nothing it produces is financial advice. But you know...it's up to you to use it as you like. The markets are crazy, so you captains need to be careful out there on the sea. 
