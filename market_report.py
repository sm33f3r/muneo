#!/usr/bin/env python3
"""
market_report.py — Standalone market data reporting tool.

Fetches live market data from CoinMarketCap (authenticated) and several
free public APIs, computes technical indicators, and outputs a structured
report as JSON (and optionally Markdown).

Read-only: no trades, no wallets, no persistent state beyond report files.

API KEY SETUP
-------------
1. Copy .env.example to .env:
       cp .env.example .env
2. Open .env and set:
       CMC_API_KEY=your_actual_key_here
3. The .env file is listed in .gitignore and will never be committed.

Get a free key at: https://coinmarketcap.com/api/
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dateutil import parser as dateutil_parser
from dotenv import load_dotenv

SCRIPT_VERSION = "1.1.0"
API_TIMEOUT = 5  # seconds

# CoinMarketCap base URL — all authenticated calls go here
CMC_BASE = "https://pro-api.coinmarketcap.com"


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

@dataclass
class TokenConfig:
    token_name: str
    cmc_slug: str           # CoinMarketCap URL slug (e.g. "sui", "solana")
    coingecko_id: str       # CoinGecko coin ID for OHLC history (e.g. "sui", "solana")
    binance_symbol: str     # Binance ticker symbol (e.g. "SUIUSDT")
    defillama_chain: str    # DeFiLlama chain name (e.g. "Sui")
    upcoming_unlocks: list[dict]
    output_prefix: str


def load_config(config_path: str) -> TokenConfig:
    with open(config_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return TokenConfig(
        token_name=raw["token_name"],
        cmc_slug=raw["cmc_slug"],
        coingecko_id=raw["coingecko_id"],
        binance_symbol=raw["binance_symbol"],
        defillama_chain=raw["defillama_chain"],
        upcoming_unlocks=raw.get("upcoming_unlocks", []),
        output_prefix=raw["output_prefix"],
    )


# ---------------------------------------------------------------------------
# Technical indicator math (no external TA library)
# ---------------------------------------------------------------------------

def compute_ema(prices: list[float], period: int) -> list[float]:
    """Exponential Moving Average using standard multiplier 2/(period+1)."""
    if len(prices) < period:
        return []
    alpha = 2.0 / (period + 1)
    ema_values: list[float] = []
    # Seed with SMA of first `period` prices
    seed = sum(prices[:period]) / period
    ema_values.append(seed)
    for price in prices[period:]:
        ema_values.append(price * alpha + ema_values[-1] * (1 - alpha))
    return ema_values


def compute_rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """
    RSI using Wilder's smoothing.
    Seeds the initial average gain/loss with a simple mean over the first
    `period` deltas, then applies exponential smoothing (alpha = 1/period).
    """
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder smoothing
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Returns (macd_line, signal_line, histogram) — most recent values only."""
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

    macd_val = macd_series[-1]
    signal_val = signal_series[-1]
    return macd_val, signal_val, macd_val - signal_val


def compute_sma(closes: list[float], period: int) -> Optional[float]:
    """Simple moving average of the last `period` closing prices."""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


# ---------------------------------------------------------------------------
# CoinMarketCap fetch functions (authenticated)
# ---------------------------------------------------------------------------

def _cmc_headers(api_key: str) -> dict[str, str]:
    return {
        "X-CMC_PRO_API_KEY": api_key,
        "Accept": "application/json",
    }


