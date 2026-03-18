#!/usr/bin/env python3
"""
market_report.py v2.0.0 — Enriched market data reporting tool.

Fetches live market data from multiple sources, computes technical indicators,
and outputs a structured report as JSON (and optionally Markdown).

Read-only: no trades, no wallets, no persistent state beyond report files.

API KEY SETUP
-------------
1. Copy .env.example to .env:
       cp .env.example .env
2. Open .env and set all required keys.
3. The .env file is listed in .gitignore and will never be committed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from dateutil import parser as dateutil_parser
from dotenv import load_dotenv

SCRIPT_VERSION = "2.0.0"
SCHEMA_VERSION = "1.0.0"
API_TIMEOUT = 10  # seconds

CMC_BASE  = "https://pro-api.coinmarketcap.com"
CG_BASE   = "https://api.coingecko.com/api/v3"
FRED_BASE = "https://api.stlouisfed.org/fred"


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

@dataclass
class TokenConfig:
    token_name: str
    coingecko_id: str
    binance_symbol: str
    defillama_chain: str
    cmc_slug: str
    upcoming_unlocks: list
    output_prefix: str
    # Optional new fields — None means the relevant fetcher will skip gracefully
    binance_futures_symbol: Optional[str] = None
    coinpaprika_id: Optional[str] = None
    cryptopanic_symbol: Optional[str] = None


def load_config(config_path: str) -> TokenConfig:
    with open(config_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return TokenConfig(
        token_name=raw["token_name"],
        coingecko_id=raw["coingecko_id"],
        binance_symbol=raw["binance_symbol"],
        defillama_chain=raw["defillama_chain"],
        cmc_slug=raw["cmc_slug"],
        upcoming_unlocks=raw.get("upcoming_unlocks", []),
        output_prefix=raw["output_prefix"],
        binance_futures_symbol=raw.get("binance_futures_symbol"),
        coinpaprika_id=raw.get("coinpaprika_id"),
        cryptopanic_symbol=raw.get("cryptopanic_symbol"),
    )


# ---------------------------------------------------------------------------
# Safe fetch wrapper
# ---------------------------------------------------------------------------

def safe_fetch(label: str, fetch_fn, fetch_errors: list = None):
    try:
        result = fetch_fn()
        print(f"  \u2713 {label}")
        return result
    except requests.exceptions.Timeout:
        msg = f"{label} — timeout"
        print(f"  \u2717 {msg}")
        if fetch_errors is not None:
            fetch_errors.append(msg)
        return None
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        if code == 429:
            msg = f"{label} — rate limited, backing off 15s"
            print(f"  \u2717 {msg}")
            if fetch_errors is not None:
                fetch_errors.append(msg)
            time.sleep(15)
        else:
            msg = f"{label} — HTTP {code}"
            print(f"  \u2717 {msg}")
            if fetch_errors is not None:
                fetch_errors.append(msg)
        return None
    except Exception as e:
        msg = f"{label} — {str(e)}"
        print(f"  \u2717 {msg}")
        if fetch_errors is not None:
            fetch_errors.append(msg)
        return None


# ---------------------------------------------------------------------------
# Technical indicator math (pure Python)
# ---------------------------------------------------------------------------

def compute_ema(prices: list, period: int) -> list:
    """EMA using standard multiplier 2/(period+1)."""
    if len(prices) < period:
        return []
    alpha = 2.0 / (period + 1)
    ema_values = []
    seed = sum(prices[:period]) / period
    ema_values.append(seed)
    for price in prices[period:]:
        ema_values.append(price * alpha + ema_values[-1] * (1 - alpha))
    return ema_values


def compute_rsi(closes: list, period: int = 14) -> Optional[float]:
    """RSI using Wilder's smoothing."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_macd(
    closes: list,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple:
    """Returns (macd_line, signal_line, histogram)."""
    ema_fast = compute_ema(closes, fast)
    ema_slow = compute_ema(closes, slow)
    fast_offset = slow - fast
    if len(ema_slow) == 0 or fast_offset > len(ema_fast):
        return None, None, None
    aligned_fast = ema_fast[fast_offset:]
    macd_series = [f - s for f, s in zip(aligned_fast, ema_slow)]
    if len(macd_series) < signal:
        return None, None, None
    signal_series = compute_ema(macd_series, signal)
    if not signal_series:
        return None, None, None
    macd_val   = macd_series[-1]
    signal_val = signal_series[-1]
    return macd_val, signal_val, macd_val - signal_val


