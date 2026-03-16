# Market Reporter

A lightweight, standalone Python tool that fetches live cryptocurrency market data from CoinMarketCap and several free public APIs, computes technical indicators, and outputs structured reports in JSON and Markdown formats. It is read-only by design — no trades, no wallets, no accounts, no persistent state beyond the report files it saves.

---

## Features

- **Price data** — current price, 24h/7d change, volume, market cap, 365-day high drawdown (via CoinMarketCap)
- **Technical indicators** — RSI (14), MACD (12/26/9), SMA 50, SMA 200, price-vs-SMA percentages (computed from CMC OHLCV data, no TA library required)
- **Sentiment** — Crypto Fear & Greed Index with 7-day trend (via CoinMarketCap)
- **Ecosystem** — on-chain TVL with change vs. prior report (via DeFiLlama)
- **CEX data** — Binance 24h ticker with CEX/DEX spread
- **Macro context** — BTC price and 24h change (fetched alongside the token in a single CMC call)
- **Upcoming token unlocks** — configurable schedule with near-term (≤30 day) warnings
- **Signal summary** — per-indicator bullish/bearish/neutral lean with an overall directional bias score
- **Multi-token support** — point the tool at any JSON config file to report on a different token

---

## Requirements

- Python 3.9 or later
- Internet access (the tool calls external APIs at runtime)
- A free **CoinMarketCap API key** — get one at [coinmarketcap.com/api](https://coinmarketcap.com/api/). The free Basic plan is sufficient.

### Python dependencies

```
requests>=2.31.0
python-dateutil>=2.8.2
python-dotenv>=1.0.0
```

---

## Installation

**1. Clone or download the project** into a folder of your choice.

**2. (Recommended) Create a virtual environment:**

```bash
python -m venv .venv

# Activate on macOS / Linux:
source .venv/bin/activate

# Activate on Windows (PowerShell):
.venv\Scripts\Activate.ps1
```

**3. Install dependencies:**

```bash
pip install -r requirements.txt
```

**4. Set up your CoinMarketCap API key:**

```bash
cp .env.example .env
```

Open `.env` in a text editor and replace `your_api_key_here` with your actual key:

```
CMC_API_KEY=your_actual_key_here
```

The `.env` file is gitignored and will never be committed. Alternatively, you can set the key as a shell environment variable directly: `export CMC_API_KEY=your_key`.

---

## Usage

Run the script from the project root directory:

```bash
python market_report.py
```

By default this loads `configs/sui.json` and writes a JSON report to the `reports/` folder.

### Options

| Flag | Description |
|---|---|
| `--config <path>` | Path to a token config file. Defaults to `configs/sui.json`. |
| `--md` | Also render and save a Markdown report, and print it to stdout. |

### Examples

```bash
# Report on SUI (default), JSON only
python market_report.py

# Report on SUI with Markdown output printed to the terminal
python market_report.py --md

# Report on Solana
python market_report.py --config configs/solana.json

# Report on Solana with Markdown output
python market_report.py --config configs/solana.json --md
```

### Output files

All reports are saved to the `reports/` directory (created automatically if it doesn't exist). Files are named using the token's `output_prefix` and a UTC timestamp:

```
reports/
  market_report_sui_20260314_153022.json
  market_report_sui_20260314_153022.md   ← only if --md is passed
```

The JSON report is always saved. The Markdown report is only saved (and printed) when `--md` is used.

> **Note:** The tool compares TVL against the most recent previously saved JSON report to compute the TVL change signal. The more regularly you run it, the more useful that trend signal becomes.

---

## Configuration

Each token is described by a small JSON file in the `configs/` directory. Here is the full schema with all fields explained:

```json
{
  "token_name": "SUI",
  "cmc_slug": "sui",
  "binance_symbol": "SUIUSDT",
  "defillama_chain": "Sui",
  "output_prefix": "market_report_sui",
  "upcoming_unlocks": [
    {
      "date": "2026-04-01",
      "amount_tokens": 42940000,
      "note": "Foundation unlock"
    }
  ]
}
```

### Field reference

| Field | Required | Description |
|---|---|---|
| `token_name` | Yes | Display name used in report headers and metadata. |
| `cmc_slug` | Yes | The token's URL slug on CoinMarketCap (e.g. `"sui"`, `"solana"`, `"ethereum"`). Find it in the URL of the token's CMC page: `coinmarketcap.com/currencies/<slug>/`. |
| `binance_symbol` | Yes | The Binance trading pair symbol (e.g. `"SUIUSDT"`, `"SOLUSDT"`). Used for CEX price and 24h ticker data. No API key required. |
| `defillama_chain` | Yes | The chain name as it appears on DeFiLlama (e.g. `"Sui"`, `"Solana"`). Used to fetch TVL data. Case-insensitive matching is applied. |
| `output_prefix` | Yes | Prefix used for saved report filenames (e.g. `"market_report_sui"`). Keep it lowercase with underscores. |
| `upcoming_unlocks` | No | Array of scheduled token unlock events. Omit the field or pass an empty array `[]` if there are none. |

### Upcoming unlock fields

| Field | Required | Description |
|---|---|---|
| `date` | Yes | Unlock date in `YYYY-MM-DD` format. |
| `amount_tokens` | Yes | Number of tokens being unlocked (integer). |
| `note` | No | Optional free-text label (e.g. `"Foundation unlock"`, `"Team vesting"`). |

Unlocks within 30 days of the report date are flagged as near-term and contribute a **bearish** signal to the signal summary.

### Adding a new token

1. Create a new file in `configs/`, e.g. `configs/ethereum.json`.
2. Fill in all required fields using the schema above.
3. Run with `--config configs/ethereum.json`.

### Tuning signal thresholds

The signal thresholds are defined directly in `market_report.py` and can be adjusted if the defaults don't suit your strategy. Each `lean_*` function near the top of the file controls one signal:

| Function | Default thresholds | Notes |
|---|---|---|
| `lean_rsi` | Bullish < 35, Bearish > 65 | Standard oversold/overbought boundaries |
| `lean_macd` | Bullish if MACD > Signal, Bearish otherwise | Ignores differences ≤ 0.0001 as neutral |
| `lean_vs_sma` | Bullish > +1%, Bearish < −1% | Applied to both SMA 50 and SMA 200 |
| `lean_fear_and_greed` | Bullish < 25, Bearish > 75 | Extreme Fear / Extreme Greed boundaries |
| `lean_fg_trend` | Bullish if rising, Bearish if falling | Based on 7-day direction |
| `lean_tvl` | Bullish > +2%, Bearish < −2% | Compares TVL to the prior saved report |
| `lean_btc` | Bullish > +1%, Bearish < −1% | BTC 24h change as macro proxy |
| `lean_upcoming_unlock` | Bearish if unlock within 30 days | Near-term window is set in `build_report` |
| `lean_volume_vs_market_cap` | Bullish > 0.15, Bearish < 0.05 | Volume/market cap activity ratio |

The overall lean is determined by `compute_overall_lean`: **bullish** or **bearish** requires a margin of 3 or more signals over the other; otherwise the result is `slight_bullish`, `slight_bearish`, or `neutral`.

---

## Data sources

| Source | Data | Auth required |
|---|---|---|
| [CoinMarketCap](https://coinmarketcap.com/api/) | Token price, market cap, volume, OHLCV history, Fear & Greed Index, BTC macro | Yes — free API key |
| [Binance](https://binance-docs.github.io/apidocs/spot/en/) | 24h CEX ticker (price, high, low, volume) | No |
| [DeFiLlama](https://defillama.com/docs/api) | Chain TVL | No |

All API calls have a 5-second timeout. If a call fails, the affected section of the report shows `N/A` values and the error is listed in the `fetch_errors` field of the JSON output (and in the `## Fetch Errors` section of the Markdown report). The tool saves a partial report as long as at least one API call succeeds.

> **Note on the 365-day high:** CoinMarketCap's free tier does not expose an all-time high endpoint. The "ATH" field in reports is the highest daily high over the 365-day OHLCV window used to compute technical indicators, not the true all-time high.

---

## Disclaimer

This tool provides market data and computed indicators for informational purposes only. It does not make price predictions and is not financial advice.