def fetch_cmc_quotes(
    slugs: list[str],
    api_key: str,
) -> tuple[Optional[dict[str, dict]], Optional[str]]:
    """
    Fetch current price/market quotes for one or more tokens from CMC.

    Returns a dict keyed by slug (e.g. {"sui": {...}, "bitcoin": {...}}).
    Each value contains the USD quote fields directly.

    CMC endpoint: GET /v1/cryptocurrency/quotes/latest
    Docs: https://coinmarketcap.com/api/documentation/v1/#operation/getV1CryptocurrencyQuotesLatest
    """
    url = f"{CMC_BASE}/v1/cryptocurrency/quotes/latest"
    params = {"slug": ",".join(slugs), "convert": "USD"}
    try:
        resp = requests.get(url, headers=_cmc_headers(api_key), params=params, timeout=API_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        raw_data: dict = payload.get("data", {})

        # CMC keys the response by numeric ID, not by slug — remap to slug
        result: dict[str, dict] = {}
        for _id, entry in raw_data.items():
            slug = entry.get("slug", "").lower()
            quote_usd = entry.get("quote", {}).get("USD", {})
            result[slug] = {
                "price":             quote_usd.get("price"),
                "volume_24h":        quote_usd.get("volume_24h"),
                "market_cap":        quote_usd.get("market_cap"),
                "percent_change_24h": quote_usd.get("percent_change_24h"),
                "percent_change_7d":  quote_usd.get("percent_change_7d"),
            }
        if not result:
            return None, f"cmc_quotes:{slugs}: empty data in response"
        return result, None
    except Exception as exc:
        return None, f"cmc_quotes:{slugs}: {exc}"


def fetch_coingecko_ohlc(
    coingecko_id: str,
    days: int = 365,
) -> tuple[Optional[dict[str, list[float]]], Optional[str]]:
    """
    Fetch daily OHLC candles from CoinGecko (free, no API key required).

    Returns {"closes": [...], "highs": [...]} in chronological order.
    Used for RSI, MACD, SMA, and the 365-day period high.

    CoinGecko's free-tier OHLC endpoint only accepts specific day values:
    1, 7, 14, 30, 90, 180, 365. We use 365 to get enough history for
    200-period indicators.

    Each candle from the API: [timestamp, open, high, low, close]
    """
    url = (
        f"https://api.coingecko.com/api/v3/coins/{coingecko_id}/ohlc"
        f"?vs_currency=usd&days={days}"
    )
    try:
        resp = requests.get(url, timeout=API_TIMEOUT)
        resp.raise_for_status()
        candles = resp.json()
        if not candles:
            return None, f"coingecko_ohlc:{coingecko_id}: empty response"
        closes = [float(c[4]) for c in candles]
        highs  = [float(c[2]) for c in candles]
        return {"closes": closes, "highs": highs}, None
    except Exception as exc:
        return None, f"coingecko_ohlc:{coingecko_id}: {exc}"


# ---------------------------------------------------------------------------
# Free public API fetch functions (no key required)
# ---------------------------------------------------------------------------

def fetch_cmc_fear_and_greed(
    api_key: str,
    limit: int = 8,
) -> tuple[Optional[list[dict]], Optional[str]]:
    """
    Fetch the last `limit` Fear & Greed readings from CoinMarketCap.

    Returns entries in newest-first order (index 0 = today).
    Each entry has keys: "value", "value_classification", "timestamp".

    CMC endpoint: GET /v3/fear-and-greed/historical
    """
    url = f"{CMC_BASE}/v3/fear-and-greed/historical"
    params = {"limit": limit}
    try:
        resp = requests.get(url, headers=_cmc_headers(api_key), params=params, timeout=API_TIMEOUT)
        resp.raise_for_status()
        entries = resp.json().get("data", [])
        if not entries:
            return None, "cmc_fear_and_greed: empty response"
        return entries, None
    except Exception as exc:
        return None, f"cmc_fear_and_greed: {exc}"


def fetch_defillama_chains() -> tuple[Optional[list[dict]], Optional[str]]:
    """Fetch all chain TVL data from DeFiLlama."""
    url = "https://api.llama.fi/v2/chains"
    try:
        resp = requests.get(url, timeout=API_TIMEOUT)
        resp.raise_for_status()
        return resp.json(), None
    except Exception as exc:
        return None, f"defillama_chains: {exc}"


def fetch_binance_ticker(symbol: str) -> tuple[Optional[dict], Optional[str]]:
    """Fetch 24hr ticker from Binance public API."""
    url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
    try:
        resp = requests.get(url, timeout=API_TIMEOUT)
        resp.raise_for_status()
        return resp.json(), None
    except Exception as exc:
        return None, f"binance_ticker:{symbol}: {exc}"


# ---------------------------------------------------------------------------
# Prior report lookup (for TVL trend)
# ---------------------------------------------------------------------------

def find_prior_report(reports_dir: Path, output_prefix: str) -> Optional[dict]:
    """
    Return the most recent previously saved JSON report for this token,
    or None if no prior report exists.
    """
    candidates = sorted(reports_dir.glob(f"{output_prefix}_*.json"))
    if not candidates:
        return None
    try:
        with open(candidates[-1], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


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
    if abs(diff) <= 0.0001:
        return "neutral"
    return "bullish" if diff > 0 else "bearish"


def lean_vs_sma(pct: Optional[float]) -> str:
    if pct is None:
        return "neutral"
    if pct > 1.0:
        return "bullish"
    if pct < -1.0:
        return "bearish"
    return "neutral"


def lean_fear_and_greed(value: Optional[int]) -> str:
    if value is None:
        return "neutral"
    if value < 25:
        return "bullish"
    if value > 75:
        return "bearish"
    return "neutral"


def lean_fg_trend(trend: Optional[str]) -> str:
    if trend == "rising":
        return "bullish"
    if trend == "falling":
        return "bearish"
    return "neutral"


def lean_tvl(tvl_change_pct: Optional[float]) -> str:
    if tvl_change_pct is None:
        return "neutral"
    if tvl_change_pct > 2.0:
        return "bullish"
    if tvl_change_pct < -2.0:
        return "bearish"
    return "neutral"


def lean_btc(btc_change_24h: Optional[float]) -> str:
    if btc_change_24h is None:
        return "neutral"
    if btc_change_24h > 1.0:
        return "bullish"
    if btc_change_24h < -1.0:
        return "bearish"
    return "neutral"


def lean_upcoming_unlock(has_near_term: bool) -> str:
    return "bearish" if has_near_term else "bullish"


def lean_volume_vs_market_cap(ratio: Optional[float]) -> str:
    if ratio is None:
        return "neutral"
    if ratio > 0.15:
        return "bullish"
    if ratio < 0.05:
        return "bearish"
    return "neutral"


def compute_overall_lean(bullish: int, bearish: int) -> str:
    if bullish >= bearish + 3:
        return "bullish"
    if bearish >= bullish + 3:
        return "bearish"
    if bullish > bearish:
        return "slight_bullish"
    if bearish > bullish:
        return "slight_bearish"
    return "neutral"


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def _fmt(value: object, decimals: int = 4) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.{decimals}f}"
    return str(value)


def _pct(value: object) -> str:
    if value is None:
        return "N/A"
    return f"{value:.2f}%"


def _usd(value: object) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, (int, float)):
        if value >= 1_000_000_000:
            return f"${value / 1_000_000_000:.2f}B"
        if value >= 1_000_000:
            return f"${value / 1_000_000:.2f}M"
        return f"${value:,.4f}"
    return str(value)