def compute_sma(closes: list, period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def compute_bollinger_bands(ohlc_raw: list) -> dict:
    """Compute 20-period Bollinger Bands (2σ) using pandas."""
    null_result = {"upper": None, "mid": None, "lower": None, "width_pct": None, "position": None}
    if not ohlc_raw or len(ohlc_raw) < 20:
        return null_result
    try:
        df = pd.DataFrame(ohlc_raw, columns=["timestamp", "open", "high", "low", "close"])
        close = df["close"].astype(float)
        rolling_mean = close.rolling(window=20).mean()
        rolling_std  = close.rolling(window=20).std()
        upper = rolling_mean + (2 * rolling_std)
        lower = rolling_mean - (2 * rolling_std)
        mid   = rolling_mean
        bb_upper = float(upper.iloc[-1])
        bb_mid   = float(mid.iloc[-1])
        bb_lower = float(lower.iloc[-1])
        if pd.isna(bb_upper) or pd.isna(bb_lower) or bb_mid == 0:
            return null_result
        bb_width_pct = ((bb_upper - bb_lower) / bb_mid) * 100
        denom = bb_upper - bb_lower
        bb_position = (float(close.iloc[-1]) - bb_lower) / denom if denom != 0 else 0.5
        return {
            "upper":     round(bb_upper, 6),
            "mid":       round(bb_mid, 6),
            "lower":     round(bb_lower, 6),
            "width_pct": round(bb_width_pct, 4),
            "position":  round(bb_position, 4),
        }
    except Exception:
        return null_result


# ---------------------------------------------------------------------------
# API fetch functions
# ---------------------------------------------------------------------------

def _cg_headers(api_key: str) -> dict:
    return {"x-cg-demo-api-key": api_key, "Accept": "application/json"}


def _cmc_headers(api_key: str) -> dict:
    return {"X-CMC_PRO_API_KEY": api_key, "Accept": "application/json"}


# Fetch 1 — CoinGecko token full data (price + community + developer + sentiment)
def fetch_cg_token_full_data(coingecko_id: str, api_key: str) -> dict:
    url = f"{CG_BASE}/coins/{coingecko_id}"
    params = {
        "localization": "false",
        "tickers": "false",
        "market_data": "true",
        "community_data": "true",
        "developer_data": "true",
        "sparkline": "false",
    }
    resp = requests.get(url, headers=_cg_headers(api_key), params=params, timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError(f"Empty response for {coingecko_id}")
    return data


# Fetch 2 — CoinPaprika coin detail (social + developer stats, free, no auth)
def fetch_coinpaprika_coin(coinpaprika_id: str) -> dict:
    url = f"https://api.coinpaprika.com/v1/coins/{coinpaprika_id}"
    resp = requests.get(url, timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError(f"Empty response for {coinpaprika_id}")
    return data


# Fetch 3 — CryptoPanic token news (developer/v2, free developer plan)
def fetch_cryptopanic_news(symbol: str, api_key: str) -> dict:
    url = "https://cryptopanic.com/api/developer/v2/posts/"
    params = {
        "auth_token": api_key,
        "currencies": symbol,
        "kind": "news",
        "public": "true",
    }
    resp = requests.get(url, params=params, timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError(f"Empty response for {symbol}")
    return data


# Fetch 4 — CoinGecko token OHLC 365d
# CoinGecko only accepts specific day values: 1, 7, 14, 30, 90, 180, 365.
# Use 365 to ensure enough candles for EMA-200 and all other indicators.
def fetch_cg_ohlc(coingecko_id: str, api_key: str) -> list:
    url = f"{CG_BASE}/coins/{coingecko_id}/ohlc"
    params = {"vs_currency": "usd", "days": "365"}
    resp = requests.get(url, headers=_cg_headers(api_key), params=params, timeout=API_TIMEOUT)
    resp.raise_for_status()
    candles = resp.json()
    if not candles:
        raise ValueError(f"Empty OHLC response for {coingecko_id}")
    return candles


# Fetch 3 — CoinGecko token market chart 30d (volume)
def fetch_cg_market_chart(coingecko_id: str, api_key: str) -> list:
    url = f"{CG_BASE}/coins/{coingecko_id}/market_chart"
    params = {"vs_currency": "usd", "days": "30", "interval": "daily"}
    resp = requests.get(url, headers=_cg_headers(api_key), params=params, timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    total_volumes = data.get("total_volumes", [])
    if not total_volumes:
        raise ValueError(f"Empty market chart response for {coingecko_id}")
    return total_volumes


# Fetch 4 — CoinGecko BTC markets
def fetch_cg_btc_markets(api_key: str) -> dict:
    url = f"{CG_BASE}/coins/markets"
    params = {"vs_currency": "usd", "ids": "bitcoin", "price_change_percentage": "24h"}
    resp = requests.get(url, headers=_cg_headers(api_key), params=params, timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError("Empty BTC markets response")
    return data[0]


# Fetch 4 — CoinGecko ETH/BTC ratio
def fetch_cg_eth_btc(api_key: str) -> float:
    url = f"{CG_BASE}/simple/price"
    params = {"ids": "ethereum", "vs_currencies": "btc"}
    resp = requests.get(url, headers=_cg_headers(api_key), params=params, timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return float(data["ethereum"]["btc"])


# Fetch 5 — DeFiLlama historical chain TVL
def fetch_defillama_historical_tvl(chain: str) -> list:
    url = f"https://api.llama.fi/v2/historicalChainTvl/{chain}"
    resp = requests.get(url, timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError(f"Empty TVL history for {chain}")
    return data


# Fetch 6 — DeFiLlama chain fees overview
def fetch_defillama_chain_fees(chain: str) -> dict:
    url = f"https://api.llama.fi/overview/fees/{chain}"
    params = {"excludeTotalDataChart": "false", "excludeTotalDataChartBreakdown": "true"}
    resp = requests.get(url, params=params, timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError(f"Empty fees response for {chain}")
    return data


# Fetch 7 — CMC global metrics latest
def fetch_cmc_global_metrics(api_key: str) -> dict:
    url = f"{CMC_BASE}/v1/global-metrics/quotes/latest"
    resp = requests.get(url, headers=_cmc_headers(api_key), timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json().get("data", {})
    if not data:
        raise ValueError("Empty global metrics response")
    return data


# Fetch 7 — CMC Fear & Greed latest
def fetch_cmc_fear_greed_latest(api_key: str) -> dict:
    url = f"{CMC_BASE}/v3/fear-and-greed/latest"
    resp = requests.get(url, headers=_cmc_headers(api_key), timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json().get("data", {})
    if not data:
        raise ValueError("Empty Fear & Greed response")
    return data


# Fetch 8 — Binance spot 24hr ticker
def fetch_binance_spot(symbol: str) -> dict:
    url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
    resp = requests.get(url, timeout=API_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


# Fetch 9 — Binance Futures open interest
def fetch_binance_oi(symbol: str) -> dict:
    url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}"
    resp = requests.get(url, timeout=API_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


# Fetch 10 — Binance Futures funding rate (last 21)
def fetch_binance_funding_rate(symbol: str) -> list:
    url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit=21"
    resp = requests.get(url, timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError(f"Empty funding rate for {symbol}")
    return data


# Fetch 11 — Binance Futures BTC long/short ratio
def fetch_binance_btc_ls_ratio() -> dict:
    url = "https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol=BTCUSDT&period=1h&limit=1"
    resp = requests.get(url, timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError("Empty BTC L/S ratio response")
    return data[0]


# Fetches 14/15 — FRED series observations
def fetch_fred_series(series_id: str, api_key: str, limit: int = 5) -> list:
    url = f"{FRED_BASE}/series/observations"
    params = {"series_id": series_id, "sort_order": "desc", "limit": limit,
              "api_key": api_key, "file_type": "json"}
    resp = requests.get(url, params=params, timeout=API_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("observations", [])


# Fetch 18 — Alpha Vantage SPY
def fetch_alpha_vantage_spy(api_key: str) -> dict:
    url = "https://www.alphavantage.co/query"
    params = {"function": "TIME_SERIES_DAILY", "symbol": "SPY",
              "outputsize": "compact", "apikey": api_key}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    ts = resp.json().get("Time Series (Daily)", {})
    if not ts:
        raise ValueError("No time series data in Alpha Vantage response")
    return ts


# ---------------------------------------------------------------------------
# Signal lean helpers
# ---------------------------------------------------------------------------

def lean_rsi(rsi: Optional[float]) -> str:
    if rsi is None:
        return "neutral"
    if rsi < 35:
        return "bullish"
    if rsi > 65:
        return "bearish"
    return "neutral"


def lean_macd(macd_line: Optional[float], signal_line: Optional[float]) -> str:
    if macd_line is None or signal_line is None:
        return "neutral"
    diff = macd_line - signal_line
    if abs(diff) <= 0.001:
        return "neutral"
    return "bullish" if diff > 0 else "bearish"


def lean_ema200(price: Optional[float], ema200: Optional[float]) -> str:
    if price is None or ema200 is None or ema200 == 0:
        return "neutral"
    pct = ((price - ema200) / ema200) * 100
    if pct > 1.0:
        return "bullish"
    if pct < -1.0:
        return "bearish"
    return "neutral"


def lean_bb(position: Optional[float]) -> Optional[str]:
    if position is None:
        return "neutral"
    if position < 0.2:
        return "bullish"
    if position > 0.8:
        return "bearish"
    return "neutral"


def lean_volume_split(up_avg: Optional[float], down_avg: Optional[float]) -> Optional[str]:
    if up_avg is None or down_avg is None:
        return None
    if up_avg > down_avg * 1.1:
        return "bullish"
    if down_avg > up_avg * 1.1:
        return "bearish"
    return "neutral"


def lean_funding_rate(avg: Optional[float]) -> str:
    if avg is None:
        return "neutral"
    if avg < -0.0003:
        return "bullish"
    if avg > 0.0005:
        return "bearish"
    return "neutral"


def lean_btc_ls(ratio: Optional[float]) -> str:
    if ratio is None:
        return "neutral"
    if ratio < 0.9:
        return "bullish"
    if ratio > 1.5:
        return "bearish"
    return "neutral"


def lean_tvl(change_pct: Optional[float]) -> str:
    if change_pct is None:
        return "neutral"
    if change_pct > 2.0:
        return "bullish"
    if change_pct < -2.0:
        return "bearish"
    return "neutral"


def lean_btc_dominance(pct: Optional[float]) -> str:
    if pct is None:
        return "neutral"
    if pct < 52.0:
        return "bullish"
    if pct > 62.0:
        return "bearish"
    return "neutral"


def lean_dxy(latest: Optional[float], prior: Optional[float]) -> str:
    if latest is None or prior is None or prior == 0:
        return "neutral"
    change_pct = ((latest - prior) / prior) * 100
    if change_pct > 0.3:
        return "bearish"
    if change_pct < -0.3:
        return "bullish"
    return "neutral"


def lean_spy(change_pct: Optional[float]) -> str:
    if change_pct is None:
        return "neutral"
    if change_pct > 0.5:
        return "bullish"
    if change_pct < -0.5:
        return "bearish"
    return "neutral"


def lean_vix(vix: Optional[float], prior: Optional[float]) -> str:
    if vix is None:
        return "neutral"
    if vix < 20 and prior is not None and vix < prior:
        return "bullish"
    if vix > 25:
        return "bearish"
    if prior is not None and prior != 0:
        pct_change = ((vix - prior) / prior) * 100
        if pct_change > 10:
            return "bearish"
    return "neutral"


def lean_fear_greed(value: Optional[int]) -> str:
    if value is None:
        return "neutral"
    if value < 25:
        return "bullish"
    if value > 75:
        return "bearish"
    return "neutral"


def lean_sentiment_votes(up_pct: Optional[float]) -> Optional[str]:
    if up_pct is None:
        return None
    if up_pct > 60:
        return "bullish"
    if up_pct < 40:
        return "bearish"
    return "neutral"


def lean_btc_direction(change_24h: Optional[float]) -> str:
    if change_24h is None:
        return "neutral"
    if change_24h > 1.0:
        return "bullish"
    if change_24h < -1.0:
        return "bearish"
    return "neutral"


def compute_overall_lean(signals: dict) -> str:
    available = [v for v in signals.values() if v is not None]
    n = len(available)
    if n == 0:
        return "neutral"
    bullish = sum(1 for v in available if v == "bullish")
    bearish = sum(1 for v in available if v == "bearish")
    if bullish / n >= 0.60:
        return "bullish"
    if bearish / n >= 0.60:
        return "bearish"
    if bullish / n >= 0.50:
        return "slight_bullish"
    if bearish / n >= 0.50:
        return "slight_bearish"
    return "neutral"


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def _fmt(value, decimals: int = 4) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.{decimals}f}"
    return str(value)


def _pct(value) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.2f}%"


def _usd(value) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, (int, float)):
        if value >= 1_000_000_000_000:
            return f"${value / 1_000_000_000_000:.2f}T"
        if value >= 1_000_000_000:
            return f"${value / 1_000_000_000:.2f}B"
        if value >= 1_000_000:
            return f"${value / 1_000_000:.2f}M"
        return f"${value:,.4f}"
    return str(value)


def _lean_icon(lean) -> str:
    if lean is None:
        return "\u2014"
    return {"bullish": "\u2705", "bearish": "\U0001f534", "neutral": "\u26a0\ufe0f"}.get(lean, "\u26a0\ufe0f")


def render_markdown(report: dict) -> str:
    meta    = report["report_metadata"]
    price   = report["price_data"]
    tech    = report["technical_indicators"]
    deriv   = report["derivatives"]
    onchain = report["on_chain"]
    news    = report.get("news", {})
    macro   = report["macro"]
    sent    = report["sentiment"]
    events  = report["upcoming_events"]
    sig     = report["signal_summary"]
    errors  = report.get("fetch_errors", [])

    lines = []

    # Header
    lines.append(f"# {meta['token']} Market Report")
    lines.append(f"**Generated:** {meta['generated_at']}  ")
    lines.append(f"**Script version:** {meta['script_version']}  ")
    lines.append(f"**Config:** `{meta['config_file']}`")
    lines.append("")

    # Price & Market
    lines.append("## Price & Market")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Price (USD) | {_usd(price.get('price_usd'))} |")
    lines.append(f"| 24h Change | {_pct(price.get('price_change_24h_pct'))} |")
    lines.append(f"| 7d Change | {_pct(price.get('price_change_7d_pct'))} |")
    lines.append(f"| 24h Volume | {_usd(price.get('volume_24h_usd'))} |")
    lines.append(f"| Market Cap | {_usd(price.get('market_cap_usd'))} |")
    lines.append(f"| Volume/MCap Ratio | {_fmt(price.get('volume_to_mcap_ratio'), 4)} |")
    lines.append(f"| ATH (USD) | {_usd(price.get('ath_usd'))} |")
    lines.append(f"| ATH Drawdown | {_pct(price.get('ath_drawdown_pct'))} |")
    lines.append(f"| CEX Price (Binance) | {_usd(price.get('cex_price_usd'))} |")
    lines.append(f"| CEX 24h High | {_usd(price.get('cex_24h_high'))} |")
    lines.append(f"| CEX 24h Low | {_usd(price.get('cex_24h_low'))} |")
    lines.append(f"| CEX/DEX Spread | {_pct(price.get('cex_dex_spread_pct'))} |")
    lines.append(f"| Volume Up Days (30d avg) | {_usd(price.get('volume_on_up_days_usd'))} |")
    lines.append(f"| Volume Down Days (30d avg) | {_usd(price.get('volume_on_down_days_usd'))} |")
    vol_lean = price.get('volume_lean')
    lines.append(f"| Volume Lean | {_lean_icon(vol_lean)} {vol_lean or 'null'} |")
    lines.append("")

    # Technical Indicators
    lines.append("## Technical Indicators")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| RSI (14) | {_fmt(tech.get('rsi_14'), 2)} |")
    rsi_lean = tech.get('rsi_lean')
    lines.append(f"| RSI Lean | {_lean_icon(rsi_lean)} {rsi_lean or 'N/A'} |")
    macd = tech.get("macd", {})
    lines.append(f"| MACD Line | {_fmt(macd.get('macd_line'), 6)} |")
    lines.append(f"| MACD Signal | {_fmt(macd.get('signal_line'), 6)} |")
    lines.append(f"| MACD Histogram | {_fmt(macd.get('histogram'), 6)} |")
    macd_lean = tech.get('macd_lean')
    lines.append(f"| MACD Lean | {_lean_icon(macd_lean)} {macd_lean or 'N/A'} |")
    lines.append(f"| SMA 50 | {_usd(tech.get('sma_50'))} |")
    lines.append(f"| Price vs SMA 50 | {_pct(tech.get('price_vs_sma50_pct'))} |")
    lines.append(f"| EMA 200 | {_usd(tech.get('ema_200'))} |")
    lines.append(f"| Price vs EMA 200 | {_pct(tech.get('price_vs_ema200_pct'))} |")
    ema200_lean = tech.get('ema200_lean')
    lines.append(f"| EMA 200 Lean | {_lean_icon(ema200_lean)} {ema200_lean or 'N/A'} |")
    bb = tech.get("bollinger_bands", {})
    lines.append(f"| BB Upper | {_usd(bb.get('upper'))} |")
    lines.append(f"| BB Mid | {_usd(bb.get('mid'))} |")
    lines.append(f"| BB Lower | {_usd(bb.get('lower'))} |")
    lines.append(f"| BB Width % | {_pct(bb.get('width_pct'))} |")
    lines.append(f"| BB Position | {_fmt(bb.get('position'), 3)} |")
    bb_lean = tech.get('bb_lean')
    lines.append(f"| BB Lean | {_lean_icon(bb_lean)} {bb_lean or 'N/A'} |")
    lines.append("")

    # Derivatives
    lines.append("## Derivatives")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Open Interest (USD) | {_usd(deriv.get('oi_usd'))} |")
    lines.append(f"| OI Lean | {deriv.get('oi_lean', 'null')} (requires period comparison) |")
    lines.append(f"| Funding Rate (latest) | {_fmt(deriv.get('funding_rate_latest'), 6)} |")
    lines.append(f"| Funding Rate (7d avg) | {_fmt(deriv.get('funding_rate_7d_avg'), 6)} |")
    fund_lean = deriv.get('funding_lean')
    lines.append(f"| Funding Lean | {_lean_icon(fund_lean)} {fund_lean or 'N/A'} |")
    lines.append(f"| BTC Long/Short Ratio | {_fmt(deriv.get('btc_long_short_ratio'), 4)} |")
    lines.append(f"| BTC Long % | {_fmt(deriv.get('btc_long_account_pct'), 2)} |")
    lines.append(f"| BTC Short % | {_fmt(deriv.get('btc_short_account_pct'), 2)} |")
    ls_lean = deriv.get('btc_ls_lean')
    lines.append(f"| BTC L/S Lean | {_lean_icon(ls_lean)} {ls_lean or 'N/A'} |")
    lines.append("")

    # On-Chain
    lines.append("## On-Chain")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| TVL (USD) | {_usd(onchain.get('tvl_usd'))} |")
    lines.append(f"| TVL 7d Change | {_pct(onchain.get('tvl_7d_change_pct'))} |")
    tvl_lean = onchain.get('tvl_lean')
    lines.append(f"| TVL Lean | {_lean_icon(tvl_lean)} {tvl_lean or 'N/A'} |")
    lines.append(f"| Chain Fees 24h (USD) | {_usd(onchain.get('chain_fees_24h_usd'))} |")
    lines.append(f"| Chain Fees 7d Avg (USD) | {_usd(onchain.get('chain_fees_7d_avg_usd'))} |")
    cf_lean = onchain.get('chain_fees_lean')
    lines.append(f"| Chain Fees Lean | {_lean_icon(cf_lean)} {cf_lean or 'null'} |")
    lines.append("")

    # News
    lines.append("## News")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Articles (24h) | {news.get('news_article_count_24h', 'N/A')} |")
    lines.append(f"| Bullish Votes (24h) | {news.get('news_bullish_votes_24h', 'N/A')} |")
    lines.append(f"| Bearish Votes (24h) | {news.get('news_bearish_votes_24h', 'N/A')} |")
    lines.append(f"| Important Votes (24h) | {news.get('news_important_votes_24h', 'N/A')} |")
    lines.append(f"| Sentiment Ratio | {_fmt(news.get('news_sentiment_ratio'), 4)} |")
    nv_lean = news.get('news_volume_lean')
    lines.append(f"| News Volume Lean | {_lean_icon(nv_lean)} {nv_lean or 'null'} |")
    spike_val = news.get('news_spike')
    lines.append(f"| Spike (>10 articles) | {'Yes' if spike_val else 'No'} |")
    lines.append("")

    # Macro
    lines.append("## Macro")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| BTC Price | {_usd(macro.get('btc_price_usd'))} |")
    lines.append(f"| BTC 24h Change | {_pct(macro.get('btc_24h_change_pct'))} |")
    lines.append(f"| BTC Direction | {macro.get('btc_direction', 'N/A')} |")
    lines.append(f"| BTC Dominance | {_fmt(macro.get('btc_dominance_pct'), 2)}% |")
    dom_lean = macro.get('btc_dominance_lean')
    lines.append(f"| BTC Dominance Lean | {_lean_icon(dom_lean)} {dom_lean or 'N/A'} |")
    lines.append(f"| Total Crypto MCap | {_usd(macro.get('total_crypto_mcap_usd'))} |")
    lines.append(f"| ETH/BTC Ratio | {_fmt(macro.get('eth_btc_ratio'), 4)} |")
    ethbtc_lean = macro.get('eth_btc_lean')
    lines.append(f"| ETH/BTC Lean | {_lean_icon(ethbtc_lean)} {ethbtc_lean or 'N/A'} |")
    lines.append(f"| DXY | {_fmt(macro.get('dxy'), 2)} |")
    dxy_lean = macro.get('dxy_lean')
    lines.append(f"| DXY Lean | {_lean_icon(dxy_lean)} {dxy_lean or 'N/A'} |")
    lines.append(f"| SPY Close | {_usd(macro.get('spy_close'))} |")
    lines.append(f"| SPY Change | {_pct(macro.get('spy_change_pct'))} |")
    spy_lean = macro.get('spy_lean')
    lines.append(f"| SPY Lean | {_lean_icon(spy_lean)} {spy_lean or 'N/A'} |")
    lines.append(f"| VIX Close | {_fmt(macro.get('vix_close'), 2)} |")
    vix_lean = macro.get('vix_lean')
    lines.append(f"| VIX Lean | {_lean_icon(vix_lean)} {vix_lean or 'N/A'} |")
    lines.append("")

    # Sentiment
    lines.append("## Sentiment")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Fear & Greed Value | {sent.get('fear_greed_value', 'N/A')} — {sent.get('fear_greed_label', 'N/A')} |")
    fg_lean = sent.get('fear_greed_lean')
    lines.append(f"| Fear & Greed Lean | {_lean_icon(fg_lean)} {fg_lean or 'N/A'} |")
    lines.append(f"| CG Sentiment Votes Up | {_fmt(sent.get('cg_sentiment_votes_up_pct'), 1)}% |")
    lines.append(f"| CG Sentiment Votes Down | {_fmt(sent.get('cg_sentiment_votes_down_pct'), 1)}% |")
    s_lean = sent.get('sentiment_lean')
    lines.append(f"| Sentiment Lean | {_lean_icon(s_lean)} {s_lean or 'null'} |")
    lines.append(f"| CG Twitter Followers | {sent.get('cg_twitter_followers', 'N/A')} |")
    lines.append(f"| CG Reddit Subscribers | {sent.get('cg_reddit_subscribers', 'N/A')} |")
    lines.append(f"| CG Reddit Active (48h) | {sent.get('cg_reddit_active_48h', 'N/A')} |")
    lines.append(f"| CG Telegram Users | {sent.get('cg_telegram_users', 'N/A')} |")
    lines.append(f"| CG GitHub Forks | {sent.get('cg_github_forks', 'N/A')} |")
    lines.append(f"| CG GitHub Stars | {sent.get('cg_github_stars', 'N/A')} |")
    lines.append(f"| CG GitHub Commits (4w) | {sent.get('cg_github_commits_4w', 'N/A')} |")
    lines.append(f"| CP Reddit Subscribers | {sent.get('cp_reddit_subscribers', 'N/A')} |")
    lines.append(f"| CP GitHub Contributors | {sent.get('cp_github_contributors', 'N/A')} |")
    lines.append(f"| CP GitHub Stars | {sent.get('cp_github_stars', 'N/A')} |")
    lines.append("")

    # Upcoming Events
    lines.append("## Upcoming Events")
    unlocks = events.get("unlocks_next_30d", [])
    if unlocks:
        lines.append("| Date | Amount (tokens) | Days Away | Note |")
        lines.append("|---|---|---|---|")
        for u in unlocks:
            lines.append(f"| {u['date']} | {u['amount_tokens']:,} | {u['days_away']} | {u.get('note', '')} |")
    else:
        lines.append("*No unlocks in the next 30 days.*")
    ul = events.get('unlock_lean')
    lines.append(f"\n**Unlock Lean:** {_lean_icon(ul)} {ul or 'N/A'}")
    lines.append("")

    # Signal Summary
    lines.append("## Signal Summary")
    overall = sig.get('overall_lean', 'N/A')
    lines.append(f"**Overall Lean:** `{overall}`")
    lines.append(
        f"Signals available: {sig.get('signals_available', 0)} | "
        f"Null: {sig.get('signals_null', 0)} | "
        f"Bullish: {sig.get('bullish_count', 0)} | "
        f"Bearish: {sig.get('bearish_count', 0)} | "
        f"Neutral: {sig.get('neutral_count', 0)}"
    )
    lines.append("")
    lines.append("| Signal | Lean |")
    lines.append("|---|---|")
    for key, lean in sig.get("signals", {}).items():
        icon = _lean_icon(lean)
        lean_str = lean if lean is not None else "null"
        lines.append(f"| {key} | {icon} {lean_str} |")
    lines.append("")

    if errors:
        lines.append("## Fetch Errors")
        for e in errors:
            lines.append(f"- `{e}`")
        lines.append("")

    lines.append("---")
    lines.append("*This report is data-only. It contains no price predictions and no financial advice.*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main report builder
# ---------------------------------------------------------------------------

def build_report(
    config: TokenConfig,
    config_path: str,
    cg_api_key: str,
    cmc_api_key: str,
    fred_api_key: str,
    alpha_vantage_api_key: str,
    cryptopanic_api_key: str = "",
) -> dict:
    fetch_errors: list = []
    now_utc = datetime.now(timezone.utc)
    today   = now_utc.date()

    # ------------------------------------------------------------------
    # [1/18] CoinGecko — token full data (price + community + developer + sentiment)
    # ------------------------------------------------------------------
    print("[1/18] Fetching CoinGecko — token full data...")
    cg_id = config.coingecko_id
    cg_token = safe_fetch(
        "CoinGecko token full data",
        lambda: fetch_cg_token_full_data(cg_id, cg_api_key),
        fetch_errors,
    )
    time.sleep(2)

    if cg_token is None:
        print("ERROR: CoinGecko token full data fetch failed — cannot proceed without price data.", file=sys.stderr)
        sys.exit(1)

    _md = cg_token.get("market_data", {})
    price_usd        = float(_md["current_price"]["usd"])
    price_change_24h = _md.get("price_change_percentage_24h_in_currency", {}).get("usd")
    price_change_7d  = _md.get("price_change_percentage_7d_in_currency", {}).get("usd")
    volume_24h       = _md.get("total_volume", {}).get("usd")
    market_cap       = _md.get("market_cap", {}).get("usd")
    ath_usd          = _md.get("ath", {}).get("usd")
    ath_drawdown_pct = _md.get("ath_change_percentage", {}).get("usd")
    vol_to_mcap      = (volume_24h / market_cap) if (volume_24h and market_cap and market_cap != 0) else None

    _comm = cg_token.get("community_data", {})
    cg_twitter_followers  = _comm.get("twitter_followers")
    cg_reddit_subscribers = _comm.get("reddit_subscribers")
    cg_reddit_active_48h  = _comm.get("reddit_active_accounts_48h")
    cg_telegram_users     = _comm.get("telegram_channel_user_count")

    cg_sentiment_votes_up_pct   = cg_token.get("sentiment_votes_up_percentage")
    cg_sentiment_votes_down_pct = cg_token.get("sentiment_votes_down_percentage")

    _dev = cg_token.get("developer_data", {})
    cg_github_forks       = _dev.get("forks")
    cg_github_stars       = _dev.get("stars")
    cg_github_commits_4w  = _dev.get("commit_count_4_weeks")

    # ------------------------------------------------------------------
    # [2/18] CoinPaprika — coin detail (social + developer stats)
    # ------------------------------------------------------------------
    print("[2/18] Fetching CoinPaprika — coin detail...")
    cp_reddit_subscribers  = None
    cp_github_contributors = None
    cp_github_stars        = None

    if config.coinpaprika_id:
        cp_id = config.coinpaprika_id
        cp_data = safe_fetch(
            "CoinPaprika coin detail",
            lambda: fetch_coinpaprika_coin(cp_id),
            fetch_errors,
        )
        if cp_data:
            try:
                for link in cp_data.get("links_extended", []):
                    ltype = link.get("type")
                    stats = link.get("stats", {})
                    if ltype == "reddit":
                        cp_reddit_subscribers = stats.get("subscribers")
                    elif ltype == "source_code":
                        cp_github_contributors = stats.get("contributors")
                        cp_github_stars        = stats.get("stars")
            except Exception as e:
                fetch_errors.append(f"coinpaprika:parse_error: {e}")
    else:
        print("  \u2717 CoinPaprika coin detail — coinpaprika_id not in config")
        fetch_errors.append("fetch_2:coinpaprika: skipped — coinpaprika_id not in config")
    time.sleep(1)

    # ------------------------------------------------------------------
    # [3/18] CryptoPanic — token news 24h
    # ------------------------------------------------------------------
    print("[3/18] Fetching CryptoPanic — token news 24h...")
    news_article_count_24h    = None
    news_bullish_votes_24h    = None
    news_bearish_votes_24h    = None
    news_important_votes_24h  = None
    news_sentiment_ratio      = None
    news_volume_lean          = None
    news_spike                = False

    if config.cryptopanic_symbol and cryptopanic_api_key:
        cp_sym = config.cryptopanic_symbol
        cp_news_data = safe_fetch(
            "CryptoPanic — token news 24h",
            lambda: fetch_cryptopanic_news(cp_sym, cryptopanic_api_key),
            fetch_errors,
        )
        if cp_news_data is not None:
            try:
                results = cp_news_data.get("results", [])
                articles_24h = []
                for article in results:
                    published_str = article.get("published_at", "")
                    try:
                        published_dt = datetime.fromisoformat(
                            published_str.replace("Z", "+00:00")
                        )
                        hours_ago = (now_utc - published_dt).total_seconds() / 3600
                        if hours_ago <= 24:
                            articles_24h.append(article)
                    except (ValueError, TypeError):
                        continue

                news_article_count_24h   = len(articles_24h)
                total_liked     = sum(a.get("votes", {}).get("liked", 0) for a in articles_24h)
                total_disliked  = sum(a.get("votes", {}).get("disliked", 0) for a in articles_24h)
                total_important = sum(a.get("votes", {}).get("important", 0) for a in articles_24h)
                news_bullish_votes_24h   = total_liked
                news_bearish_votes_24h   = total_disliked
                news_important_votes_24h = total_important

                total_votes = total_liked + total_disliked
                if total_votes > 0:
                    news_sentiment_ratio = round(total_liked / total_votes, 4)
                else:
                    news_sentiment_ratio = None

                if news_sentiment_ratio is not None:
                    if news_sentiment_ratio > 0.65:
                        news_volume_lean = "bullish"
                    elif news_sentiment_ratio < 0.35:
                        news_volume_lean = "bearish"
                    else:
                        news_volume_lean = "neutral"
                else:
                    news_volume_lean = "neutral"

                news_spike = news_article_count_24h > 10
            except Exception as e:
                fetch_errors.append(f"CryptoPanic — fetch failed: {e}")
        else:
            fetch_errors.append(f"CryptoPanic — fetch failed: no data returned")
    elif not config.cryptopanic_symbol:
        pass  # cryptopanic_symbol absent — skip silently, all fields remain None/False
    time.sleep(2)

    # ------------------------------------------------------------------
    # [4/18] CoinGecko — token OHLC 365d
    # ------------------------------------------------------------------
    print("[4/18] Fetching CoinGecko — token OHLC 365d...")
    ohlc_raw = safe_fetch(
        "CoinGecko OHLC 365d",
        lambda: fetch_cg_ohlc(cg_id, cg_api_key),
        fetch_errors,
    )
    time.sleep(2)

    if ohlc_raw is None:
        print("ERROR: CoinGecko OHLC fetch failed — cannot compute technical indicators.", file=sys.stderr)
        sys.exit(1)

    closes = [float(c[4]) for c in ohlc_raw]

    rsi_14                = compute_rsi(closes)
    macd_line, macd_signal, macd_hist = compute_macd(closes)
    sma_50                = compute_sma(closes, 50)
    ema_200_list          = compute_ema(closes, 200)
    ema_200               = ema_200_list[-1] if ema_200_list else None
    price_vs_sma50_pct    = ((price_usd - sma_50) / sma_50 * 100) if sma_50 else None
    price_vs_ema200_pct   = ((price_usd - ema_200) / ema_200 * 100) if ema_200 else None
    bb                    = compute_bollinger_bands(ohlc_raw)

    # ------------------------------------------------------------------
    # [5/18] CoinGecko — token market chart 30d (volume)
    # ------------------------------------------------------------------
    print("[5/18] Fetching CoinGecko — token market chart 30d (volume)...")
    market_chart_raw = safe_fetch(
        "CoinGecko token market chart 30d (volume)",
        lambda: fetch_cg_market_chart(cg_id, cg_api_key),
        fetch_errors,
    )
    time.sleep(2)

    volume_by_date: dict = {}
    if market_chart_raw:
        for entry in market_chart_raw:
            date_str = datetime.utcfromtimestamp(entry[0] / 1000).strftime("%Y-%m-%d")
            volume_by_date[date_str] = float(entry[1])

    up_day_volumes   = []
    down_day_volumes = []
    for entry in ohlc_raw:
        date_str    = datetime.utcfromtimestamp(entry[0] / 1000).strftime("%Y-%m-%d")
        open_price  = float(entry[1])
        close_price = float(entry[4])
        if date_str in volume_by_date:
            vol = volume_by_date[date_str]
            if close_price > open_price:
                up_day_volumes.append(vol)
            else:
                down_day_volumes.append(vol)

    volume_on_up_days   = sum(up_day_volumes) / len(up_day_volumes) if up_day_volumes else None
    volume_on_down_days = sum(down_day_volumes) / len(down_day_volumes) if down_day_volumes else None

    if volume_on_up_days is not None and volume_on_down_days is not None:
        if volume_on_up_days > volume_on_down_days * 1.1:
            volume_lean = "bullish"
        elif volume_on_down_days > volume_on_up_days * 1.1:
            volume_lean = "bearish"
        else:
            volume_lean = "neutral"
    else:
        volume_lean = None

    # ------------------------------------------------------------------
    # [6/18] CoinGecko — BTC markets
    # ------------------------------------------------------------------
    print("[6/18] Fetching CoinGecko — BTC markets...")
    cg_btc = safe_fetch(
        "CoinGecko BTC markets",
        lambda: fetch_cg_btc_markets(cg_api_key),
        fetch_errors,
    )
    time.sleep(2)

    btc_price_usd  = float(cg_btc["current_price"]) if cg_btc else None
    btc_24h_change = cg_btc.get("price_change_percentage_24h_in_currency") if cg_btc else None

    # ------------------------------------------------------------------
    # [7/18] CoinGecko — ETH/BTC ratio
    # ------------------------------------------------------------------
    print("[7/18] Fetching CoinGecko — ETH/BTC ratio...")
    eth_btc_ratio = safe_fetch(
        "CoinGecko ETH/BTC ratio",
        lambda: fetch_cg_eth_btc(cg_api_key),
        fetch_errors,
    )
    time.sleep(2)

    # ------------------------------------------------------------------
    # [8/18] DeFiLlama — historical chain TVL
    # ------------------------------------------------------------------
    print("[8/18] Fetching DeFiLlama — historical chain TVL...")
    chain = config.defillama_chain
    defillama_data = safe_fetch(
        "DeFiLlama historical TVL",
        lambda: fetch_defillama_historical_tvl(chain),
        fetch_errors,
    )
    time.sleep(1)

    tvl_usd           = None
    tvl_7d_change_pct = None

    if defillama_data:
        recent_tvl = defillama_data[-10:]
        tvl_usd = float(recent_tvl[-1]["tvl"])
        target_ts = (now_utc - timedelta(days=7)).timestamp()
        closest = min(recent_tvl, key=lambda e: abs(e["date"] - target_ts))
        tvl_7d_ago = float(closest["tvl"])
        if tvl_7d_ago and tvl_7d_ago != 0:
            tvl_7d_change_pct = ((tvl_usd - tvl_7d_ago) / tvl_7d_ago) * 100

    # ------------------------------------------------------------------
    # [9/18] DeFiLlama — chain fees overview
    # ------------------------------------------------------------------
    print("[9/18] Fetching DeFiLlama — chain fees overview...")
    chain_fees_24h_usd    = None
    chain_fees_7d_avg_usd = None
    chain_fees_lean       = None

    fees_data = safe_fetch(
        "DeFiLlama — chain fees overview",
        lambda: fetch_defillama_chain_fees(chain),
        fetch_errors,
    )
    time.sleep(1)

    if fees_data:
        total_data_chart = fees_data.get("totalDataChart", [])
        if total_data_chart:
            chain_fees_24h_usd = float(total_data_chart[-1][1])
            last_7 = total_data_chart[-7:]
            chain_fees_7d_avg_usd = sum(float(e[1]) for e in last_7) / len(last_7)
            if chain_fees_24h_usd > chain_fees_7d_avg_usd * 1.15:
                chain_fees_lean = "bullish"
            elif chain_fees_24h_usd < chain_fees_7d_avg_usd * 0.85:
                chain_fees_lean = "bearish"
            else:
                chain_fees_lean = "neutral"
        else:
            fetch_errors.append(f"DeFiLlama fees: no data available for chain {chain}")
    elif fees_data is None:
        fetch_errors.append(f"DeFiLlama fees: no data available for chain {chain}")

    # ------------------------------------------------------------------
    # [10/18] CMC — global metrics latest
    # ------------------------------------------------------------------
    print("[10/18] Fetching CMC — global metrics latest...")
    cmc_global = safe_fetch(
        "CMC global metrics",
        lambda: fetch_cmc_global_metrics(cmc_api_key),
        fetch_errors,
    )
    time.sleep(2)

    btc_dominance    = None
    total_mcap_usd   = None

    if cmc_global:
        btc_dominance  = cmc_global.get("btc_dominance")
        quote_usd      = cmc_global.get("quote", {}).get("USD", {})
        total_mcap_usd = quote_usd.get("total_market_cap")

    # ------------------------------------------------------------------
    # [11/18] CMC — Fear & Greed latest
    # ------------------------------------------------------------------
    print("[11/18] Fetching CMC — Fear & Greed latest...")
    fg_data = safe_fetch(
        "CMC Fear & Greed",
        lambda: fetch_cmc_fear_greed_latest(cmc_api_key),
        fetch_errors,
    )
    time.sleep(2)

    fg_value = None
    fg_label = None

    if fg_data:
        try:
            fg_value = int(fg_data["value"])
            fg_label = fg_data.get("value_classification")
        except (KeyError, ValueError) as e:
            fetch_errors.append(f"fear_greed:parse_error: {e}")

    # ------------------------------------------------------------------
    # [12/18] Binance spot — CEX price + 24h stats
    # ------------------------------------------------------------------
    print("[12/18] Fetching Binance spot — CEX price + 24h stats...")
    bsym = config.binance_symbol
    binance_spot = safe_fetch(
        "Binance spot",
        lambda: fetch_binance_spot(bsym),
        fetch_errors,
    )
    time.sleep(1)

    cex_price      = None
    cex_24h_high   = None
    cex_24h_low    = None
    cex_dex_spread = None

    if binance_spot:
        try:
            cex_price    = float(binance_spot["lastPrice"])
            cex_24h_high = float(binance_spot["highPrice"])
            cex_24h_low  = float(binance_spot["lowPrice"])
            if price_usd and price_usd != 0:
                cex_dex_spread = ((cex_price - price_usd) / price_usd) * 100
        except (KeyError, ValueError) as e:
            fetch_errors.append(f"binance_spot:parse_error: {e}")

    # ------------------------------------------------------------------
    # [13/18] Binance Futures — token open interest
    # ------------------------------------------------------------------
    print("[13/18] Fetching Binance Futures — token open interest...")
    oi_usd = None

    if config.binance_futures_symbol:
        fsym = config.binance_futures_symbol
        oi_data = safe_fetch(
            "Binance Futures OI",
            lambda: fetch_binance_oi(fsym),
            fetch_errors,
        )
        if oi_data:
            try:
                oi_usd = float(oi_data["openInterest"]) * price_usd
            except (KeyError, ValueError) as e:
                fetch_errors.append(f"binance_futures_oi:parse_error: {e}")
    else:
        print("  \u2717 Binance Futures OI — binance_futures_symbol not in config")
        fetch_errors.append("fetch_11:binance_futures_oi: skipped — binance_futures_symbol not in config")
    time.sleep(1)

    # ------------------------------------------------------------------
    # [14/18] Binance Futures — token funding rate
    # ------------------------------------------------------------------
    print("[14/18] Fetching Binance Futures — token funding rate...")
    funding_rate_latest = None
    funding_rate_7d_avg = None

    if config.binance_futures_symbol:
        fsym = config.binance_futures_symbol
        funding_data = safe_fetch(
            "Binance Futures funding rate",
            lambda: fetch_binance_funding_rate(fsym),
            fetch_errors,
        )
        if funding_data:
            try:
                rates = [float(e["fundingRate"]) for e in funding_data]
                funding_rate_latest = rates[-1]
                funding_rate_7d_avg = sum(rates) / len(rates)
            except (KeyError, ValueError) as e:
                fetch_errors.append(f"binance_funding:parse_error: {e}")
    else:
        print("  \u2717 Binance Futures funding rate — binance_futures_symbol not in config")
        fetch_errors.append("fetch_12:binance_futures_funding: skipped — binance_futures_symbol not in config")
    time.sleep(1)

    # ------------------------------------------------------------------
    # [15/18] Binance Futures — BTC long/short ratio
    # ------------------------------------------------------------------
    print("[15/18] Fetching Binance Futures — BTC long/short ratio...")
    ls_data = safe_fetch(
        "Binance Futures BTC L/S ratio",
        lambda: fetch_binance_btc_ls_ratio(),
        fetch_errors,
    )
    time.sleep(1)

    btc_ls_ratio  = None
    btc_long_pct  = None
    btc_short_pct = None

    if ls_data:
        try:
            btc_ls_ratio  = float(ls_data["longShortRatio"])
            btc_long_pct  = float(ls_data["longAccount"]) * 100
            btc_short_pct = float(ls_data["shortAccount"]) * 100
        except (KeyError, ValueError) as e:
            fetch_errors.append(f"binance_btc_ls:parse_error: {e}")

    # ------------------------------------------------------------------
    # [16/18] FRED — DXY latest
    # ------------------------------------------------------------------
    print("[16/18] Fetching FRED — DXY latest...")
    dxy_latest = None
    dxy_prior  = None

    if fred_api_key:
        dxy_data = safe_fetch(
            "FRED DXY",
            lambda: fetch_fred_series("DTWEXBGS", fred_api_key, 5),
            fetch_errors,
        )
        if dxy_data:
            non_null = [o for o in dxy_data if o.get("value") not in (".", None, "")]
            if non_null:
                dxy_latest = float(non_null[0]["value"])
            if len(non_null) >= 2:
                dxy_prior = float(non_null[1]["value"])
    else:
        print("  \u2717 FRED DXY — FRED_API_KEY not set")
        fetch_errors.append("fetch_14:fred_dxy: skipped — FRED_API_KEY not set")
    time.sleep(1)

    # ------------------------------------------------------------------
    # [17/18] FRED — VIX latest
    # ------------------------------------------------------------------
    print("[17/18] Fetching FRED — VIX latest...")
    vix_latest = None
    vix_prior  = None

    if fred_api_key:
        vix_data = safe_fetch(
            "FRED VIX",
            lambda: fetch_fred_series("VIXCLS", fred_api_key, 5),
            fetch_errors,
        )
        if vix_data:
            non_null = [o for o in vix_data if o.get("value") not in (".", None, "")]
            if non_null:
                vix_latest = float(non_null[0]["value"])
            if len(non_null) >= 2:
                vix_prior = float(non_null[1]["value"])
    else:
        print("  \u2717 FRED VIX — FRED_API_KEY not set")
        fetch_errors.append("fetch_15:fred_vix: skipped — FRED_API_KEY not set")
    time.sleep(1)

    # ------------------------------------------------------------------
    # [18/18] Alpha Vantage — SPY compact
    # ------------------------------------------------------------------
    print("[18/18] Fetching Alpha Vantage — SPY compact...")
    spy_close      = None
    spy_prev_close = None
    spy_change_pct = None

    if alpha_vantage_api_key:
        av_data = safe_fetch(
            "Alpha Vantage SPY",
            lambda: fetch_alpha_vantage_spy(alpha_vantage_api_key),
            fetch_errors,
        )
        if av_data:
            try:
                dates = sorted(av_data.keys(), reverse=True)
                spy_close      = float(av_data[dates[0]]["4. close"])
                spy_prev_close = float(av_data[dates[1]]["4. close"]) if len(dates) >= 2 else None
                if spy_prev_close and spy_prev_close != 0:
                    spy_change_pct = ((spy_close - spy_prev_close) / spy_prev_close) * 100
            except (KeyError, IndexError, ValueError) as e:
                fetch_errors.append(f"alpha_vantage_spy:parse_error: {e}")
    else:
        print("  \u2717 Alpha Vantage SPY — ALPHA_VANTAGE_API_KEY not set")
        fetch_errors.append("fetch_16:alpha_vantage_spy: skipped — ALPHA_VANTAGE_API_KEY not set")
    time.sleep(15)

    # ------------------------------------------------------------------
    # Upcoming unlocks
    # ------------------------------------------------------------------
    unlocks_next_30d = []
    has_near_term    = False

    for unlock in config.upcoming_unlocks:
        try:
            unlock_date = dateutil_parser.parse(unlock["date"]).date()
            days_away   = (unlock_date - today).days
            if 0 <= days_away <= 30:
                has_near_term = True
                unlocks_next_30d.append({
                    "date":          unlock["date"],
                    "amount_tokens": unlock["amount_tokens"],
                    "note":          unlock.get("note", ""),
                    "days_away":     days_away,
                })
        except Exception as exc:
            fetch_errors.append(f"upcoming_unlocks:parse_error: {exc}")

    unlock_lean = "bearish" if has_near_term else "bullish"

    # ------------------------------------------------------------------
    # Compute all 18 signal leans
    # ------------------------------------------------------------------
    rsi_lean_val     = lean_rsi(rsi_14)
    macd_lean_val    = lean_macd(macd_line, macd_signal)
    ema200_lean_val  = lean_ema200(price_usd, ema_200)
    bb_lean_val      = lean_bb(bb.get("position"))
    volume_lean_val  = volume_lean
    oi_lean_val      = None  # requires period comparison
    funding_lean_val = lean_funding_rate(funding_rate_7d_avg)
    btc_ls_lean_val  = lean_btc_ls(btc_ls_ratio)
    tvl_lean_val        = lean_tvl(tvl_7d_change_pct)
    chain_fees_lean_val = chain_fees_lean
    dom_lean_val        = lean_btc_dominance(btc_dominance)
    eth_btc_lean_val = "neutral"  # single snapshot — direction indeterminate
    dxy_lean_val     = lean_dxy(dxy_latest, dxy_prior)
    spy_lean_val     = lean_spy(spy_change_pct)
    vix_lean_val     = lean_vix(vix_latest, vix_prior)
    fg_lean_val      = lean_fear_greed(fg_value)
    sent_lean_val    = lean_sentiment_votes(cg_sentiment_votes_up_pct)
    news_volume_lean_val = news_volume_lean

    btc_direction = lean_btc_direction(btc_24h_change)

    signals = {
        "rsi_lean":              rsi_lean_val,
        "macd_lean":             macd_lean_val,
        "ema200_lean":           ema200_lean_val,
        "bb_lean":               bb_lean_val,
        "volume_lean":           volume_lean_val,
        "oi_lean":               oi_lean_val,
        "funding_lean":          funding_lean_val,
        "btc_ls_lean":           btc_ls_lean_val,
        "tvl_lean":              tvl_lean_val,
        "chain_fees_lean":       chain_fees_lean_val,
        "btc_dominance_lean":    dom_lean_val,
        "eth_btc_lean":          eth_btc_lean_val,
        "dxy_lean":              dxy_lean_val,
        "spy_lean":              spy_lean_val,
        "vix_lean":              vix_lean_val,
        "fear_greed_lean":       fg_lean_val,
        "news_volume_lean":      news_volume_lean_val,
        "sentiment_lean":        sent_lean_val,
    }

    available = {k: v for k, v in signals.items() if v is not None}
    null_sigs  = {k: v for k, v in signals.items() if v is None}
    bullish_count = sum(1 for v in available.values() if v == "bullish")
    bearish_count = sum(1 for v in available.values() if v == "bearish")
    neutral_count = sum(1 for v in available.values() if v == "neutral")
    overall_lean  = compute_overall_lean(signals)

    # ------------------------------------------------------------------
    # Assemble report
    # ------------------------------------------------------------------
    return {
        "report_metadata": {
            "generated_at":   now_utc.strftime("%Y-%m-%dT%H:%M:%S UTC"),
            "token":          config.token_name,
            "config_file":    config_path,
            "script_version": SCRIPT_VERSION,
            "schema_version": SCHEMA_VERSION,
        },
        "price_data": {
            "price_usd":               round(price_usd, 6),
            "price_change_24h_pct":    round(price_change_24h, 4)  if price_change_24h  is not None else None,
            "price_change_7d_pct":     round(price_change_7d, 4)   if price_change_7d   is not None else None,
            "volume_24h_usd":          round(volume_24h, 2)        if volume_24h        is not None else None,
            "market_cap_usd":          round(market_cap, 2)        if market_cap        is not None else None,
            "volume_to_mcap_ratio":    round(vol_to_mcap, 6)       if vol_to_mcap       is not None else None,
            "ath_usd":                 round(ath_usd, 6)           if ath_usd           is not None else None,
            "ath_drawdown_pct":        round(ath_drawdown_pct, 4)  if ath_drawdown_pct  is not None else None,
            "cex_price_usd":           round(cex_price, 6)         if cex_price         is not None else None,
            "cex_24h_high":            round(cex_24h_high, 6)      if cex_24h_high      is not None else None,
            "cex_24h_low":             round(cex_24h_low, 6)       if cex_24h_low       is not None else None,
            "cex_dex_spread_pct":      round(cex_dex_spread, 6)    if cex_dex_spread    is not None else None,
            "volume_on_up_days_usd":   volume_on_up_days,
            "volume_on_down_days_usd": volume_on_down_days,
            "volume_lean":             volume_lean_val,
        },
        "technical_indicators": {
            "rsi_14":              round(rsi_14, 4)            if rsi_14            is not None else None,
            "rsi_lean":            rsi_lean_val,
            "macd": {
                "macd_line":   round(macd_line, 6)   if macd_line   is not None else None,
                "signal_line": round(macd_signal, 6) if macd_signal is not None else None,
                "histogram":   round(macd_hist, 6)   if macd_hist   is not None else None,
            },
            "macd_lean":           macd_lean_val,
            "sma_50":              round(sma_50, 6)            if sma_50            is not None else None,
            "price_vs_sma50_pct":  round(price_vs_sma50_pct, 4)  if price_vs_sma50_pct  is not None else None,
            "ema_200":             round(ema_200, 6)           if ema_200           is not None else None,
            "price_vs_ema200_pct": round(price_vs_ema200_pct, 4) if price_vs_ema200_pct is not None else None,
            "ema200_lean":         ema200_lean_val,
            "bollinger_bands":     bb,
            "bb_lean":             bb_lean_val,
        },
        "derivatives": {
            "oi_usd":                round(oi_usd, 2)          if oi_usd          is not None else None,
            "oi_lean":               None,
            "funding_rate_latest":   funding_rate_latest,
            "funding_rate_7d_avg":   funding_rate_7d_avg,
            "funding_lean":          funding_lean_val,
            "btc_long_short_ratio":  round(btc_ls_ratio, 4)   if btc_ls_ratio   is not None else None,
            "btc_long_account_pct":  round(btc_long_pct, 4)   if btc_long_pct   is not None else None,
            "btc_short_account_pct": round(btc_short_pct, 4)  if btc_short_pct  is not None else None,
            "btc_ls_lean":           btc_ls_lean_val,
        },
        "on_chain": {
            "tvl_usd":               round(tvl_usd, 2)               if tvl_usd               is not None else None,
            "tvl_7d_change_pct":     round(tvl_7d_change_pct, 4)     if tvl_7d_change_pct     is not None else None,
            "tvl_lean":              tvl_lean_val,
            "chain_fees_24h_usd":    round(chain_fees_24h_usd, 2)    if chain_fees_24h_usd    is not None else None,
            "chain_fees_7d_avg_usd": round(chain_fees_7d_avg_usd, 2) if chain_fees_7d_avg_usd is not None else None,
            "chain_fees_lean":       chain_fees_lean,
        },
        "news": {
            "news_article_count_24h":   news_article_count_24h,
            "news_bullish_votes_24h":   news_bullish_votes_24h,
            "news_bearish_votes_24h":   news_bearish_votes_24h,
            "news_important_votes_24h": news_important_votes_24h,
            "news_sentiment_ratio":     news_sentiment_ratio,
            "news_volume_lean":         news_volume_lean,
            "news_spike":               news_spike,
        },
        "macro": {
            "btc_price_usd":        round(btc_price_usd, 2)   if btc_price_usd  is not None else None,
            "btc_24h_change_pct":   round(btc_24h_change, 4)  if btc_24h_change is not None else None,
            "btc_direction":        btc_direction,
            "btc_dominance_pct":    round(btc_dominance, 4)   if btc_dominance  is not None else None,
            "btc_dominance_lean":   dom_lean_val,
            "total_crypto_mcap_usd": total_mcap_usd,
            "eth_btc_ratio":        round(eth_btc_ratio, 6)   if eth_btc_ratio  is not None else None,
            "eth_btc_lean":         eth_btc_lean_val,
            "dxy":                  round(dxy_latest, 4)       if dxy_latest     is not None else None,
            "dxy_lean":             dxy_lean_val,
            "spy_close":            round(spy_close, 4)        if spy_close      is not None else None,
            "spy_change_pct":       round(spy_change_pct, 4)   if spy_change_pct is not None else None,
            "spy_lean":             spy_lean_val,
            "vix_close":            round(vix_latest, 4)       if vix_latest     is not None else None,
            "vix_lean":             vix_lean_val,
        },
        "sentiment": {
            "fear_greed_value":             fg_value,
            "fear_greed_label":             fg_label,
            "fear_greed_lean":              fg_lean_val,
            "cg_sentiment_votes_up_pct":    cg_sentiment_votes_up_pct,
            "cg_sentiment_votes_down_pct":  cg_sentiment_votes_down_pct,
            "sentiment_lean":               sent_lean_val,
            "cg_twitter_followers":         cg_twitter_followers,
            "cg_reddit_subscribers":        cg_reddit_subscribers,
            "cg_reddit_active_48h":         cg_reddit_active_48h,
            "cg_telegram_users":            cg_telegram_users,
            "cg_github_forks":              cg_github_forks,
            "cg_github_stars":              cg_github_stars,
            "cg_github_commits_4w":         cg_github_commits_4w,
            "cp_reddit_subscribers":        cp_reddit_subscribers,
            "cp_github_contributors":       cp_github_contributors,
            "cp_github_stars":              cp_github_stars,
        },
        "upcoming_events": {
            "unlocks_next_30d": unlocks_next_30d,
            "unlock_lean":      unlock_lean,
        },
        "signal_summary": {
            "signals_evaluated": 18,
            "signals_available": len(available),
            "signals_null":      len(null_sigs),
            "bullish_count":     bullish_count,
            "bearish_count":     bearish_count,
            "neutral_count":     neutral_count,
            "overall_lean":      overall_lean,
            "signals":           signals,
        },
        "fetch_errors": fetch_errors,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description=f"Fetch live market data and produce a structured report (v{SCRIPT_VERSION})."
    )
    parser.add_argument(
        "--config",
        default="configs/sui.json",
        help="Path to token config JSON file (default: configs/sui.json)",
    )
    parser.add_argument(
        "--md",
        action="store_true",
        help="Also output a Markdown report (saved to reports/)",
    )
    args = parser.parse_args()

    script_dir      = Path(__file__).parent.resolve()
    config_path_abs = (script_dir / args.config).resolve()
    reports_dir     = script_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    env_path = script_dir / ".env"
    load_dotenv(dotenv_path=env_path)

    def _key(name: str) -> str:
        val = os.environ.get(name, "").strip()
        if not val:
            print(f"WARNING: {name} is not set — related fetches will be skipped.", file=sys.stderr)
        return val

    cg_api_key            = _key("COINGECKO_API_KEY")
    cmc_api_key           = _key("CMC_API_KEY")
    fred_api_key          = _key("FRED_API_KEY")
    alpha_vantage_api_key = _key("ALPHA_VANTAGE_API_KEY")
    cryptopanic_api_key   = _key("CRYPTOPANIC_API_KEY")

    if not cg_api_key:
        print("ERROR: COINGECKO_API_KEY is not set — cannot fetch price data.", file=sys.stderr)
        sys.exit(1)

    if not cmc_api_key:
        print("ERROR: CMC_API_KEY is not set — cannot fetch Fear & Greed data.", file=sys.stderr)
        sys.exit(1)

    if not config_path_abs.exists():
        print(f"ERROR: config file not found: {config_path_abs}", file=sys.stderr)
        sys.exit(1)

    config = load_config(str(config_path_abs))
    print(f"Fetching market data for {config.token_name} (v{SCRIPT_VERSION})...")

    report = build_report(
        config=config,
        config_path=args.config,
        cg_api_key=cg_api_key,
        cmc_api_key=cmc_api_key,
        fred_api_key=fred_api_key,
        alpha_vantage_api_key=alpha_vantage_api_key,
        cryptopanic_api_key=cryptopanic_api_key,
    )

    ts        = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    prefix    = config.output_prefix
    json_path = reports_dir / f"{prefix}_{ts}.json"
    md_path   = reports_dir / f"{prefix}_{ts}.md"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nJSON report saved: {json_path}")

    if args.md:
        md_content = render_markdown(report)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)
        print(f"Markdown report saved: {md_path}")
        print()
        print(md_content)

    if report["fetch_errors"]:
        print("\nFetch errors/notes:", file=sys.stderr)
        for e in report["fetch_errors"]:
            print(f"  - {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