def _lean_icon(lean: str) -> str:
    return {"bullish": "✅", "bearish": "🔴", "neutral": "⚠️"}.get(lean, "⚠️")


def render_markdown(report: dict) -> str:
    meta   = report["report_metadata"]
    price  = report["price_data"]
    tech   = report["technical_indicators"]
    sent   = report["sentiment"]
    eco    = report["ecosystem"]
    cex    = report["cex_data"]
    macro  = report["macro"]
    events = report["upcoming_events"]
    sig    = report["signal_summary"]
    errors = report.get("fetch_errors", [])

    lines: list[str] = []

    # Header
    lines.append(f"# {meta['token']} Market Report")
    lines.append(f"**Generated:** {meta['generated_at']}  ")
    lines.append(f"**Script version:** {meta['script_version']}  ")
    lines.append(f"**Config:** `{meta['config_file']}`")
    lines.append("")

    # Price Data
    lines.append("## Price Data")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Current Price (USD) | {_usd(price['current_price_usd'])} |")
    lines.append(f"| 24h Change | {_pct(price['price_change_24h_pct'])} |")
    lines.append(f"| 7d Change | {_pct(price['price_change_7d_pct'])} |")
    lines.append(f"| 24h Volume | {_usd(price['volume_24h_usd'])} |")
    lines.append(f"| Market Cap | {_usd(price['market_cap_usd'])} |")
    lines.append(f"| Volume / Market Cap Ratio | {_fmt(price['volume_to_market_cap_ratio'])} |")
    lines.append(f"| 365-Day High (USD) | {_usd(price['ath_usd'])} |")
    lines.append(f"| Drawdown from 365-Day High | {_pct(price['drawdown_from_ath_pct'])} |")
    lines.append(f"| Source | {price['source']} |")
    lines.append("")

    # Technical Indicators
    lines.append("## Technical Indicators")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| RSI (14) | {_fmt(tech['rsi_14'], 2)} |")
    lines.append(f"| MACD Line | {_fmt(tech['macd_line'])} |")
    lines.append(f"| MACD Signal Line | {_fmt(tech['macd_signal_line'])} |")
    lines.append(f"| MACD Histogram | {_fmt(tech['macd_histogram'])} |")
    lines.append(f"| MACD Crossover | {tech['macd_crossover'] or 'N/A'} |")
    lines.append(f"| SMA 50 | {_usd(tech['sma_50'])} |")
    lines.append(f"| SMA 200 | {_usd(tech['sma_200'])} |")
    lines.append(f"| Price vs SMA 50 | {_pct(tech['price_vs_sma50_pct'])} |")
    lines.append(f"| Price vs SMA 200 | {_pct(tech['price_vs_sma200_pct'])} |")
    lines.append(f"| Source | {tech['source']} |")
    lines.append("")

    # Sentiment
    lines.append("## Sentiment")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Fear & Greed Index | {sent['fear_and_greed_value']} — {sent['fear_and_greed_label'] or 'N/A'} |")
    lines.append(f"| 7-Day Trend | {sent['fg_7day_trend'] or 'N/A'} |")
    history_str = ", ".join(str(v) for v in sent["fg_7day_history"]) if sent["fg_7day_history"] else "N/A"
    lines.append(f"| 7-Day History (oldest to newest) | {history_str} |")
    lines.append(f"| Source | {sent['source']} |")
    lines.append("")

    # Ecosystem
    lines.append("## Ecosystem")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| TVL (USD) | {_usd(eco['tvl_usd'])} |")
    lines.append(f"| TVL Change vs Prior Report | {_pct(eco['tvl_change_pct'])} |")
    lines.append(f"| TVL Trend | {eco['tvl_trend'] or 'N/A'} |")
    lines.append(f"| Source | {eco['source']} |")
    lines.append("")

    # CEX Data
    lines.append("## CEX Data (Binance)")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Binance Price (USD) | {_usd(cex['binance_price_usd'])} |")
    lines.append(f"| 24h High | {_usd(cex['binance_24h_high'])} |")
    lines.append(f"| 24h Low | {_usd(cex['binance_24h_low'])} |")
    lines.append(f"| 24h Volume (base asset) | {_fmt(cex['binance_24h_volume'], 2)} |")
    lines.append(f"| CEX/DEX Spread | {_pct(cex['cex_dex_spread_pct'])} |")
    lines.append(f"| Source | {cex['source']} |")
    lines.append("")

    # Macro
    lines.append("## Macro Context")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| BTC Price (USD) | {_usd(macro['btc_price_usd'])} |")
    lines.append(f"| BTC 24h Change | {_pct(macro['btc_change_24h_pct'])} |")
    lines.append(f"| Source | {macro['source']} |")
    lines.append("")

    # Upcoming Events
    lines.append("## Upcoming Events")
    unlocks = events["unlocks"]
    if unlocks:
        lines.append("| Date | Amount (tokens) | Days Until | Near-Term | Note |")
        lines.append("|---|---|---|---|---|")
        for u in unlocks:
            near = "YES" if u.get("near_term") else "No"
            lines.append(
                f"| {u['date']} | {u['amount_tokens']:,} | {u['days_until']} | {near} | {u.get('note', '')} |"
            )
    else:
        lines.append("*No upcoming unlocks configured.*")
    lines.append("")

    # Signal Summary
    lines.append("## Signal Summary")
    lines.append("| Signal | Value | Lean |")
    lines.append("|---|---|---|")
    for key, info in sig.get("signals", {}).items():
        icon = _lean_icon(info.get("lean", "neutral"))
        val_str = _fmt(info.get("value")) if info.get("value") is not None else "N/A"
        lines.append(f"| {key} | {val_str} | {icon} {info.get('lean', 'neutral')} |")
    lines.append("")
    lines.append(f"**Bullish:** {sig['bullish_count']}  |  **Bearish:** {sig['bearish_count']}  |  **Neutral:** {sig['neutral_count']}")
    lines.append("")
    lines.append(f"**Overall Lean:** `{sig['overall_lean']}`")
    lines.append("")
    lines.append(f"> {sig['summary_note']}")
    lines.append("")

    if errors:
        lines.append("## Fetch Errors")
        for e in errors:
            lines.append(f"- `{e}`")
        lines.append("")

    lines.append("---")
    lines.append("*This report contains market data only. It is not financial advice and makes no price predictions.*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main report builder
# ---------------------------------------------------------------------------

def build_report(
    config: TokenConfig,
    config_path: str,
    reports_dir: Path,
    api_key: str,
) -> dict:
    fetch_errors: list[str] = []
    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()

    # ------------------------------------------------------------------
    # 1 & 4. CMC quotes — token + BTC in a single API call
    # ------------------------------------------------------------------
    quotes, err = fetch_cmc_quotes([config.cmc_slug, "bitcoin"], api_key)
    if err:
        fetch_errors.append(err)

    token_q = (quotes or {}).get(config.cmc_slug, {})
    btc_q   = (quotes or {}).get("bitcoin", {})

    current_price:    Optional[float] = token_q.get("price")
    price_change_24h: Optional[float] = token_q.get("percent_change_24h")
    price_change_7d:  Optional[float] = token_q.get("percent_change_7d")
    volume_24h:       Optional[float] = token_q.get("volume_24h")
    market_cap:       Optional[float] = token_q.get("market_cap")
    vol_mc_ratio:     Optional[float] = None
    if volume_24h and market_cap and market_cap != 0:
        vol_mc_ratio = volume_24h / market_cap

    btc_price:      Optional[float] = btc_q.get("price")
    btc_change_24h: Optional[float] = btc_q.get("percent_change_24h")

    # ------------------------------------------------------------------
    # 2. CMC OHLCV historical → technical indicators + period high
    # ------------------------------------------------------------------
    ohlcv, err = fetch_coingecko_ohlc(config.coingecko_id)
    if err:
        fetch_errors.append(err)

    closes: list[float] = (ohlcv or {}).get("closes", [])
    highs:  list[float] = (ohlcv or {}).get("highs", [])

    rsi_14:              Optional[float] = None
    macd_line:           Optional[float] = None
    macd_signal:         Optional[float] = None
    macd_hist:           Optional[float] = None
    macd_crossover:      Optional[str]   = None
    sma_50:              Optional[float] = None
    sma_200:             Optional[float] = None
    price_vs_sma50_pct:  Optional[float] = None
    price_vs_sma200_pct: Optional[float] = None
    # Period high derived from 365-day OHLCV highs (CMC has no ATH endpoint)
    period_high:         Optional[float] = max(highs) if highs else None
    drawdown_from_high:  Optional[float] = None

    if closes:
        rsi_14 = compute_rsi(closes)
        macd_line, macd_signal, macd_hist = compute_macd(closes)
        sma_50  = compute_sma(closes, 50)
        sma_200 = compute_sma(closes, 200)

        ref = current_price if current_price is not None else closes[-1]
        if sma_50 and sma_50 != 0:
            price_vs_sma50_pct = ((ref - sma_50) / sma_50) * 100
        if sma_200 and sma_200 != 0:
            price_vs_sma200_pct = ((ref - sma_200) / sma_200) * 100

        if macd_line is not None and macd_signal is not None:
            diff = macd_line - macd_signal
            if abs(diff) <= 0.0001:
                macd_crossover = "neutral"
            elif diff > 0:
                macd_crossover = "bullish_crossover"
            else:
                macd_crossover = "bearish_crossover"

    if period_high and current_price and period_high != 0:
        drawdown_from_high = ((current_price - period_high) / period_high) * 100

    # ------------------------------------------------------------------
    # 3. Fear & Greed (CoinMarketCap)
    # ------------------------------------------------------------------
    fg_entries, err = fetch_cmc_fear_and_greed(api_key)
    if err:
        fetch_errors.append(err)

    fg_value:       Optional[int] = None
    fg_label:       Optional[str] = None
    fg_7day_trend:  Optional[str] = None
    fg_7day_history: list[int]    = []

    if fg_entries:
        try:
            fg_value = int(fg_entries[0]["value"])
            fg_label = fg_entries[0].get("value_classification")
            # Indices 1–7: yesterday through 7 days ago; reverse for oldest→newest
            raw_history = [int(e["value"]) for e in fg_entries[1:8]]
            fg_7day_history = list(reversed(raw_history))
            prior_fg = int(fg_entries[7]["value"]) if len(fg_entries) >= 8 else None
            if prior_fg is not None:
                diff = fg_value - prior_fg
                fg_7day_trend = "rising" if diff > 3 else ("falling" if diff < -3 else "flat")
            else:
                fg_7day_trend = "insufficient_data"
        except (IndexError, KeyError, ValueError) as exc:
            fetch_errors.append(f"fear_and_greed:parse_error: {exc}")

    # ------------------------------------------------------------------
    # 5. DeFiLlama TVL (no key required)
    # ------------------------------------------------------------------
    chains_data, err = fetch_defillama_chains()
    if err:
        fetch_errors.append(err)

    tvl_usd:        Optional[float] = None
    tvl_change_pct: Optional[float] = None
    tvl_trend:      Optional[str]   = None

    if chains_data:
        target = config.defillama_chain.lower()
        matched = next(
            (c for c in chains_data if c.get("name", "").lower() == target), None
        )
        if matched:
            tvl_usd = matched.get("tvl")
        else:
            fetch_errors.append(f"defillama_chains: no chain matched '{config.defillama_chain}'")

    prior_report = find_prior_report(reports_dir, config.output_prefix)
    if prior_report and tvl_usd is not None:
        prior_tvl = prior_report.get("ecosystem", {}).get("tvl_usd")
        if prior_tvl and prior_tvl != 0:
            tvl_change_pct = ((tvl_usd - prior_tvl) / prior_tvl) * 100
            tvl_trend = "increasing" if tvl_change_pct > 2.0 else ("decreasing" if tvl_change_pct < -2.0 else "stable")
        else:
            tvl_trend = "insufficient_data"
    else:
        tvl_trend = "insufficient_data"

    # ------------------------------------------------------------------
    # 6. Binance CEX data (no key required)
    # ------------------------------------------------------------------
    binance_data, err = fetch_binance_ticker(config.binance_symbol)
    if err:
        fetch_errors.append(err)

    binance_price:      Optional[float] = None
    binance_24h_high:   Optional[float] = None
    binance_24h_low:    Optional[float] = None
    binance_24h_volume: Optional[float] = None
    cex_dex_spread_pct: Optional[float] = None

    if binance_data:
        try:
            binance_price      = float(binance_data["lastPrice"])
            binance_24h_high   = float(binance_data["highPrice"])
            binance_24h_low    = float(binance_data["lowPrice"])
            binance_24h_volume = float(binance_data["volume"])
            if current_price is not None and binance_price != 0:
                cex_dex_spread_pct = ((current_price - binance_price) / binance_price) * 100
        except (KeyError, ValueError) as exc:
            fetch_errors.append(f"binance_ticker:parse_error: {exc}")

    # ------------------------------------------------------------------
    # 7. Upcoming unlocks (from config — no API call)
    # ------------------------------------------------------------------
    unlock_entries: list[dict] = []
    has_near_term_unlock = False

    for unlock in config.upcoming_unlocks:
        try:
            unlock_date = dateutil_parser.parse(unlock["date"]).date()
            days_until  = (unlock_date - today).days
            near_term   = days_until <= 30
            if near_term:
                has_near_term_unlock = True
            unlock_entries.append({
                "date":          unlock["date"],
                "amount_tokens": unlock["amount_tokens"],
                "days_until":    days_until,
                "near_term":     near_term,
                "note":          unlock.get("note", ""),
            })
        except Exception as exc:
            fetch_errors.append(f"upcoming_unlocks:parse_error: {exc}")

    # ------------------------------------------------------------------
    # Signal summary
    # ------------------------------------------------------------------
    signals: dict[str, dict] = {
        "rsi": {
            "value": round(rsi_14, 4) if rsi_14 is not None else None,
            "lean": lean_rsi(rsi_14),
        },
        "macd": {
            "value": round(macd_line - macd_signal, 6) if macd_line is not None and macd_signal is not None else None,
            "lean": lean_macd(macd_line, macd_signal),
        },
        "price_vs_sma50": {
            "value": round(price_vs_sma50_pct, 4) if price_vs_sma50_pct is not None else None,
            "lean": lean_vs_sma(price_vs_sma50_pct),
        },
        "price_vs_sma200": {
            "value": round(price_vs_sma200_pct, 4) if price_vs_sma200_pct is not None else None,
            "lean": lean_vs_sma(price_vs_sma200_pct),
        },
        "fear_and_greed": {
            "value": fg_value,
            "lean": lean_fear_and_greed(fg_value),
        },
        "fg_7day_trend": {
            "value": fg_7day_trend,
            "lean": lean_fg_trend(fg_7day_trend),
        },
        "tvl_trend": {
            "value": round(tvl_change_pct, 4) if tvl_change_pct is not None else None,
            "lean": lean_tvl(tvl_change_pct),
        },
        "btc_24h": {
            "value": round(btc_change_24h, 4) if btc_change_24h is not None else None,
            "lean": lean_btc(btc_change_24h),
        },
        "upcoming_unlock": {
            "value": has_near_term_unlock,
            "lean": lean_upcoming_unlock(has_near_term_unlock),
        },
        "volume_vs_market_cap": {
            "value": round(vol_mc_ratio, 6) if vol_mc_ratio is not None else None,
            "lean": lean_volume_vs_market_cap(vol_mc_ratio),
        },
    }

    bullish_count = sum(1 for s in signals.values() if s["lean"] == "bullish")
    bearish_count = sum(1 for s in signals.values() if s["lean"] == "bearish")
    neutral_count = sum(1 for s in signals.values() if s["lean"] == "neutral")
    overall_lean  = compute_overall_lean(bullish_count, bearish_count)

    summary_note = (
        f"{bullish_count} of {len(signals)} indicators lean bullish, "
        f"{bearish_count} bearish, {neutral_count} neutral — "
        + {
            "bullish":        "multiple indicators align to the upside.",
            "bearish":        "multiple indicators align to the downside.",
            "slight_bullish": "mixed market with slight bullish tilt.",
            "slight_bearish": "mixed market with slight bearish pressure.",
            "neutral":        "no clear directional bias in the data.",
        }[overall_lean]
    )

    # ------------------------------------------------------------------
    # Assemble full report
    # ------------------------------------------------------------------
    return {
        "report_metadata": {
            "generated_at":  now_utc.isoformat(),
            "token":         config.token_name,
            "config_file":   config_path,
            "script_version": SCRIPT_VERSION,
        },
        "price_data": {
            "current_price_usd":        current_price,
            "price_change_24h_pct":     price_change_24h,
            "price_change_7d_pct":      price_change_7d,
            "volume_24h_usd":           volume_24h,
            "market_cap_usd":           market_cap,
            "volume_to_market_cap_ratio": vol_mc_ratio,
            # CMC has no ATH endpoint; this is the highest daily-high over the
            # 365-day OHLCV window used to compute technical indicators.
            "ath_usd":                  period_high,
            "drawdown_from_ath_pct":    drawdown_from_high,
            "source":                   "coinmarketcap (365-day high, not true ATH)",
        },
        "technical_indicators": {
            "rsi_14":             round(rsi_14, 4)      if rsi_14      is not None else None,
            "macd_line":          round(macd_line, 6)   if macd_line   is not None else None,
            "macd_signal_line":   round(macd_signal, 6) if macd_signal is not None else None,
            "macd_histogram":     round(macd_hist, 6)   if macd_hist   is not None else None,
            "macd_crossover":     macd_crossover,
            "sma_50":             round(sma_50, 4)      if sma_50      is not None else None,
            "sma_200":            round(sma_200, 4)     if sma_200     is not None else None,
            "price_vs_sma50_pct":  round(price_vs_sma50_pct,  4) if price_vs_sma50_pct  is not None else None,
            "price_vs_sma200_pct": round(price_vs_sma200_pct, 4) if price_vs_sma200_pct is not None else None,
            "source":             "computed_from_coingecko_ohlc",
        },
        "sentiment": {
            "fear_and_greed_value": fg_value,
            "fear_and_greed_label": fg_label,
            "fg_7day_trend":        fg_7day_trend,
            "fg_7day_history":      fg_7day_history,
            "source":               "coinmarketcap",
        },
        "ecosystem": {
            "tvl_usd":        tvl_usd,
            "tvl_change_pct": round(tvl_change_pct, 4) if tvl_change_pct is not None else None,
            "tvl_trend":      tvl_trend,
            "source":         "defillama",
        },
        "cex_data": {
            "binance_price_usd":    binance_price,
            "binance_24h_high":     binance_24h_high,
            "binance_24h_low":      binance_24h_low,
            "binance_24h_volume":   binance_24h_volume,
            "cex_dex_spread_pct":   round(cex_dex_spread_pct, 6) if cex_dex_spread_pct is not None else None,
            "source":               "binance",
        },
        "macro": {
            "btc_price_usd":      btc_price,
            "btc_change_24h_pct": btc_change_24h,
            "source":             "coinmarketcap",
        },
        "upcoming_events": {
            "unlocks": unlock_entries,
        },
        "signal_summary": {
            "bullish_count": bullish_count,
            "bearish_count": bearish_count,
            "neutral_count": neutral_count,
            "overall_lean":  overall_lean,
            "summary_note":  summary_note,
            "signals":       signals,
        },
        "fetch_errors": fetch_errors,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # Reconfigure stdout to UTF-8 on Windows so emoji in Markdown renders correctly
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="Fetch live market data via CoinMarketCap and produce a structured report."
    )
    parser.add_argument(
        "--config",
        default="configs/sui.json",
        help="Path to token config JSON file (default: configs/sui.json)",
    )
    parser.add_argument(
        "--md",
        action="store_true",
        help="Also output a Markdown report (printed to stdout + saved to reports/)",
    )
    args = parser.parse_args()

    # Resolve all paths relative to the script's own directory
    script_dir     = Path(__file__).parent.resolve()
    config_path_abs = (script_dir / args.config).resolve()
    reports_dir    = script_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Load the .env file from the project root (silent if absent — env var may
    # already be set in the shell environment)
    env_path = script_dir / ".env"
    load_dotenv(dotenv_path=env_path)

    # Retrieve the CMC API key
    api_key = os.environ.get("CMC_API_KEY", "").strip()
    if not api_key:
        print(
            "ERROR: CMC_API_KEY is not set.\n"
            "  1. Copy .env.example to .env\n"
            "  2. Open .env and set:  CMC_API_KEY=your_key_here\n"
            "  Get a free key at: https://coinmarketcap.com/api/",
            file=sys.stderr,
        )
        sys.exit(1)

    # Validate config file
    if not config_path_abs.exists():
        print(f"ERROR: config file not found: {config_path_abs}", file=sys.stderr)
        sys.exit(1)

    config = load_config(str(config_path_abs))
    print(f"Fetching market data for {config.token_name}...", file=sys.stderr)

    report = build_report(config, args.config, reports_dir, api_key)

    # Exit with code 1 only if every data section came back empty
    price_ok     = report["price_data"]["current_price_usd"] is not None
    tech_ok      = report["technical_indicators"]["rsi_14"] is not None
    sentiment_ok = report["sentiment"]["fear_and_greed_value"] is not None
    tvl_ok       = report["ecosystem"]["tvl_usd"] is not None
    cex_ok       = report["cex_data"]["binance_price_usd"] is not None
    macro_ok     = report["macro"]["btc_price_usd"] is not None

    if not any([price_ok, tech_ok, sentiment_ok, tvl_ok, cex_ok, macro_ok]):
        print("ERROR: All API calls failed. Check your network connection and API key.", file=sys.stderr)
        for e in report["fetch_errors"]:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    # Write output files
    ts        = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    prefix    = config.output_prefix
    json_path = reports_dir / f"{prefix}_{ts}.json"
    md_path   = reports_dir / f"{prefix}_{ts}.md"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"JSON report saved: {json_path}", file=sys.stderr)

    if args.md:
        md_content = render_markdown(report)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)
        print(f"Markdown report saved: {md_path}", file=sys.stderr)
        print()
        print(md_content)

    if report["fetch_errors"]:
        print("\nNon-fatal fetch errors:", file=sys.stderr)
        for e in report["fetch_errors"]:
            print(f"  - {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
